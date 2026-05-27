from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field


# ─── Candidates ───────────────────────────────────────────────────────────────

class CandidateCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # frontend sends "name"; backend model uses first_name/last_name
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str
    phone: str | None = None
    # frontend sends "location"; model column is current_location
    location: str | None = None
    current_location: str | None = None
    resume_path: str | None = None
    skills: list[str] | None = None
    experience_years: float | None = None
    desired_titles: list[str] | None = None
    # frontend sends "linkedin_url"; model column is linkedin_profile_url
    linkedin_url: str | None = None
    linkedin_profile_url: str | None = None
    portfolio_url: str | None = None
    master_username: str | None = None
    master_password: str | None = None


class CandidateUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    current_location: str | None = None
    skills: list[str] | None = None
    experience_years: float | None = None
    desired_titles: list[str] | None = None
    linkedin_profile_url: str | None = None
    portfolio_url: str | None = None
    master_username: str | None = None
    master_password: str | None = None
    is_profile_complete: bool | None = None
    parsed_resume: dict[str, Any] | None = None


class CandidateOut(BaseModel):
    id: UUID
    first_name: str | None = None
    last_name: str | None = None
    email: str
    phone: str | None = None
    current_location: str | None = None
    resume_path: str | None = None
    parsed_resume: dict[str, Any] | None = None
    skills: list[str] | None = None
    experience_years: float | None = None
    desired_titles: list[str] | None = None
    linkedin_profile_url: str | None = None
    portfolio_url: str | None = None
    master_username: str | None = None
    master_password: str | None = None
    is_profile_complete: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def name(self) -> str:
        return " ".join(filter(None, [self.first_name, self.last_name]))


class ResumePathRequest(BaseModel):
    file_path: str
    candidate_id: UUID | None = None


class ResumePathOut(BaseModel):
    original_path: str
    resume_path: str
    copied: bool


# ─── Jobs ─────────────────────────────────────────────────────────────────────

class JobBulkImport(BaseModel):
    urls: list[str]


class JobOut(BaseModel):
    id: UUID
    platform_id: UUID | None = None
    url: str
    source_url: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    description: str | None = None
    required_skills: dict | None = None
    status: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class JobStatusUpdate(BaseModel):
    status: str


# ─── Applications ─────────────────────────────────────────────────────────────

class ApplicationStart(BaseModel):
    candidate_id: UUID
    job_ids: list[UUID]


class ApplicationOut(BaseModel):
    id: UUID
    candidate_id: UUID
    job_id: UUID
    status: str
    platform: str | None = None
    attempt_count: int
    last_error: str | None = None
    screenshot_path: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ManualAnswer(BaseModel):
    question_id: UUID
    answer: str


class UnansweredQuestionOut(BaseModel):
    id: UUID
    application_id: UUID
    candidate_id: UUID | None = None
    platform: str | None = None
    field_label: str | None = None
    field_type: str | None = None
    options: list[str] | None = None
    is_required: bool
    recruiter_answer: str | None = None
    answered_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PlatformRunOut(BaseModel):
    id: UUID
    application_id: UUID
    platform: str
    adapter_version: str | None = None
    steps_completed: int
    total_steps: int | None = None
    current_step_name: str | None = None
    status: str
    manual_reason: str | None = None
    screenshot_path: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ─── Credentials ──────────────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    candidate_id: UUID
    # Normalized platform key, e.g. "workday", "greenhouse", "linkedin", "indeed"
    platform: str
    email: str
    password: str


class CredentialUpdate(BaseModel):
    platform: str | None = None
    email: str | None = None
    password: str | None = None


class CredentialOut(BaseModel):
    id: UUID
    candidate_id: UUID | None = None
    platform: str
    email: str
    password: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ─── FormAnswers ──────────────────────────────────────────────────────────────

class FormAnswerCreate(BaseModel):
    candidate_id: UUID
    # Normalized platform key, e.g. "workday", "greenhouse". NULL = global/cross-platform
    platform: str | None = None
    question_text: str | None = None
    answer: str | None = None
    field_type: int = 1
    options: dict | None = None
    priority: int = 0


class FormAnswerUpdate(BaseModel):
    platform: str | None = None
    question_text: str | None = None
    answer: str | None = None
    field_type: int | None = None
    options: dict | None = None
    priority: int | None = None


class FormAnswerOut(BaseModel):
    id: UUID
    candidate_id: UUID
    platform: str | None = None
    question_text: str | None = None
    answer: str | None = None
    field_type: int
    options: dict | None = None
    priority: int
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
