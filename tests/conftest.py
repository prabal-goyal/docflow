import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from alembic import command

TEST_DATABASE_URL = os.environ.get(
    "DOCFLOW_TEST_DATABASE_URL",
    "postgresql+psycopg://docflow:docflow@localhost:5432/docflow_test",
)
TEST_JWT_SECRET = "test-only-secret-that-is-long-enough-1234567890"

# Forced (not setdefault) so tests can never point at a real database or reuse a real secret.
os.environ["DOCFLOW_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["DOCFLOW_JWT_SECRET"] = TEST_JWT_SECRET

from docflow.db import get_db  # noqa: E402  (must import after the environment is set)
from docflow.jobs import get_enqueue  # noqa: E402
from docflow.main import create_app  # noqa: E402
from docflow.models import Role, User  # noqa: E402
from docflow.security import hash_password  # noqa: E402
from docflow.storage import get_storage  # noqa: E402
from tests.fakes import InMemoryStorage, RecordingQueue  # noqa: E402

DEFAULT_PASSWORD = "correct-horse-battery-staple"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Wipes the test database once per run and builds it with the real migrations."""
    database = make_url(TEST_DATABASE_URL).database or ""
    assert database.endswith("_test"), f"Refusing to wipe non-test database {database!r}"

    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(config, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """Each test runs inside a transaction that is rolled back afterwards. Commits made by the
    code under test become savepoints, so tests stay isolated without truncating tables."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def job_queue() -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture
def app(db: Session, storage: InMemoryStorage, job_queue: RecordingQueue) -> FastAPI:
    """The real app wired to the test database and to in-memory storage and queue.
    Tests can add further dependency_overrides (e.g. settings) before making requests."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_enqueue] = lambda: job_queue
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


MakeUser = Callable[..., User]


@pytest.fixture
def make_user(db: Session) -> MakeUser:
    counter = 0

    def _make_user(
        role: Role = Role.SUBMITTER,
        *,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> User:
        nonlocal counter
        counter += 1
        user = User(
            email=email or f"{role.value}{counter}@example.com",
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
            must_change_password=must_change_password,
        )
        db.add(user)
        # Committed like real users, so a rollback inside the code under test cannot remove it.
        db.commit()
        return user

    return _make_user


LoginAs = Callable[[User], dict[str, str]]


@pytest.fixture
def login_as(client: TestClient) -> LoginAs:
    """Logs in through the real endpoint and returns an Authorization header."""

    def _login_as(user: User, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
        response = client.post("/auth/login", data={"username": user.email, "password": password})
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _login_as
