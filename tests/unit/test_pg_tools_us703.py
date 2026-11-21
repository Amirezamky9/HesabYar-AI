"""Unit tests for the pg_dump/pg_restore runner (US-703).

The subprocess runner and the psycopg connector are injected, so no
binary and no database is needed here: these tests pin the DSN
parsing, the argv/env split (the password NEVER appears in argv),
the version parsing, the identifier validation, and the error
translation (stderr scrubbed of the password). The real binaries
are exercised by the live drill test (tests/infrastructure).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from psycopg import sql

from hesabyar.domain.backup.errors import BackupConfigurationError, BackupToolError
from hesabyar.infrastructure.operations.pg_tools import (
    PgDumpRestoreRunner,
    maintenance_url,
    parse_database_url,
    with_database_name,
)

DSN = "postgresql://hesabyar:s3cr3t%40pass@db.internal:5433/hesabyar_test"


class FakeCompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records invocations; simulates pg_dump writing its -f file."""

    def __init__(
        self,
        *,
        dump_bytes: bytes = b"fake pg_dump custom-format archive",
        fail_with: str | None = None,
        version_stdout: str = "pg_dump (PostgreSQL) 16.4\n",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.dump_bytes = dump_bytes
        self.fail_with = fail_with
        self.version_stdout = version_stdout

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": list(args), "kwargs": kwargs})
        if "--version" in args:
            return FakeCompletedProcess(args, 0, self.version_stdout, "")
        if self.fail_with is not None:
            return FakeCompletedProcess(args, 1, "", self.fail_with)
        if "-f" in args:
            target = Path(args[args.index("-f") + 1])
            target.write_bytes(self.dump_bytes)
        return FakeCompletedProcess(args, 0, "", "")


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[Any] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def execute(self, query: Any) -> None:
        self.executed.append(query)


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.closed = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True

    def cursor(self) -> FakeCursor:
        return self.cursor_obj


# ---------------------------------------------------------------------------
# DSN parsing + URL rewriting
# ---------------------------------------------------------------------------


def test_parse_database_url_decodes_the_password() -> None:
    parts = parse_database_url(DSN)
    assert parts.host == "db.internal"
    assert parts.port == 5433
    assert parts.user == "hesabyar"
    assert parts.password == "s3cr3t@pass"
    assert parts.database == "hesabyar_test"


def test_parse_database_url_strips_the_sqlalchemy_prefix() -> None:
    parts = parse_database_url("postgresql+psycopg://user:pw@h:5432/dbname")
    assert parts.host == "h"
    assert parts.database == "dbname"
    assert parts.password == "pw"


def test_parse_database_url_rejects_bad_urls() -> None:
    with pytest.raises(BackupConfigurationError, match="postgresql://"):
        parse_database_url("mysql://user:pw@h/db")
    with pytest.raises(BackupConfigurationError, match="no database name"):
        parse_database_url("postgresql://user:pw@h:5432/")


def test_maintenance_and_with_database_name_urls() -> None:
    assert maintenance_url(DSN) == (
        "postgresql://hesabyar:s3cr3t%40pass@db.internal:5433/postgres"
    )
    assert with_database_name(DSN, "hesabyar_drill_x") == (
        "postgresql://hesabyar:s3cr3t%40pass@db.internal:5433/hesabyar_drill_x"
    )
    assert maintenance_url("postgresql+psycopg://u:p@h/db").startswith(
        "postgresql+psycopg://u:p@h/postgres"
    )


# ---------------------------------------------------------------------------
# Tool versions
# ---------------------------------------------------------------------------


def _runner(fake: FakeRunner, **kwargs: Any) -> PgDumpRestoreRunner:
    """A runner whose binary lookup always succeeds (hermetic unit tests)."""
    return PgDumpRestoreRunner(
        runner=fake,  # type: ignore[arg-type]
        which=lambda path: f"/usr/bin/{path}",
        **kwargs,
    )


def test_check_tools_reports_versions() -> None:
    runner = _runner(FakeRunner())
    info = runner.check_tools()
    assert info.pg_dump_version == "16.4"
    assert info.pg_restore_version == "16.4"


def test_check_tools_fails_when_the_binary_is_missing() -> None:
    runner = PgDumpRestoreRunner(
        runner=FakeRunner(),  # type: ignore[arg-type]
        which=lambda path: None,
    )
    with pytest.raises(BackupToolError, match="not found on PATH"):
        runner.check_tools()


def test_check_tools_fails_on_unparseable_version() -> None:
    runner = _runner(FakeRunner(version_stdout="no version here\n"))
    with pytest.raises(BackupToolError, match="Could not parse a version"):
        runner.check_tools()


# ---------------------------------------------------------------------------
# dump / restore
# ---------------------------------------------------------------------------


def test_dump_passes_the_password_via_env_only() -> None:
    fake = FakeRunner()
    runner = _runner(fake)
    result = runner.dump(DSN)
    assert result.data == fake.dump_bytes
    assert result.database == "hesabyar_test"
    assert result.pg_dump_version == "16.4"
    dump_call = next(c for c in fake.calls if "-Fc" in c["args"])
    argv = dump_call["args"]
    assert argv[0] == "pg_dump"
    assert "-Fc" in argv and "-f" in argv
    # The password is nowhere in argv (it travels via PGPASSWORD).
    assert not any("s3cr3t" in arg for arg in argv)
    env = dump_call["kwargs"]["env"]
    assert env["PGPASSWORD"] == "s3cr3t@pass"
    assert env["PGHOST"] == "db.internal"
    assert env["PGPORT"] == "5433"
    assert env["PGUSER"] == "hesabyar"
    assert env["PGDATABASE"] == "hesabyar_test"


def test_dump_without_a_password_clears_pgpassword() -> None:
    fake = FakeRunner()
    runner = _runner(fake)
    runner.dump("postgresql://user@h/db")
    dump_call = next(c for c in fake.calls if "-Fc" in c["args"])
    assert "PGPASSWORD" not in dump_call["kwargs"]["env"]


def test_dump_failure_scrubs_the_password_from_stderr() -> None:
    fake = FakeRunner(fail_with="FATAL: password authentication failed for user hesabyar (s3cr3t@pass)")
    runner = _runner(fake)
    with pytest.raises(BackupToolError) as excinfo:
        runner.dump(DSN)
    assert "s3cr3t@pass" not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    assert "pg_dump failed (exit 1)" in str(excinfo.value)


def test_restore_uses_no_owner_flags_for_scratch_only() -> None:
    fake = FakeRunner()
    runner = _runner(fake)
    runner.restore(DSN, b"dump-bytes", no_owner=True)
    restore_call = next(c for c in fake.calls if c["args"][0] == "pg_restore")
    assert "--exit-on-error" in restore_call["args"]
    assert "--no-owner" in restore_call["args"]
    assert "--no-privileges" in restore_call["args"]

    fake2 = FakeRunner()
    runner2 = _runner(fake2)
    runner2.restore(DSN, b"dump-bytes", no_owner=False)
    restore_call2 = next(c for c in fake2.calls if c["args"][0] == "pg_restore")
    assert "--no-owner" not in restore_call2["args"]
    assert "--no-privileges" not in restore_call2["args"]


def test_timeout_is_translated() -> None:
    def _timeout(*args: Any, **kwargs: Any) -> Any:
        if "--version" in args[0]:
            return FakeCompletedProcess(args[0], 0, "pg_dump (PostgreSQL) 16.4\n", "")
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

    runner = PgDumpRestoreRunner(runner=_timeout, timeout_seconds=5, which=lambda path: f"/usr/bin/{path}")  # type: ignore[arg-type]
    with pytest.raises(BackupToolError, match="timed out after 5s"):
        runner.dump(DSN)


# ---------------------------------------------------------------------------
# create / drop database
# ---------------------------------------------------------------------------


def test_create_and_drop_database_use_identifiers() -> None:
    connection = FakeConnection()

    def _connect(conninfo: str, **kwargs: Any) -> FakeConnection:
        assert conninfo == "postgresql://admin:pw@h:5432/postgres"
        assert kwargs["autocommit"] is True
        return connection

    runner = PgDumpRestoreRunner(connect=_connect)  # type: ignore[arg-type]
    runner.create_database("postgresql+psycopg://admin:pw@h:5432/postgres", "hesabyar_drill_x")
    runner.drop_database("postgresql://admin:pw@h:5432/postgres", "hesabyar_drill_x")
    executed = connection.cursor_obj.executed
    assert len(executed) == 2
    assert isinstance(executed[0], sql.Composed)
    assert "CREATE DATABASE" in str(executed[0])
    assert "hesabyar_drill_x" in str(executed[0])
    assert "DROP DATABASE IF EXISTS" in str(executed[1])
    assert "FORCE" in str(executed[1])


def test_create_database_rejects_unsafe_names() -> None:
    runner = PgDumpRestoreRunner(connect=lambda *a, **k: FakeConnection())  # type: ignore[arg-type]
    for bad in ("", "db; DROP TABLE x", "has-dash", "has.dot", "x" * 64, "9starts-with-digit"):
        with pytest.raises(BackupConfigurationError, match="Refusing to create/drop"):
            runner.create_database("postgresql://admin@h/postgres", bad)
        with pytest.raises(BackupConfigurationError, match="Refusing to create/drop"):
            runner.drop_database("postgresql://admin@h/postgres", bad)


def test_create_database_failure_is_typed() -> None:
    def _boom(conninfo: str, **kwargs: Any) -> Any:
        raise RuntimeError("permission denied for database postgres")

    runner = PgDumpRestoreRunner(connect=_boom)  # type: ignore[arg-type]
    with pytest.raises(BackupToolError, match="Could not create scratch database"):
        runner.create_database("postgresql://admin@h/postgres", "hesabyar_drill_x")
