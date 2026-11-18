from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from docflow.roles import Role

MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (prefix DOCFLOW_) or a .env file.

    Secrets have no defaults on purpose: the app refuses to start without them.
    """

    model_config = SettingsConfigDict(env_prefix="DOCFLOW_", env_file=".env", extra="ignore")

    app_name: str = "DocFlow"
    environment: str = "local"
    version: str = "0.1.0"
    log_level: str = "INFO"

    database_url: str
    jwt_secret: SecretStr = Field(min_length=MIN_JWT_SECRET_LENGTH)
    access_token_minutes: int = Field(default=30, gt=0)

    redis_url: str = "redis://localhost:6379/0"

    # S3 credentials are NOT settings: boto3 reads them from the standard AWS sources
    # (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY locally, the instance's IAM role on AWS).
    s3_bucket: str = "docflow-documents"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None  # set for a local S3-compatible server; None means AWS S3

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(default=20, gt=0)

    # The role that acts at each review stage, in order. A customer with more stages changes
    # this list, not the code.
    approval_stages: list[Role] = Field(default=[Role.REVIEWER, Role.APPROVER], min_length=1)
    # An upload still UPLOADED after this long was probably never queued (broker outage).
    stuck_upload_minutes: int = Field(default=10, gt=0)

    # Local development only: lets webhooks reach a receiver inside Docker over http. Never set
    # this in production; it disables the SSRF protection.
    webhook_allow_private_targets: bool = False
    webhook_timeout_seconds: float = Field(default=10, gt=0)
    # KMS key (id, ARN or alias) that encrypts webhook signing secrets. Required in production.
    webhook_secret_kms_key_id: str | None = None

    # Read from the standard OPENAI_API_KEY variable (no DOCFLOW_ prefix). Only the worker needs it.
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    llm_model: str = "gpt-6-luna"
    llm_reasoning_effort: Literal["none", "low", "medium", "high"] = "medium"
    llm_timeout_seconds: float = Field(default=120, gt=0)
    llm_max_output_tokens: int = Field(default=25_000, gt=0)
    # USD per 1M tokens (input, output), from OpenAI's pricing page on 2026-11-17. Used only for
    # cost estimates; a model missing here is logged with no cost rather than a wrong one.
    llm_prices_per_million: dict[str, tuple[Decimal, Decimal]] = {
        "gpt-6-luna": (Decimal("0.10"), Decimal("0.50")),
        "gpt-6.1-terra": (Decimal("1.00"), Decimal("5.00")),
        "gpt-6.1-sol": (Decimal("2.00"), Decimal("10.00")),
        "gpt-6-astra": (Decimal("10.00"), Decimal("50.00")),
    }

    @model_validator(mode="after")
    def production_is_locked_down(self) -> "Settings":
        # Fail at startup rather than run production with development shortcuts.
        if self.environment == "production":
            if not self.webhook_secret_kms_key_id:
                raise ValueError("production requires DOCFLOW_WEBHOOK_SECRET_KMS_KEY_ID")
            if self.webhook_allow_private_targets:
                raise ValueError("webhook_allow_private_targets must be false in production")
        return self

    @field_validator("approval_stages")
    @classmethod
    def stages_are_review_roles(cls, stages: list[Role]) -> list[Role]:
        # Separation of duties: submitters never review, admins never decide, and no role
        # approves twice.
        if any(role in (Role.SUBMITTER, Role.ADMIN) for role in stages):
            raise ValueError("approval stages may only use review roles, not submitter or admin")
        if len(set(stages)) != len(stages):
            raise ValueError("each role may act at only one approval stage")
        return stages


@lru_cache
def get_settings() -> Settings:
    # Cached so the environment is parsed once per process; tests can override the dependency.
    return Settings()  # required fields come from the environment
