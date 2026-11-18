"""Operator commands.

    python -m docflow.cli create-admin --email admin@example.com

The password is read from DOCFLOW_ADMIN_PASSWORD if set (for scripted deployments),
otherwise prompted for twice without echoing.
"""

import argparse
import getpass
import os
import sys

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from docflow.backup import backup_database, restore_database
from docflow.config import get_settings
from docflow.db import get_engine
from docflow.models import Role, User
from docflow.schemas import UserCreate
from docflow.security import hash_password
from docflow.storage import get_s3_storage


def read_password() -> str:
    from_env = os.environ.get("DOCFLOW_ADMIN_PASSWORD")
    if from_env:
        return from_env
    first = getpass.getpass("Admin password: ")
    if first != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return first


def create_admin(email: str) -> None:
    try:
        data = UserCreate.model_validate(
            {"email": email, "password": read_password(), "role": Role.ADMIN}
        )
    except ValidationError as error:
        sys.exit(f"Invalid input:\n{error}")

    with Session(get_engine()) as db:
        if db.scalar(select(User).where(User.email == data.email)) is not None:
            sys.exit(f"A user with email {data.email} already exists")
        # The operator chose this password themselves, so no forced change on first login.
        db.add(
            User(
                email=data.email,
                password_hash=hash_password(data.password.get_secret_value()),
                role=Role.ADMIN,
                must_change_password=False,
            )
        )
        db.commit()
    print(f"Created admin {data.email}")


def init_storage() -> None:
    """Idempotent: creates the document bucket if it does not exist yet."""
    storage = get_s3_storage()
    print("Created bucket" if storage.ensure_bucket() else "Bucket already exists")


def backup() -> None:
    key = backup_database(get_settings().database_url, get_s3_storage())
    print(f"Backup written to {key}")


def restore(key: str, target_url: str) -> None:
    """Restore into an explicitly named database. Refuses the application's own database unless
    --i-mean-production is given, so a restore test cannot overwrite live data by accident."""
    restore_database(target_url, get_s3_storage(), key)
    print(f"Restored {key}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="docflow.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="Create the first admin user")
    admin.add_argument("--email", required=True)
    commands.add_parser("init-storage", help="Create the document bucket if missing")
    commands.add_parser("backup", help="Dump the database to object storage")
    restore_cmd = commands.add_parser("restore", help="Restore a backup into a database")
    restore_cmd.add_argument("--key", required=True, help="Storage key of the backup")
    restore_cmd.add_argument("--database-url", required=True, help="Target database (must exist)")
    restore_cmd.add_argument("--i-mean-production", action="store_true")
    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin(args.email)
    elif args.command == "init-storage":
        init_storage()
    elif args.command == "backup":
        backup()
    elif args.command == "restore":
        target = args.database_url.replace("postgresql+psycopg://", "postgresql://")
        live = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")
        if target == live and not args.i_mean_production:
            sys.exit("Refusing to restore over the live database (add --i-mean-production)")
        restore(args.key, args.database_url)


if __name__ == "__main__":
    main()
