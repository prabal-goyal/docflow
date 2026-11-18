import uuid

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from docflow.config import get_settings
from docflow.models import Role
from tests.conftest import LoginAs, MakeUser
from tests.fakes import InMemoryStorage, RecordingQueue, make_pdf


def upload(
    client: TestClient,
    headers: dict[str, str],
    data: bytes,
    idempotency_key: str | None = None,
) -> httpx2.Response:
    if idempotency_key is not None:
        headers = {**headers, "Idempotency-Key": idempotency_key}
    response: httpx2.Response = client.post(
        "/submissions",
        files={"file": ("invoice.pdf", data, "application/pdf")},
        headers=headers,
    )
    return response


@pytest.fixture
def submitter_headers(make_user: MakeUser, login_as: LoginAs) -> dict[str, str]:
    return login_as(make_user(Role.SUBMITTER))


# --- upload ------------------------------------------------------------------------------------


def test_upload_stores_file_and_queues_extraction(
    client: TestClient,
    submitter_headers: dict[str, str],
    storage: InMemoryStorage,
    job_queue: RecordingQueue,
) -> None:
    pdf = make_pdf()

    response = upload(client, submitter_headers, pdf)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "uploaded"
    assert body["document"]["size_bytes"] == len(pdf)
    assert body["document"]["page_count"] is None  # set later by the worker
    submission_id = uuid.UUID(body["id"])
    assert storage.objects[f"submissions/{submission_id}/original.pdf"] == pdf
    assert job_queue.enqueued == [submission_id]


def test_non_pdf_is_rejected(
    client: TestClient, submitter_headers: dict[str, str], job_queue: RecordingQueue
) -> None:
    response = upload(client, submitter_headers, b"GIF89a not a pdf")

    assert response.status_code == 415
    assert job_queue.enqueued == []


def test_file_over_size_limit_is_rejected(
    app: FastAPI, client: TestClient, submitter_headers: dict[str, str], job_queue: RecordingQueue
) -> None:
    small_limit = get_settings().model_copy(update={"max_upload_bytes": 1000})
    app.dependency_overrides[get_settings] = lambda: small_limit

    response = upload(client, submitter_headers, make_pdf() + b"\0" * 2000)

    assert response.status_code == 413
    assert job_queue.enqueued == []


@pytest.mark.parametrize("role", [Role.REVIEWER, Role.APPROVER])
def test_reviewers_cannot_upload(
    client: TestClient, make_user: MakeUser, login_as: LoginAs, role: Role
) -> None:
    response = upload(client, login_as(make_user(role)), make_pdf())

    assert response.status_code == 403


def test_upload_requires_login(client: TestClient) -> None:
    assert upload(client, {}, make_pdf()).status_code == 401


def test_storage_outage_returns_503_and_saves_nothing(
    client: TestClient,
    submitter_headers: dict[str, str],
    storage: InMemoryStorage,
    job_queue: RecordingQueue,
) -> None:
    storage.fail = True

    response = upload(client, submitter_headers, make_pdf(), idempotency_key="k1")

    assert response.status_code == 503
    assert job_queue.enqueued == []
    # Nothing was committed, so the client can safely retry with the same key.
    storage.fail = False
    assert upload(client, submitter_headers, make_pdf(), idempotency_key="k1").status_code == 202


def test_queue_outage_still_accepts_the_upload(
    client: TestClient, submitter_headers: dict[str, str], job_queue: RecordingQueue
) -> None:
    job_queue.fail = True

    response = upload(client, submitter_headers, make_pdf())

    assert response.status_code == 202
    assert response.json()["status"] == "uploaded"


# --- idempotency -------------------------------------------------------------------------------


def test_same_key_and_file_returns_the_original_submission(
    client: TestClient, submitter_headers: dict[str, str], job_queue: RecordingQueue
) -> None:
    pdf = make_pdf()

    first = upload(client, submitter_headers, pdf, idempotency_key="retry-123")
    second = upload(client, submitter_headers, pdf, idempotency_key="retry-123")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(job_queue.enqueued) == 1


def test_same_key_with_different_file_is_a_conflict(
    client: TestClient, submitter_headers: dict[str, str]
) -> None:
    upload(client, submitter_headers, make_pdf(pages=1), idempotency_key="retry-123")

    response = upload(client, submitter_headers, make_pdf(pages=2), idempotency_key="retry-123")

    assert response.status_code == 409


def test_idempotency_keys_are_scoped_per_user(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    pdf = make_pdf()
    first = upload(client, login_as(make_user()), pdf, idempotency_key="shared-key")
    second = upload(client, login_as(make_user()), pdf, idempotency_key="shared-key")

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]


def test_uploads_without_key_are_never_deduplicated(
    client: TestClient, submitter_headers: dict[str, str]
) -> None:
    pdf = make_pdf()

    first = upload(client, submitter_headers, pdf)
    second = upload(client, submitter_headers, pdf)

    assert first.json()["id"] != second.json()["id"]


# --- reading -----------------------------------------------------------------------------------


def test_submitter_reads_own_submission(
    client: TestClient, submitter_headers: dict[str, str]
) -> None:
    submission_id = upload(client, submitter_headers, make_pdf()).json()["id"]

    response = client.get(f"/submissions/{submission_id}", headers=submitter_headers)

    assert response.status_code == 200
    assert response.json()["id"] == submission_id


def test_other_submitters_get_404_not_403(
    client: TestClient, submitter_headers: dict[str, str], make_user: MakeUser, login_as: LoginAs
) -> None:
    submission_id = upload(client, submitter_headers, make_pdf()).json()["id"]

    response = client.get(f"/submissions/{submission_id}", headers=login_as(make_user()))

    assert response.status_code == 404


@pytest.mark.parametrize("role", [Role.REVIEWER, Role.APPROVER, Role.ADMIN])
def test_staff_can_read_any_submission(
    client: TestClient,
    submitter_headers: dict[str, str],
    make_user: MakeUser,
    login_as: LoginAs,
    role: Role,
) -> None:
    submission_id = upload(client, submitter_headers, make_pdf()).json()["id"]

    response = client.get(f"/submissions/{submission_id}", headers=login_as(make_user(role)))

    assert response.status_code == 200


def test_unknown_submission_is_404(client: TestClient, submitter_headers: dict[str, str]) -> None:
    response = client.get(f"/submissions/{uuid.uuid4()}", headers=submitter_headers)

    assert response.status_code == 404
