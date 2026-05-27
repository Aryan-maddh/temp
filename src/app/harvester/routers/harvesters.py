from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, get_db
from app.harvester.engine.job_harvester import LINKEDIN_DOMAIN, run_job_harvester
from app.core.models import Credential, JobHarvesterConfig, JobHarvesterItem, JobHarvesterRun

router = APIRouter()
_scheduled_configs: set[uuid.UUID] = set()


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class HarvesterConfigCreate(BaseModel):
    source: str = "linkedin"
    keyword: str
    location: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    max_jobs_per_run: int = 15
    schedule_enabled: bool = False
    schedule_time: str | None = None
    timezone: str = "Asia/Kolkata"
    schedule_jitter_minutes: int = 20
    linkedin_email: str | None = None
    linkedin_password: str | None = None


class HarvesterConfigUpdate(BaseModel):
    keyword: str | None = None
    location: str | None = None
    filters: dict[str, Any] | None = None
    max_jobs_per_run: int | None = None
    schedule_enabled: bool | None = None
    schedule_time: str | None = None
    timezone: str | None = None
    schedule_jitter_minutes: int | None = None
    linkedin_email: str | None = None
    linkedin_password: str | None = None


def _row(obj) -> dict:
    result: dict = {}
    for col in obj.__table__.columns:
        val = getattr(obj, col.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, uuid.UUID):
            val = str(val)
        result[col.name] = val
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_source(source: str) -> str:
    normalized = source.strip().lower()
    if normalized != "linkedin":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only LinkedIn is supported")
    return normalized


def _validate_time(value: str | None) -> None:
    if not value:
        return
    try:
        h, m = [int(p) for p in value.split(":", 1)]
        assert 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Schedule time must be HH:MM")


async def _upsert_linkedin_credential(db: AsyncSession, email: str | None, password: str | None) -> None:
    if not email and not password:
        return
    if not email or not password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Both LinkedIn email and password are required")
    result = await db.execute(
        select(Credential)
        .where(Credential.candidate_id.is_(None))
        .where(Credential.platform == LINKEDIN_DOMAIN)
    )
    cred = result.scalars().first()
    if cred is None:
        db.add(Credential(candidate_id=None, platform=LINKEDIN_DOMAIN, email=email, password=password))
    else:
        cred.email = email
        cred.password = password


async def _create_run(db: AsyncSession, config_id: uuid.UUID) -> JobHarvesterRun:
    config = await db.get(JobHarvesterConfig, config_id)
    if not config:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Harvester config not found")
    if config.status in {"queued", "running"}:
        active_result = await db.execute(
            select(JobHarvesterRun)
            .where(JobHarvesterRun.config_id == config.id)
            .where(JobHarvesterRun.status.in_(["queued", "running"]))
            .order_by(JobHarvesterRun.created_at.desc())
        )
        active_run = active_result.scalars().first()
        now = datetime.now(timezone.utc)
        stale_unstarted = (
            active_run is not None
            and active_run.status == "queued"
            and active_run.started_at is None
            and active_run.created_at is not None
            and active_run.created_at < now - timedelta(minutes=2)
        )
        if stale_unstarted:
            active_run.status = "needs_manual"
            active_run.error = "Queued run did not start; reset before creating a new run"
            active_run.completed_at = now
            config.status = "idle"
        else:
            raise HTTPException(status.HTTP_409_CONFLICT, "Harvester is already running")
    result = await db.execute(
        select(Credential).where(Credential.candidate_id.is_(None)).where(Credential.platform == LINKEDIN_DOMAIN)
    )
    if not result.scalars().first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add LinkedIn email/password before running")
    run = JobHarvesterRun(config_id=config.id)
    config.status = "queued"
    db.add(run)
    await db.flush()
    return run


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_harvester(payload: HarvesterConfigCreate, db: AsyncSession = Depends(get_db)):
    _validate_source(payload.source)
    _validate_time(payload.schedule_time)
    await _upsert_linkedin_credential(db, payload.linkedin_email, payload.linkedin_password)
    data = payload.model_dump(exclude={"linkedin_email", "linkedin_password"})
    data["source"] = _validate_source(payload.source)
    config = JobHarvesterConfig(**data)
    db.add(config)
    await db.flush()
    return _row(config)


@router.get("")
async def list_harvesters(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(JobHarvesterConfig).order_by(JobHarvesterConfig.created_at.desc()))
    return [_row(c) for c in result.scalars().all()]


@router.put("/{config_id}")
async def update_harvester(config_id: uuid.UUID, payload: HarvesterConfigUpdate, db: AsyncSession = Depends(get_db)):
    config = await db.get(JobHarvesterConfig, config_id)
    if not config:
        raise HTTPException(404, "Harvester config not found")
    await _upsert_linkedin_credential(db, payload.linkedin_email, payload.linkedin_password)
    data = payload.model_dump(exclude={"linkedin_email", "linkedin_password"}, exclude_unset=True, exclude_none=True)
    if "schedule_time" in data:
        _validate_time(data["schedule_time"])
    for field, value in data.items():
        setattr(config, field, value)
    await db.flush()
    return _row(config)


@router.post("/{config_id}/run", status_code=201)
async def run_harvester_now(config_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    run = await _create_run(db, config_id)
    run_id = run.id
    await db.commit()
    asyncio.create_task(run_job_harvester(run_id))
    return _row(run)


@router.get("/{config_id}/runs")
async def list_harvester_runs(config_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(JobHarvesterRun)
        .where(JobHarvesterRun.config_id == config_id)
        .order_by(JobHarvesterRun.created_at.desc())
        .limit(30)
    )
    return [_row(r) for r in result.scalars().all()]


@router.get("/runs/{run_id}/items")
async def list_harvester_items(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(JobHarvesterItem)
        .where(JobHarvesterItem.run_id == run_id)
        .order_by(JobHarvesterItem.created_at.desc())
    )
    return [_row(i) for i in result.scalars().all()]


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _due_today(config: JobHarvesterConfig, now_utc: datetime) -> bool:
    if not config.schedule_enabled or not config.schedule_time:
        return False
    try:
        tz = ZoneInfo(config.timezone or "Asia/Kolkata")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Kolkata")
    local = now_utc.astimezone(tz)
    h, m = [int(p) for p in config.schedule_time.split(":", 1)]
    scheduled = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if local < scheduled:
        return False
    if config.last_run_at and config.last_run_at.astimezone(tz).date() == local.date():
        return False
    return True


async def _run_scheduled(config_id: uuid.UUID, jitter_minutes: int) -> None:
    _scheduled_configs.add(config_id)
    try:
        if jitter_minutes > 0:
            await asyncio.sleep(random.randint(0, jitter_minutes * 60))
        async with AsyncSessionLocal() as db:
            run = await _create_run(db, config_id)
            run_id = run.id
            await db.commit()
        await run_job_harvester(run_id)
    finally:
        _scheduled_configs.discard(config_id)


async def scheduler_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(JobHarvesterConfig)
                    .where(JobHarvesterConfig.schedule_enabled.is_(True))
                    .where(JobHarvesterConfig.status == "idle")
                )
                configs = result.scalars().all()
            now_utc = datetime.now(timezone.utc)
            for config in configs:
                if config.id in _scheduled_configs:
                    continue
                if _due_today(config, now_utc):
                    asyncio.create_task(_run_scheduled(config.id, max(config.schedule_jitter_minutes or 0, 0)))
        except Exception:
            pass
