"""The extraction workflow for one submission. Plain functions, so tests can run it without a
message broker; the Celery task in worker.py is a thin wrapper around process_submission."""

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from enum import Enum, auto

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from docflow.extraction import Extractor, PermanentExtractionError, RetryableExtractionError
from docflow.jobs import TASK_TIME_LIMIT_SECONDS
from docflow.models import Submission, SubmissionStatus
from docflow.pdf import InvalidPdfError, inspect_pdf
from docflow.storage import ObjectStorage, StorageError

logger = logging.getLogger(__name__)

FIRST_REVIEW_STAGE = 1

# A claim older than the longest a task can run means the worker holding it is gone.
CLAIM_LEASE = timedelta(seconds=TASK_TIME_LIMIT_SECONDS + 60)


class ClaimBusyError(Exception):
    """Another worker holds a live claim on this submission. Check again after the lease."""


class Claim(Enum):
    CLAIMED = auto()
    BUSY = auto()
    NOTHING_TO_DO = auto()


def claim(db: Session, submission_id: uuid.UUID) -> Claim:
    """Atomically takes the submission for extraction if it is waiting (UPLOADED) or if a
    previous worker's claim has expired. Celery delivers at least once, so this is what keeps
    two deliveries of the same task from both doing the work."""
    claimed = db.scalar(
        update(Submission)
        .where(
            Submission.id == submission_id,
            or_(
                Submission.status == SubmissionStatus.UPLOADED,
                (Submission.status == SubmissionStatus.EXTRACTING)
                & (Submission.claimed_at < func.now() - CLAIM_LEASE),
            ),
        )
        .values(status=SubmissionStatus.EXTRACTING, claimed_at=func.now())
        .returning(Submission.id)
    )
    db.commit()
    if claimed is not None:
        return Claim.CLAIMED
    current = db.scalar(select(Submission.status).where(Submission.id == submission_id))
    return Claim.BUSY if current == SubmissionStatus.EXTRACTING else Claim.NOTHING_TO_DO


def process_submission(
    submission_id: uuid.UUID,
    db: Session,
    storage: ObjectStorage,
    get_extractor: Callable[[], Extractor],
    max_pages: int,
) -> None:
    """Ends in IN_REVIEW or EXTRACTION_FAILED, except for transient failures: those raise
    StorageError or RetryableExtractionError after releasing the claim, and ClaimBusyError
    when another worker is still on it. The caller retries all three later."""
    match claim(db, submission_id):
        case Claim.NOTHING_TO_DO:
            logger.info("Submission %s is not waiting for extraction; skipping", submission_id)
            return
        case Claim.BUSY:
            raise ClaimBusyError(f"Submission {submission_id} is being extracted by another worker")
        case Claim.CLAIMED:
            pass

    submission = db.scalars(select(Submission).where(Submission.id == submission_id)).one()
    try:
        pdf = storage.get(submission.document.storage_key)
        submission.document.page_count = inspect_pdf(pdf, max_pages).page_count
        result = get_extractor().extract(pdf)
    except (InvalidPdfError, PermanentExtractionError) as error:
        logger.warning("Extraction failed for submission %s: %s", submission_id, error)
        submission.status = SubmissionStatus.EXTRACTION_FAILED
        submission.failure_reason = str(error)
        submission.claimed_at = None
        db.commit()
        return
    except (StorageError, RetryableExtractionError):
        db.rollback()
        release(db, submission_id)
        raise

    submission.fields = result.fields
    submission.status = SubmissionStatus.IN_REVIEW
    submission.stage = FIRST_REVIEW_STAGE
    submission.failure_reason = None
    submission.claimed_at = None
    db.commit()
    logger.info("Submission %s extracted and ready for review", submission_id)


def release(db: Session, submission_id: uuid.UUID) -> None:
    """EXTRACTING -> UPLOADED, so the next retry can claim the submission again."""
    db.execute(
        update(Submission)
        .where(Submission.id == submission_id, Submission.status == SubmissionStatus.EXTRACTING)
        .values(status=SubmissionStatus.UPLOADED, claimed_at=None)
    )
    db.commit()


def give_up(db: Session, submission_id: uuid.UUID, reason: str) -> None:
    """Marks a submission failed after its retries are exhausted. Not applied to a submission
    another worker is actively extracting."""
    db.execute(
        update(Submission)
        .where(Submission.id == submission_id, Submission.status == SubmissionStatus.UPLOADED)
        .values(status=SubmissionStatus.EXTRACTION_FAILED, failure_reason=reason)
    )
    db.commit()
