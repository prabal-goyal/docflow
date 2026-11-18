from fastapi import FastAPI

from docflow.api import auth, health, submissions, users, webhooks
from docflow.config import get_settings
from docflow.errors import register_error_handlers
from docflow.logs import configure_logging
from docflow.middleware import RequestLoggingMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(submissions.router)
    app.include_router(webhooks.router)
    register_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)
    return app


configure_logging(get_settings().log_level)
app = create_app()
