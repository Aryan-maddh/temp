from __future__ import annotations

import re
import uuid
from pathlib import Path
from shutil import copy2
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.models import Candidate, Credential
from app.core.schemas import (
    CandidateCreate,
    CandidateOut,
    CandidateUpdate,
    CredentialOut,
    ResumePathOut,
    ResumePathRequest,
)

router = APIRouter()


# ── Extended candidate response that includes connectivity status ──────────────

class CandidateConnectedOut(CandidateOut):
    credential_count: int = 0
    outlook_connected: bool = False
    connected_outlook_accounts: list[str] = []


def _outlook_accounts_for_email(email: str) -> list[str]:
    """Return Outlook accounts connected for a candidate email (token file exists)."""
    token_dir = Path("sessions/outlook_tokens")
    if not token_dir.exists():
        return []
    # Match token files that contain the candidate's email address prefix
    safe = re.sub(r"[^a-zA-Z0-9._+-]", "_", email.strip().lower())
    matches = list(token_dir.glob(f"{safe}.json"))
    # Also scan all tokens and check via MSAL
    accounts: list[str] = []
    for token_file in token_dir.glob("*.json"):
        try:
            import msal  # type: ignore[import]
            cache = msal.SerializableTokenCache()
            cache.deserialize(token_file.read_text(encoding="utf-8"))
            app_msal = msal.PublicClientApplication(
                client_id=str(__import__("os").getenv("OUTLOOK_CLIENT_ID") or ""),
                authority="https://login.microsoftonline.com/common",
                token_cache=cache,
            )
            for acct in app_msal.get_accounts():
                username = str(acct.get("username") or "")
                if username and username not in accounts:
                    accounts.append(username)
        except Exception:
            pass
    return accounts


# ── GET /api/candidates/connected ─────────────────────────────────────────────
# Declared BEFORE /{candidate_id} so the literal path is matched first.

@router.get("/connected", response_model=list[CandidateConnectedOut])
async def list_connected_candidates(db: AsyncSession = Depends(get_db)):
    """Return candidates that have at least one stored credential or connected Outlook account."""
    # Sub-query: count credentials per candidate
    cred_counts_q = (
        await db.execute(
            select(Credential.candidate_id, func.count(Credential.id).label("cnt"))
            .group_by(Credential.candidate_id)
        )
    )
    cred_map: dict[uuid.UUID, int] = {row.candidate_id: row.cnt for row in cred_counts_q}

    all_candidates = (await db.execute(select(Candidate).order_by(Candidate.created_at.desc()))).scalars().all()

    # Gather all Outlook accounts once (avoids hitting MSAL per-candidate)
    outlook_accounts: list[str] = []
    token_dir = Path("sessions/outlook_tokens")
    if token_dir.exists():
        for token_file in token_dir.glob("*.json"):
            try:
                import msal  # type: ignore[import]
                cache = msal.SerializableTokenCache()
                cache.deserialize(token_file.read_text(encoding="utf-8"))
                msal_app = msal.PublicClientApplication(
                    client_id=str(__import__("os").getenv("OUTLOOK_CLIENT_ID") or ""),
                    authority="https://login.microsoftonline.com/common",
                    token_cache=cache,
                )
                for acct in msal_app.get_accounts():
                    username = str(acct.get("username") or "")
                    if username and username not in outlook_accounts:
                        outlook_accounts.append(username)
            except Exception:
                pass

    result: list[CandidateConnectedOut] = []
    for candidate in all_candidates:
        cred_count = cred_map.get(candidate.id, 0)
        # Match Outlook accounts that belong to this candidate's email
        candidate_email = (candidate.email or "").lower()
        linked_outlook = [a for a in outlook_accounts if a.lower() == candidate_email]
        is_connected = cred_count > 0 or bool(linked_outlook)
        if is_connected:
            base = CandidateOut.model_validate(candidate).model_dump()
            result.append(CandidateConnectedOut(
                **base,
                credential_count=cred_count,
                outlook_connected=bool(linked_outlook),
                connected_outlook_accounts=linked_outlook,
            ))
    return result


@router.post("", response_model=CandidateOut, status_code=201)
async def create_candidate(
    payload: CandidateCreate,
    db: AsyncSession = Depends(get_db),
):
    data = payload.model_dump(exclude_none=True)

    # Map frontend field names → model column names
    if "name" in data and "first_name" not in data:
        parts = data.pop("name").split(" ", 1)
        data["first_name"] = parts[0]
        if len(parts) > 1:
            data["last_name"] = parts[1]
    else:
        data.pop("name", None)

    if "location" in data and "current_location" not in data:
        data["current_location"] = data.pop("location")
    else:
        data.pop("location", None)

    if "linkedin_url" in data and "linkedin_profile_url" not in data:
        data["linkedin_profile_url"] = data.pop("linkedin_url")
    else:
        data.pop("linkedin_url", None)

    candidate = Candidate(**data)
    db.add(candidate)
    await db.flush()
    return candidate


@router.get("", response_model=list[CandidateOut])
async def list_candidates(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Candidate).order_by(Candidate.created_at.desc()))
    return result.scalars().all()


@router.get("/{candidate_id}", response_model=CandidateOut)
async def get_candidate(
    candidate_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")
    return candidate


@router.put("/{candidate_id}", response_model=CandidateOut)
async def update_candidate(
    candidate_id: uuid.UUID,
    payload: CandidateUpdate,
    db: AsyncSession = Depends(get_db),
):
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(candidate, field, value)
    await db.flush()
    return candidate


@router.delete("/{candidate_id}", status_code=204)
async def delete_candidate(
    candidate_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")
    await db.delete(candidate)


@router.post("/{candidate_id}/resume", response_model=CandidateOut)
async def upload_resume(
    candidate_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    candidate = await db.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(422, "Only PDF files are accepted")

    resumes_dir = Path("resumes")
    resumes_dir.mkdir(exist_ok=True)
    dest = resumes_dir / f"{candidate_id}.pdf"
    dest.write_bytes(await file.read())

    candidate.resume_path = str(dest)
    extracted = _extract_pdf_text(dest)
    candidate.parsed_resume = {"text": extracted} if extracted else None
    await db.flush()
    return candidate


@router.post("/resume-path", response_model=ResumePathOut)
async def create_resume_path(
    payload: ResumePathRequest,
    db: AsyncSession = Depends(get_db),
):
    source = Path(payload.file_path).expanduser()
    if not source.is_file():
        raise HTTPException(404, "Resume file not found")
    if source.suffix.lower() != ".pdf":
        raise HTTPException(422, "Only PDF files are accepted")

    resumes_dir = Path("resumes")
    resumes_dir.mkdir(exist_ok=True)
    if source.resolve().parent == resumes_dir.resolve():
        dest = source
    else:
        dest = _unique_resume_path(source.name)

    copied = source.resolve() != dest.resolve()
    if copied:
        copy2(source, dest)

    if payload.candidate_id is not None:
        candidate = await db.get(Candidate, payload.candidate_id)
        if not candidate:
            raise HTTPException(404, "Candidate not found")
        candidate.resume_path = dest.as_posix()
        extracted = _extract_pdf_text(dest)
        candidate.parsed_resume = {"text": extracted} if extracted else None
        await db.flush()

    return ResumePathOut(
        original_path=str(source),
        resume_path=dest.as_posix(),
        copied=copied,
    )


def _unique_resume_path(filename: str) -> Path:
    resumes_dir = Path("resumes")
    dest = resumes_dir / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    counter = 1
    while dest.exists():
        dest = resumes_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return dest


def _extract_pdf_text(path: Path) -> str | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        return text or None
    except Exception:
        return None
