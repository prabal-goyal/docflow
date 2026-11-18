"""The Celery app and the API's way of enqueueing work. The API sends tasks by name, so it
never imports the worker's code."""

import uuid
from collections.abc import Callable
from functools import lru_cache

from celery import Celery

from docflow.config import get_settings

EXTRACT_TASK = "docflow.extract_submission"
DISPATCH_WEBHOOKS_TASK = "docflow.dispatch_webhooks"
# How often the scheduler sends due webhook deliveries; also the worst-case added delay.
WEBHOOK_DISPATCH_INTERVAL_SECONDS = 10.0

# Hard ceiling on one task, including parsing an untrusted PDF and the LLM call.
TASK_SOFT_TIME_LIMIT_SECONDS = 300
TASK_TIME_LIMIT_SECONDS = 330

EnqueueExtraction = Callable[[uuid.UUID], None]


@lru_cache
def get_celery() -> Celery:
    app = Celery("docflow", broker=get_settings().redis_url)
    app.conf.update(
        # Acknowledge a task only after it finishes, so a crashed worker's task is redelivered.
        # This makes delivery at-least-once, which is why the pipeline claims work atomically.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=TASK_TIME_LIMIT_SECONDS,
        broker_connection_retry_on_startup=True,
        # Run by the `scheduler` service (celery beat), executed by the worker.
        beat_schedule={
            "dispatch-webhooks": {
                "task": DISPATCH_WEBHOOKS_TASK,
                "schedule": WEBHOOK_DISPATCH_INTERVAL_SECONDS,
                # A missed run is pointless: the next one picks up everything that is due.
                "options": {"expires": WEBHOOK_DISPATCH_INTERVAL_SECONDS},
            }
        },
    )
    return app


def get_enqueue() -> EnqueueExtraction:
    """FastAPI dependency, replaced in tests so no broker is needed."""

    def enqueue(submission_id: uuid.UUID) -> None:
        get_celery().send_task(EXTRACT_TASK, args=[str(submission_id)])

    return enqueue
