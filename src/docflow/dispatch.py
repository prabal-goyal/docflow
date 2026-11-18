"""Sends due webhook deliveries. Runs from a scheduled Celery task every few seconds.

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED and a lease (next_attempt_at moved into the
future), so overlapping runs never send the same delivery twice, and a delivery claimed by a
worker that crashed becomes due again when its lease expires."""

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

import httpx2
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from docflow.config import Settings
from docflow.models import DeliveryStatus, WebhookDelivery
from docflow.secret_cipher import SecretCipher, SecretDecryptionError
from docflow.ssrf import (
    Resolver,
    UnsafeUrlError,
    pin_to_vetted_address,
    resolve_host,
    validate_target,
)
from docflow.webhooks import canonical_body, sign

logger = logging.getLogger(__name__)

# Delay before attempts 2..6. Attempt 1 is immediate; about 15 hours from first to last.
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=12),
)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
CLAIM_LEASE = timedelta(seconds=60)
BATCH_SIZE = 20
MAX_ERROR_CHARS = 500


def claim_due(db: Session, limit: int = BATCH_SIZE) -> list[uuid.UUID]:
    due = (
        select(WebhookDelivery.id)
        .where(
            WebhookDelivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.DELIVERING]),
            WebhookDelivery.next_attempt_at <= func.now(),
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(
        db.scalars(
            update(WebhookDelivery)
            .where(WebhookDelivery.id.in_(due.scalar_subquery()))
            .values(status=DeliveryStatus.DELIVERING, next_attempt_at=func.now() + CLAIM_LEASE)
            .returning(WebhookDelivery.id)
        )
    )
    db.commit()
    return claimed


def deliver(
    db: Session,
    delivery_id: uuid.UUID,
    client: httpx2.Client,
    settings: Settings,
    cipher: SecretCipher,
    resolve: Resolver = resolve_host,
) -> DeliveryStatus | None:
    """Sends one claimed delivery. Returns its new status, or None if it was not ours to send."""
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.status != DeliveryStatus.DELIVERING:
        return None
    endpoint = delivery.endpoint
    delivery.attempt_count += 1
    now = datetime.now(UTC)

    if not endpoint.active:
        return finish(db, delivery, DeliveryStatus.FAILED, None, "Endpoint is deactivated")
    try:
        # Checked again on every send: DNS can change after the endpoint was registered.
        addresses = validate_target(endpoint.url, settings.webhook_allow_private_targets, resolve)
        target = pin_to_vetted_address(endpoint.url, addresses)
    except UnsafeUrlError as error:
        return finish(db, delivery, DeliveryStatus.FAILED, None, f"Unsafe target: {error}")

    try:
        secret = cipher.decrypt(endpoint.secret)
    except SecretDecryptionError as error:
        return finish(
            db, delivery, DeliveryStatus.FAILED, None, f"Signing secret unusable: {error}"
        )
    except (BotoCoreError, ClientError) as error:
        # KMS unreachable or throttled: transient, try again later.
        return retry_or_fail(db, delivery, None, f"KMS error: {type(error).__name__}", now)

    body = canonical_body(delivery.event.payload)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DocFlow-Webhooks/1",
        "DocFlow-Event-Id": str(delivery.event_id),
        "DocFlow-Timestamp": str(timestamp),
        "DocFlow-Signature": sign(secret, timestamp, body),
    }
    if target.host_header:
        headers["Host"] = target.host_header
    try:
        response = client.post(
            target.url,
            content=body,
            headers=headers,
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,
            extensions={"sni_hostname": target.sni_hostname} if target.sni_hostname else None,
        )
    except httpx2.HTTPError as error:
        return retry_or_fail(db, delivery, None, f"{type(error).__name__}: {error}", now)

    if 200 <= response.status_code < 300:
        delivery.delivered_at = now
        return finish(db, delivery, DeliveryStatus.SUCCEEDED, response.status_code, None)
    return retry_or_fail(db, delivery, response.status_code, f"HTTP {response.status_code}", now)


def retry_or_fail(
    db: Session,
    delivery: WebhookDelivery,
    status_code: int | None,
    error: str,
    now: datetime,
) -> DeliveryStatus:
    if delivery.attempt_count >= MAX_ATTEMPTS:
        return finish(db, delivery, DeliveryStatus.FAILED, status_code, error)
    delivery.next_attempt_at = now + RETRY_DELAYS[delivery.attempt_count - 1]
    return finish(db, delivery, DeliveryStatus.PENDING, status_code, error)


def finish(
    db: Session,
    delivery: WebhookDelivery,
    status: DeliveryStatus,
    status_code: int | None,
    error: str | None,
) -> DeliveryStatus:
    delivery.status = status
    delivery.last_status_code = status_code
    delivery.last_error = error[:MAX_ERROR_CHARS] if error else None
    db.commit()
    if status == DeliveryStatus.FAILED:
        logger.warning("Webhook delivery %s failed for good: %s", delivery.id, error)
    return status


def dispatch_due(
    open_session: Callable[[], AbstractContextManager[Session]],
    client: httpx2.Client,
    settings: Settings,
    cipher: SecretCipher,
    resolve: Resolver = resolve_host,
) -> int:
    """Claims a batch and delivers each one in its own transaction. Returns how many were sent."""
    with open_session() as db:
        claimed = claim_due(db)
    for delivery_id in claimed:
        with open_session() as db:
            try:
                deliver(db, delivery_id, client, settings, cipher, resolve)
            except Exception:
                # Leave it DELIVERING: the lease expires and the next run retries it.
                logger.exception("Unexpected error delivering webhook %s", delivery_id)
    return len(claimed)


def redeliver(db: Session, delivery_id: uuid.UUID) -> WebhookDelivery | None:
    """Admin action: queue a delivery again with a fresh set of attempts."""
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return None
    db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id)
        .values(
            status=DeliveryStatus.PENDING,
            attempt_count=0,
            next_attempt_at=func.now(),
            last_error=None,
        )
    )
    db.commit()
    db.refresh(delivery)
    return delivery
