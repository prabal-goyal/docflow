from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from docflow.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping replaces dead connections (e.g. after a database restart)
    # instead of failing the request that happens to get one.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def get_db() -> Iterator[Session]:
    """One session per request. Endpoints commit explicitly; anything uncommitted is rolled back."""
    with Session(get_engine()) as session:
        yield session
