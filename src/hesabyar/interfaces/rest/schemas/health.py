"""Response schemas for the health, readiness and installation endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from hesabyar import __version__

ComponentStatusValue = Literal["ok", "error", "unknown"]


class HealthResponse(BaseModel):
    """Liveness probe: the process is up. No dependency is checked."""

    status: Literal["ok"] = "ok"
    service: str = "hesabyar"
    version: str = __version__
    environment: str = Field(description="Runtime environment (dev/test/staging/production)")


class ComponentStatus(BaseModel):
    """Status of one dependency probed by readiness / installation."""

    name: str
    status: ComponentStatusValue
    detail: str | None = Field(
        default=None,
        description="Short failure hint; never contains URLs, credentials or internals",
    )


class ReadinessResponse(BaseModel):
    """Readiness probe: every dependency must be ok for status 'ready'."""

    status: Literal["ready", "not_ready"]
    checks: list[ComponentStatus]


class MigrationStatus(BaseModel):
    """HesabYar Alembic state. Read-only: never runs migrations (spec §7.2)."""

    current_revision: str | None = Field(
        default=None, description="Revision applied to the database (null when unreadable)"
    )
    head_revision: str | None = Field(
        default=None, description="Head revision in the migration scripts (null when ambiguous)"
    )
    up_to_date: bool = Field(
        default=False, description="True when the applied revision equals the single head"
    )
    status: Literal["ok", "degraded", "unknown"] = Field(
        description="'ok' when up to date, 'degraded' when behind/unreadable, "
        "'unknown' when the migration scripts cannot be found"
    )


class TrytonModuleInfo(BaseModel):
    """One activated Tryton module (name + version from ir.module)."""

    name: str
    version: str | None = Field(
        default=None, description="Module version; null when Tryton reports none (core modules)"
    )


class TrytonStatus(BaseModel):
    """Tryton engine state: release + activated modules (read-only)."""

    status: ComponentStatusValue
    release: str | None = Field(
        default=None, description="Tryton server release (e.g. '8.2'); null when unreachable"
    )
    database: str = Field(description="Tryton database name (not a connection URL)")
    installed_modules: list[TrytonModuleInfo] = Field(default_factory=list)


class InstallationResponse(BaseModel):
    """Admin-only installation and schema status (spec §7.6)."""

    service: str = "hesabyar"
    version: str = __version__
    environment: str
    database: ComponentStatus
    migration: MigrationStatus
    tryton: TrytonStatus
