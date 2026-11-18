"""Review operations. Each one: lock the submission row, ask the state machine whether the action
is allowed, apply it, re-run checks if values changed, record an audit event, commit. One
transaction per operation, so a change and its audit event are saved together or not at all."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docflow import fieldpaths
from docflow.audit import AuditAction, record_event
from docflow.checks import run_checks
from docflow.config import Settings
from docflow.errors import InvalidRequestError, NotFoundError
from docflow.invoice import InvoiceExtraction
from docflow.jobs import EnqueueExtraction
from docflow.models import FieldQuery, QueryStatus, Role, Submission, SubmissionStatus, User
from docflow.workflow import Action, Actor, InvalidTransitionError, State, transition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldChange:
    field: str
    value: str | None
    reason: str


@dataclass(frozen=True)
class QueryRequest:
    field: str
    question: str


@dataclass(frozen=True)
class Answer:
    query_id: uuid.UUID
    response: str
    corrected_value: str | None


# --- shared steps ------------------------------------------------------------------------------


def lock_submission(db: Session, submission_id: uuid.UUID, user: User) -> Submission:
    """SELECT ... FOR UPDATE: a second reviewer acting at the same moment waits, then sees the
    new state and gets a 409 instead of overwriting the first decision."""
    submission = db.scalar(
        select(Submission).where(Submission.id == submission_id).with_for_update()
    )
    if submission is None or (user.role == Role.SUBMITTER and submission.submitter_id != user.id):
        raise NotFoundError("Submission not found")
    return submission


def plan(
    submission: Submission, action: Action, user: User, settings: Settings
) -> tuple[State, State]:
    """Asks the state machine whether this user may do this now, without changing anything.
    Permission and state errors (403/409) therefore come before input errors (422)."""
    before = State(submission.status, submission.stage)
    actor = Actor(role=user.role, is_owner=submission.submitter_id == user.id)
    return before, transition(before, action, actor, settings.approval_stages)


def move(submission: Submission, state: State) -> None:
    submission.status, submission.stage = state.status, state.stage


def stage_of(state: State) -> int:
    if state.stage is None:  # unreachable: the state machine only allows these actions in a stage
        raise InvalidTransitionError("Submission is not in a review stage")
    return state.stage


def require_fields(submission: Submission) -> dict[str, Any]:
    if submission.fields is None:
        raise InvalidTransitionError("This submission has no extracted fields")
    return submission.fields


def recheck(submission: Submission) -> None:
    """Re-runs the deterministic checks on the current values."""
    invoice = InvoiceExtraction.model_validate(require_fields(submission))
    text = submission.document.text_content or ""
    flags = run_checks(
        invoice,
        text,
        today=datetime.now(UTC).date(),
        human_edited=frozenset(submission.human_edited_fields),
    )
    submission.flags = [flag.to_dict() for flag in flags]


def set_field(submission: Submission, path: fieldpaths.FieldPath, value: str | None) -> str | None:
    """Changes one value, marks it human-edited, and returns the old value."""
    fields = require_fields(submission)
    old = fieldpaths.get_value(fields, path)
    submission.fields = fieldpaths.with_value(fields, path, value)
    if str(path) not in submission.human_edited_fields:
        submission.human_edited_fields = [*submission.human_edited_fields, str(path)]
    return old


# --- operations --------------------------------------------------------------------------------


def edit_fields(
    db: Session,
    submission_id: uuid.UUID,
    user: User,
    changes: list[FieldChange],
    settings: Settings,
) -> Submission:
    submission = lock_submission(db, submission_id, user)
    before, after = plan(submission, Action.EDIT_FIELDS, user, settings)
    fields = require_fields(submission)
    paths = [fieldpaths.parse(change.field, fields) for change in changes]  # validate all first
    move(submission, after)

    for change, path in zip(changes, paths, strict=True):
        old = set_field(submission, path, change.value)
        record_event(
            db,
            submission.id,
            AuditAction.FIELD_EDITED,
            actor_id=user.id,
            stage=before.stage,
            details={"field": str(path), "old": old, "new": change.value, "reason": change.reason},
        )
    recheck(submission)
    db.commit()
    return submission


def raise_queries(
    db: Session,
    submission_id: uuid.UUID,
    user: User,
    queries: list[QueryRequest],
    settings: Settings,
) -> Submission:
    submission = lock_submission(db, submission_id, user)
    before, after = plan(submission, Action.RAISE_QUERY, user, settings)
    fields = require_fields(submission)
    paths = [fieldpaths.parse(query.field, fields) for query in queries]
    move(submission, after)

    for query, path in zip(queries, paths, strict=True):
        db.add(
            FieldQuery(
                submission_id=submission.id,
                stage=stage_of(before),
                field=str(path),
                question=query.question,
                raised_by=user.id,
            )
        )
    record_event(
        db,
        submission.id,
        AuditAction.QUERY_RAISED,
        actor_id=user.id,
        stage=before.stage,
        details={
            "queries": [
                {"field": str(path), "question": query.question}
                for query, path in zip(queries, paths, strict=True)
            ]
        },
    )
    db.commit()
    return submission


def answer_queries(
    db: Session,
    submission_id: uuid.UUID,
    user: User,
    answers: list[Answer],
    settings: Settings,
) -> Submission:
    submission = lock_submission(db, submission_id, user)
    before, after = plan(submission, Action.ANSWER_QUERIES, user, settings)

    open_queries = {q.id: q for q in submission.queries if q.status == QueryStatus.OPEN}
    answered_ids = [answer.query_id for answer in answers]
    if len(set(answered_ids)) != len(answered_ids) or set(answered_ids) != set(open_queries):
        raise InvalidRequestError(
            f"Answer every open query exactly once ({len(open_queries)} open)"
        )

    move(submission, after)
    now = datetime.now(UTC)
    corrected = False
    for answer in answers:
        query = open_queries[answer.query_id]
        query.status = QueryStatus.ANSWERED
        query.response = answer.response
        query.corrected_value = answer.corrected_value
        query.answered_by = user.id
        query.answered_at = now
        if answer.corrected_value is not None:
            path = fieldpaths.parse(query.field, require_fields(submission))
            set_field(submission, path, answer.corrected_value)
            corrected = True

    if corrected:
        recheck(submission)
    record_event(
        db,
        submission.id,
        AuditAction.QUERY_ANSWERED,
        actor_id=user.id,
        stage=before.stage,
        details={
            "answers": [
                {
                    "query_id": str(a.query_id),
                    "field": open_queries[a.query_id].field,
                    "response": a.response,
                    "corrected_value": a.corrected_value,
                }
                for a in answers
            ]
        },
    )
    db.commit()
    return submission


def decide(
    db: Session,
    submission_id: uuid.UUID,
    user: User,
    approve: bool,
    comment: str | None,
    settings: Settings,
) -> Submission:
    submission = lock_submission(db, submission_id, user)
    flag_count = len(submission.flags or [])
    action = Action.APPROVE if approve else Action.REJECT
    before, after = plan(submission, action, user, settings)

    if not approve and not comment:
        raise InvalidRequestError("A rejection needs a reason")
    if approve and flag_count and not comment:
        # A person may override the checks, but never silently.
        raise InvalidRequestError(
            f"This submission has {flag_count} open flag(s); add a comment acknowledging them"
        )
    move(submission, after)

    if not approve:
        event = AuditAction.REJECTED
    elif submission.status == SubmissionStatus.APPROVED:
        event = AuditAction.APPROVED
    else:
        event = AuditAction.STAGE_APPROVED
    record_event(
        db,
        submission.id,
        event,
        actor_id=user.id,
        stage=before.stage,
        details={"comment": comment, "flags_acknowledged": flag_count if approve else 0},
    )
    db.commit()
    return submission


def retry_extraction(
    db: Session,
    submission_id: uuid.UUID,
    user: User,
    enqueue: EnqueueExtraction,
    settings: Settings,
) -> Submission:
    submission = lock_submission(db, submission_id, user)
    before, after = plan(submission, Action.RETRY_EXTRACTION, user, settings)
    stuck_since = datetime.now(UTC) - timedelta(minutes=settings.stuck_upload_minutes)
    if submission.status == SubmissionStatus.UPLOADED and submission.updated_at > stuck_since:
        raise InvalidTransitionError(
            "This upload is still waiting to be processed; retry is only for failed or "
            f"stuck submissions (older than {settings.stuck_upload_minutes} minutes)"
        )
    move(submission, after)
    previous_reason = submission.failure_reason
    submission.failure_reason = None
    submission.claimed_at = None
    record_event(
        db,
        submission.id,
        AuditAction.EXTRACTION_RETRIED,
        actor_id=user.id,
        details={"previous_status": before.status.value, "previous_reason": previous_reason},
    )
    db.commit()

    try:
        enqueue(submission.id)
    except Exception:
        # Saved as UPLOADED either way; the admin can retry again once the broker is back.
        logger.exception("Could not enqueue retried submission %s", submission.id)
    return submission
