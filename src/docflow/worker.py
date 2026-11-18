"""Celery worker entry point: celery -A docflow.worker worker"""

import logging
import uuid

import httpx2
from celery import Task
from celery.signals import setup_logging, task_postrun, task_prerun
from sqlalchemy.orm import Session

from docflow.backup import backup_database
from docflow.config import get_settings
from docflow.db import get_engine
from docflow.dispatch import dispatch_due
from docflow.extraction import RetryableExtractionError
from docflow.jobs import BACKUP_DATABASE_TASK, DISPATCH_WEBHOOKS_TASK, EXTRACT_TASK, get_celery
from docflow.logs import configure_logging, correlation_id
from docflow.openai_extractor import get_extractor
from docflow.pipeline import CLAIM_LEASE, ClaimBusyError, give_up, process_submission
from docflow.secret_cipher import get_cipher
from docflow.storage import StorageError, get_s3_storage

logger = logging.getLogger(__name__)

celery_app = get_celery()


@setup_logging.connect
def use_json_logs(**_: object) -> None:
    """Connecting this signal stops Celery from installing its own log format."""
    configure_logging(get_settings().log_level)


@task_prerun.connect
def tag_logs_with_task_id(task_id: str | None = None, **_: object) -> None:
    correlation_id.set(task_id)


@task_postrun.connect
def clear_task_id(**_: object) -> None:
    correlation_id.set(None)


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
        return dispatch_due(lambda: Session(get_engine()), client, get_settings(), get_cipher())


@celery_app.task(name=BACKUP_DATABASE_TASK)
def backup_database_task() -> str:
    """Nightly pg_dump to object storage. Failures raise, so they show up as task errors in logs."""
    return backup_database(get_settings().database_url, get_s3_storage())
