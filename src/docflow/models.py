import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Role(StrEnum):
    SUBMITTER = "submitter"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    ADMIN = "admin"


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
    __table_args__ = (UniqueConstraint("submitter_id", "idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    submitter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[SubmissionStatus] = mapped_column(
        pg_enum(SubmissionStatus, "submission_status"), default=SubmissionStatus.UPLOADED
    )
    stage: Mapped[int | None] = mapped_column(Integer)
    fields: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # When a worker claimed this submission for extraction. The claim is a lease: if the worker
    # dies, another delivery of the task can take over once the lease has expired.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    document: Mapped["Document"] = relationship(back_populates="submission")


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
    created_at: Mapped[datetime] = created_at_column()

    submission: Mapped[Submission] = relationship(back_populates="document")
