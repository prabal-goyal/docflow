from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (prefix DOCFLOW_) or a .env file."""

    model_config = SettingsConfigDict(env_prefix="DOCFLOW_", env_file=".env", extra="ignore")

    app_name: str = "DocFlow"
    environment: str = "local"
    version: str = "0.1.0"


@lru_cache
def get_settings() -> Settings:
    # Cached so the environment is parsed once per process; tests can override the dependency.
    return Settings()
