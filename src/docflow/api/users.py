from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from docflow.deps import DbSession, require_roles
from docflow.models import Role, User
from docflow.schemas import UserCreate, UserOut
from docflow.security import hash_password

router = APIRouter(prefix="/users", tags=["users"])

AdminUser = Annotated[User, Depends(require_roles(Role.ADMIN))]

MAX_PAGE_SIZE = 200


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, db: DbSession, _admin: AdminUser) -> UserOut:
    """Creates a user with a temporary password; they must change it on first login."""
    user = User(
        email=body.email,
        password_hash=hash_password(body.password.get_secret_value()),
        role=body.role,
        must_change_password=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists"
        ) from None
    return UserOut.model_validate(user)


@router.get("")
def list_users(
    db: DbSession,
    _admin: AdminUser,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[UserOut]:
    users = db.scalars(select(User).order_by(User.created_at, User.id).limit(limit).offset(offset))
    return [UserOut.model_validate(u) for u in users]
