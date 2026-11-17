from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from docflow.config import Settings, get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str


@router.get("/health")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Liveness check: the process is up. Dependency checks belong in /ready (added in M1)."""
    return HealthResponse(status="ok", environment=settings.environment, version=settings.version)
