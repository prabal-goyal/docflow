import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from docflow.config import Settings, get_settings
from docflow.deps import DbSession

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str


class ReadyResponse(BaseModel):
    status: str
    database: str


@router.get("/health")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Liveness: the process is up. Never touches dependencies, so a database outage
    does not make the orchestrator restart healthy API processes."""
    return HealthResponse(status="ok", environment=settings.environment, version=settings.version)


@router.get("/ready")
def ready(db: DbSession, response: Response) -> ReadyResponse:
    """Readiness: can this instance serve traffic? Load balancers stop routing to it on 503."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Readiness check failed: database unreachable")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="unavailable", database="unreachable")
    return ReadyResponse(status="ok", database="ok")
