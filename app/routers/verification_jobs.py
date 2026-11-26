"""Email verification job queue: progress, items and pause / resume /
retry / cancel controls.  See :mod:`app.verification_queue`."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Lead, VerificationJob, VerificationJobItem
from app import verification_queue as vq

router = APIRouter(prefix="/api/verification-jobs", tags=["verification-jobs"])

_ITEM_STATUSES = {"pending", "processing", "done", "failed", "cancelled"}


async def _run_action(db: AsyncSession, coro):
    try:
        result = await coro
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except vq.JobActionError as exc:
        raise HTTPException(409, str(exc))
    await db.commit()
    return result


@router.get("")
async def list_verification_jobs(
    campaign_id: int | None = Query(None, description="Only jobs for this campaign"),
    active_only: bool = Query(False, description="Only queued, running or paused jobs"),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List verification jobs, newest first, with progress counts."""
    query = select(VerificationJob).order_by(VerificationJob.id.desc()).limit(limit)
    if campaign_id is not None:
        query = query.where(VerificationJob.campaign_id == campaign_id)
    if active_only:
        query = query.where(VerificationJob.status.in_(vq.ACTIVE_JOB_STATUSES))
    jobs = (await db.execute(query)).scalars().all()
    return [await vq.job_summary(db, job) for job in jobs]


@router.get("/{job_id}")
async def get_verification_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Progress of one job: totals, remaining, done, failed and results by status."""
    job = await db.get(VerificationJob, job_id)
    if job is None:
        raise HTTPException(404, f"Verification job {job_id} not found")
    return await vq.job_summary(db, job)


@router.get("/{job_id}/items")
async def list_verification_job_items(
    job_id: int,
    status: str | None = Query(None, description="pending, processing, done, failed or cancelled"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Leads in a job with their item status, result and error message."""
    if await db.get(VerificationJob, job_id) is None:
        raise HTTPException(404, f"Verification job {job_id} not found")
    query = (
        select(VerificationJobItem, Lead.email)
        .join(Lead, Lead.id == VerificationJobItem.lead_id)
        .where(VerificationJobItem.job_id == job_id)
        .order_by(VerificationJobItem.id)
        .limit(limit)
        .offset(offset)
    )
    if status:
        if status not in _ITEM_STATUSES:
            raise HTTPException(400, f"status must be one of {sorted(_ITEM_STATUSES)}")
        query = query.where(VerificationJobItem.status == status)
    rows = (await db.execute(query)).all()
    return [
        {
            "id": item.id,
            "lead_id": item.lead_id,
            "email": email,
            "status": item.status,
            "result_status": item.result_status,
            "message": item.message,
            "attempts": item.attempts,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }
        for item, email in rows
    ]


@router.post("/{job_id}/pause")
async def pause_verification_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Pause after the lead currently being checked."""
    job = await _run_action(db, vq.pause_job(db, job_id))
    return await vq.job_summary(db, job)


@router.post("/{job_id}/resume")
async def resume_verification_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Resume a paused job where it stopped."""
    job = await _run_action(db, vq.resume_job(db, job_id))
    return await vq.job_summary(db, job)


@router.post("/{job_id}/retry-failed")
async def retry_failed_verification_items(job_id: int, db: AsyncSession = Depends(get_db)):
    """Queue the job's failed leads again."""
    job, retried = await _run_action(db, vq.retry_failed(db, job_id))
    return {"retried": retried, "job": await vq.job_summary(db, job)}


@router.post("/{job_id}/cancel")
async def cancel_verification_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Stop the job. Leads it did not verify are released and will send unverified."""
    job, released = await _run_action(db, vq.cancel_job(db, job_id))
    return {"released": released, "job": await vq.job_summary(db, job)}
