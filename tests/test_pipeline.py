from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from docflow.extraction import (
    Extractor,
    PermanentExtractionError,
    RetryableExtractionError,
)
from docflow.models import Document, Role, Submission, SubmissionStatus
from docflow.pipeline import (
    CLAIM_LEASE,
    Claim,
    ClaimBusyError,
    claim,
    give_up,
    process_submission,
)
from docflow.storage import StorageError
from tests.conftest import MakeUser
from tests.fakes import FakeExtractor, InMemoryStorage, make_pdf

MAX_PAGES = 5

NewSubmission = Callable[[bytes], Submission]


@pytest.fixture
def new_submission(db: Session, make_user: MakeUser, storage: InMemoryStorage) -> NewSubmission:
    """Creates an UPLOADED submission whose PDF is already in storage, as the API leaves it."""

    def _new(pdf: bytes) -> Submission:
        submission = Submission(submitter_id=make_user(Role.SUBMITTER).id)
        db.add(submission)
        db.flush()
        key = f"submissions/{submission.id}/original.pdf"
        submission.document = Document(storage_key=key, sha256="0" * 64, size_bytes=len(pdf))
        storage.objects[key] = pdf
        db.commit()
        return submission

    return _new


def run(
    submission: Submission, db: Session, storage: InMemoryStorage, extractor: Extractor
) -> None:
    process_submission(submission.id, db, storage, lambda: extractor, MAX_PAGES)
    db.refresh(submission)


def status_of(db: Session, submission: Submission) -> SubmissionStatus:
    db.refresh(submission)
    return submission.status


def test_success_moves_to_first_review_stage(
    db: Session, storage: InMemoryStorage, new_submission: NewSubmission
) -> None:
    submission = new_submission(make_pdf(pages=2))
    extractor = FakeExtractor(fields={"invoice_number": {"value": "INV-42"}})

    run(submission, db, storage, extractor)

    assert submission.status == SubmissionStatus.IN_REVIEW
    assert submission.stage == 1
    assert submission.fields == {"invoice_number": {"value": "INV-42"}}
    assert submission.document.page_count == 2
    assert submission.failure_reason is None


def test_running_twice_does_the_work_once(
    db: Session, storage: InMemoryStorage, new_submission: NewSubmission
) -> None:
    """Celery may deliver the same task twice; the second run must be a no-op."""
    submission = new_submission(make_pdf())
    extractor = FakeExtractor()

    run(submission, db, storage, extractor)
    run(submission, db, storage, extractor)

    assert extractor.calls == 1
    assert submission.status == SubmissionStatus.IN_REVIEW


@pytest.mark.parametrize(
    ("pdf", "reason"),
    [
        (make_pdf(password="secret"), "password-protected"),
        (make_pdf(pages=MAX_PAGES + 1), "the limit is 5"),
        (b"%PDF-1.7 this is not really a pdf", "could not be read"),
    ],
)
def test_unprocessable_pdfs_fail_without_calling_the_extractor(
    db: Session, storage: InMemoryStorage, new_submission: NewSubmission, pdf: bytes, reason: str
) -> None:
    submission = new_submission(pdf)
    extractor = FakeExtractor()

    run(submission, db, storage, extractor)

    assert submission.status == SubmissionStatus.EXTRACTION_FAILED
    assert reason in (submission.failure_reason or "")
    assert extractor.calls == 0


def test_permanent_extractor_error_fails_the_submission(
    db: Session, storage: InMemoryStorage, new_submission: NewSubmission
) -> None:
    submission = new_submission(make_pdf())
    extractor = FakeExtractor(error=PermanentExtractionError("model refused"))

    run(submission, db, storage, extractor)

    assert submission.status == SubmissionStatus.EXTRACTION_FAILED
    assert submission.failure_reason == "model refused"


@pytest.mark.parametrize(
    "error", [RetryableExtractionError("rate limited"), StorageError("storage is down")]
)
def test_transient_errors_release_the_submission_for_a_retry(
    db: Session,
    storage: InMemoryStorage,
    new_submission: NewSubmission,
    error: Exception,
) -> None:
    submission = new_submission(make_pdf())
    if isinstance(error, StorageError):
        storage.fail = True
        extractor = FakeExtractor()
    else:
        extractor = FakeExtractor(error=error)

    with pytest.raises(type(error)):
        run(submission, db, storage, extractor)

    assert status_of(db, submission) == SubmissionStatus.UPLOADED

    # The retry succeeds once the problem is gone.
    storage.fail = False
    run(submission, db, storage, FakeExtractor())
    assert status_of(db, submission) == SubmissionStatus.IN_REVIEW


def test_give_up_marks_a_released_submission_as_failed(
    db: Session, new_submission: NewSubmission
) -> None:
    submission = new_submission(make_pdf())

    give_up(db, submission.id, "Gave up after 3 retries: rate limited")
    db.refresh(submission)

    assert submission.status == SubmissionStatus.EXTRACTION_FAILED
    assert submission.failure_reason == "Gave up after 3 retries: rate limited"


# --- claims and crash recovery -----------------------------------------------------------------


def test_live_claim_by_another_worker_is_reported_busy(
    db: Session, storage: InMemoryStorage, new_submission: NewSubmission
) -> None:
    submission = new_submission(make_pdf())
    assert claim(db, submission.id) == Claim.CLAIMED  # first worker takes it
    extractor = FakeExtractor()

    with pytest.raises(ClaimBusyError):
        run(submission, db, storage, extractor)

    assert extractor.calls == 0


def test_expired_claim_from_a_crashed_worker_is_taken_over(
    db: Session, storage: InMemoryStorage, new_submission: NewSubmission
) -> None:
    submission = new_submission(make_pdf())
    claim(db, submission.id)
    # Simulate a worker that claimed it long ago and then died.
    db.execute(
        update(Submission)
        .where(Submission.id == submission.id)
        .values(claimed_at=func.now() - CLAIM_LEASE - timedelta(seconds=1))
    )
    db.commit()

    run(submission, db, storage, FakeExtractor())

    assert status_of(db, submission) == SubmissionStatus.IN_REVIEW
    assert submission.claimed_at is None
