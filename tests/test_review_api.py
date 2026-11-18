"""The review workflow through the HTTP API, against a real database."""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from docflow.models import AuditEvent, Document, Role, Submission, SubmissionStatus, User
from tests.conftest import LoginAs, MakeUser
from tests.fakes import RecordingQueue
from tests.invoices import SAMPLE_TEXT, field, line, make_invoice

Headers = dict[str, str]
NewSubmission = Callable[..., Submission]


@pytest.fixture
def submitter(make_user: MakeUser) -> User:
    return make_user(Role.SUBMITTER)


@pytest.fixture
def headers(login_as: LoginAs, make_user: MakeUser, submitter: User) -> dict[Role, Headers]:
    """One logged-in user per role; the submitter owns the submissions made below."""
    return {
        Role.SUBMITTER: login_as(submitter),
        Role.REVIEWER: login_as(make_user(Role.REVIEWER)),
        Role.APPROVER: login_as(make_user(Role.APPROVER)),
        Role.ADMIN: login_as(make_user(Role.ADMIN)),
    }


@pytest.fixture
def new_submission(db: Session, submitter: User) -> NewSubmission:
    """A submission as the worker leaves it: extracted, checked and waiting at a stage."""

    def _new(
        status: SubmissionStatus = SubmissionStatus.IN_REVIEW,
        stage: int | None = 1,
        invoice: Any = None,
        flags: list[dict[str, str]] | None = None,
    ) -> Submission:
        submission = Submission(
            submitter_id=submitter.id,
            status=status,
            stage=stage,
            fields=(invoice or make_invoice()).model_dump(mode="json"),
            flags=flags or [],
        )
        db.add(submission)
        db.flush()
        submission.document = Document(
            storage_key=f"submissions/{submission.id}/original.pdf",
            sha256="0" * 64,
            size_bytes=1,
            page_count=1,
            text_content=SAMPLE_TEXT,
        )
        db.commit()
        return submission

    return _new


def post(client: TestClient, path: str, who: Headers, body: dict[str, Any]) -> httpx2.Response:
    response: httpx2.Response = client.post(path, json=body, headers=who)
    return response


def approve(
    client: TestClient, sid: uuid.UUID, who: Headers, comment: str | None = None
) -> httpx2.Response:
    return post(
        client, f"/submissions/{sid}/decision", who, {"decision": "approve", "comment": comment}
    )


def audit_actions(client: TestClient, sid: uuid.UUID, who: Headers) -> list[str]:
    return [e["action"] for e in client.get(f"/submissions/{sid}/audit", headers=who).json()]


FLAG = [{"field": "total", "code": "arithmetic_mismatch", "message": "x"}]


# --- approvals ---------------------------------------------------------------------------------


def test_two_stage_approval(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission().id

    stage_one = approve(client, sid, headers[Role.REVIEWER])
    final = approve(client, sid, headers[Role.APPROVER])

    assert (stage_one.json()["status"], stage_one.json()["stage"]) == ("in_review", 2)
    assert (final.json()["status"], final.json()["stage"]) == ("approved", None)
    assert audit_actions(client, sid, headers[Role.ADMIN]) == ["stage_approved", "approved"]


@pytest.mark.parametrize(
    ("stage", "role"),
    [
        (1, Role.APPROVER),  # stage 2's role acting at stage 1
        (2, Role.REVIEWER),  # stage 1's role acting at stage 2
        (1, Role.SUBMITTER),  # no self-approval
        (1, Role.ADMIN),  # admins manage, they do not decide
    ],
)
def test_only_the_stage_role_can_decide(
    client: TestClient,
    headers: dict[Role, Headers],
    new_submission: NewSubmission,
    stage: int,
    role: Role,
) -> None:
    sid = new_submission(stage=stage).id

    assert approve(client, sid, headers[role]).status_code == 403


def test_deciding_twice_is_a_conflict(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission(stage=2).id
    approve(client, sid, headers[Role.APPROVER])

    assert approve(client, sid, headers[Role.APPROVER]).status_code == 409


def test_approving_with_flags_needs_an_acknowledging_comment(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission(flags=FLAG).id

    without = approve(client, sid, headers[Role.REVIEWER])
    with_comment = approve(client, sid, headers[Role.REVIEWER], "Checked: rounding on the invoice")

    assert without.status_code == 422
    assert with_comment.status_code == 200
    [event] = client.get(f"/submissions/{sid}/audit", headers=headers[Role.ADMIN]).json()
    assert event["details"] == {
        "comment": "Checked: rounding on the invoice",
        "flags_acknowledged": 1,
    }


def test_rejection_needs_a_reason_and_is_final(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission().id
    reject = {"decision": "reject"}

    assert (
        post(client, f"/submissions/{sid}/decision", headers[Role.REVIEWER], reject).status_code
        == 422
    )
    rejected = post(
        client,
        f"/submissions/{sid}/decision",
        headers[Role.REVIEWER],
        {**reject, "comment": "Not our supplier"},
    )

    assert rejected.json()["status"] == "rejected"
    assert approve(client, sid, headers[Role.REVIEWER]).status_code == 409


def test_permission_error_comes_before_input_error(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    """A submitter rejecting without a reason is told they cannot reject at all (403), not that
    the reason is missing (422)."""
    sid = new_submission().id

    response = post(
        client, f"/submissions/{sid}/decision", headers[Role.SUBMITTER], {"decision": "reject"}
    )

    assert response.status_code == 403


# --- edits -------------------------------------------------------------------------------------


def test_reviewer_edit_fixes_a_flag_and_is_audited(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    wrong = make_invoice(total=field("3800.00", "3,742.00"))
    sid = new_submission(invoice=wrong, flags=FLAG).id

    response = client.patch(
        f"/submissions/{sid}/fields",
        json={"changes": [{"field": "total", "value": "3742.00", "reason": "Typo in extraction"}]},
        headers=headers[Role.REVIEWER],
    )

    body = response.json()
    assert response.status_code == 200
    assert body["fields"]["total"] == {"value": "3742.00", "source_quote": None}
    assert body["human_edited_fields"] == ["total"]
    # Checks re-ran: the arithmetic is fixed, and the edited value is not flagged as ungrounded.
    assert body["flags"] == []
    [event] = client.get(f"/submissions/{sid}/audit", headers=headers[Role.ADMIN]).json()
    assert event["action"] == "field_edited"
    assert event["details"] == {
        "field": "total",
        "old": "3800.00",
        "new": "3742.00",
        "reason": "Typo in extraction",
    }


def test_edit_that_breaks_the_arithmetic_is_flagged(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission().id

    response = client.patch(
        f"/submissions/{sid}/fields",
        json={"changes": [{"field": "line_items[0].amount", "value": "1.00", "reason": "test"}]},
        headers=headers[Role.REVIEWER],
    )

    codes = {(f["field"], f["code"]) for f in response.json()["flags"]}
    assert ("line_items[0].amount", "arithmetic_mismatch") in codes


def test_edit_with_an_unknown_field_changes_nothing(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission().id
    changes = [
        {"field": "total", "value": "1.00", "reason": "x"},
        {"field": "line_items[9].amount", "value": "1.00", "reason": "x"},
    ]

    response = client.patch(
        f"/submissions/{sid}/fields", json={"changes": changes}, headers=headers[Role.REVIEWER]
    )

    assert response.status_code == 422
    after = client.get(f"/submissions/{sid}", headers=headers[Role.REVIEWER]).json()
    assert after["fields"]["total"]["value"] == "3742.00"
    assert audit_actions(client, sid, headers[Role.ADMIN]) == []


# --- queries -----------------------------------------------------------------------------------


def raise_query(
    client: TestClient, sid: uuid.UUID, who: Headers, field_path: str = "total"
) -> httpx2.Response:
    return post(
        client,
        f"/submissions/{sid}/queries",
        who,
        {"queries": [{"field": field_path, "question": "Please confirm this value"}]},
    )


def test_query_loop_returns_to_the_same_stage_with_the_correction(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission(stage=2).id

    raised = raise_query(client, sid, headers[Role.APPROVER])
    [query] = raised.json()["queries"]
    answered = post(
        client,
        f"/submissions/{sid}/answers",
        headers[Role.SUBMITTER],
        {
            "answers": [
                {
                    "query_id": query["id"],
                    "response": "Typo on our side",
                    "corrected_value": "3742.50",
                }
            ]
        },
    )

    assert (raised.json()["status"], raised.json()["stage"]) == ("query_raised", 2)
    body = answered.json()
    assert (body["status"], body["stage"]) == ("in_review", 2)
    assert body["fields"]["total"]["value"] == "3742.50"
    assert body["queries"][0]["status"] == "answered"
    # The corrected total no longer matches subtotal + tax: the re-run checks catch it.
    assert ("total", "arithmetic_mismatch") in {(f["field"], f["code"]) for f in body["flags"]}
    assert audit_actions(client, sid, headers[Role.ADMIN]) == ["query_raised", "query_answered"]


def test_answer_without_correction_keeps_the_value(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission().id
    [query] = raise_query(client, sid, headers[Role.REVIEWER]).json()["queries"]

    answered = post(
        client,
        f"/submissions/{sid}/answers",
        headers[Role.SUBMITTER],
        {"answers": [{"query_id": query["id"], "response": "The value is correct"}]},
    )

    assert answered.json()["fields"]["total"]["value"] == "3742.00"
    assert answered.json()["human_edited_fields"] == []


def test_every_open_query_must_be_answered(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission().id
    body = {
        "queries": [
            {"field": "total", "question": "?"},
            {"field": "currency", "question": "?"},
        ]
    }
    first, _ = post(client, f"/submissions/{sid}/queries", headers[Role.REVIEWER], body).json()[
        "queries"
    ]

    response = post(
        client,
        f"/submissions/{sid}/answers",
        headers[Role.SUBMITTER],
        {"answers": [{"query_id": first["id"], "response": "ok"}]},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("role", [Role.REVIEWER, Role.APPROVER])
def test_only_the_submitter_answers(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission, role: Role
) -> None:
    sid = new_submission().id
    [query] = raise_query(client, sid, headers[Role.REVIEWER]).json()["queries"]

    response = post(
        client,
        f"/submissions/{sid}/answers",
        headers[role],
        {"answers": [{"query_id": query["id"], "response": "ok"}]},
    )

    assert response.status_code == 403


def test_other_submitters_cannot_see_or_answer(
    client: TestClient,
    headers: dict[Role, Headers],
    new_submission: NewSubmission,
    make_user: MakeUser,
    login_as: LoginAs,
) -> None:
    sid = new_submission().id
    [query] = raise_query(client, sid, headers[Role.REVIEWER]).json()["queries"]
    stranger = login_as(make_user(Role.SUBMITTER))

    answer = post(
        client,
        f"/submissions/{sid}/answers",
        stranger,
        {"answers": [{"query_id": query["id"], "response": "ok"}]},
    )

    assert answer.status_code == 404
    assert client.get(f"/submissions/{sid}/audit", headers=stranger).status_code == 404


def test_decisions_are_blocked_while_a_query_is_open(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    sid = new_submission().id
    raise_query(client, sid, headers[Role.REVIEWER])

    assert approve(client, sid, headers[Role.REVIEWER]).status_code == 409


# --- listing -----------------------------------------------------------------------------------


def listed(client: TestClient, who: Headers, **params: Any) -> set[str]:
    return {s["id"] for s in client.get("/submissions", params=params, headers=who).json()}


def test_awaiting_me_is_each_roles_work_queue(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    stage_one = str(new_submission(stage=1).id)
    stage_two = str(new_submission(stage=2).id)
    queried = str(new_submission(status=SubmissionStatus.QUERY_RAISED, stage=1).id)
    failed = str(new_submission(status=SubmissionStatus.EXTRACTION_FAILED, stage=None).id)

    assert listed(client, headers[Role.REVIEWER], awaiting_me=True) == {stage_one}
    assert listed(client, headers[Role.APPROVER], awaiting_me=True) == {stage_two}
    assert listed(client, headers[Role.SUBMITTER], awaiting_me=True) == {queried}
    assert listed(client, headers[Role.ADMIN], awaiting_me=True) == {failed}


def test_submitters_only_list_their_own(
    client: TestClient,
    headers: dict[Role, Headers],
    new_submission: NewSubmission,
    make_user: MakeUser,
    login_as: LoginAs,
) -> None:
    mine = str(new_submission().id)

    assert listed(client, headers[Role.SUBMITTER]) == {mine}
    assert listed(client, login_as(make_user(Role.SUBMITTER))) == set()
    assert mine in listed(client, headers[Role.REVIEWER], status="in_review", stage=1)


# --- retry -------------------------------------------------------------------------------------


def retry(client: TestClient, sid: uuid.UUID, who: Headers) -> httpx2.Response:
    return post(client, f"/submissions/{sid}/retry-extraction", who, {})


def test_admin_retries_a_failed_extraction(
    client: TestClient,
    headers: dict[Role, Headers],
    new_submission: NewSubmission,
    job_queue: RecordingQueue,
) -> None:
    sid = new_submission(status=SubmissionStatus.EXTRACTION_FAILED, stage=None).id

    response = retry(client, sid, headers[Role.ADMIN])

    assert response.json()["status"] == "uploaded"
    assert job_queue.enqueued == [sid]
    assert audit_actions(client, sid, headers[Role.ADMIN]) == ["extraction_retried"]


def test_recent_uploads_cannot_be_retried_but_stuck_ones_can(
    client: TestClient,
    db: Session,
    headers: dict[Role, Headers],
    new_submission: NewSubmission,
    job_queue: RecordingQueue,
) -> None:
    submission = new_submission(status=SubmissionStatus.UPLOADED, stage=None)

    assert retry(client, submission.id, headers[Role.ADMIN]).status_code == 409

    db.execute(
        update(Submission)
        .where(Submission.id == submission.id)
        .values(updated_at=func.now() - timedelta(minutes=11))
    )
    db.commit()
    assert retry(client, submission.id, headers[Role.ADMIN]).status_code == 200
    assert job_queue.enqueued == [submission.id]


@pytest.mark.parametrize("role", [Role.SUBMITTER, Role.REVIEWER, Role.APPROVER])
def test_only_admins_retry(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission, role: Role
) -> None:
    sid = new_submission(status=SubmissionStatus.EXTRACTION_FAILED, stage=None).id

    assert retry(client, sid, headers[role]).status_code == 403


# --- audit log ---------------------------------------------------------------------------------


def test_upload_writes_an_audit_event(client: TestClient, headers: dict[Role, Headers]) -> None:
    from tests.fakes import make_pdf

    response = client.post(
        "/submissions",
        files={"file": ("invoice.pdf", make_pdf(), "application/pdf")},
        headers=headers[Role.SUBMITTER],
    )

    sid = response.json()["id"]
    [event] = client.get(f"/submissions/{sid}/audit", headers=headers[Role.SUBMITTER]).json()
    assert event["action"] == "uploaded"
    assert event["actor_id"] is not None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET action = 'approved'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_audit_log_cannot_be_changed_even_with_direct_sql(
    client: TestClient,
    db: Session,
    headers: dict[Role, Headers],
    new_submission: NewSubmission,
    statement: str,
) -> None:
    sid = new_submission().id
    approve(client, sid, headers[Role.REVIEWER])

    savepoint = db.begin_nested()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text(statement))
    savepoint.rollback()

    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_line_item_edit_uses_line_paths(
    client: TestClient, headers: dict[Role, Headers], new_submission: NewSubmission
) -> None:
    invoice = make_invoice(
        line_items=[
            line(
                hs_code="82071910",
                quotes={"hs_code": "8207.19.10", "unit_price": "1,250.00", "amount": "2,500.00"},
            )
        ],
        subtotal=field("2500.00", "2,500.00"),
        total=field("2500.00", "2,500.00"),
    )
    sid = new_submission(invoice=invoice).id

    response = client.patch(
        f"/submissions/{sid}/fields",
        json={
            "changes": [
                {
                    "field": "line_items[0].hs_code",
                    "value": "82071990",
                    "reason": "Wrong subheading",
                }
            ]
        },
        headers=headers[Role.REVIEWER],
    )

    assert response.json()["fields"]["line_items"][0]["hs_code"]["value"] == "82071990"
    assert response.json()["human_edited_fields"] == ["line_items[0].hs_code"]
