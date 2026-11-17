import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from docflow.models import Role, SubmissionStatus

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserCreate(BaseModel):
    email: EmailStr
    password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    role: Role

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, value: str) -> str:
        return normalize_email(value)


class UserOut(BaseModel):
    """Public view of a user. Deliberately has no password_hash field."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: Role
    is_active: bool
    must_change_password: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool


class ChangePasswordRequest(BaseModel):
    current_password: SecretStr
    new_password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sha256: str
    size_bytes: int
    page_count: int | None


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: SubmissionStatus
    stage: int | None
    failure_reason: str | None
    fields: dict[str, Any] | None
    document: DocumentOut
    created_at: datetime
    updated_at: datetime
