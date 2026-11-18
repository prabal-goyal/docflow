"""Limits password guessing: after too many failed logins for one email from one IP address, further
attempts are refused for the rest of the window, even with the right password."""

import hashlib
import logging
from functools import lru_cache
from typing import Any, Protocol

import redis

from docflow.config import get_settings

logger = logging.getLogger(__name__)

MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60


class LoginLimiter(Protocol):
    def retry_after(self, email: str, ip: str) -> int | None:
        """Seconds until login is allowed again, or None if it is allowed now."""
        ...

    def record_failure(self, email: str, ip: str) -> None: ...

    def reset(self, email: str, ip: str) -> None: ...


class RedisCommands(Protocol):
    """The Redis commands the limiter uses."""

    def get(self, name: str) -> Any: ...

    def incr(self, name: str) -> Any: ...

    def expire(self, name: str, time: int) -> Any: ...

    def ttl(self, name: str) -> Any: ...

    def delete(self, *names: str) -> Any: ...


def failure_key(email: str, ip: str) -> str:
    # Hashed so email addresses are not stored in Redis.
    digest = hashlib.sha256(f"{email}|{ip}".encode()).hexdigest()
    return f"login-failures:{digest}"


class RedisLoginLimiter:
    """Fixed window counter per (email, IP). If Redis is unreachable it fails open (logins still
    work) and logs the error: being unable to log in is worse than briefly losing the limit."""

    def __init__(
        self,
        client: RedisCommands,
        max_failures: int = MAX_FAILURES,
        window_seconds: int = WINDOW_SECONDS,
    ) -> None:
        self._redis = client
        self._max = max_failures
        self._window = window_seconds

    def retry_after(self, email: str, ip: str) -> int | None:
        key = failure_key(email, ip)
        try:
            count = self._redis.get(key)
            if count is None or int(count) < self._max:
                return None
            ttl = int(self._redis.ttl(key))
        except redis.RedisError:
            logger.exception("Login rate limiter unavailable; allowing the attempt")
            return None
        return max(ttl, 1)

    def record_failure(self, email: str, ip: str) -> None:
        key = failure_key(email, ip)
        try:
            if int(self._redis.incr(key)) == 1:
                self._redis.expire(key, self._window)  # the window starts at the first failure
        except redis.RedisError:
            logger.exception("Login rate limiter unavailable; failure not counted")

    def reset(self, email: str, ip: str) -> None:
        try:
            self._redis.delete(failure_key(email, ip))
        except redis.RedisError:
            logger.exception("Login rate limiter unavailable; counter not reset")


@lru_cache
def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5
    )


def get_login_limiter() -> LoginLimiter:
    """FastAPI dependency, replaced in tests with an in-memory limiter."""
    return RedisLoginLimiter(_redis_client())
