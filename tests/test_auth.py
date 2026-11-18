from datetime import timedelta

import httpx2
from fastapi.testclient import TestClient

from docflow.config import get_settings
from docflow.models import Role
from docflow.security import create_access_token
from tests.conftest import DEFAULT_PASSWORD, LoginAs, MakeUser

NEW_PASSWORD = "a-brand-new-password-42"


def login(client: TestClient, email: str, password: str = DEFAULT_PASSWORD) -> httpx2.Response:
    response: httpx2.Response = client.post(
        "/auth/login", data={"username": email, "password": password}
    )
    return response


# --- login -------------------------------------------------------------------------------------


def test_login_returns_token(client: TestClient, make_user: MakeUser) -> None:
    user = make_user(Role.REVIEWER)

    response = login(client, user.email)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["must_change_password"] is False


def test_login_email_is_case_insensitive(client: TestClient, make_user: MakeUser) -> None:
    user = make_user(email="mixed@example.com")

    assert login(client, "  MIXED@Example.com ").status_code == 200
    assert user.email == "mixed@example.com"


def test_wrong_password_and_unknown_email_get_the_same_error(
    client: TestClient, make_user: MakeUser
) -> None:
    user = make_user()

    wrong_password = login(client, user.email, "not-the-right-password")
    unknown_email = login(client, "nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_inactive_user_cannot_log_in(client: TestClient, make_user: MakeUser) -> None:
    user = make_user(is_active=False)

    response = login(client, user.email)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


# --- tokens ------------------------------------------------------------------------------------


def test_me_returns_profile_without_password_hash(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    user = make_user(Role.APPROVER)

    response = client.get("/auth/me", headers=login_as(user))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == user.email
    assert body["role"] == "approver"
    assert "password_hash" not in body


def test_missing_token_is_rejected(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401


def test_expired_token_is_rejected(client: TestClient, make_user: MakeUser) -> None:
    user = make_user()
    token = create_access_token(user.id, get_settings(), expires_in=timedelta(seconds=-1))

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_tampered_token_is_rejected(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    headers = login_as(make_user())
    token = headers["Authorization"].removeprefix("Bearer ")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 401


def test_token_stops_working_when_user_is_deactivated(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    user = make_user()
    headers = login_as(user)

    user.is_active = False

    assert client.get("/auth/me", headers=headers).status_code == 401


# --- temporary passwords -----------------------------------------------------------------------


def test_temporary_password_blocks_everything_except_password_change(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    user = make_user(Role.ADMIN, must_change_password=True)
    headers = login_as(user)

    assert client.get("/users", headers=headers).status_code == 403
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_change_password_unlocks_account(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    user = make_user(Role.ADMIN, must_change_password=True)
    headers = login_as(user)

    response = client.post(
        "/auth/change-password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 204
    assert user.must_change_password is False
    assert client.get("/users", headers=headers).status_code == 200
    assert login(client, user.email, DEFAULT_PASSWORD).status_code == 401
    assert login(client, user.email, NEW_PASSWORD).status_code == 200


def test_change_password_requires_correct_current_password(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    user = make_user()

    response = client.post(
        "/auth/change-password",
        json={"current_password": "wrong-current-password", "new_password": NEW_PASSWORD},
        headers=login_as(user),
    )

    assert response.status_code == 400


def test_change_password_rejects_reusing_the_same_password(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    user = make_user()

    response = client.post(
        "/auth/change-password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD},
        headers=login_as(user),
    )

    assert response.status_code == 400


def test_change_password_rejects_short_new_password(
    client: TestClient, make_user: MakeUser, login_as: LoginAs
) -> None:
    user = make_user()

    response = client.post(
        "/auth/change-password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": "short"},
        headers=login_as(user),
    )

    assert response.status_code == 422
