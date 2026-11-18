import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from docflow.models import AuditEvent


class AuditAction(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTED = "extracted"
    EXTRACTION_FAILED = "extraction_failed"
    EXTRACTION_RETRIED = "extraction_retried"
    FIELD_EDITED = "field_edited"
    QUERY_RAISED = "query_raised"
    QUERY_ANSWERED = "query_answered"
    STAGE_APPROVED = "stage_approved"
    APPROVED = "approved"
    REJECTED = "rejected"


def record_event(
    db: Session,
    submission_id: uuid.UUID,
    action: AuditAction,
    *,
    actor_id: uuid.UUID | None,
    stage: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Adds the event to the current transaction, so it is saved if and only if the change it
    describes is saved."""
    db.add(
        AuditEvent(
            submission_id=submission_id,
            actor_id=actor_id,
            action=action.value,
            stage=stage,
            details=details or {},
        )
    )
