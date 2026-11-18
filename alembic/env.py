import os
from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context
from docflow.models import Base

config = context.config
if config.config_file_name is not None:
    # Keep loggers that already exist (the app's), or running migrations in-process silences them.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    # Tests pass the URL in directly; everywhere else it comes from the environment.
    # Read straight from the environment (not Settings) so running migrations does not
    # require application secrets such as the JWT key.
    url = config.attributes.get("database_url") or os.environ.get("DOCFLOW_DATABASE_URL")
    if not url:
        raise RuntimeError("DOCFLOW_DATABASE_URL is not set")
    return str(url)


def run_migrations() -> None:
    engine = create_engine(database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
