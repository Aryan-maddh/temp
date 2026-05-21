from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import RunLog


LOG_CHANNEL = "run_logs"


def log_channel(application_id: UUID) -> str:
    return f"logs_{application_id}"


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def serialize_run_log(log: RunLog) -> dict[str, object]:
    return {
        "id": str(log.id),
        "application_id": str(log.application_id) if log.application_id else None,
        "level": log.log_level,
        "message": log.message,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


async def write_run_log(
    session: AsyncSession,
    application_id: UUID,
    level: str,
    message: str,
) -> RunLog:
    log = RunLog(application_id=application_id, log_level=level, message=message)
    session.add(log)
    await session.flush()
    await session.refresh(log)

    payload = json.dumps(serialize_run_log(log), default=_json_default)
    await session.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": LOG_CHANNEL, "payload": payload})
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": log_channel(application_id), "payload": payload},
    )
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": f"app_logs_{application_id}", "payload": payload},
    )
    return log


async def list_run_logs(session: AsyncSession, application_id: UUID) -> list[RunLog]:
    result = await session.execute(
        select(RunLog)
        .where(RunLog.application_id == application_id)
        .order_by(RunLog.created_at.asc())
    )
    return list(result.scalars().all())