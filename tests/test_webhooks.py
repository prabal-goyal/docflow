"""Webhooks end to end against a real database: admin API, the outbox written by business
actions, and the dispatcher talking to a fake HTTP server."""

import json
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from docflow.api.webhooks import get_resolver
from docflow.config import Settings, get_settings
from docflow.dispatch import MAX_ATTEMPTS, claim_due, dispatch_due, redeliver
from docflow.models import (
    DeliveryStatus,
    Document,
    Role,
    Submission,
    SubmissionStatus,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookEvent,
)
from docflow.webhooks import WebhookEventType, emit, verify
from tests.conftest import LoginAs, MakeUser
from tests.invoices import SAMPLE_TEXT, make_invoice

PUBLIC_IP = "93.184.216.34"
URL = "https://hooks.customer.example/docflow"
SETTINGS: Settings = get_settings().model_copy(update={"webhook_allow_private_targets": False})


def public_dns(host: str) -> list[str]:
    return [PUBLIC_IP]


@pytest.fixture
def admin_headers(app: FastAPI, make_user: MakeUser, login_as: LoginAs) -> dict[str, str]:
    app.dependency_overrides[get_resolver] = lambda: public_dns
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    return login_as(make_user(Role.ADMIN))


def make_endpoint(
    db: Session, event_types: list[str] | None = None, active: bool = True, url: str = URL
) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        url=url,
        secret="whsec_test",
        event_types=event_types or [WebhookEventType.SUBMISSION_APPROVED.value],
        active=active,
    )
    db.add(endpoint)
    db.commit()
    return endpoint


def deliveries_for(db: Session, endpoint: WebhookEndpoint) -> list[WebhookDelivery]:
    query = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
    return list(db.scalars(query))


# --- admin API ---------------------------------------------------------------------------------


def test_secret_is_shown_once(client: TestClient, admin_headers: dict[str, str]) -> None:
    created = client.post(
        "/webhooks",
        json={"url": URL, "event_types": ["submission.approved"]},
        headers=admin_headers,
    )

    assert created.status_code == 201
    assert created.json()["secret"].startswith("whsec_")
    [listed] = client.get("/webhooks", headers=admin_headers).json()
    assert "secret" not in listed


@pytest.mark.parametrize(
    ("url", "dns"),
    [
        ("http://hooks.customer.example/x", [PUBLIC_IP]),  # not https
        ("https://internal.example/x", ["10.0.0.7"]),  # private address
        ("https://metadata.example/x", ["169.254.169.254"]),  # cloud metadata service
    ],
)
def test_unsafe_urls_are_rejected(
    app: FastAPI, client: TestClient, admin_headers: dict[str, str], url: str, dns: list[str]
) -> None:
    app.dependency_overrides[get_resolver] = lambda: lambda host: dns

    response = client.post(
        "/webhooks",
        json={"url": url, "event_types": ["submission.approved"]},
        headers=admin_headers,
    )

    assert response.status_code == 422


def test_test_events_cannot_be_subscribed_to(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/webhooks", json={"url": URL, "event_types": ["webhook.test"]}, headers=admin_headers
    )

    assert response.status_code == 422


@pytest.mark.parametrize("role", [Role.SUBMITTER, Role.REVIEWER, Role.APPROVER])
def test_only_admins_manage_webhooks(
    client: TestClient, make_user: MakeUser, login_as: LoginAs, role: Role
) -> None:
    headers = login_as(make_user(role))

    assert client.get("/webhooks", headers=headers).status_code == 403
    body = {"url": URL, "event_types": ["submission.approved"]}
    assert client.post("/webhooks", json=body, headers=headers).status_code == 403


def test_rotate_secret_returns_a_new_one(client: TestClient, admin_headers: dict[str, str]) -> None:
    body = {"url": URL, "event_types": ["submission.approved"]}
    created = client.post("/webhooks", json=body, headers=admin_headers).json()

    rotated = client.post(f"/webhooks/{created['id']}/rotate-secret", headers=admin_headers).json()

    assert rotated["secret"] != created["secret"]


def test_test_event_goes_only_to_that_endpoint(
    db: Session, client: TestClient, admin_headers: dict[str, str]
) -> None:
    target = make_endpoint(db)
    other = make_endpoint(db)

    response = client.post(f"/webhooks/{target.id}/test", headers=admin_headers)

    assert response.status_code == 202
    assert len(deliveries_for(db, target)) == 1
    assert deliveries_for(db, other) == []


# --- outbox ------------------------------------------------------------------------------------


@pytest.fixture
def in_review(db: Session, make_user: MakeUser) -> Callable[[int], Submission]:
    def _new(stage: int) -> Submission:
        submission = Submission(
            submitter_id=make_user(Role.SUBMITTER).id,
            status=SubmissionStatus.IN_REVIEW,
            stage=stage,
            fields=make_invoice().model_dump(mode="json"),
            flags=[],
        )
        db.add(submission)
        db.flush()
        submission.document = Document(
            storage_key="k", sha256="0" * 64, size_bytes=1, text_content=SAMPLE_TEXT
        )
        db.commit()
        return submission

    return _new


def decide(client: TestClient, sid: uuid.UUID, headers: dict[str, str], decision: str) -> None:
    body: dict[str, Any] = {"decision": decision, "comment": "reason"}
    response = client.post(f"/submissions/{sid}/decision", json=body, headers=headers)
    assert response.status_code == 200, response.text


def test_final_approval_writes_one_event_and_a_delivery_per_subscriber(
    db: Session,
    client: TestClient,
    in_review: Callable[[int], Submission],
    make_user: MakeUser,
    login_as: LoginAs,
) -> None:
    approvals = make_endpoint(db, ["submission.approved"])
    everything = make_endpoint(db, ["submission.approved", "submission.rejected"])
    rejections_only = make_endpoint(db, ["submission.rejected"])
    inactive = make_endpoint(db, ["submission.approved"], active=False)
    submission = in_review(2)

    decide(client, submission.id, login_as(make_user(Role.APPROVER)), "approve")

    [event] = db.scalars(select(WebhookEvent)).all()
    assert event.type == "submission.approved"
    assert event.payload["data"] == {
        "submission_id": str(submission.id),
        "status": "approved",
        "stage": None,
    }
    assert len(deliveries_for(db, approvals)) == 1
    assert len(deliveries_for(db, everything)) == 1
    assert deliveries_for(db, rejections_only) == []
    assert deliveries_for(db, inactive) == []


def test_stage_one_approval_is_not_announced(
    db: Session,
    client: TestClient,
    in_review: Callable[[int], Submission],
    make_user: MakeUser,
    login_as: LoginAs,
) -> None:
    make_endpoint(db, ["submission.approved"])

    decide(client, in_review(1).id, login_as(make_user(Role.REVIEWER)), "approve")

    assert db.scalars(select(WebhookEvent)).all() == []


def test_payload_carries_no_invoice_data(
    db: Session,
    client: TestClient,
    in_review: Callable[[int], Submission],
    make_user: MakeUser,
    login_as: LoginAs,
) -> None:
    make_endpoint(db, ["submission.rejected"])

    decide(client, in_review(1).id, login_as(make_user(Role.REVIEWER)), "reject")

    [event] = db.scalars(select(WebhookEvent)).all()
    assert "INV-2026-0042" not in json.dumps(event.payload)


def test_event_is_not_written_if_the_change_is_rolled_back(db: Session) -> None:
    make_endpoint(db)
    savepoint = db.begin_nested()
    emit(db, WebhookEventType.SUBMISSION_APPROVED, {"submission_id": "x"})
    savepoint.rollback()

    assert db.scalar(select(func.count()).select_from(WebhookDelivery)) == 0


# --- dispatcher --------------------------------------------------------------------------------


class FakeReceiver:
    """An HTTP server double: records requests and answers with a scripted response."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.respond = respond or (lambda request: httpx2.Response(204))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.respond(request)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handler))


def queued(db: Session, endpoint: WebhookEndpoint) -> WebhookDelivery:
    emit(db, WebhookEventType.SUBMISSION_APPROVED, {"submission_id": "s1"})
    db.commit()
    [delivery] = deliveries_for(db, endpoint)
    return delivery


def run_dispatch(db: Session, receiver: FakeReceiver, settings: Settings = SETTINGS) -> int:
    return dispatch_due(lambda: nullcontext(db), receiver.client(), settings, public_dns)


def test_successful_delivery_is_signed_and_verifiable(db: Session) -> None:
    endpoint = make_endpoint(db)
    delivery = queued(db, endpoint)
    receiver = FakeReceiver()

    sent = run_dispatch(db, receiver)

    db.refresh(delivery)
    assert sent == 1
    assert delivery.status == DeliveryStatus.SUCCEEDED
    assert delivery.attempt_count == 1
    [request] = receiver.requests
    assert request.headers["DocFlow-Event-Id"] == str(delivery.event_id)
    assert verify(
        endpoint.secret,
        request.headers["DocFlow-Timestamp"],
        request.headers["DocFlow-Signature"],
        request.content,
        now=int(datetime.now(UTC).timestamp()),
    )
    assert json.loads(request.content)["type"] == "submission.approved"


@pytest.mark.parametrize(
    "respond",
    [
        lambda request: httpx2.Response(500),
        lambda request: httpx2.Response(301, headers={"Location": "https://elsewhere.example"}),
        lambda request: (_ for _ in ()).throw(httpx2.ConnectTimeout("timed out")),
    ],
    ids=["server-error", "redirect-not-followed", "timeout"],
)
def test_failed_attempt_is_retried_after_a_minute(
    db: Session, respond: Callable[[httpx2.Request], httpx2.Response]
) -> None:
    delivery = queued(db, make_endpoint(db))
    before = datetime.now(UTC)

    run_dispatch(db, FakeReceiver(respond))

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempt_count == 1
    assert delivery.last_error
    wait = delivery.next_attempt_at - before
    assert timedelta(seconds=55) < wait < timedelta(seconds=65)


def test_delivery_fails_for_good_after_the_last_attempt(db: Session) -> None:
    delivery = queued(db, make_endpoint(db))
    delivery.attempt_count = MAX_ATTEMPTS - 1
    db.commit()

    run_dispatch(db, FakeReceiver(lambda request: httpx2.Response(503)))

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.last_status_code == 503


def test_not_yet_due_deliveries_are_left_alone(db: Session) -> None:
    delivery = queued(db, make_endpoint(db))
    db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery.id)
        .values(next_attempt_at=func.now() + timedelta(minutes=5))
    )
    db.commit()
    receiver = FakeReceiver()

    assert run_dispatch(db, receiver) == 0
    assert receiver.requests == []


def test_claimed_delivery_is_not_sent_twice_but_an_expired_claim_is_taken_over(
    db: Session,
) -> None:
    delivery = queued(db, make_endpoint(db))
    assert claim_due(db) == [delivery.id]  # another worker claimed it
    receiver = FakeReceiver()

    assert run_dispatch(db, receiver) == 0  # its lease is still running

    db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery.id)
        .values(next_attempt_at=func.now() - timedelta(seconds=1))
    )
    db.commit()
    assert run_dispatch(db, receiver) == 1  # that worker died; the lease expired
    assert len(receiver.requests) == 1


def test_deactivated_endpoint_is_not_called(db: Session) -> None:
    endpoint = make_endpoint(db)
    delivery = queued(db, endpoint)
    endpoint.active = False
    db.commit()
    receiver = FakeReceiver()

    run_dispatch(db, receiver)

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.FAILED
    assert receiver.requests == []


def test_target_is_rechecked_before_every_send(db: Session) -> None:
    """The host was public at registration but now resolves to the metadata service."""
    delivery = queued(db, make_endpoint(db))
    receiver = FakeReceiver()

    dispatch_due(
        lambda: nullcontext(db), receiver.client(), SETTINGS, lambda h: ["169.254.169.254"]
    )

    db.refresh(delivery)
    assert delivery.status == DeliveryStatus.FAILED
    assert "Unsafe target" in (delivery.last_error or "")
    assert receiver.requests == []


def test_redeliver_queues_a_failed_delivery_again(
    db: Session, client: TestClient, admin_headers: dict[str, str]
) -> None:
    delivery = queued(db, make_endpoint(db))
    delivery.status, delivery.attempt_count = DeliveryStatus.FAILED, MAX_ATTEMPTS
    db.commit()

    response = client.post(f"/webhook-deliveries/{delivery.id}/redeliver", headers=admin_headers)

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["attempt_count"] == 0
    assert run_dispatch(db, FakeReceiver()) == 1


def test_unknown_delivery_cannot_be_redelivered(db: Session) -> None:
    assert redeliver(db, uuid.uuid4()) is None


def test_admin_sees_the_delivery_log(
    db: Session, client: TestClient, admin_headers: dict[str, str]
) -> None:
    endpoint = make_endpoint(db)
    queued(db, endpoint)
    run_dispatch(db, FakeReceiver())

    [entry] = client.get(f"/webhooks/{endpoint.id}/deliveries", headers=admin_headers).json()

    assert entry["status"] == "succeeded"
    assert entry["last_status_code"] == 204
