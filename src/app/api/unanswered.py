"""Cross-application unanswered questions router.

Surfaces all pending form fields in one place so the ManualAnswersPage
can collect answers without navigating per-application.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.engine.platform_adapters import platform_key_for_url
from app.engine.site_rules import save_field_rule
from app.core.models import Application, FormAnswer, Job, UnansweredQuestion
from app.core.schemas import UnansweredQuestionOut

router = APIRouter()


class UnansweredQuestionWithDomain(UnansweredQuestionOut):
    domain: str | None = None
    manual_blocker: bool = False
    last_error: str | None = None


class BatchAnswerItem(BaseModel):
    question_id: uuid.UUID
    answer: str


class BatchAnswerPayload(BaseModel):
    answers: list[BatchAnswerItem]
    save_rule: bool = False


class BatchAnswerResult(BaseModel):
    answered_count: int
    application_restarted: list[uuid.UUID] = []


class UniqueQuestion(BaseModel):
    field_label: str
    field_type: str | None = None
    options: list[str] | None = None
    count: int
    question_ids: list[uuid.UUID]
    sample_domain: str | None = None


class AnswerByLabelPayload(BaseModel):
    field_label: str
    answer: str
    field_type: str | None = None
    save_rule: bool = True


def _rule_action_for_field_type(field_type: object) -> str:
    normalized = str(field_type or "").strip().lower()
    if "select" in normalized or "dropdown" in normalized or "combobox" in normalized:
        return "select"
    if "radio" in normalized:
        return "radio"
    if "checkbox" in normalized or normalized == "check":
        return "check"
    return "fill"


async def _queue_resolved_applications(
    db: AsyncSession,
    application_ids: set[uuid.UUID] | list[uuid.UUID],
) -> list[uuid.UUID]:
    restarted: list[uuid.UUID] = []
    await db.flush()
    for app_id in dict.fromkeys(application_ids):
        app = await db.get(Application, app_id)
        if not app or app.status not in {"needs_manual", "validation_error", "failed"}:
            continue
        remaining = (await db.execute(
            select(UnansweredQuestion.id).where(
                UnansweredQuestion.application_id == app_id,
                UnansweredQuestion.answered_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        if remaining:
            continue
        app.status = "queued"
        app.last_error = None
        app.started_at = None
        app.completed_at = None
        restarted.append(app_id)
    return restarted


def _dispatch_restarted(application_ids: list[uuid.UUID]) -> None:
    try:
        from app.api.applications import spawn_dispatch
    except Exception:
        return
    for app_id in application_ids:
        spawn_dispatch(str(app_id))


# ── GET /api/unanswered-questions ──────────────────────────────────────────────

@router.get("", response_model=list[UnansweredQuestionWithDomain])
async def list_unanswered(
    include_answered: bool = False,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(UnansweredQuestion)
        .options(
            selectinload(UnansweredQuestion.application).selectinload(Application.job)
        )
        .order_by(UnansweredQuestion.created_at.desc())
    )
    if not include_answered:
        stmt = stmt.where(UnansweredQuestion.answered_at.is_(None))

    rows = (await db.execute(stmt)).scalars().all()

    out: list[UnansweredQuestionWithDomain] = []
    for q in rows:
        job_url = ""
        if q.application and q.application.job:
            job_url = q.application.job.url or ""
        domain = urlparse(job_url).netloc or "Unknown domain"
        data = UnansweredQuestionOut.model_validate(q).model_dump()
        out.append(UnansweredQuestionWithDomain(**data, domain=domain))
    return out


# ── POST /api/unanswered-questions/batch-answer ────────────────────────────────
# Declared before /{question_id}/answer so FastAPI matches the literal path first.

@router.post("/batch-answer", response_model=BatchAnswerResult)
async def batch_answer(
    payload: BatchAnswerPayload,
    db: AsyncSession = Depends(get_db),
):
    answered_count = 0
    affected_app_ids: set[uuid.UUID] = set()

    for item in payload.answers:
        if not item.answer.strip():
            continue

        result = await db.execute(
            select(UnansweredQuestion)
            .options(
                selectinload(UnansweredQuestion.application).selectinload(Application.job)
            )
            .where(UnansweredQuestion.id == item.question_id)
        )
        q = result.scalar_one_or_none()
        if not q or q.answered_at:
            continue

        q.recruiter_answer = item.answer.strip()
        q.answered_at = datetime.now(timezone.utc)
        answered_count += 1
        affected_app_ids.add(q.application_id)

        if payload.save_rule and q.field_label and q.candidate_id:
            job_url = ""
            if q.application and q.application.job:
                job_url = q.application.job.url or ""
            raw_domain = urlparse(job_url).netloc or ""
            platform = platform_key_for_url(job_url) if job_url else raw_domain

            existing = (await db.execute(
                select(FormAnswer).where(
                    FormAnswer.candidate_id == q.candidate_id,
                    FormAnswer.platform == platform,
                    FormAnswer.question_text == q.field_label,
                )
            )).scalar_one_or_none()

            if existing:
                existing.answer = item.answer.strip()
            else:
                db.add(FormAnswer(
                    candidate_id=q.candidate_id,
                    platform=platform,
                    question_text=q.field_label,
                    answer=item.answer.strip(),
                ))

            if raw_domain and q.field_label and q.field_type:
                save_field_rule(
                    domain=raw_domain,
                    label=q.field_label,
                    field_type=str(q.field_type or ""),
                    action=_rule_action_for_field_type(q.field_type),
                    value=item.answer.strip(),
                    source="manual",
                    reason="manual answer from user",
                    options=list(q.options or []) or None,
                )

    # Restart any needs_manual application that now has all required questions answered.
    # We collect the IDs first, commit the status change, THEN spawn the restart task.
    # Spawning before commit caused a race: the new task opened a fresh session,
    # didn't see the uncommitted status="queued", and silently failed its atomic
    # claim — leaving the application stuck on "needs_manual" forever.
    restarted = await _queue_resolved_applications(db, affected_app_ids)

    # Always commit answers and status changes before spawning.
    await db.commit()

    # Spawn dispatches now that the rows are visible to other sessions.
    if restarted:
        _dispatch_restarted(restarted)

    return BatchAnswerResult(answered_count=answered_count, application_restarted=restarted)


# ── POST /api/unanswered-questions/{question_id}/answer ───────────────────────

@router.post("/{question_id}/answer", response_model=UnansweredQuestionOut)
async def answer_question(
    question_id: uuid.UUID,
    payload: BatchAnswerItem,
    db: AsyncSession = Depends(get_db),
):
    q = (await db.execute(
        select(UnansweredQuestion)
        .options(selectinload(UnansweredQuestion.application).selectinload(Application.job))
        .where(UnansweredQuestion.id == question_id)
    )).scalar_one_or_none()
    if not q:
        raise HTTPException(404, "Question not found")

    answer = payload.answer.strip()
    q.recruiter_answer = answer
    q.answered_at = datetime.now(timezone.utc)

    # Also save to form_answers + form_rules so future applications can reuse
    # the answer without re-asking the candidate. The batch-answer endpoint
    # does this when save_rule=true; the single-answer endpoint must do the
    # same or candidate-provided answers stay siloed to one application.
    if q.field_label and q.candidate_id:
        job_url = ""
        if q.application and q.application.job:
            job_url = q.application.job.url or ""
        raw_domain = urlparse(job_url).netloc or q.domain or ""
        platform = platform_key_for_url(job_url) if job_url else raw_domain
        existing = (await db.execute(
            select(FormAnswer).where(
                FormAnswer.candidate_id == q.candidate_id,
                FormAnswer.platform == platform,
                FormAnswer.question_text == q.field_label,
            )
        )).scalar_one_or_none()
        if existing:
            existing.answer = answer
        else:
            db.add(FormAnswer(
                candidate_id=q.candidate_id,
                platform=platform,
                question_text=q.field_label,
                answer=answer,
            ))
        if raw_domain and q.field_label and q.field_type:
            save_field_rule(
                domain=raw_domain,
                label=q.field_label,
                field_type=str(q.field_type or ""),
                action=_rule_action_for_field_type(q.field_type),
                value=answer,
                source="manual",
                reason="manual answer via single-question endpoint",
                options=list(q.options or []) or None,
            )

    restarted = await _queue_resolved_applications(db, [q.application_id])
    await db.commit()
    if restarted:
        _dispatch_restarted(restarted)
    return q


# ── GET /api/unanswered-questions/unique ──────────────────────────────────────
# Must be declared AFTER batch-answer but BEFORE /{question_id}/answer so the
# literal path "unique" is matched before the UUID pattern.

@router.get("/unique", response_model=list[UniqueQuestion])
async def list_unique_unanswered(
    db: AsyncSession = Depends(get_db),
):
    """Deduplicated view: one entry per field_label, with count of pending applications."""
    rows = (await db.execute(
        select(UnansweredQuestion)
        .options(
            selectinload(UnansweredQuestion.application).selectinload(Application.job)
        )
        .where(UnansweredQuestion.answered_at.is_(None))
        .order_by(UnansweredQuestion.created_at.asc())
    )).scalars().all()

    # Group by normalised label (case-insensitive, stripped)
    groups: dict[str, UniqueQuestion] = {}
    for q in rows:
        key = str(q.field_label or "").strip().lower()
        if not key:
            continue
        job_url = ""
        if q.application and q.application.job:
            job_url = q.application.job.url or ""
        domain = urlparse(job_url).netloc or q.domain or ""

        if key not in groups:
            groups[key] = UniqueQuestion(
                field_label=str(q.field_label or ""),
                field_type=q.field_type,
                options=list(q.options) if q.options else None,
                count=0,
                question_ids=[],
                sample_domain=domain or None,
            )
        entry = groups[key]
        entry.count += 1
        entry.question_ids.append(q.id)
        # Prefer the entry with the richest options list
        current_opts = entry.options or []
        new_opts = list(q.options) if q.options else []
        if len(new_opts) > len(current_opts):
            entry.options = new_opts
            entry.field_type = q.field_type

    return sorted(groups.values(), key=lambda u: u.count, reverse=True)


# ── POST /api/unanswered-questions/answer-all-by-label ────────────────────────

@router.post("/answer-all-by-label", response_model=BatchAnswerResult)
async def answer_all_by_label(
    payload: AnswerByLabelPayload,
    db: AsyncSession = Depends(get_db),
):
    """Answer every pending question that shares the same field_label, and save a rule."""
    if not payload.answer.strip() or not payload.field_label.strip():
        raise HTTPException(400, "field_label and answer are required")

    label_key = payload.field_label.strip().lower()
    rows = (await db.execute(
        select(UnansweredQuestion)
        .options(
            selectinload(UnansweredQuestion.application).selectinload(Application.job)
        )
        .where(UnansweredQuestion.answered_at.is_(None))
    )).scalars().all()

    matching = [q for q in rows if str(q.field_label or "").strip().lower() == label_key]
    if not matching:
        return BatchAnswerResult(answered_count=0)

    now = datetime.now(timezone.utc)
    affected_app_ids: set[uuid.UUID] = set()

    for q in matching:
        q.recruiter_answer = payload.answer.strip()
        q.answered_at = now
        affected_app_ids.add(q.application_id)

        if payload.save_rule and q.candidate_id:
            job_url = ""
            if q.application and q.application.job:
                job_url = q.application.job.url or ""
            raw_domain = urlparse(job_url).netloc or q.domain or ""
            platform = platform_key_for_url(job_url) if job_url else raw_domain
            field_type = payload.field_type or str(q.field_type or "")

            existing = (await db.execute(
                select(FormAnswer).where(
                    FormAnswer.candidate_id == q.candidate_id,
                    FormAnswer.platform == platform,
                    FormAnswer.question_text == q.field_label,
                )
            )).scalar_one_or_none()
            if existing:
                existing.answer = payload.answer.strip()
            else:
                db.add(FormAnswer(
                    candidate_id=q.candidate_id,
                    platform=platform,
                    question_text=q.field_label,
                    answer=payload.answer.strip(),
                ))

            if raw_domain and q.field_label and field_type:
                save_field_rule(
                    domain=raw_domain,
                    label=q.field_label,
                    field_type=field_type,
                    action=_rule_action_for_field_type(field_type),
                    value=payload.answer.strip(),
                    source="manual",
                    reason="answer-all-by-label: taught from training run",
                    options=list(q.options or []) or None,
                )

    restarted = await _queue_resolved_applications(db, affected_app_ids)
    await db.commit()
    if restarted:
        _dispatch_restarted(restarted)

    return BatchAnswerResult(answered_count=len(matching), application_restarted=restarted)