import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.models import Job
from backend.schemas import JobBulkImport, JobOut, JobStatusUpdate

router = APIRouter()

_VALID_STATUSES = {"active", "expired", "removed"}


@router.post("/bulk", response_model=list[JobOut], status_code=201)
async def bulk_import_jobs(
    payload: JobBulkImport,
    db: AsyncSession = Depends(get_db),
):
    created: list[Job] = []
    for raw_url in payload.urls:
        url = raw_url.strip()
        if not url:
            continue
        existing = await db.execute(select(Job).where(Job.url == url))
        if existing.scalar_one_or_none():
            continue
        parsed = urlparse(url)
        title = (parsed.netloc + parsed.path).strip("/") or url
        job = Job(url=url, title=title)
        db.add(job)
        await db.flush()
        created.append(job)
    return created


@router.get("", response_model=list[JobOut])
async def list_jobs(
    status: str | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Job).order_by(Job.created_at.desc())
    if status:
        stmt = stmt.where(Job.status == status)
    if search:
        stmt = stmt.where(Job.title.ilike(f"%{search}%"))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.put("/{job_id}/status", response_model=JobOut)
async def update_job_status(
    job_id: uuid.UUID,
    payload: JobStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    if payload.status not in _VALID_STATUSES:
        raise HTTPException(422, f"status must be one of: {', '.join(sorted(_VALID_STATUSES))}")
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.status = payload.status
    await db.flush()
    return job