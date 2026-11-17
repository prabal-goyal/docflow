import uuid
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from docflow.config import Settings

JWT_ALGORITHM = "HS256"

_password_hash = PasswordHash.recommended()  # argon2id

# Verified against when a login email does not exist, so unknown emails take as long as wrong
# passwords and response time does not reveal which emails are registered.
_DUMMY_HASH = _password_hash.hash("dummy-password-for-constant-time-login")


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


def burn_password_check(password: str) -> None:
    _password_hash.verify(password, _DUMMY_HASH)


def create_access_token(
    user_id: uuid.UUID, settings: Settings, expires_in: timedelta | None = None
) -> str:
    """The token carries only the user id. Role and active status are read from the database
    on every request, so changes take effect immediately instead of when the token expires."""
    now = datetime.now(UTC)
    default_lifetime = timedelta(minutes=settings.access_token_minutes)
    lifetime = expires_in if expires_in is not None else default_lifetime
    claims = {"sub": str(user_id), "iat": now, "exp": now + lifetime}
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID | None:
    """Returns the user id, or None if the token is invalid, tampered with or expired."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
        )
        return uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError):
        return None
