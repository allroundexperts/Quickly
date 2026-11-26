"""Email verification queue: enqueueing from the add-leads / verify paths,
worker processing, failures and retry, pause / resume, cancel, and restart
recovery.  Leads must never be left stuck in "pending"."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import verification_queue as vq
from app.app_settings import (
    EMAIL_VERIFICATION_CUSTOM_FIELD,
    EMAIL_VERIFICATION_CUSTOM_URL,
    EMAIL_VERIFICATION_CUSTOM_VALID_VALUES,
    EMAIL_VERIFICATION_ENABLED,
    EMAIL_VERIFICATION_PROVIDER,
    put_setting,
)
from app.email_verification import INVALID, UNKNOWN, VALID, VerificationResult
from app.models import Lead, VerificationJob, VerificationJobItem
# Captured before the autouse fixture patches it
_REAL_LOAD_MIN_INTERVAL = vq.load_min_interval

from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_inbox,
    make_lead,
)


async def _enable_custom_provider(session):
    await put_setting(session, EMAIL_VERIFICATION_ENABLED, "true")
    await put_setting(session, EMAIL_VERIFICATION_PROVIDER, "custom")
    await put_setting(session, EMAIL_VERIFICATION_CUSTOM_URL, "https://verify.test/v?email={email}")
    await put_setting(session, EMAIL_VERIFICATION_CUSTOM_FIELD, "status")
    await put_setting(session, EMAIL_VERIFICATION_CUSTOM_VALID_VALUES, '["ok"]')
    await session.commit()


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """Tests drain the queue in a tight loop; the rate limit has its own test."""
    async def _zero(db):
        return 0.0

    monkeypatch.setattr(vq, "load_min_interval", _zero)
    monkeypatch.setattr(vq, "_last_request_at", None)


@pytest.fixture
def fake_verifier(monkeypatch):
    """Replace the provider with a scripted one: {email: result | Exception}."""
    script: dict[str, object] = {}
    calls: list[str] = []

    async def _verify(email):
        calls.append(email)
        outcome = script.get(email, VALID)
        if outcome == "error":
            return VerificationResult(email, UNKNOWN, message="HTTP 503", error=True)
        return VerificationResult(email, outcome, message=f"scripted {outcome}")

    real_loader = vq.load_email_verifier

    async def _loader(db):
        verifier, reason = await real_loader(db)
        return (_verify, None) if verifier else (None, reason)

    monkeypatch.setattr(vq, "load_email_verifier", _loader)
    return script, calls


async def _drain(max_steps: int = 50) -> None:
    for _ in range(max_steps):
        if not await vq.process_next_item():
            return
    raise AssertionError("worker did not go idle")


async def _statuses(session) -> dict[str, str | None]:
    session.expire_all()
    return dict((await session.execute(select(Lead.email, Lead.email_verification_status))).all())


async def _campaign_with_leads(session, *emails, status=None):
    campaign = await make_campaign(session)
    leads = []
    for email in emails:
        lead = await make_lead(session, email=email)
        lead.email_verification_status = status
        await make_campaign_lead(session, campaign.id, lead.id)
        leads.append(lead)
    await session.commit()
    return campaign, leads


# ── Enqueueing ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_add_with_verify_but_disabled_does_not_leave_pending(session):
    from app.routers.campaigns import CampaignLeadAdd, bulk_add_leads_to_campaign

    campaign = await make_campaign(session)
    inbox = await make_inbox(session, email="sender@test.com")
    await make_campaign_inbox(session, campaign.id, inbox.id)
    await session.flush()

    result = await bulk_add_leads_to_campaign(
        campaign.id, [CampaignLeadAdd(email="someone@test.com")], verify_emails=True, db=session,
    )

    assert result["verification_queued"] is False
    assert result["verification_job_id"] is None
    assert "disabled" in (result["verification_skipped_reason"] or "")
    assert (await _statuses(session))["someone@test.com"] is None
    assert (await session.execute(select(VerificationJob))).scalars().all() == []


@pytest.mark.asyncio
async def test_bulk_add_with_verify_and_enabled_creates_job(session):
    from app.routers.campaigns import CampaignLeadAdd, bulk_add_leads_to_campaign

    await _enable_custom_provider(session)
    campaign = await make_campaign(session)
    inbox = await make_inbox(session, email="sender@test.com")
    await make_campaign_inbox(session, campaign.id, inbox.id)
    await session.flush()

    result = await bulk_add_leads_to_campaign(
        campaign.id, [CampaignLeadAdd(email="someone@test.com")], verify_emails=True, db=session,
    )
    await session.commit()

    assert result["verification_queued"] is True
    job = await session.get(VerificationJob, result["verification_job_id"])
    assert (job.campaign_id, job.status, job.source) == (campaign.id, "queued", "leads_added")
    assert (await _statuses(session))["someone@test.com"] == "pending"


@pytest.mark.asyncio
async def test_enqueue_when_disabled_releases_stuck_pending(session):
    _, (stuck, bad) = await _campaign_with_leads(session, "stuck@test.com", "bad@test.com")
    stuck.email_verification_status = "pending"
    bad.email_verification_status = "invalid"
    await session.commit()

    job, reason, queued = await vq.enqueue_verification(
        session, [stuck.id, bad.id], campaign_id=None,
    )
    await session.commit()

    assert job is None and queued == 0 and "disabled" in reason
    statuses = await _statuses(session)
    assert statuses["stuck@test.com"] is None
    assert statuses["bad@test.com"] == "invalid"


@pytest.mark.asyncio
async def test_verify_endpoint_rejects_when_verification_cannot_run(session):
    from app.routers.campaigns import trigger_verification_for_campaign

    campaign = await make_campaign(session)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await trigger_verification_for_campaign(campaign.id, reverify=False, db=session)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_endpoint_queues_unverified_and_stuck_and_merges_repeat_clicks(session):
    from app.routers.campaigns import trigger_verification_for_campaign

    await _enable_custom_provider(session)
    campaign, (fresh, stuck, done) = await _campaign_with_leads(
        session, "fresh@test.com", "stuck@test.com", "done@test.com",
    )
    stuck.email_verification_status = "pending"
    done.email_verification_status = "valid"
    await session.commit()

    res = await trigger_verification_for_campaign(campaign.id, reverify=False, db=session)
    assert res["queued"] == 2
    assert res["total_verified"] == 1
    assert res["job"]["total"] == 2 and res["job"]["status"] == "queued"

    # A second click while the job is active adds nothing and reuses the job
    again = await trigger_verification_for_campaign(campaign.id, reverify=False, db=session)
    assert again["queued"] == 0
    assert again["job"]["id"] == res["job"]["id"]


# ── Worker ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_processes_job_and_marks_failures(session, fake_verifier):
    script, _ = fake_verifier
    script.update({"bad@test.com": INVALID, "flaky@test.com": "error"})
    await _enable_custom_provider(session)
    campaign, leads = await _campaign_with_leads(session, "good@test.com", "bad@test.com", "flaky@test.com")

    job, _, _ = await vq.enqueue_verification(session, [l.id for l in leads], campaign_id=campaign.id)
    job_id = job.id
    await session.commit()
    await _drain()

    session.expire_all()
    summary = await vq.job_summary(session, await session.get(VerificationJob, job_id))
    assert summary["status"] == "completed"
    assert (summary["done"], summary["failed"], summary["remaining"]) == (2, 1, 0)
    assert summary["results"] == {"valid": 1, "invalid": 1}

    statuses = await _statuses(session)
    assert statuses == {"good@test.com": "valid", "bad@test.com": "invalid", "flaky@test.com": "pending"}

    failed = (
        await session.execute(select(VerificationJobItem).where(VerificationJobItem.status == "failed"))
    ).scalar_one()
    assert failed.message == "HTTP 503" and failed.attempts == 1


@pytest.mark.asyncio
async def test_retry_failed_requeues_and_completes(session, fake_verifier):
    script, _ = fake_verifier
    script["flaky@test.com"] = "error"
    await _enable_custom_provider(session)
    campaign, leads = await _campaign_with_leads(session, "flaky@test.com")
    job, _, _ = await vq.enqueue_verification(session, [leads[0].id], campaign_id=campaign.id)
    job_id = job.id
    await session.commit()
    await _drain()

    script["flaky@test.com"] = VALID
    _, retried = await vq.retry_failed(session, job_id)
    await session.commit()
    assert retried == 1
    await _drain()

    session.expire_all()
    summary = await vq.job_summary(session, await session.get(VerificationJob, job_id))
    assert (summary["status"], summary["done"], summary["failed"]) == ("completed", 1, 0)
    assert (await _statuses(session))["flaky@test.com"] == "valid"


@pytest.mark.asyncio
async def test_pause_stops_processing_and_resume_continues(session, fake_verifier):
    _, calls = fake_verifier
    await _enable_custom_provider(session)
    campaign, leads = await _campaign_with_leads(session, "a@test.com", "b@test.com")
    job, _, _ = await vq.enqueue_verification(session, [l.id for l in leads], campaign_id=campaign.id)
    job_id = job.id
    await session.commit()

    assert await vq.process_next_item()  # first lead
    await vq.pause_job(session, job_id)
    await session.commit()
    assert not await vq.process_next_item()  # paused: nothing to do
    assert calls == ["a@test.com"]

    with pytest.raises(vq.JobActionError):
        await vq.pause_job(session, job_id)

    await vq.resume_job(session, job_id)
    await session.commit()
    await _drain()
    assert calls == ["a@test.com", "b@test.com"]
    session.expire_all()
    assert (await session.get(VerificationJob, job_id)).status == "completed"


@pytest.mark.asyncio
async def test_job_pauses_with_reason_when_verification_gets_disabled(session, fake_verifier):
    await _enable_custom_provider(session)
    campaign, leads = await _campaign_with_leads(session, "a@test.com")
    job, _, _ = await vq.enqueue_verification(session, [leads[0].id], campaign_id=campaign.id)
    job_id = job.id
    await put_setting(session, EMAIL_VERIFICATION_ENABLED, "false")
    await session.commit()

    await vq.process_next_item()
    session.expire_all()
    paused = await session.get(VerificationJob, job_id)
    assert paused.status == "paused" and "disabled" in paused.last_error
    assert (await _statuses(session))["a@test.com"] == "pending"


@pytest.mark.asyncio
async def test_cancel_releases_unverified_leads(session, fake_verifier):
    script, _ = fake_verifier
    script["flaky@test.com"] = "error"
    await _enable_custom_provider(session)
    campaign, leads = await _campaign_with_leads(
        session, "flaky@test.com", "never@test.com", "pre@test.com",
    )
    job, _, _ = await vq.enqueue_verification(session, [l.id for l in leads], campaign_id=campaign.id)
    job_id = job.id
    await session.commit()
    assert await vq.process_next_item()  # flaky → failed

    _, released = await vq.cancel_job(session, job_id)
    await session.commit()

    assert released == 3
    statuses = await _statuses(session)
    assert statuses == {"flaky@test.com": None, "never@test.com": None, "pre@test.com": None}
    session.expire_all()
    summary = await vq.job_summary(session, await session.get(VerificationJob, job_id))
    assert (summary["status"], summary["cancelled"], summary["remaining"]) == ("cancelled", 3, 0)
    with pytest.raises(vq.JobActionError):
        await vq.retry_failed(session, job_id)


@pytest.mark.asyncio
async def test_interrupted_items_are_requeued_on_startup(session, fake_verifier):
    await _enable_custom_provider(session)
    campaign, leads = await _campaign_with_leads(session, "a@test.com")
    job, _, _ = await vq.enqueue_verification(session, [leads[0].id], campaign_id=campaign.id)
    job_id = job.id
    await session.flush()
    item = (await session.execute(select(VerificationJobItem))).scalar_one()
    item.status = "processing"  # as if the app died mid-request
    job.status = "running"
    await session.commit()

    await vq._recover_interrupted_items()
    await _drain()

    assert (await _statuses(session))["a@test.com"] == "valid"


@pytest.mark.asyncio
async def test_job_items_endpoint_lists_failed_with_email(session, fake_verifier):
    from app.routers.verification_jobs import list_verification_job_items

    script, _ = fake_verifier
    script["flaky@test.com"] = "error"
    await _enable_custom_provider(session)
    campaign, leads = await _campaign_with_leads(session, "ok@test.com", "flaky@test.com")
    job, _, _ = await vq.enqueue_verification(session, [l.id for l in leads], campaign_id=campaign.id)
    job_id = job.id
    await session.commit()
    await _drain()

    items = await list_verification_job_items(job_id, status="failed", limit=50, offset=0, db=session)
    assert [(i["email"], i["status"], i["message"]) for i in items] == [("flaky@test.com", "failed", "HTTP 503")]


@pytest.mark.asyncio
async def test_worker_rate_limits_requests_from_setting(session, fake_verifier, monkeypatch):
    import time

    from app.app_settings import EMAIL_VERIFICATION_MIN_INTERVAL

    monkeypatch.undo()  # drop the no_rate_limit / fake_verifier patches...
    monkeypatch.setattr(vq, "_last_request_at", None)
    stamps: list[float] = []

    async def _verify(email):
        stamps.append(time.monotonic())
        return VerificationResult(email, VALID)

    async def _loader(db):
        return _verify, None

    monkeypatch.setattr(vq, "load_email_verifier", _loader)  # ...but keep a fake provider

    await _enable_custom_provider(session)
    await put_setting(session, EMAIL_VERIFICATION_MIN_INTERVAL, "0.3")
    campaign, leads = await _campaign_with_leads(session, "a@test.com", "b@test.com", "c@test.com")
    await vq.enqueue_verification(session, [l.id for l in leads], campaign_id=campaign.id)
    await session.commit()
    await _drain()

    assert len(stamps) == 3
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.29 for g in gaps), gaps


@pytest.mark.asyncio
async def test_min_interval_setting_parsing(session):
    from app.app_settings import EMAIL_VERIFICATION_MIN_INTERVAL

    assert await _REAL_LOAD_MIN_INTERVAL(session) == 2.0  # default
    for raw, expected in (("5", 5.0), ("0.5", 0.5), ("-1", 0.0), ("abc", 2.0), ("", 2.0)):
        await put_setting(session, EMAIL_VERIFICATION_MIN_INTERVAL, raw)
        await session.commit()
        assert await _REAL_LOAD_MIN_INTERVAL(session) == expected, raw
