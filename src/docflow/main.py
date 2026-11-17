from fastapi import FastAPI

from docflow.api import health
from docflow.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.include_router(health.router)
    return app


app = create_app()
