from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from docflow.config import Settings, get_settings
from docflow.db import get_db
from docflow.models import Role, User
from docflow.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_authenticated_user(
    token: Annotated[str, Depends(oauth2_scheme)], db: DbSession, settings: AppSettings
) -> User:
    """A valid token for an existing, active user. Does NOT check must_change_password,
    so it is only used by the endpoints a user needs in order to change their password."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    user_id = decode_access_token(token, settings)
    if user_id is None:
        raise unauthorized
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user


AuthenticatedUser = Annotated[User, Depends(get_authenticated_user)]


def get_current_user(user: AuthenticatedUser) -> User:
    """The default for protected endpoints: also blocks users still on a temporary password."""
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required. Use POST /auth/change-password.",
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*allowed: Role) -> Callable[[User], User]:
    """Dependency factory: Depends(require_roles(Role.ADMIN)) allows only admins."""

    def check_role(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to do this",
            )
        return user

    return check_role
