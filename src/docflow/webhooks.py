"""Webhook events: signing, and writing events to the outbox inside the caller's transaction."""

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docflow.models import Submission, WebhookDelivery, WebhookEndpoint, WebhookEvent

SIGNATURE_VERSION = "v1"
# Receivers should reject requests whose timestamp is further than this from their clock.
TIMESTAMP_TOLERANCE_SECONDS = 300


class WebhookEventType(StrEnum):
    SUBMISSION_APPROVED = "submission.approved"
    SUBMISSION_REJECTED = "submission.rejected"
    SUBMISSION_QUERY_RAISED = "submission.query_raised"
    SUBMISSION_EXTRACTION_FAILED = "submission.extraction_failed"
    TEST = "webhook.test"  # sent only by POST /webhooks/{id}/test


SUBSCRIBABLE_EVENTS = frozenset(WebhookEventType) - {WebhookEventType.TEST}


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def canonical_body(payload: dict[str, Any]) -> bytes:
    """The exact bytes sent and signed. Deterministic, so every retry is byte-identical."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over "{timestamp}.{body}". Signing the timestamp lets receivers reject
    replayed requests."""
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify(secret: str, timestamp: str, signature: str, body: bytes, now: int) -> bool:
    """Reference verification, as a receiver should implement it."""
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent_at) > TIMESTAMP_TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, sent_at, body), signature)


def emit(
    db: Session,
    event_type: WebhookEventType,
    data: dict[str, Any],
    only_endpoint: WebhookEndpoint | None = None,
) -> WebhookEvent:
    """Writes the event and one pending delivery per subscribed, active endpoint. Uses the
    caller's transaction: the event exists if and only if the change it describes is saved."""
    event = WebhookEvent(type=event_type.value, payload={})
    db.add(event)
    db.flush()  # assigns the id used in the payload
    event.payload = {
        "id": str(event.id),
        "type": event_type.value,
        "created_at": datetime.now(UTC).isoformat(),
        "data": data,
    }
    if only_endpoint is not None:
        endpoints = [only_endpoint]
    else:
        endpoints = list(
            db.scalars(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.active.is_(True),
                    WebhookEndpoint.event_types.contains([event_type.value]),
                )
            )
        )
    for endpoint in endpoints:
        db.add(WebhookDelivery(event_id=event.id, endpoint_id=endpoint.id))
    return event


def submission_data(submission: Submission, **extra: Any) -> dict[str, Any]:
    """Thin payload: identifiers and status only, never invoice values. Receivers fetch details
    through the API with their own credentials."""
    return {
        "submission_id": str(submission.id),
        "status": submission.status.value,
        "stage": submission.stage,
        **extra,
    }
