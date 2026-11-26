"""Durable email-verification queue.

Bulk verification used to run as a fire-and-forget ``asyncio`` task: progress
was invisible, a restart silently stopped it, and leads it had not reached
were left in ``pending`` with no way to resume.  This module replaces that
with database-backed jobs (:class:`~app.models.VerificationJob`) made of one
item per lead (:class:`~app.models.VerificationJobItem`), processed by a
single in-process worker.

* Items are processed one at a time, oldest job first, each in its own
  transaction, so progress is saved continuously and survives restarts.
* Pausing a job takes effect before its next item; resuming picks up where
  it stopped.
* Items whose request fails (timeout, HTTP error, bad JSON) are marked
  ``failed`` and can be retried.  Their leads stay ``pending`` (not sent)
  until they are retried or the job is cancelled.
* Cancelling a job releases its unprocessed leads so they send unverified.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time as _monotonic
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.email_verification import (
    BLOCK_SEND_STATUSES,
    PENDING,
    UNKNOWN,
    VerificationResult,
)
from app.models import CampaignLead, Lead, QueueSlot, VerificationJob, VerificationJobItem
from app.time import utcnow

log = logging.getLogger("quickly.verification_queue")

ACTIVE_JOB_STATUSES = ("queued", "running", "paused")
RUNNABLE_JOB_STATUSES = ("queued", "running")

# Idle worker re-checks the database this often even without a wake-up, which
# also covers jobs created in a transaction that committed after notify_worker().
IDLE_POLL_SECONDS = 5.0
# Newly verified leads are scheduled in batches (scheduling is per campaign).
SCHEDULE_BATCH_SIZE = 20
SCHEDULE_MAX_DELAY_SECONDS = 30.0

Verifier = Callable[[str], Awaitable[VerificationResult]]

_wake = asyncio.Event()
_worker_task: asyncio.Task | None = None
# Rate limit: the worker never starts two verification requests closer
# together than this (seconds). Refreshed from settings on every item.
_min_interval = 2.0
_last_request_at: float | None = None
_to_schedule: list[int] = []
_last_schedule_flush = _monotonic.monotonic()


class JobActionError(Exception):
    """Raised when a job control action is not allowed in the job's state."""


# ── Provider configuration ──────────────────────────────────────────────────

async def load_email_verifier(db: AsyncSession) -> tuple[Verifier | None, str | None]:
    """Return ``(verify_fn, None)`` when email verification can actually run,
    or ``(None, reason)`` when it is disabled or not usable (no API key,
    invalid custom provider config)."""
    from app.app_settings import (
        EMAIL_VERIFICATION_API_KEY,
        EMAIL_VERIFICATION_CUSTOM_BODY,
        EMAIL_VERIFICATION_CUSTOM_FIELD,
        EMAIL_VERIFICATION_CUSTOM_HEADERS,
        EMAIL_VERIFICATION_CUSTOM_INVALID_VALUES,
        EMAIL_VERIFICATION_CUSTOM_METHOD,
        EMAIL_VERIFICATION_CUSTOM_URL,
        EMAIL_VERIFICATION_CUSTOM_VALID_VALUES,
        EMAIL_VERIFICATION_ENABLED,
        EMAIL_VERIFICATION_PROVIDER,
        get_setting,
    )
    from app.email_verification import CustomHttpProvider, verify_single

    enabled = (await get_setting(db, EMAIL_VERIFICATION_ENABLED) or "false").lower() in ("true", "1", "yes")
    if not enabled:
        return None, "Email verification is disabled in Settings"
    provider_name = await get_setting(db, EMAIL_VERIFICATION_PROVIDER) or "mailtester_ninja"

    if provider_name == "custom":
        provider = CustomHttpProvider(
            await get_setting(db, EMAIL_VERIFICATION_CUSTOM_URL) or "",
            await get_setting(db, EMAIL_VERIFICATION_CUSTOM_FIELD) or "",
            json.loads(await get_setting(db, EMAIL_VERIFICATION_CUSTOM_VALID_VALUES) or "[]"),
            json.loads(await get_setting(db, EMAIL_VERIFICATION_CUSTOM_INVALID_VALUES) or "[]"),
            await get_setting(db, EMAIL_VERIFICATION_CUSTOM_METHOD) or "GET",
            headers=json.loads(await get_setting(db, EMAIL_VERIFICATION_CUSTOM_HEADERS) or "{}"),
            body_template=await get_setting(db, EMAIL_VERIFICATION_CUSTOM_BODY) or "",
        )
        if provider.config_error:
            return None, f"Custom email verification provider misconfigured: {provider.config_error}"

        async def _verify_custom(email: str) -> VerificationResult:
            return await provider.verify(email, "")
        return _verify_custom, None

    api_key = await get_setting(db, EMAIL_VERIFICATION_API_KEY) or ""
    if not api_key:
        return None, "Email verification is enabled but no API key is configured"

    async def _verify_builtin(email: str) -> VerificationResult:
        return await verify_single(email, api_key, provider_name)
    return _verify_builtin, None


async def load_min_interval(db: AsyncSession) -> float:
    """Configured minimum seconds between verification requests (>= 0)."""
    from app.app_settings import (
        EMAIL_VERIFICATION_MIN_INTERVAL,
        EMAIL_VERIFICATION_MIN_INTERVAL_DEFAULT,
        get_setting,
    )

    raw = await get_setting(db, EMAIL_VERIFICATION_MIN_INTERVAL)
    try:
        value = float(raw) if raw not in (None, "") else EMAIL_VERIFICATION_MIN_INTERVAL_DEFAULT
    except ValueError:
        value = EMAIL_VERIFICATION_MIN_INTERVAL_DEFAULT
    return max(0.0, value)


def seconds_until_next_request() -> float:
    """How long the worker must still wait before its next request."""
    if _last_request_at is None:
        return 0.0
    return max(0.0, _min_interval - (_monotonic.monotonic() - _last_request_at))


# ── Scheduling helpers ──────────────────────────────────────────────────────

async def schedule_leads(db: AsyncSession, lead_ids: list[int]) -> None:
    """Reserve queue slots for every campaign enrollment of *lead_ids*.

    Eligibility (verification status, pauses, replies, ...) is enforced by
    ``reserve_slots_for_new_leads_bulk`` itself.
    """
    if not lead_ids:
        return
    from app.queue_logic import reserve_slots_for_new_leads_bulk

    res = await db.execute(
        select(CampaignLead.id, CampaignLead.campaign_id).where(CampaignLead.lead_id.in_(lead_ids))
    )
    groups: dict[int, list[int]] = {}
    for cl_id, campaign_id in res.all():
        groups.setdefault(campaign_id, []).append(cl_id)
    for campaign_id, cl_ids in groups.items():
        try:
            await reserve_slots_for_new_leads_bulk(db, cl_ids, campaign_id)
        except Exception:
            log.exception("Failed to schedule verified leads for campaign %s", campaign_id)


async def release_pending_leads(db: AsyncSession, lead_ids: list[int]) -> list[int]:
    """Clear ``pending`` on *lead_ids* (only those still pending) and schedule
    them, so they send unverified instead of staying blocked."""
    if not lead_ids:
        return []
    res = await db.execute(
        select(Lead.id).where(Lead.id.in_(lead_ids), Lead.email_verification_status == PENDING)
    )
    released = [r[0] for r in res.all()]
    if released:
        await db.execute(
            update(Lead).where(Lead.id.in_(released)).values(email_verification_status=None)
        )
        await db.flush()
        await schedule_leads(db, released)
    return released


async def _flush_scheduling(force: bool = False) -> None:
    global _last_schedule_flush
    if not _to_schedule:
        _last_schedule_flush = _monotonic.monotonic()
        return
    due = (
        force
        or len(_to_schedule) >= SCHEDULE_BATCH_SIZE
        or _monotonic.monotonic() - _last_schedule_flush >= SCHEDULE_MAX_DELAY_SECONDS
    )
    if not due:
        return
    batch = list(dict.fromkeys(_to_schedule))
    _to_schedule.clear()
    _last_schedule_flush = _monotonic.monotonic()
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await schedule_leads(db, batch)
        await db.commit()
    log.info("Scheduled %d verified lead(s)", len(batch))


# ── Enqueueing ──────────────────────────────────────────────────────────────

def notify_worker() -> None:
    """Wake the worker so newly queued work starts without waiting for a poll."""
    _wake.set()


async def enqueue_verification(
    db: AsyncSession,
    lead_ids: list[int],
    *,
    campaign_id: int | None,
    reverify: bool = False,
    source: str = "campaign_verify",
) -> tuple[VerificationJob | None, str | None, int]:
    """Queue *lead_ids* for verification using the caller's session.

    The caller commits.  Leads are added to an existing active job for the
    same campaign (and mode) when there is one, otherwise a new job is
    created.  Unless *reverify* is set, leads are marked ``pending`` so they
    are not sent before their result is known.

    Returns ``(job, None, newly_queued)``, or ``(None, reason, 0)`` when
    verification can't run; in that case any of the leads stuck in
    ``pending`` are released.
    """
    lead_ids = list(dict.fromkeys(lead_ids))
    verifier, reason = await load_email_verifier(db)
    if verifier is None:
        if not reverify:
            await release_pending_leads(db, lead_ids)
        return None, reason, 0
    if not lead_ids:
        return None, "No leads to verify", 0

    campaign_clause = (
        VerificationJob.campaign_id.is_(None)
        if campaign_id is None
        else VerificationJob.campaign_id == campaign_id
    )
    res = await db.execute(
        select(VerificationJob)
        .where(
            campaign_clause,
            VerificationJob.reverify == reverify,
            VerificationJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(VerificationJob.id.desc())
        .limit(1)
    )
    job = res.scalar_one_or_none()
    if job is None:
        job = VerificationJob(
            campaign_id=campaign_id, status="queued", reverify=reverify, source=source,
        )
        db.add(job)
        await db.flush()
        already: set[int] = set()
    else:
        res = await db.execute(
            select(VerificationJobItem.lead_id).where(
                VerificationJobItem.job_id == job.id,
                VerificationJobItem.status.in_(("pending", "processing")),
            )
        )
        already = {r[0] for r in res.all()}
        if job.status == "running":
            job.status = "queued"  # let the worker pick it up again in order

    new_ids = [lid for lid in lead_ids if lid not in already]
    db.add_all(VerificationJobItem(job_id=job.id, lead_id=lid, status="pending") for lid in new_ids)
    if not reverify and lead_ids:
        await db.execute(
            update(Lead).where(Lead.id.in_(lead_ids)).values(email_verification_status=PENDING)
        )
    await db.flush()
    notify_worker()
    log.info(
        "Queued %d lead(s) for verification in job %s (campaign=%s, reverify=%s)",
        len(new_ids), job.id, campaign_id, reverify,
    )
    return job, None, len(new_ids)


async def enqueue_verification_in_new_session(
    lead_ids: list[int],
    *,
    campaign_id: int | None,
    reverify: bool = False,
    source: str = "campaign_verify",
) -> None:
    """Fire-and-forget helper for callers without a session of their own."""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await enqueue_verification(
            db, lead_ids, campaign_id=campaign_id, reverify=reverify, source=source,
        )
        await db.commit()
    notify_worker()


# ── Worker ──────────────────────────────────────────────────────────────────

async def _record_error(message: str) -> None:
    """Surface the latest verification error to System Health."""
    from app.app_settings import EMAIL_VERIFICATION_LAST_ERROR, EMAIL_VERIFICATION_LAST_ERROR_AT, put_setting
    from app.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await put_setting(db, EMAIL_VERIFICATION_LAST_ERROR, message[:500])
            await put_setting(db, EMAIL_VERIFICATION_LAST_ERROR_AT, utcnow().isoformat() + "Z")
            await db.commit()
    except Exception:
        log.debug("Could not record verification error", exc_info=True)


async def _apply_result(
    db: AsyncSession, job: VerificationJob, lead: Lead, vr: VerificationResult,
) -> None:
    """Write a successful verification result to the lead and its queue."""
    old_status = lead.email_verification_status
    new_status = vr.status
    changed = old_status != new_status
    if job.reverify and not changed:
        return

    lead.email_verification_status = new_status
    lead.email_verification_result = vr.raw
    if new_status in BLOCK_SEND_STATUSES:
        cl_ids = (
            await db.execute(select(CampaignLead.id).where(CampaignLead.lead_id == lead.id))
        ).scalars().all()
        if cl_ids:
            from sqlalchemy import delete

            await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id.in_(cl_ids)))
    else:
        _to_schedule.append(lead.id)

    if job.reverify and changed:
        try:
            from app.database import AsyncSessionLocal
            from app.webhooks import fire_webhook_event

            async with AsyncSessionLocal() as wdb:
                await fire_webhook_event(wdb, "lead.verification_changed", {
                    "lead_id": lead.id,
                    "lead_email": lead.email,
                    "old_verification_status": old_status,
                    "new_verification_status": new_status,
                    "old_lead_status": lead.status,
                    "new_lead_status": lead.status,
                })
        except Exception:
            log.debug("verification_changed webhook failed", exc_info=True)


async def process_next_item() -> bool:
    """Process one queued item.  Returns True when there may be more work."""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        job = (
            await db.execute(
                select(VerificationJob)
                .where(VerificationJob.status.in_(RUNNABLE_JOB_STATUSES))
                .order_by(VerificationJob.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return False

        verifier, reason = await load_email_verifier(db)
        if verifier is None:
            # Settings changed under a running job: pause it with the reason
            # instead of failing every remaining lead.
            job.status = "paused"
            job.last_error = reason
            await db.commit()
            log.warning("Paused verification job %s: %s", job.id, reason)
            await _record_error(reason or "Email verification unavailable")
            return True

        item = (
            await db.execute(
                select(VerificationJobItem)
                .where(VerificationJobItem.job_id == job.id, VerificationJobItem.status == "pending")
                .order_by(VerificationJobItem.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if item is None:
            job.status = "completed"
            job.finished_at = utcnow()
            await db.commit()
            log.info("Verification job %s completed", job.id)
            await _flush_scheduling(force=True)
            return True

        global _min_interval, _last_request_at
        _min_interval = await load_min_interval(db)
        wait = seconds_until_next_request()
        if wait > 0:
            # Throttle without holding a transaction open, then re-check: the
            # job may have been paused or cancelled while we waited.
            await db.rollback()
            await asyncio.sleep(wait)
            return True

        if job.status == "queued":
            job.status = "running"
            job.started_at = job.started_at or utcnow()
            job.last_error = None
        item.status = "processing"
        item.attempts = (item.attempts or 0) + 1
        await db.commit()

        lead = await db.get(Lead, item.lead_id)
        if lead is None:
            item.status = "cancelled"
            item.message = "Lead was deleted"
            await db.commit()
            return True

        _last_request_at = _monotonic.monotonic()
        try:
            vr = await verifier(lead.email)
        except Exception as exc:  # providers should not raise, but never trust that
            log.exception("Verifier raised for lead %s", lead.id)
            vr = VerificationResult(lead.email, UNKNOWN, message=f"{type(exc).__name__}: {exc}", error=True)

        # Re-read the item: the job may have been cancelled while we waited.
        await db.refresh(item)
        if item.status != "processing":
            return True

        if vr.error:
            item.status = "failed"
            item.message = vr.message or "Verification request failed"
            await db.commit()
            log.info("Verification failed for lead %s (%s): %s", lead.id, lead.email, item.message)
            await _record_error(f"{lead.email}: {item.message}")
            return True

        await db.refresh(lead)
        await _apply_result(db, job, lead, vr)
        item.status = "done"
        item.result_status = vr.status
        item.message = vr.message
        await db.commit()
        log.info("Verified lead %s (%s): %s - %s", lead.id, lead.email, vr.status, vr.message)

    await _flush_scheduling()
    return True


async def _recover_interrupted_items() -> None:
    """Items left ``processing`` by a crash or restart go back to ``pending``."""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            update(VerificationJobItem)
            .where(VerificationJobItem.status == "processing")
            .values(status="pending")
        )
        await db.commit()
        if res.rowcount:
            log.info("Requeued %d interrupted verification item(s)", res.rowcount)


async def _worker_loop() -> None:
    log.info("Email verification worker started")
    while True:
        try:
            more = await process_next_item()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Email verification worker error")
            more = False
        if more:
            await asyncio.sleep(0)
            continue
        try:
            await _flush_scheduling(force=True)
        except Exception:
            log.exception("Failed to schedule verified leads")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=IDLE_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def start_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    await _recover_interrupted_items()
    _worker_task = asyncio.create_task(_worker_loop(), name="email-verification-worker")


async def stop_worker() -> None:
    global _worker_task
    task, _worker_task = _worker_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ── Job inspection and control ──────────────────────────────────────────────

async def job_summary(db: AsyncSession, job: VerificationJob) -> dict[str, Any]:
    counts = dict(
        (
            await db.execute(
                select(VerificationJobItem.status, func.count())
                .where(VerificationJobItem.job_id == job.id)
                .group_by(VerificationJobItem.status)
            )
        ).all()
    )
    results = dict(
        (
            await db.execute(
                select(VerificationJobItem.result_status, func.count())
                .where(VerificationJobItem.job_id == job.id, VerificationJobItem.status == "done")
                .group_by(VerificationJobItem.result_status)
            )
        ).all()
    )
    total = sum(counts.values())
    pending = counts.get("pending", 0) + counts.get("processing", 0)
    return {
        "id": job.id,
        "campaign_id": job.campaign_id,
        "status": job.status,
        "reverify": job.reverify,
        "source": job.source,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "total": total,
        "remaining": pending,
        "processing": counts.get("processing", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "results": {k or "unknown": v for k, v in results.items()},
    }


async def _get_job(db: AsyncSession, job_id: int) -> VerificationJob:
    # populate_existing: the worker updates jobs in its own sessions, so never
    # act on a stale copy from this session's identity map.
    job = await db.get(VerificationJob, job_id, populate_existing=True)
    if job is None:
        raise LookupError(f"Verification job {job_id} not found")
    return job


async def pause_job(db: AsyncSession, job_id: int) -> VerificationJob:
    job = await _get_job(db, job_id)
    if job.status not in RUNNABLE_JOB_STATUSES:
        raise JobActionError(f"Cannot pause a {job.status} job")
    job.status = "paused"
    await db.flush()
    return job


async def resume_job(db: AsyncSession, job_id: int) -> VerificationJob:
    job = await _get_job(db, job_id)
    if job.status != "paused":
        raise JobActionError(f"Cannot resume a {job.status} job")
    _, reason = await load_email_verifier(db)
    if reason:
        raise JobActionError(reason)
    job.status = "queued"
    job.last_error = None
    await db.flush()
    notify_worker()
    return job


async def retry_failed(db: AsyncSession, job_id: int) -> tuple[VerificationJob, int]:
    job = await _get_job(db, job_id)
    if job.status == "cancelled":
        raise JobActionError("Cannot retry items of a cancelled job")
    res = await db.execute(
        update(VerificationJobItem)
        .where(VerificationJobItem.job_id == job.id, VerificationJobItem.status == "failed")
        .values(status="pending", message=None)
    )
    retried = res.rowcount or 0
    if retried and job.status == "completed":
        job.status = "queued"
        job.finished_at = None
    await db.flush()
    notify_worker()
    return job, retried


async def cancel_job(db: AsyncSession, job_id: int) -> tuple[VerificationJob, int]:
    """Stop the job.  Leads it never verified are released to send unverified."""
    job = await _get_job(db, job_id)
    if job.status in ("completed", "cancelled"):
        raise JobActionError(f"Job is already {job.status}")
    res = await db.execute(
        select(VerificationJobItem.lead_id).where(
            VerificationJobItem.job_id == job.id,
            VerificationJobItem.status.in_(("pending", "failed")),
        )
    )
    lead_ids = [r[0] for r in res.all()]
    await db.execute(
        update(VerificationJobItem)
        .where(
            VerificationJobItem.job_id == job.id,
            VerificationJobItem.status.in_(("pending", "failed")),
        )
        .values(status="cancelled")
    )
    job.status = "cancelled"
    job.finished_at = utcnow()
    released = [] if job.reverify else await release_pending_leads(db, lead_ids)
    await db.flush()
    return job, len(released)
