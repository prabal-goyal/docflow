import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from docflow.models import AttemptOutcome, DeliveryStatus, QueryStatus, Role, SubmissionStatus
from docflow.webhooks import WebhookEventType

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


class FieldQueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    stage: int
    field: str
    question: str
    status: QueryStatus
    raised_by: uuid.UUID
    response: str | None
    corrected_value: str | None
    answered_by: uuid.UUID | None
    answered_at: datetime | None
    created_at: datetime


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submitter_id: uuid.UUID
    status: SubmissionStatus
    stage: int | None
    failure_reason: str | None
    fields: dict[str, Any] | None
    flags: list[dict[str, str]] | None
    human_edited_fields: list[str]
    queries: list[FieldQueryOut]
    document: DocumentOut
    created_at: datetime
    updated_at: datetime


class SubmissionSummary(BaseModel):
    """List view: enough to triage a work queue without the full extraction."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submitter_id: uuid.UUID
    status: SubmissionStatus
    stage: int | None
    flag_count: int
    created_at: datetime
    updated_at: datetime


Text = Field(min_length=1, max_length=2000)


class FieldChangeIn(BaseModel):
    field: str = Field(min_length=1, max_length=100)
    value: str | None = Field(max_length=2000)
    reason: str = Text


class EditFieldsRequest(BaseModel):
    changes: list[FieldChangeIn] = Field(min_length=1, max_length=100)


class QueryIn(BaseModel):
    field: str = Field(min_length=1, max_length=100)
    question: str = Text


class RaiseQueriesRequest(BaseModel):
    queries: list[QueryIn] = Field(min_length=1, max_length=100)


class AnswerIn(BaseModel):
    query_id: uuid.UUID
    response: str = Text
    corrected_value: str | None = Field(default=None, max_length=2000)


class AnswerQueriesRequest(BaseModel):
    answers: list[AnswerIn] = Field(min_length=1, max_length=100)


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    stage: int | None
    details: dict[str, Any]
    created_at: datetime


class ExtractionAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    model: str
    reasoning_effort: str
    outcome: AttemptOutcome
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    estimated_cost_usd: Decimal | None
    latency_ms: int
    response_id: str | None
    error: str | None
    created_at: datetime


class WebhookEndpointIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    event_types: list[WebhookEventType] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=255)

    @field_validator("event_types")
    @classmethod
    def no_test_events(cls, types: list[WebhookEventType]) -> list[WebhookEventType]:
        if WebhookEventType.TEST in types:
            raise ValueError("webhook.test is sent on request only and cannot be subscribed to")
        return sorted(set(types))


class WebhookEndpointUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    event_types: list[WebhookEventType] | None = Field(default=None, min_length=1)
    active: bool | None = None
    description: str | None = Field(default=None, max_length=255)

    @field_validator("event_types")
    @classmethod
    def no_test_events(cls, types: list[WebhookEventType] | None) -> list[WebhookEventType] | None:
        if types is not None and WebhookEventType.TEST in types:
            raise ValueError("webhook.test cannot be subscribed to")
        return None if types is None else sorted(set(types))


class WebhookEndpointOut(BaseModel):
    """No secret here: it is shown once, at creation or rotation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    event_types: list[str]
    active: bool
    description: str | None
    created_at: datetime
    updated_at: datetime


class WebhookEndpointWithSecret(WebhookEndpointOut):
    secret: str


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
