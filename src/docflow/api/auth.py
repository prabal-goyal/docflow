from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from docflow.deps import AppSettings, AuthenticatedUser, DbSession
from docflow.models import User
from docflow.schemas import ChangePasswordRequest, Token, UserOut, normalize_email
from docflow.security import (
    burn_password_check,
    create_access_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_LOGIN = "Invalid email or password"


@router.post("/login")
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession, settings: AppSettings
) -> Token:
    """OAuth2 password form (the `username` field holds the email).

    The standard form format is what makes Swagger's Authorize button work.
    """
    user = db.scalar(select(User).where(User.email == normalize_email(form.username)))
    if user is None:
        burn_password_check(form.password)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_LOGIN)
    # One message for every failure, so callers cannot tell which emails exist or are disabled.
    if not verify_password(form.password, user.password_hash) or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_LOGIN)

    return Token(
        access_token=create_access_token(user.id, settings),
        must_change_password=user.must_change_password,
    )


@router.get("/me")
def me(user: AuthenticatedUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: ChangePasswordRequest, user: AuthenticatedUser, db: DbSession) -> None:
    current = body.current_password.get_secret_value()
    new = body.new_password.get_secret_value()
    if not verify_password(current, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is wrong"
        )
    if new == current:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be different"
        )
    user.password_hash = hash_password(new)
    user.must_change_password = False
    db.commit()
