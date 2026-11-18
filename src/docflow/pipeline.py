"""The extraction workflow for one submission. Plain functions, so tests can run it without a
message broker; the Celery task in worker.py is a thin wrapper around process_submission."""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, auto

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from docflow.audit import AuditAction, record_event
from docflow.checks import run_checks
from docflow.config import Settings
from docflow.extraction import (
    Extractor,
    PermanentExtractionError,
    RetryableExtractionError,
    Usage,
)
from docflow.jobs import TASK_TIME_LIMIT_SECONDS
from docflow.models import AttemptOutcome, ExtractionAttempt, Submission, SubmissionStatus
from docflow.pdf import InvalidPdfError, inspect_pdf
from docflow.storage import ObjectStorage, StorageError

logger = logging.getLogger(__name__)

FIRST_REVIEW_STAGE = 1
TOKENS_PER_MILLION = Decimal(1_000_000)

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


def estimate_cost(usage: Usage, settings: Settings) -> Decimal | None:
    prices = settings.llm_prices_per_million.get(usage.model)
    if prices is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    input_price, output_price = prices
    return (
        usage.input_tokens * input_price + usage.output_tokens * output_price
    ) / TOKENS_PER_MILLION


def record_attempt(
    db: Session,
    submission_id: uuid.UUID,
    settings: Settings,
    started: float,
    usage: Usage | None,
    error: str | None,
) -> None:
    db.add(
        ExtractionAttempt(
            submission_id=submission_id,
            provider="openai",
            model=usage.model if usage else settings.llm_model,
            reasoning_effort=settings.llm_reasoning_effort,
            outcome=AttemptOutcome.FAILED if error else AttemptOutcome.SUCCEEDED,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            reasoning_tokens=usage.reasoning_tokens if usage else None,
            estimated_cost_usd=estimate_cost(usage, settings) if usage else None,
            latency_ms=round((time.monotonic() - started) * 1000),
            response_id=usage.response_id if usage else None,
            error=error,
        )
    )


def process_submission(
    submission_id: uuid.UUID,
    db: Session,
    storage: ObjectStorage,
    get_extractor: Callable[[], Extractor],
    settings: Settings,
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
        info = inspect_pdf(pdf, settings.max_pdf_pages)
        submission.document.page_count = info.page_count
        submission.document.text_content = info.text
        extractor = get_extractor()
    except (InvalidPdfError, PermanentExtractionError) as error:
        fail(db, submission, str(error))
        return
    except StorageError:
        db.rollback()
        release(db, submission_id)
        raise

    started = time.monotonic()
    try:
        result = extractor.extract(pdf)
    except PermanentExtractionError as error:
        record_attempt(db, submission_id, settings, started, error.usage, str(error))
        fail(db, submission, str(error))
        return
    except RetryableExtractionError as error:
        db.rollback()
        record_attempt(db, submission_id, settings, started, None, str(error))
        db.commit()
        release(db, submission_id)
        raise

    record_attempt(db, submission_id, settings, started, result.usage, None)
    flags = run_checks(result.invoice, info.text, today=datetime.now(UTC).date())
    submission.fields = result.invoice.model_dump(mode="json")
    submission.flags = [flag.to_dict() for flag in flags]
    submission.status = SubmissionStatus.IN_REVIEW
    submission.stage = FIRST_REVIEW_STAGE
    submission.failure_reason = None
    submission.claimed_at = None
    record_event(
        db,
        submission_id,
        AuditAction.EXTRACTED,
        actor_id=None,
        stage=FIRST_REVIEW_STAGE,
        details={"flag_count": len(flags), "model": result.usage.model if result.usage else None},
    )
    db.commit()
    logger.info("Submission %s extracted with %d flag(s)", submission_id, len(flags))


def fail(db: Session, submission: Submission, reason: str) -> None:
    logger.warning("Extraction failed for submission %s: %s", submission.id, reason)
    submission.status = SubmissionStatus.EXTRACTION_FAILED
    submission.failure_reason = reason
    submission.claimed_at = None
    record_event(
        db, submission.id, AuditAction.EXTRACTION_FAILED, actor_id=None, details={"reason": reason}
    )
    db.commit()


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
    failed = db.scalar(
        update(Submission)
        .where(Submission.id == submission_id, Submission.status == SubmissionStatus.UPLOADED)
        .values(status=SubmissionStatus.EXTRACTION_FAILED, failure_reason=reason)
        .returning(Submission.id)
    )
    if failed is not None:
        record_event(
            db,
            submission_id,
            AuditAction.EXTRACTION_FAILED,
            actor_id=None,
            details={"reason": reason},
        )
    db.commit()
