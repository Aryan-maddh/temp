"""CRUD API for per-candidate job-site credentials (platform + email + password)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.models import Candidate, Credential
from app.core.schemas import CredentialCreate, CredentialOut, CredentialUpdate

router = APIRouter()


@router.get("", response_model=list[CredentialOut])
async def list_credentials(
    candidate_id: uuid.UUID | None = None,
    platform: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List all credentials, optionally filtered by candidate and/or platform."""
    stmt = select(Credential).order_by(Credential.created_at.desc())
    if candidate_id:
        stmt = stmt.where(Credential.candidate_id == candidate_id)
    if platform:
        stmt = stmt.where(Credential.platform == platform)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=CredentialOut, status_code=201)
async def create_credential(
    payload: CredentialCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a new credential for a candidate (upserts on candidate_id + platform)."""
    candidate = await db.get(Candidate, payload.candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")

    # Upsert: update if (candidate_id, platform) already exists
    existing = (await db.execute(
        select(Credential).where(
            Credential.candidate_id == payload.candidate_id,
            Credential.platform == payload.platform,
        )
    )).scalar_one_or_none()

    if existing:
        existing.email = payload.email
        existing.password = payload.password
        await db.flush()
        return existing

    cred = Credential(**payload.model_dump())
    db.add(cred)
    await db.flush()
    return cred


@router.get("/{credential_id}", response_model=CredentialOut)
async def get_credential(
    credential_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    cred = await db.get(Credential, credential_id)
    if not cred:
        raise HTTPException(404, "Credential not found")
    return cred


@router.put("/{credential_id}", response_model=CredentialOut)
async def update_credential(
    credential_id: uuid.UUID,
    payload: CredentialUpdate,
    db: AsyncSession = Depends(get_db),
):
    cred = await db.get(Credential, credential_id)
    if not cred:
        raise HTTPException(404, "Credential not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(cred, field, value)
    await db.flush()
    return cred


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    cred = await db.get(Credential, credential_id)
    if not cred:
        raise HTTPException(404, "Credential not found")
    await db.delete(cred)
