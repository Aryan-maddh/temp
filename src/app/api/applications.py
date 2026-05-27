import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, get_db
from app.core.models import Application, Candidate, Job, PlatformRun, RunLog, UnansweredQuestion
from app.core.schemas import (
    ApplicationOut,
    ApplicationStart,
    ManualAnswer,
    PlatformRunOut,
    UnansweredQuestionOut,
)

router = APIRouter()
log = logging.getLogger(__name__)

# Keep references to in-flight dispatch tasks so the asyncio loop's GC doesn't
# cancel them mid-run. (asyncio.create_task only weakly references the task.)
_DISPATCH_TASKS: set[asyncio.Task] = set()


def _track_task(task: asyncio.Task) -> None:
    _DISPATCH_TASKS.add(task)
    task.add_done_callback(_DISPATCH_TASKS.discard)


@router.post("/start", response_model=list[ApplicationOut], status_code=201)
async def start_applications(
    payload: ApplicationStart,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    candidate = await db.get(Candidate, payload.candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")

    created: list[Application] = []
    for job_id in payload.job_ids:
        job = await db.get(Job, job_id)
        if not job:
            continue

        # Skip if a successful application already exists for this pair
        already_applied = await db.execute(
            select(Application).where(
                Application.candidate_id == payload.candidate_id,
                Application.job_id == job_id,
                Application.status == "applied",
            )
        )
        if already_applied.scalar_one_or_none():
            continue

        app = Application(
            candidate_id=payload.candidate_id,
            job_id=job_id,
            status="queued",
        )
        db.add(app)
        await db.flush()
        created.append(app)

    # Commit explicitly so the spawned tasks see status="queued" in their own
    # session. (get_db's auto-commit happens after the route handler returns,
    # which is too late — asyncio.create_task fires immediately.)
    await db.commit()

    for app in created:
        spawn_dispatch(str(app.id))

    return created


@router.get("", response_model=list[ApplicationOut])
async def list_applications(
    status: str | None = None,
    candidate_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Application).order_by(Application.created_at.desc())
    if status:
        stmt = stmt.where(Application.status == status)
    if candidate_id:
        stmt = stmt.where(Application.candidate_id == candidate_id)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{application_id}", response_model=ApplicationOut)
async def get_application(
    application_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    app = await db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")
    return app


@router.post("/{application_id}/retry", response_model=ApplicationOut)
async def retry_application(
    application_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    app = await db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")
    if app.status not in {"failed", "needs_manual"}:
        raise HTTPException(409, f"Cannot retry application with status '{app.status}'")

    app.status = "queued"
    app.last_error = None
    app.started_at = None
    app.completed_at = None
    await db.commit()  # commit BEFORE spawning so the new task sees queued

    spawn_dispatch(str(application_id))
    return app


@router.get("/{application_id}/unanswered", response_model=list[UnansweredQuestionOut])
async def get_unanswered(
    application_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    app = await db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")

    result = await db.execute(
        select(UnansweredQuestion)
        .where(
            UnansweredQuestion.application_id == application_id,
            UnansweredQuestion.answered_at.is_(None),
        )
        .order_by(UnansweredQuestion.created_at)
    )
    return result.scalars().all()


@router.post("/{application_id}/answer", response_model=UnansweredQuestionOut)
async def submit_answer(
    application_id: uuid.UUID,
    payload: ManualAnswer,
    db: AsyncSession = Depends(get_db),
):
    app = await db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")

    question = await db.get(UnansweredQuestion, payload.question_id)
    if not question or question.application_id != application_id:
        raise HTTPException(404, "Question not found for this application")

    question.recruiter_answer = payload.answer
    question.answered_at = datetime.now(timezone.utc)
    await db.flush()
    return question


@router.get("/{application_id}/platform_run", response_model=PlatformRunOut | None)
async def get_platform_run(
    application_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    app = await db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")

    result = await db.execute(
        select(PlatformRun)
        .where(PlatformRun.application_id == application_id)
        .order_by(PlatformRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _dispatch(application_id: str) -> None:
    """Run an application end-to-end in the background.

    Critical: previous version silently swallowed all exceptions, so when
    Playwright/browser/adapter failed the user saw the application stuck on
    "queued" with no error. Now we mark the application as failed and write
    a RunLog entry so the UI surfaces what broke.
    """
    try:
        from app.engine.browser import run_application
    except ImportError as exc:
        log.error("browser engine not importable: %s", exc)
        await _mark_dispatch_failed(application_id, f"Browser engine import failed: {exc}")
        return

    try:
        await run_application(application_id)
    except Exception as exc:
        log.exception("dispatch failed for application %s", application_id)
        await _mark_dispatch_failed(application_id, str(exc))


async def _mark_dispatch_failed(application_id: str, error: str) -> None:
    """Write a failure record so the user can see what went wrong."""
    try:
        app_uuid = uuid.UUID(application_id)
    except ValueError:
        return
    try:
        async with AsyncSessionLocal() as db:
            app = await db.get(Application, app_uuid)
            if app and app.status in ("queued", "running"):
                app.status = "failed"
                app.last_error = (error or "dispatch failed")[:2000]
                app.completed_at = datetime.now(timezone.utc)
            db.add(RunLog(
                application_id=app_uuid,
                log_level="error",
                message=f"Dispatch failed: {error[:500]}",
            ))
            await db.commit()
    except Exception:
        log.exception("could not persist dispatch failure for %s", application_id)


def spawn_dispatch(application_id: str) -> None:
    """Schedule _dispatch on the running event loop and retain a reference.

    Used by both the start endpoint and the unanswered-questions batch-answer
    restart path. Returns immediately.
    """
    try:
        task = asyncio.create_task(_dispatch(application_id))
        _track_task(task)
    except RuntimeError:
        log.error("spawn_dispatch called outside event loop for %s", application_id)