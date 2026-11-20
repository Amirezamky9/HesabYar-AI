"""Operational health checks for the REST API (US-101, spec §7.6).

All probes are read-only:

- HesabYar database: a ``SELECT 1`` through a short-lived SQLAlchemy
  engine. The connection URL never appears in a response.
- Tryton adapter: the Phase 0 ``TrytonRestClient.ping()`` (a minimal REST
  read). No accounting operation is performed.
- Alembic revision: read from the migration scripts (``ScriptDirectory``)
  and the database's ``alembic_version`` table (``MigrationContext``).
  Nothing here ever runs a migration or any DDL (spec §7.2).
- Tryton release and installed modules: modules come from REST
  (``ir.module``, state ``activated``). The server release comes from the
  documented read-only JSON-RPC call ``common.server.version`` -- the REST
  API exposes no version endpoint, and the core ``ir`` module reports an
  empty version. This is a server-level metadata call, not an accounting
  operation, so the adapter's REST-only rule (docs/adapter.md) is
  untouched; no model data is touched through JSON-RPC.

Failures are logged server-side and reported as generic statuses; probe
exceptions never reach a response body.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.interfaces.rest.schemas.health import (
    ComponentStatus,
    InstallationResponse,
    MigrationStatus,
    ReadinessResponse,
    TrytonModuleInfo,
    TrytonStatus,
)

logger = logging.getLogger(__name__)

# Health probes must fail fast; they run on the request path of /readiness.
CHECK_TIMEOUT_SECONDS = 5.0

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


def resolve_migrations_dir() -> Path:
    """Return the Alembic migrations directory (env override for packaging)."""
    override = os.environ.get("HESABYAR_MIGRATIONS_DIR")
    return Path(override) if override else _DEFAULT_MIGRATIONS_DIR


class HealthService:
    """Read-only system-status probes. Stateless; safe to build per request."""

    def __init__(self, settings: AppSettings, migrations_dir: Path | None = None) -> None:
        self._settings = settings
        self._migrations_dir = migrations_dir or resolve_migrations_dir()

    # -- HesabYar database -------------------------------------------------

    def _engine(self) -> Engine:
        """Short-lived engine for one probe; never pooled across requests."""
        return create_engine(
            self._settings.database.sqlalchemy_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": CHECK_TIMEOUT_SECONDS},
        )

    def check_database(self) -> ComponentStatus:
        """True when the HesabYar database answers ``SELECT 1``."""
        engine = self._engine()
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return ComponentStatus(name="hesabyar_database", status="ok")
        except Exception as exc:
            # Logged only: the detail below must stay generic (no URL, no
            # driver message) so nothing sensitive can leak into a response.
            logger.warning("HesabYar database health check failed: %s", exc)
            return ComponentStatus(
                name="hesabyar_database",
                status="error",
                detail="Database connection failed",
            )
        finally:
            engine.dispose()

    # -- Tryton ------------------------------------------------------------

    def _tryton_client(self) -> TrytonRestClient:
        tryton = self._settings.tryton
        return TrytonRestClient(
            tryton.base_url,
            tryton.database,
            tryton.api_key,
            timeout=CHECK_TIMEOUT_SECONDS,
        )

    def check_tryton(self) -> ComponentStatus:
        """True when the Tryton engine answers a minimal REST read."""
        try:
            with self._tryton_client() as client:
                if client.ping():
                    return ComponentStatus(name="tryton_adapter", status="ok")
        except Exception as exc:
            logger.warning("Tryton health check failed: %s", exc)
        return ComponentStatus(
            name="tryton_adapter",
            status="error",
            detail="Tryton engine is unreachable",
        )

    def _tryton_release(self) -> str | None:
        """Server release (e.g. ``'8.2'``) or None when it cannot be read."""
        url = f"{self._settings.tryton.base_url.rstrip('/')}/rpc/"
        try:
            with httpx.Client(timeout=CHECK_TIMEOUT_SECONDS, trust_env=False) as client:
                response = client.post(
                    url,
                    json={"id": 1, "method": "common.server.version", "params": []},
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Tryton version probe failed: %s", exc)
            return None
        result = payload.get("result") if isinstance(payload, dict) else None
        return result if isinstance(result, str) and result else None

    def _tryton_modules(self) -> list[TrytonModuleInfo] | None:
        """Activated Tryton modules (name + version), or None when unreadable."""
        try:
            with self._tryton_client() as client:
                records = client.search(
                    "ir.module",
                    domain=[["state", "=", "activated"]],
                    fields=["name", "version"],
                    order=[["name", "ASC"]],
                )
        except Exception as exc:
            logger.warning("Tryton module listing failed: %s", exc)
            return None
        modules: list[TrytonModuleInfo] = []
        for record in records:
            name = record.get("name")
            if not isinstance(name, str):
                continue
            version = record.get("version")
            modules.append(
                TrytonModuleInfo(
                    name=name,
                    version=version if isinstance(version, str) and version else None,
                )
            )
        return modules

    def tryton_status(self) -> TrytonStatus:
        """Tryton release + activated modules (read-only)."""
        release = self._tryton_release()
        modules = self._tryton_modules()
        if modules is None:
            return TrytonStatus(
                status="error",
                release=release,
                database=self._settings.tryton.database,
                installed_modules=[],
            )
        return TrytonStatus(
            status="ok",
            release=release,
            database=self._settings.tryton.database,
            installed_modules=modules,
        )

    # -- Alembic -----------------------------------------------------------

    def _current_revision(self) -> str | None:
        """Revision applied to the database, or None when it cannot be read."""
        engine = self._engine()
        try:
            with engine.connect() as connection:
                return MigrationContext.configure(connection).get_current_revision()
        except Exception as exc:
            logger.warning("Could not read current Alembic revision: %s", exc)
            return None
        finally:
            engine.dispose()

    def migration_status(self) -> MigrationStatus:
        """HesabYar Alembic state. Reports state only -- never runs DDL."""
        if not (self._migrations_dir / "env.py").is_file():
            logger.warning("Migrations directory not found: %s", self._migrations_dir)
            return MigrationStatus(status="unknown")
        try:
            config = Config()
            config.set_main_option("script_location", str(self._migrations_dir))
            heads = ScriptDirectory.from_config(config).get_heads()
        except Exception as exc:
            logger.warning("Could not read Alembic script directory: %s", exc)
            return MigrationStatus(status="unknown")
        head = heads[0] if len(heads) == 1 else None
        current = self._current_revision()
        if current is None:
            return MigrationStatus(head_revision=head, up_to_date=False, status="degraded")
        up_to_date = head is not None and current == head
        return MigrationStatus(
            current_revision=current,
            head_revision=head,
            up_to_date=up_to_date,
            status="ok" if up_to_date else "degraded",
        )

    # -- aggregates --------------------------------------------------------

    def readiness(self) -> ReadinessResponse:
        """All dependency checks; 'ready' only when every check is ok."""
        checks = [self.check_database(), self.check_tryton()]
        ready = all(check.status == "ok" for check in checks)
        return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)

    def installation_status(self) -> InstallationResponse:
        """Full installation + schema status for the admin endpoint."""
        return InstallationResponse(
            environment=self._settings.env.value,
            database=self.check_database(),
            migration=self.migration_status(),
            tryton=self.tryton_status(),
        )
