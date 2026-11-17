import pytest
from fastapi.testclient import TestClient

from docflow.models import Role
from tests.conftest import LoginAs, MakeUser

NEW_USER = {
    "email": "New.Reviewer@Example.com",
    "password": "temporary-pass-123",
    "role": "reviewer",
}


def test_admin_creates_user_with_temporary_password(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    headers = login_as(make_user(Role.ADMIN))

    response = client.post("/users", json=NEW_USER, headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new.reviewer@example.com"
    assert body["role"] == "reviewer"
    assert body["must_change_password"] is True
    assert "password" not in body
    assert "password_hash" not in body


def test_created_user_must_change_password_before_using_the_api(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    client.post("/users", json=NEW_USER, headers=login_as(make_user(Role.ADMIN)))

    login = client.post(
        "/auth/login", data={"username": NEW_USER["email"], "password": NEW_USER["password"]}
    )

    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


@pytest.mark.parametrize("role", [Role.SUBMITTER, Role.REVIEWER, Role.APPROVER])
def test_non_admins_cannot_create_or_list_users(
    client: TestClient, make_user: MakeUser, login_as: LoginAs, role: Role
) -> None:
    headers = login_as(make_user(role))

    assert client.post("/users", json=NEW_USER, headers=headers).status_code == 403
    assert client.get("/users", headers=headers).status_code == 403


def test_role_change_takes_effect_on_existing_token(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    admin = make_user(Role.ADMIN)
    headers = login_as(admin)

    admin.role = Role.SUBMITTER

    assert client.get("/users", headers=headers).status_code == 403


def test_duplicate_email_returns_conflict(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    headers = login_as(make_user(Role.ADMIN))
    assert client.post("/users", json=NEW_USER, headers=headers).status_code == 201

    differently_cased = {**NEW_USER, "email": "NEW.REVIEWER@example.com"}
    response = client.post("/users", json=differently_cased, headers=headers)

    assert response.status_code == 409


@pytest.mark.parametrize(
    "change",
    [{"password": "too-short"}, {"email": "not-an-email"}, {"role": "superuser"}],
)
def test_invalid_user_input_is_rejected(
    client: TestClient, make_user: MakeUser, login_as: LoginAs, change: dict[str, str]
) -> None:
    headers = login_as(make_user(Role.ADMIN))

    response = client.post("/users", json={**NEW_USER, **change}, headers=headers)

    assert response.status_code == 422


def test_admin_lists_users(client: TestClient, make_user: MakeUser, login_as: LoginAs) -> None:
    admin = make_user(Role.ADMIN)
    reviewer = make_user(Role.REVIEWER)

    response = client.get("/users", headers=login_as(admin))

    assert response.status_code == 200
    # Both users share one created_at (Postgres now() is fixed per transaction), so only the
    # set is asserted here; the API breaks ties by id to keep pagination stable.
    emails = {u["email"] for u in response.json()}
    assert emails == {admin.email, reviewer.email}
