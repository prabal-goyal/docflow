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

from docflow.db import get_engine
from docflow.models import Role, User
from docflow.schemas import UserCreate
from docflow.security import hash_password


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="docflow.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="Create the first admin user")
    admin.add_argument("--email", required=True)
    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin(args.email)


if __name__ == "__main__":
    main()
