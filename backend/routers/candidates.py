import uuid
from pathlib import Path
from shutil import copy2

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.models import Candidate
from backend.schemas import (
    CandidateCreate,
    CandidateOut,
    CandidateUpdate,
    ResumePathRequest,
    ResumePathOut,
)

router = APIRouter()


@router.post("", response_model=CandidateOut, status_code=201)
async def create_candidate(
    payload: CandidateCreate,
    db: AsyncSession = Depends(get_db),
):
    candidate = Candidate(**payload.model_dump())
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
