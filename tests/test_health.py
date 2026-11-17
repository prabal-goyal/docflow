from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from docflow.config import Settings, get_settings
from docflow.db import get_db
from docflow.main import create_app

# Nothing listens on port 1, so connecting fails immediately.
UNREACHABLE_DATABASE_URL = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"


def test_health_returns_ok_with_environment() -> None:
    app = create_app()
    test_settings: Settings = get_settings().model_copy(update={"environment": "test"})
    app.dependency_overrides[get_settings] = lambda: test_settings

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test", "version": "0.1.0"}


def test_ready_reports_database_ok(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_ready_returns_503_when_database_is_unreachable() -> None:
    engine = create_engine(UNREACHABLE_DATABASE_URL, connect_args={"connect_timeout": 2})

    def broken_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = broken_db

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}
    engine.dispose()
