from datetime import UTC, datetime

from docflow.backup import backup_key, libpq_args


def test_connection_arguments_keep_the_password_out_of_the_command_line() -> None:
    args, env = libpq_args("postgresql+psycopg://docflow:s3cret@db:5433/docflow")

    assert args == [
        "--host",
        "db",
        "--port",
        "5433",
        "--username",
        "docflow",
        "--dbname",
        "docflow",
    ]
    assert "s3cret" not in " ".join(args)
    assert env["PGPASSWORD"] == "s3cret"


def test_backup_keys_are_dated_and_sortable() -> None:
    key = backup_key(datetime(2026, 11, 18, 2, 30, 5, tzinfo=UTC))

    assert key == "backups/2026/11/18/docflow-20261118T023005Z.dump"
