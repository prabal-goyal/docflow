from fastapi import FastAPI

from docflow.api import auth, health, submissions, users, webhooks
from docflow.config import get_settings
from docflow.errors import register_error_handlers


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(submissions.router)
    app.include_router(webhooks.router)
    register_error_handlers(app)
    return app


app = create_app()
