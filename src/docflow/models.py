import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from docflow.roles import Role

__all__ = ["Role"]  # re-exported: Role lives in docflow.roles so settings can use it

# Predictable constraint names, so later migrations can alter or drop them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def pg_enum(enum_class: type[StrEnum], name: str) -> Enum:
    """Store the enum's lowercase values (not its member names) in a named Postgres enum type."""
    return Enum(enum_class, name=name, values_callable=lambda members: [m.value for m in members])


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def event_time_column() -> Mapped[datetime]:
    """clock_timestamp() is the time of the insert itself. now() would give every row written in
    one transaction the same time (the transaction's start)."""
    return mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SubmissionStatus(StrEnum):
    # All statuses from the plan are defined now, so later milestones need no enum migration.
    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    EXTRACTION_FAILED = "extraction_failed"
    IN_REVIEW = "in_review"
    QUERY_RAISED = "query_raised"
    APPROVED = "approved"
    REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"
    # Emails are lowercased by the API; this constraint enforces it in the database too.
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(pg_enum(Role, "user_role"))
    is_active: Mapped[bool] = mapped_column(default=True)
    must_change_password: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Submission(Base):
    __tablename__ = "submissions"
    # A retried upload with the same Idempotency-Key from the same user maps to one submission.
    __table_args__ = (
        UniqueConstraint("submitter_id", "idempotency_key"),
        Index("ix_submissions_status_stage", "status", "stage"),  # work-queue listing
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    submitter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[SubmissionStatus] = mapped_column(
        pg_enum(SubmissionStatus, "submission_status"), default=SubmissionStatus.UPLOADED
    )
    stage: Mapped[int | None] = mapped_column(Integer)
    fields: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    flags: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    # Field paths changed by a person. They have no source quote, so grounding skips them.
    human_edited_fields: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # When a worker claimed this submission for extraction. The claim is a lease: if the worker
    # dies, another delivery of the task can take over once the lease has expired.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    document: Mapped["Document"] = relationship(back_populates="submission")
    queries: Mapped[list["FieldQuery"]] = relationship(
        back_populates="submission", order_by="[FieldQuery.created_at, FieldQuery.id]"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), unique=True
    )
    storage_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)  # set by the worker
    # The PDF's text layer, kept so checks can be re-run after edits without re-reading the PDF.
    text_content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()

    submission: Mapped[Submission] = relationship(back_populates="document")


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExtractionAttempt(Base):
    """One call to the LLM provider, kept even when it failed: failed calls can still cost money,
    and this table is the audit trail for cost, latency and model choice."""

    __tablename__ = "extraction_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    reasoning_effort: Mapped[str] = mapped_column(String(20))
    outcome: Mapped[AttemptOutcome] = mapped_column(pg_enum(AttemptOutcome, "attempt_outcome"))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    response_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class QueryStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"


class FieldQuery(Base):
    """A reviewer's question about one field, answered by the submitter."""

    __tablename__ = "field_queries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[int] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(100))
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[QueryStatus] = mapped_column(
        pg_enum(QueryStatus, "query_status"), default=QueryStatus.OPEN
    )
    raised_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    response: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = event_time_column()

    submission: Mapped[Submission] = relationship(back_populates="queries")


class AuditEvent(Base):
    """Append-only history. A database trigger rejects UPDATE, DELETE and TRUNCATE, and the
    foreign key does not cascade, so a submission with history cannot be deleted either."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Strict insertion order, generated by the database. Timestamps alone can tie.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id"), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))  # None = system
    # A string, not a database enum, so new actions need no migration; AuditAction validates it.
    action: Mapped[str] = mapped_column(String(50))
    stage: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = event_time_column()
