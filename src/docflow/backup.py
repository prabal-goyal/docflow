"""Database backups to object storage, and restores for recovery and restore tests.

pg_dump's custom format is compressed and lets pg_restore recreate the schema and data, including
the audit-log trigger. Backups go to backups/YYYY/MM/DD/ in the document bucket; the bucket's
lifecycle rule (Terraform) deletes them after the retention period.
"""

import logging
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import URL, make_url

from docflow.storage import ObjectStorage

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "backups/"


def libpq_args(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Connection arguments for pg_dump/pg_restore, with the password in the environment so it
    never appears in the process list."""
    url: URL = make_url(database_url)
    args = ["--host", url.host or "localhost", "--port", str(url.port or 5432)]
    if url.username:
        args += ["--username", url.username]
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return [*args, "--dbname", url.database or ""], env


def backup_key(now: datetime) -> str:
    return f"{BACKUP_PREFIX}{now:%Y/%m/%d}/docflow-{now:%Y%m%dT%H%M%SZ}.dump"


def backup_database(database_url: str, storage: ObjectStorage, now: datetime | None = None) -> str:
    """Dumps the database and uploads it. Returns the storage key. Raises on any failure, so a
    broken backup is loud, never silent."""
    key = backup_key(now or datetime.now(UTC))
    connection, env = libpq_args(database_url)
    with tempfile.TemporaryDirectory() as workdir:
        dump = Path(workdir) / "docflow.dump"
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--file", str(dump), *connection],
            env=env,
            check=True,
            capture_output=True,
        )
        storage.put(key, dump.read_bytes(), "application/octet-stream")
    logger.info("Database backup uploaded", extra={"key": key})
    return key


def restore_database(target_url: str, storage: ObjectStorage, key: str) -> None:
    """Restores a backup into target_url, which must be an existing database. Objects that already
    exist there are dropped first (--clean)."""
    connection, env = libpq_args(target_url)
    with tempfile.TemporaryDirectory() as workdir:
        dump = Path(workdir) / "docflow.dump"
        dump.write_bytes(storage.get(key))
        subprocess.run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--exit-on-error",
                *connection,
                str(dump),
            ],
            env=env,
            check=True,
            capture_output=True,
        )
    logger.info("Database restored", extra={"key": key})
