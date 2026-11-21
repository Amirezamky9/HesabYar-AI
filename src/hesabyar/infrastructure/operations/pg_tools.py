"""pg_dump / pg_restore runner + scratch-database administration (US-703).

PostgreSQL-native consistent backups (spec §15.4): ``pg_dump -Fc``
(custom format, compressed, restorable with pg_restore) for both
databases. This is the operations layer's one sanctioned use of
direct database access to Tryton's database — dumping and restoring
whole databases, the same way the operator runs pg_dump by hand.
No method here reads or writes Tryton TABLES (spec §19.2); the
drill's Tryton checks go through the REST adapter.

Security rules (enforced here, not by callers):

- The DSN password NEVER appears in argv, logs, or error messages:
  it travels via the ``PGPASSWORD`` environment variable of the
  child process only, and pg_dump/pg_restore stderr is scrubbed of
  the password before it is attached to any error.
- Database names for create/drop are validated as safe identifiers
  and passed as ``psycopg.sql.Identifier`` — never interpolated.
- ``check_tools`` fails fast when the binaries are missing, so a
  worker image without postgresql-client fails the job with a clear
  message instead of a bare ``FileNotFoundError`` mid-run.

Version note: pg_dump refuses to dump a server NEWER than itself
("aborting because of server version mismatch"). The runner
surfaces that stderr verbatim (scrubbed) so the fix — install a
postgresql-client at least as new as the server — is obvious.

The reverse mismatch is subtler: pg_dump 17+ writes
``SET transaction_timeout = 0;`` at the top of EVERY dump (based on
the CLIENT version, not the server's — a pg_dump 17 dumping a PG16
server still writes it), and a PG16-or-older server rejects that
SET with "unrecognized configuration parameter". In a default
pg_restore run the failed SET is reported and the restore
CONTINUES (exit status 1 is the only cost); with
``--exit-on-error`` the restore aborts at the first statement and
the target stays empty. So ``restore`` deliberately runs WITHOUT
``--exit-on-error`` and tolerates ONLY errors that are a
version-mismatched GUC SET (see ``_only_benign_guc_errors``) —
every other pg_restore error still fails the restore.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from hesabyar.domain.backup.errors import BackupConfigurationError, BackupToolError
from hesabyar.domain.backup.ports import DumpResult, PgToolsInfo

#: A database name create/drop will accept: a plain identifier, no
#: quoting tricks, within PostgreSQL's 63-byte limit.
_SAFE_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

#: ``pg_dump (PostgreSQL) 16.4`` -> ``16.4``.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

#: pg_restore prints the failing statement after each query error:
#: ``Command was: SET transaction_timeout = 0;``.
_COMMAND_WAS_RE = re.compile(r"^Command was:\s*(?P<command>.*)$", re.MULTILINE)

#: pg_restore groups stderr into blocks starting with this prefix.
_ERROR_BLOCK_RE = re.compile(r"(?=^pg_restore: (?:error|warning): )", re.MULTILINE)


def _only_benign_guc_errors(stderr: str) -> bool:
    """True when EVERY pg_restore error is a version-mismatched GUC SET.

    A pg_dump 17+ archive restored into a PG16-or-older server fails
    its preamble ``SET transaction_timeout = 0;`` (and, for future
    pg_dump versions, any other SET for a parameter the old server
    does not know). That failure is inherently harmless: the
    parameter does not exist on the target server, so there is
    nothing to set, and the restore continues past it. Any other
    error — a failed CREATE, a missing role, a corrupt archive —
    means the restore did NOT complete and must fail the drill.
    """
    blocks = [b for b in _ERROR_BLOCK_RE.split(stderr) if b.strip()]
    errors = [b for b in blocks if b.startswith("pg_restore: error:")]
    if not errors:
        return False
    for block in errors:
        if "unrecognized configuration parameter" not in block:
            return False
        command_match = _COMMAND_WAS_RE.search(block)
        if command_match is not None:
            command = command_match.group("command").strip()
            if not command.upper().startswith("SET "):
                return False
    return True

#: pg_dump/pg_restore write their version to stdout.
_VERSION_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class _DsnParts:
    host: str | None
    port: int | None
    user: str | None
    password: str | None
    database: str


def parse_database_url(database_url: str) -> _DsnParts:
    """Split a PostgreSQL DSN into connection parts (no secret handling here).

    Accepts ``postgresql://`` and ``postgresql+psycopg://`` (the
    SQLAlchemy dialect prefix is stripped). Raises
    ``BackupConfigurationError`` on a non-PostgreSQL URL or a missing
    database name.
    """
    url = database_url
    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://") :]
    if not url.startswith("postgresql://"):
        raise BackupConfigurationError(
            f"Unsupported database URL scheme in {url.split('://', 1)[0]!r}; "
            "expected postgresql://."
        )
    parsed = urllib.parse.urlparse(url)
    database = parsed.path.lstrip("/")
    if not database:
        raise BackupConfigurationError(
            "The database URL has no database name (expected postgresql://.../<dbname>)."
        )
    password = (
        urllib.parse.unquote(parsed.password) if parsed.password is not None else None
    )
    return _DsnParts(
        host=parsed.hostname,
        port=parsed.port,
        user=urllib.parse.unquote(parsed.username) if parsed.username else None,
        password=password,
        database=urllib.parse.unquote(database),
    )


def with_database_name(database_url: str, name: str) -> str:
    """The same DSN with the database name replaced by ``name``."""
    url = database_url
    prefix = ""
    if url.startswith("postgresql+psycopg://"):
        prefix, url = "postgresql+psycopg://", "postgresql://" + url[len("postgresql+psycopg://") :]
    parsed = urllib.parse.urlparse(url)
    replaced = parsed._replace(path="/" + urllib.parse.quote(name))
    rebuilt = urllib.parse.urlunparse(replaced)
    if prefix:
        rebuilt = prefix + rebuilt[len("postgresql://") :]
    return rebuilt


def maintenance_url(database_url: str) -> str:
    """The same DSN with the database name replaced by ``postgres``.

    The restore drill connects here (a role with CREATEDB) to create
    and drop scratch databases.
    """
    return with_database_name(database_url, "postgres")


def _validate_db_name(name: str) -> None:
    if not _SAFE_DB_NAME.match(name):
        raise BackupConfigurationError(
            f"Refusing to create/drop database {name!r}: names must match "
            "[A-Za-z_][A-Za-z0-9_]{0,62} (no quoting, no dots, no dashes)."
        )


# ``subprocess.run``-compatible callable, injectable for tests.
SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]

# ``psycopg.connect``-compatible callable, injectable for tests.
ConnectCallable = Callable[..., Any]


class PgDumpRestoreRunner:
    """``DatabaseDumperPort`` backed by the pg_dump/pg_restore binaries."""

    def __init__(
        self,
        *,
        pg_dump_path: str = "pg_dump",
        pg_restore_path: str = "pg_restore",
        timeout_seconds: int = 1800,
        runner: SubprocessRunner | None = None,
        connect: ConnectCallable | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self._pg_dump_path = pg_dump_path
        self._pg_restore_path = pg_restore_path
        self._timeout_seconds = timeout_seconds
        self._runner = runner or subprocess.run
        self._connect = connect or psycopg.connect
        # Injectable so the unit tests do not depend on the real
        # binaries being installed on the machine running them.
        self._which = which or shutil.which

    # -- tools ---------------------------------------------------------------

    def _tool_version(self, path: str) -> str:
        """``<tool> --version`` -> ``"16.4"`` (raises when unusable)."""
        resolved = self._which(path)
        if resolved is None:
            raise BackupToolError(
                f"{path} was not found on PATH. Install postgresql-client "
                "(at least as new as the PostgreSQL server) on the worker image."
            )
        try:
            result = self._runner(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupToolError(f"Could not run {path} --version: {exc}") from exc
        if result.returncode != 0:
            raise BackupToolError(
                f"{path} --version failed (exit {result.returncode}): "
                f"{(result.stderr or '').strip()}"
            )
        match = _VERSION_RE.search(result.stdout or "")
        if match is None:
            raise BackupToolError(
                f"Could not parse a version from {path} --version output: "
                f"{(result.stdout or '').strip()!r}"
            )
        return f"{match.group(1)}.{match.group(2)}"

    def check_tools(self) -> PgToolsInfo:
        """Verify pg_dump + pg_restore exist and report their versions."""
        return PgToolsInfo(
            pg_dump_path=self._pg_dump_path,
            pg_dump_version=self._tool_version(self._pg_dump_path),
            pg_restore_path=self._pg_restore_path,
            pg_restore_version=self._tool_version(self._pg_restore_path),
        )

    # -- subprocess plumbing ---------------------------------------------------

    @staticmethod
    def _env_for(parts: _DsnParts) -> dict[str, str]:
        """Child-process environment for one DSN (password via env only)."""
        env = dict(os.environ)
        if parts.host is not None:
            env["PGHOST"] = parts.host
        if parts.port is not None:
            env["PGPORT"] = str(parts.port)
        if parts.user is not None:
            env["PGUSER"] = parts.user
        env["PGDATABASE"] = parts.database
        if parts.password is not None:
            env["PGPASSWORD"] = parts.password
        else:
            env.pop("PGPASSWORD", None)
        return env

    @staticmethod
    def _redact(text: str, parts: _DsnParts) -> str:
        """Scrub the DSN password out of tool output before it is logged/raised."""
        if parts.password:
            return text.replace(parts.password, "***")
        return text

    def _run_tool(
        self,
        args: list[str],
        parts: _DsnParts,
        *,
        tool: str,
        tolerate_errors: Callable[[str], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one pg tool; translate failures into ``BackupToolError``.

        ``tolerate_errors`` (restore only) receives the scrubbed
        stderr of a failed run and returns True when the failure is
        benign — the result is returned instead of raising.
        """
        try:
            result = self._runner(
                args,
                capture_output=True,
                text=True,
                env=self._env_for(parts),
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise BackupToolError(
                f"{tool} was not found on PATH. Install postgresql-client "
                "(at least as new as the PostgreSQL server) on the worker image."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackupToolError(
                f"{tool} timed out after {self._timeout_seconds}s "
                f"(database {parts.database!r})."
            ) from exc
        except OSError as exc:
            raise BackupToolError(f"Could not run {tool}: {exc}") from exc
        if result.returncode != 0:
            stderr = self._redact((result.stderr or "").strip(), parts)
            if tolerate_errors is not None and tolerate_errors(stderr):
                return result
            raise BackupToolError(
                f"{tool} failed (exit {result.returncode}) for database "
                f"{parts.database!r}: {stderr}"
            )
        return result

    # -- dump / restore --------------------------------------------------------

    def dump(self, database_url: str) -> DumpResult:
        """Dump one database (``pg_dump -Fc``) to bytes."""
        parts = parse_database_url(database_url)
        info = self.check_tools()
        with tempfile.TemporaryDirectory(prefix="hesabyar-dump-") as tmpdir:
            dump_path = Path(tmpdir) / "dump.fc"
            self._run_tool(
                [self._pg_dump_path, "-Fc", "-f", str(dump_path)],
                parts,
                tool="pg_dump",
            )
            data = dump_path.read_bytes()
        return DumpResult(
            data=data, pg_dump_version=info.pg_dump_version, database=parts.database
        )

    def restore(self, database_url: str, data: bytes, *, no_owner: bool) -> None:
        """Restore a dump into an (empty) database with pg_restore.

        Deliberately WITHOUT ``--exit-on-error``: a pg_dump 17+
        archive's preamble SETs a parameter (``transaction_timeout``)
        a PG16-or-older server does not know, and ``--exit-on-error``
        would abort the restore at that first, harmless statement
        (CI run #32). Instead pg_restore runs to completion and the
        stderr is inspected: the restore succeeds only when every
        error is a version-mismatched GUC SET — any real error
        still raises.
        """
        parts = parse_database_url(database_url)
        self.check_tools()
        # pg_restore requires -d/--dbname (or -f/--file): the PGDATABASE
        # env var alone does not substitute, and without -d it exits 1.
        args = [self._pg_restore_path, "-d", parts.database]
        if no_owner:
            args += ["--no-owner", "--no-privileges"]
        with tempfile.TemporaryDirectory(prefix="hesabyar-restore-") as tmpdir:
            dump_path = Path(tmpdir) / "dump.fc"
            dump_path.write_bytes(data)
            args.append(str(dump_path))
            self._run_tool(
                args, parts, tool="pg_restore", tolerate_errors=_only_benign_guc_errors
            )

    # -- scratch-database administration ---------------------------------------

    def _admin_conninfo(self, admin_url: str) -> str:
        """A psycopg conninfo for an admin DSN (no SQLAlchemy prefix)."""
        conninfo = admin_url
        if conninfo.startswith("postgresql+psycopg://"):
            conninfo = "postgresql://" + conninfo[len("postgresql+psycopg://") :]
        if not conninfo.startswith("postgresql://"):
            raise BackupConfigurationError(
                "The admin database URL must use the postgresql:// scheme."
            )
        return conninfo

    def create_database(self, admin_url: str, name: str) -> None:
        """CREATE DATABASE on the server ``admin_url`` points at."""
        _validate_db_name(name)
        conninfo = self._admin_conninfo(admin_url)
        try:
            with self._connect(conninfo, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except BackupConfigurationError:
            raise
        except Exception as exc:
            raise BackupToolError(
                f"Could not create scratch database {name!r}: {exc}"
            ) from exc

    def drop_database(self, admin_url: str, name: str) -> None:
        """DROP DATABASE IF EXISTS (WITH FORCE) on ``admin_url``'s server."""
        _validate_db_name(name)
        conninfo = self._admin_conninfo(admin_url)
        try:
            with self._connect(conninfo, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )
        except BackupConfigurationError:
            raise
        except Exception as exc:
            raise BackupToolError(
                f"Could not drop scratch database {name!r}: {exc}"
            ) from exc
