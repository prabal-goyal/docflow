import hashlib
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from docflow.deps import AppSettings, CurrentUser, DbSession, require_roles
from docflow.jobs import EnqueueExtraction, get_enqueue
from docflow.models import Document, ExtractionAttempt, Role, Submission, User
from docflow.pdf import looks_like_pdf
from docflow.schemas import ExtractionAttemptOut, SubmissionOut
from docflow.storage import ObjectStorage, StorageError, get_storage

router = APIRouter(prefix="/submissions", tags=["submissions"])
logger = logging.getLogger(__name__)

Uploader = Annotated[User, Depends(require_roles(Role.SUBMITTER, Role.ADMIN))]
Storage = Annotated[ObjectStorage, Depends(get_storage)]
Enqueue = Annotated[EnqueueExtraction, Depends(get_enqueue)]

READ_CHUNK_BYTES = 64 * 1024


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
    db.commit()

    # Enqueue only after the commit, so the worker can never look for a row that is not there yet.
    try:
        enqueue(submission.id)
    except Exception:
        # The upload itself succeeded and is saved, so the client still gets 202. The submission
        # stays UPLOADED and can be re-queued; see docs/decisions/0002 for the recovery path.
        logger.exception("Could not enqueue extraction for submission %s", submission.id)

    return SubmissionOut.model_validate(submission)


@router.get("/{submission_id}")
def get_submission(submission_id: uuid.UUID, db: DbSession, user: CurrentUser) -> SubmissionOut:
    submission = db.get(Submission, submission_id)
    # Submitters only see their own. A 404 (not 403) avoids confirming that other IDs exist.
    if submission is None or (user.role == Role.SUBMITTER and submission.submitter_id != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return SubmissionOut.model_validate(submission)


@router.get("/{submission_id}/attempts")
def list_extraction_attempts(
    submission_id: uuid.UUID,
    db: DbSession,
    _admin: Annotated[User, Depends(require_roles(Role.ADMIN))],
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
