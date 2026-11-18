import hashlib
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import ColumnElement, false, select
from sqlalchemy.exc import IntegrityError

from docflow import review
from docflow.audit import AuditAction, record_event
from docflow.config import Settings
from docflow.deps import AppSettings, CurrentUser, DbSession, require_roles
from docflow.errors import NotFoundError
from docflow.jobs import EnqueueExtraction, get_enqueue
from docflow.models import (
    AuditEvent,
    Document,
    ExtractionAttempt,
    Role,
    Submission,
    SubmissionStatus,
    User,
)
from docflow.pdf import looks_like_pdf
from docflow.schemas import (
    AnswerQueriesRequest,
    AuditEventOut,
    DecisionRequest,
    EditFieldsRequest,
    ExtractionAttemptOut,
    RaiseQueriesRequest,
    SubmissionOut,
    SubmissionSummary,
)
from docflow.storage import ObjectStorage, StorageError, get_storage

router = APIRouter(prefix="/submissions", tags=["submissions"])
logger = logging.getLogger(__name__)

Uploader = Annotated[User, Depends(require_roles(Role.SUBMITTER, Role.ADMIN))]
Storage = Annotated[ObjectStorage, Depends(get_storage)]
Enqueue = Annotated[EnqueueExtraction, Depends(get_enqueue)]

AdminUser = Annotated[User, Depends(require_roles(Role.ADMIN))]

READ_CHUNK_BYTES = 64 * 1024
MAX_PAGE_SIZE = 200


def read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Reads at most max_bytes. Starlette has already spooled the body to a temporary file
    (memory is safe); the hard cap on request size belongs in the reverse proxy (M6)."""
    chunks: list[bytes] = []
    total = 0
    while chunk := upload.file.read(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File is larger than {max_bytes // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def find_by_idempotency_key(db: DbSession, submitter_id: uuid.UUID, key: str) -> Submission | None:
    return db.scalar(
        select(Submission).where(
            Submission.submitter_id == submitter_id, Submission.idempotency_key == key
        )
    )


def replay(existing: Submission, sha256: str, response: Response) -> SubmissionOut:
    """Same key + same file: return the original. Same key + different file: client bug."""
    if existing.document.sha256 != sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Idempotency-Key was already used for a different file",
        )
    response.status_code = status.HTTP_200_OK
    return SubmissionOut.model_validate(existing)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"description": "Replay of an earlier request with the same Idempotency-Key"},
        409: {"description": "Idempotency-Key reused with a different file"},
        413: {"description": "File too large"},
        415: {"description": "Not a PDF"},
    },
)
def create_submission(
    file: UploadFile,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    storage: Storage,
    enqueue: Enqueue,
    user: Uploader,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> SubmissionOut:
    """Upload an invoice PDF. Extraction runs in the background; poll GET /submissions/{id}."""
    data = read_limited(file, settings.max_upload_bytes)
    if not looks_like_pdf(data):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="The file is not a PDF"
        )
    sha256 = hashlib.sha256(data).hexdigest()

    if idempotency_key and (existing := find_by_idempotency_key(db, user.id, idempotency_key)):
        return replay(existing, sha256, response)

    submission = Submission(submitter_id=user.id, idempotency_key=idempotency_key)
    db.add(submission)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request with the same key won the race; answer as a replay.
        db.rollback()
        winner = find_by_idempotency_key(db, user.id, idempotency_key) if idempotency_key else None
        if winner is None:
            raise
        return replay(winner, sha256, response)

    storage_key = f"submissions/{submission.id}/original.pdf"
    submission.document = Document(storage_key=storage_key, sha256=sha256, size_bytes=len(data))
    try:
        storage.put(storage_key, data, "application/pdf")
    except StorageError:
        logger.exception("Upload of submission %s to object storage failed", submission.id)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File storage is unavailable, please retry",
        ) from None
    record_event(
        db,
        submission.id,
        AuditAction.UPLOADED,
        actor_id=user.id,
        details={"sha256": sha256, "size_bytes": len(data)},
    )
    db.commit()

    # Enqueue only after the commit, so the worker can never look for a row that is not there yet.
    try:
        enqueue(submission.id)
    except Exception:
        # The upload itself succeeded and is saved, so the client still gets 202. The submission
        # stays UPLOADED and can be re-queued; see docs/decisions/0002 for the recovery path.
        logger.exception("Could not enqueue extraction for submission %s", submission.id)

    return SubmissionOut.model_validate(submission)


def visible_submission(db: DbSession, submission_id: uuid.UUID, user: User) -> Submission:
    submission = db.get(Submission, submission_id)
    # Submitters only see their own. A 404 (not 403) avoids confirming that other IDs exist.
    if submission is None or (user.role == Role.SUBMITTER and submission.submitter_id != user.id):
        raise NotFoundError("Submission not found")
    return submission


def awaiting(user: User, settings: Settings) -> ColumnElement[bool]:
    """What this user can act on right now."""
    if user.role == Role.SUBMITTER:
        return Submission.status == SubmissionStatus.QUERY_RAISED
    if user.role == Role.ADMIN:
        return Submission.status == SubmissionStatus.EXTRACTION_FAILED
    if user.role not in settings.approval_stages:
        return false()
    stage = settings.approval_stages.index(user.role) + 1
    return (Submission.status == SubmissionStatus.IN_REVIEW) & (Submission.stage == stage)


@router.get("")
def list_submissions(
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    status_filter: Annotated[SubmissionStatus | None, Query(alias="status")] = None,
    stage: Annotated[int | None, Query(ge=1)] = None,
    awaiting_me: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SubmissionSummary]:
    """Newest first. Submitters see only their own submissions."""
    query = select(Submission)
    if user.role == Role.SUBMITTER:
        query = query.where(Submission.submitter_id == user.id)
    if status_filter is not None:
        query = query.where(Submission.status == status_filter)
    if stage is not None:
        query = query.where(Submission.stage == stage)
    if awaiting_me:
        query = query.where(awaiting(user, settings))
    query = query.order_by(Submission.created_at.desc(), Submission.id).limit(limit).offset(offset)
    return [
        SubmissionSummary(
            id=s.id,
            submitter_id=s.submitter_id,
            status=s.status,
            stage=s.stage,
            flag_count=len(s.flags or []),
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in db.scalars(query)
    ]


@router.get("/{submission_id}")
def get_submission(submission_id: uuid.UUID, db: DbSession, user: CurrentUser) -> SubmissionOut:
    return SubmissionOut.model_validate(visible_submission(db, submission_id, user))


@router.patch("/{submission_id}/fields")
def edit_fields(
    submission_id: uuid.UUID,
    body: EditFieldsRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> SubmissionOut:
    """Correct extracted values during review. Checks are re-run."""
    changes = [review.FieldChange(c.field, c.value, c.reason) for c in body.changes]
    submission = review.edit_fields(db, submission_id, user, changes, settings)
    return SubmissionOut.model_validate(submission)


@router.post("/{submission_id}/queries")
def raise_queries(
    submission_id: uuid.UUID,
    body: RaiseQueriesRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> SubmissionOut:
    """Ask the submitter about specific fields instead of rejecting the whole submission."""
    queries = [review.QueryRequest(q.field, q.question) for q in body.queries]
    submission = review.raise_queries(db, submission_id, user, queries, settings)
    return SubmissionOut.model_validate(submission)


@router.post("/{submission_id}/answers")
def answer_queries(
    submission_id: uuid.UUID,
    body: AnswerQueriesRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> SubmissionOut:
    """Answer every open query, optionally correcting the value. Returns to the same stage."""
    answers = [review.Answer(a.query_id, a.response, a.corrected_value) for a in body.answers]
    submission = review.answer_queries(db, submission_id, user, answers, settings)
    return SubmissionOut.model_validate(submission)


@router.post("/{submission_id}/decision")
def decide(
    submission_id: uuid.UUID,
    body: DecisionRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> SubmissionOut:
    """Approve (next stage, or final approval) or reject (needs a reason; final)."""
    approve = body.decision == "approve"
    submission = review.decide(db, submission_id, user, approve, body.comment, settings)
    return SubmissionOut.model_validate(submission)


@router.post("/{submission_id}/retry-extraction")
def retry_extraction(
    submission_id: uuid.UUID,
    db: DbSession,
    admin: AdminUser,
    settings: AppSettings,
    enqueue: Enqueue,
) -> SubmissionOut:
    """Re-queue a failed submission, or one stuck in UPLOADED (e.g. after a broker outage)."""
    submission = review.retry_extraction(db, submission_id, admin, enqueue, settings)
    return SubmissionOut.model_validate(submission)


@router.get("/{submission_id}/audit")
def audit_trail(submission_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[AuditEventOut]:
    """Full history, oldest first. Staff see any submission; submitters see their own."""
    visible_submission(db, submission_id, user)
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.submission_id == submission_id).order_by(AuditEvent.seq)
    )
    return [AuditEventOut.model_validate(e) for e in events]


@router.get("/{submission_id}/attempts")
def list_extraction_attempts(
    submission_id: uuid.UUID, db: DbSession, _admin: AdminUser
) -> list[ExtractionAttemptOut]:
    """Admin audit trail: every LLM call for this submission with tokens, cost and latency."""
    if db.get(Submission, submission_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    attempts = db.scalars(
        select(ExtractionAttempt)
        .where(ExtractionAttempt.submission_id == submission_id)
        .order_by(ExtractionAttempt.created_at, ExtractionAttempt.id)
    )
    return [ExtractionAttemptOut.model_validate(a) for a in attempts]
