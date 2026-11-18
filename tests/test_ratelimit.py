import pytest
import redis
from fastapi.testclient import TestClient

from docflow.models import Role
from docflow.ratelimit import RedisLoginLimiter, failure_key
from tests.conftest import DEFAULT_PASSWORD, MakeUser


class FakeRedis:
    """Just the commands the limiter uses, with TTLs we can inspect."""

    def __init__(self, broken: bool = False) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.broken = broken

    def _check(self) -> None:
        if self.broken:
            raise redis.ConnectionError("down")

    def get(self, name: str) -> bytes | None:
        key = name
        self._check()
        return str(self.values[key]).encode() if key in self.values else None

    def incr(self, name: str) -> int:
        key = name
        self._check()
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, name: str, time: int) -> None:
        self.ttls[name] = time

    def ttl(self, name: str) -> int:
        return self.ttls.get(name, -1)

    def delete(self, *names: str) -> None:
        self._check()
        for name in names:
            self.values.pop(name, None)


def limiter(fake: FakeRedis) -> RedisLoginLimiter:
    return RedisLoginLimiter(fake, max_failures=3, window_seconds=900)


def test_blocks_after_max_failures_until_the_window_ends() -> None:
    fake = FakeRedis()
    rl = limiter(fake)
    for _ in range(2):
        rl.record_failure("a@x.com", "1.2.3.4")
    assert rl.retry_after("a@x.com", "1.2.3.4") is None

    rl.record_failure("a@x.com", "1.2.3.4")

    assert rl.retry_after("a@x.com", "1.2.3.4") == 900
    assert rl.retry_after("a@x.com", "5.6.7.8") is None  # other IP unaffected
    assert rl.retry_after("b@x.com", "1.2.3.4") is None  # other account unaffected


def test_window_is_set_once_at_the_first_failure() -> None:
    fake = FakeRedis()
    rl = limiter(fake)
    rl.record_failure("a@x.com", "ip")
    fake.ttls[failure_key("a@x.com", "ip")] = 100  # time passes
    rl.record_failure("a@x.com", "ip")

    assert fake.ttls[failure_key("a@x.com", "ip")] == 100  # not extended


def test_keys_do_not_contain_the_email() -> None:
    assert "a@x.com" not in failure_key("a@x.com", "1.2.3.4")


def test_redis_outage_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    rl = limiter(FakeRedis(broken=True))

    rl.record_failure("a@x.com", "ip")
    assert rl.retry_after("a@x.com", "ip") is None
    assert "rate limiter unavailable" in caplog.text


# --- through the API (in-memory limiter, max 10) ------------------------------------------------


def attempt(client: TestClient, email: str, password: str) -> int:
    return client.post("/auth/login", data={"username": email, "password": password}).status_code


def test_tenth_failure_blocks_even_the_correct_password(
    client: TestClient, make_user: MakeUser
) -> None:
    user = make_user(Role.SUBMITTER)
    for _ in range(10):
        assert attempt(client, user.email, "wrong-password-123") == 401

    response = client.post(
        "/auth/login", data={"username": user.email, "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"


def test_unknown_emails_are_limited_too(client: TestClient) -> None:
    for _ in range(10):
        attempt(client, "nobody@example.com", "guess-guess-guess")

    assert attempt(client, "nobody@example.com", "guess-guess-guess") == 429


def test_successful_login_resets_the_counter(client: TestClient, make_user: MakeUser) -> None:
    user = make_user(Role.SUBMITTER)
    for _ in range(9):
        attempt(client, user.email, "wrong-password-123")
    assert attempt(client, user.email, DEFAULT_PASSWORD) == 200

    for _ in range(9):
        assert attempt(client, user.email, "wrong-password-123") == 401
