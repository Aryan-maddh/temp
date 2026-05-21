import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy import UUID as SA_UUID
from sqlalchemy import TIMESTAMP
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from backend.db import Base
from backend.db_table_names import (
    TABLE_APPLICATION_SCREENSHOTS,
    TABLE_APPLICATIONS,
    TABLE_CANDIDATES,
    TABLE_CREDENTIALS,
    TABLE_FORM_ANSWERS,
    TABLE_JOB_HARVESTER_CONFIGS,
    TABLE_JOB_HARVESTER_ITEMS,
    TABLE_JOB_HARVESTER_RUNS,
    TABLE_JOBS,
    TABLE_PLATFORM_RUNS,
    TABLE_RUN_LOGS,
    TABLE_UNANSWERED_QUESTIONS,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Candidate(Base):
    __tablename__ = TABLE_CANDIDATES

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(SA_UUID(as_uuid=True))
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    current_location: Mapped[str | None] = mapped_column(Text)
    resume_path: Mapped[str | None] = mapped_column(Text)
    parsed_resume: Mapped[dict | None] = mapped_column(JSONB)
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    experience_years: Mapped[float | None] = mapped_column(Integer)
    desired_titles: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    linkedin_profile_url: Mapped[str | None] = mapped_column(Text)
    portfolio_url: Mapped[str | None] = mapped_column(Text)
    master_username: Mapped[str | None] = mapped_column(Text)
    master_password: Mapped[str | None] = mapped_column(Text)
    is_profile_complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    @property
    def name(self) -> str:
        return " ".join(filter(None, [self.first_name, self.last_name]))

    @property
    def location(self) -> str | None:
        return self.current_location

    @property
    def linkedin_url(self) -> str | None:
        return self.linkedin_profile_url

    @property
    def extra_answers(self) -> dict:
        if isinstance(self.parsed_resume, dict):
            return self.parsed_resume.get("extra_answers", {})
        return {}

    credentials: Mapped[list["Credential"]] = relationship(
        "Credential", back_populates="candidate", cascade="all, delete-orphan"
    )
    applications: Mapped[list["Application"]] = relationship(
        "Application", back_populates="candidate", cascade="all, delete-orphan"
    )
    form_answers: Mapped[list["FormAnswer"]] = relationship(
        "FormAnswer", back_populates="candidate", cascade="all, delete-orphan"
    )
    unanswered_questions: Mapped[list["UnansweredQuestion"]] = relationship(
        "UnansweredQuestion", back_populates="candidate"
    )


class Job(Base):
    __tablename__ = TABLE_JOBS

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    platform_id: Mapped[uuid.UUID | None] = mapped_column(SA_UUID(as_uuid=True))
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    required_skills: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    applications: Mapped[list["Application"]] = relationship(
        "Application", back_populates="job", cascade="all, delete-orphan"
    )
    harvested_items: Mapped[list["JobHarvesterItem"]] = relationship(
        "JobHarvesterItem", back_populates="job"
    )


class Credential(Base):
    __tablename__ = TABLE_CREDENTIALS
    __table_args__ = (UniqueConstraint("candidate_id", "domain", name="uq_credential_candidate_domain"),)

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_CANDIDATES}.id", ondelete="CASCADE"), nullable=True
    )
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )

    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="credentials")


class Application(Base):
    __tablename__ = TABLE_APPLICATIONS

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_CANDIDATES}.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_JOBS}.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    platform: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )

    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="applications")
    job: Mapped["Job"] = relationship("Job", back_populates="applications")
    platform_runs: Mapped[list["PlatformRun"]] = relationship(
        "PlatformRun", back_populates="application", cascade="all, delete-orphan"
    )
    unanswered_questions: Mapped[list["UnansweredQuestion"]] = relationship(
        "UnansweredQuestion", back_populates="application", cascade="all, delete-orphan"
    )
    run_logs: Mapped[list["RunLog"]] = relationship(
        "RunLog", back_populates="application", cascade="all, delete-orphan"
    )
    vision_screenshots: Mapped[list["ApplicationScreenshot"]] = relationship(
        "ApplicationScreenshot", back_populates="application", cascade="all, delete-orphan"
    )


class PlatformRun(Base):
    __tablename__ = TABLE_PLATFORM_RUNS

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_APPLICATIONS}.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    adapter_version: Mapped[str | None] = mapped_column(Text)
    steps_completed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_steps: Mapped[int | None] = mapped_column(Integer)
    current_step_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    manual_reason: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    application: Mapped["Application"] = relationship("Application", back_populates="platform_runs")


class UnansweredQuestion(Base):
    __tablename__ = TABLE_UNANSWERED_QUESTIONS

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_APPLICATIONS}.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_CANDIDATES}.id", ondelete="SET NULL"), nullable=True
    )
    domain: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    field_label: Mapped[str | None] = mapped_column(Text)
    field_type: Mapped[str | None] = mapped_column(Text)
    options: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    recruiter_answer: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )

    application: Mapped["Application"] = relationship(
        "Application", back_populates="unanswered_questions"
    )
    candidate: Mapped["Candidate"] = relationship(
        "Candidate", back_populates="unanswered_questions"
    )


class FormAnswer(Base):
    __tablename__ = TABLE_FORM_ANSWERS

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_CANDIDATES}.id", ondelete="CASCADE"), nullable=False
    )
    domain: Mapped[str | None] = mapped_column(Text)
    question_text: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    field_type: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    options: Mapped[dict | None] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="form_answers")


class RunLog(Base):
    __tablename__ = TABLE_RUN_LOGS

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_APPLICATIONS}.id", ondelete="CASCADE"), nullable=False
    )
    log_level: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )

    application: Mapped["Application"] = relationship("Application", back_populates="run_logs")


# ── Harvester models ──────────────────────────────────────────────────────────

class ApplicationScreenshot(Base):
    __tablename__ = TABLE_APPLICATION_SCREENSHOTS

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_APPLICATIONS}.id", ondelete="CASCADE"), nullable=True
    )
    step_number: Mapped[int | None] = mapped_column(Integer)
    page_type: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    action_taken: Mapped[str | None] = mapped_column(Text)
    action_target: Mapped[str | None] = mapped_column(Text)
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now()
    )

    application: Mapped["Application | None"] = relationship(
        "Application", back_populates="vision_screenshots"
    )


class JobHarvesterConfig(Base):
    __tablename__ = TABLE_JOB_HARVESTER_CONFIGS

    id: Mapped[uuid.UUID] = mapped_column(SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="linkedin")
    keyword: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    max_jobs_per_run: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    schedule_time: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Asia/Kolkata")
    schedule_jitter_minutes: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    status: Mapped[str] = mapped_column(Text, default="idle", server_default="idle")
    last_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now())

    runs: Mapped[list["JobHarvesterRun"]] = relationship("JobHarvesterRun", back_populates="config")


class JobHarvesterRun(Base):
    __tablename__ = TABLE_JOB_HARVESTER_RUNS

    id: Mapped[uuid.UUID] = mapped_column(SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_id: Mapped[uuid.UUID | None] = mapped_column(SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_JOB_HARVESTER_CONFIGS}.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now())

    config: Mapped["JobHarvesterConfig | None"] = relationship("JobHarvesterConfig", back_populates="runs")
    items: Mapped[list["JobHarvesterItem"]] = relationship("JobHarvesterItem", back_populates="run")


class JobHarvesterItem(Base):
    __tablename__ = TABLE_JOB_HARVESTER_ITEMS

    id: Mapped[uuid.UUID] = mapped_column(SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_JOB_HARVESTER_RUNS}.id"), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(SA_UUID(as_uuid=True), ForeignKey(f"{TABLE_JOBS}.id"), nullable=True)
    source_platform: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    external_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    experience: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="seen", server_default="seen")
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=_utcnow, server_default=func.now())

    run: Mapped["JobHarvesterRun | None"] = relationship("JobHarvesterRun", back_populates="items")
    job: Mapped["Job | None"] = relationship("Job", back_populates="harvested_items")