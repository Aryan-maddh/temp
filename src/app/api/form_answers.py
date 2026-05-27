"""CRUD API for candidate form answers (stored answers for job-application fields)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.models import Candidate, FormAnswer
from app.core.schemas import FormAnswerCreate, FormAnswerOut, FormAnswerUpdate

router = APIRouter()


@router.get("", response_model=list[FormAnswerOut])
async def list_form_answers(
    candidate_id: uuid.UUID | None = None,
    platform: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List stored form answers, optionally filtered by candidate and/or platform."""
    stmt = select(FormAnswer).order_by(FormAnswer.priority.desc(), FormAnswer.created_at.desc())
    if candidate_id:
        stmt = stmt.where(FormAnswer.candidate_id == candidate_id)
    if platform:
        stmt = stmt.where(FormAnswer.platform == platform)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=FormAnswerOut, status_code=201)
async def create_form_answer(
    payload: FormAnswerCreate,
    db: AsyncSession = Depends(get_db),
):
    """Save a new form answer for a candidate (upserts on candidate_id + platform + question_text)."""
    candidate = await db.get(Candidate, payload.candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")

    # Upsert: update if (candidate_id, platform, question_text) already exists
    if payload.question_text:
        existing = (await db.execute(
            select(FormAnswer).where(
                FormAnswer.candidate_id == payload.candidate_id,
                FormAnswer.platform == payload.platform,
                FormAnswer.question_text == payload.question_text,
            )
        )).scalar_one_or_none()

        if existing:
            for field, value in payload.model_dump(exclude_unset=True, exclude={"candidate_id"}).items():
                setattr(existing, field, value)
            await db.flush()
            return existing

    answer = FormAnswer(**payload.model_dump())
    db.add(answer)
    await db.flush()
    return answer


@router.get("/{answer_id}", response_model=FormAnswerOut)
async def get_form_answer(
    answer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    answer = await db.get(FormAnswer, answer_id)
    if not answer:
        raise HTTPException(404, "Form answer not found")
    return answer


@router.put("/{answer_id}", response_model=FormAnswerOut)
async def update_form_answer(
    answer_id: uuid.UUID,
    payload: FormAnswerUpdate,
    db: AsyncSession = Depends(get_db),
):
    answer = await db.get(FormAnswer, answer_id)
    if not answer:
        raise HTTPException(404, "Form answer not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(answer, field, value)
    await db.flush()
    return answer


@router.delete("/{answer_id}", status_code=204)
async def delete_form_answer(
    answer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    answer = await db.get(FormAnswer, answer_id)
    if not answer:
        raise HTTPException(404, "Form answer not found")
    await db.delete(answer)
