from fastapi.testclient import TestClient

from docflow.config import Settings, get_settings
from docflow.main import create_app


def test_health_returns_ok_with_environment() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(environment="test")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test", "version": "0.1.0"}
