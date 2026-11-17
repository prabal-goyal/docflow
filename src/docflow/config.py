from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (prefix DOCFLOW_) or a .env file.

    Secrets have no defaults on purpose: the app refuses to start without them.
    """

    model_config = SettingsConfigDict(env_prefix="DOCFLOW_", env_file=".env", extra="ignore")

    app_name: str = "DocFlow"
    environment: str = "local"
    version: str = "0.1.0"

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


@lru_cache
def get_settings() -> Settings:
    # Cached so the environment is parsed once per process; tests can override the dependency.
    return Settings()  # required fields come from the environment
