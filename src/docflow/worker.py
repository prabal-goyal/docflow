"""Celery worker entry point: celery -A docflow.worker worker"""

import logging
import uuid

import httpx2
from celery import Task
from sqlalchemy.orm import Session

from docflow.config import get_settings
from docflow.db import get_engine
from docflow.dispatch import dispatch_due
from docflow.extraction import RetryableExtractionError
from docflow.jobs import DISPATCH_WEBHOOKS_TASK, EXTRACT_TASK, get_celery
from docflow.openai_extractor import get_extractor
from docflow.pipeline import CLAIM_LEASE, ClaimBusyError, give_up, process_submission
from docflow.storage import StorageError, get_s3_storage

logger = logging.getLogger(__name__)

celery_app = get_celery()

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 30


@celery_app.task(bind=True, name=EXTRACT_TASK, max_retries=MAX_RETRIES)
def extract_submission(self: Task, submission_id: str) -> None:
    sid = uuid.UUID(submission_id)
    with Session(get_engine()) as db:
        try:
            process_submission(sid, db, get_s3_storage(), get_extractor, get_settings())
        except ClaimBusyError as error:
            # Another worker holds the claim. Look again once its lease would have expired;
            # if that worker died, this delivery takes over.
            raise self.retry(exc=error, countdown=int(CLAIM_LEASE.total_seconds())) from error
        except (StorageError, RetryableExtractionError) as error:
            if self.request.retries >= MAX_RETRIES:
                logger.error("Giving up on submission %s: %s", sid, error)
                give_up(db, sid, f"Gave up after {MAX_RETRIES} retries: {error}")
                return
            # Exponential backoff: 30s, 60s, 120s.
            raise self.retry(
                exc=error, countdown=RETRY_BASE_SECONDS * 2**self.request.retries
            ) from error


@celery_app.task(name=DISPATCH_WEBHOOKS_TASK)
def dispatch_webhooks() -> int:
    """Sends due webhook deliveries. Overlapping runs are safe: claims use SKIP LOCKED."""
    with httpx2.Client() as client:
        return dispatch_due(lambda: Session(get_engine()), client, get_settings())
