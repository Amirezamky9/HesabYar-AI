"""Configuration models and clock abstractions for HesabYar."""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from enum import StrEnum
from functools import lru_cache
from typing import Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hesabyar.domain.common.dates import FiscalDate


class Environment(StrEnum):
    """Runtime execution environment."""

    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production(self) -> bool:
        return self == Environment.PRODUCTION

    @property
    def is_test(self) -> bool:
        return self == Environment.TEST


class DatabaseSettings(BaseModel):
    """PostgreSQL 16+ database configuration."""

    url: str = Field(
        default="postgresql://hesabyar:hesabyar_pass@127.0.0.1:15445/hesabyar_test",
        description="PostgreSQL connection URI with psycopg driver",
    )
    pool_size: int = Field(default=5, ge=1, le=100, description="SQLAlchemy connection pool size")
    max_overflow: int = Field(default=10, ge=0, le=100, description="Maximum overflow connections")
    pool_timeout_seconds: float = Field(
        default=30.0, ge=1.0, description="Timeout waiting for connection from pool"
    )
    connect_timeout_seconds: float = Field(
        default=10.0, ge=1.0, description="Database connection timeout"
    )
    echo_sql: bool = Field(default=False, description="Log executed SQL statements")

    @property
    def sqlalchemy_url(self) -> str:
        """Return SQLAlchemy-compatible connection URL using the psycopg (v3) driver."""
        url = self.url
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://"):]
        return url


class TrytonSettings(BaseModel):
    """Tryton accounting-engine connection settings (REST API).

    Defaults match the local development install documented in
    docs/dev-setup.md §3-§4. The default API key is the dev-only
    application key provisioned in the test database; production
    installations MUST override it via ``HESABYAR_TRYTON__API_KEY``.
    """

    base_url: str = Field(
        default="http://localhost:8000",
        description="Tryton server base URL (no trailing slash)",
    )
    database: str = Field(
        default="hesabyar_tryton_test",
        description="Tryton database name",
    )
    api_key: str = Field(
        default="hesabyar-dev-rest-key",
        description="Tryton user-application key for the REST API (dev default only)",
    )
    timeout_seconds: float = Field(
        # 120s (not 30s): heavy engine actions exceed 30s over a
        # remote DB — a single stock-move do measured 41s and a
        # 14-line inventory confirm 53s against Supabase (US-515,
        # 2026-11-18). With 30s the client timed out even though the
        # server committed, surfacing false errors for operations
        # that had already posted. Overridable per deployment via
        # HESABYAR_TRYTON__TIMEOUT_SECONDS.
        default=120.0,
        ge=1.0,
        description="Timeout for Tryton REST calls",
    )


# Dev-only pepper for API-key HMAC-SHA-256 hashing. It exists so a fresh
# checkout works without secrets; production MUST override it via
# HESABYAR_AUTH__API_KEY_PEPPER (enforced by AppSettings validation).
# Rotating the pepper invalidates every stored key hash, so treat it as a
# long-lived secret, not a per-deploy value.
DEV_API_KEY_PEPPER = "hesabyar-dev-pepper-do-not-use-in-production"


class AuthSettings(BaseModel):
    """API-key authentication settings (spec §7.5)."""

    api_key_pepper: str = Field(
        default=DEV_API_KEY_PEPPER,
        min_length=1,
        description=(
            "Server-side pepper keying the HMAC-SHA-256 hash of API-key "
            "secrets. Dev default only — production must set a long random "
            "value via HESABYAR_AUTH__API_KEY_PEPPER."
        ),
    )
    credentials_encryption_key: str | None = Field(
        default=None,
        description=(
            "Fernet key (URL-safe base64, 32 bytes) encrypting stored "
            "bank/store credentials (US-503). No dev default: when unset, "
            "storing credentials fails closed with a typed error. "
            "Generate with Fernet.generate_key() and set via "
            "HESABYAR_AUTH__CREDENTIALS_ENCRYPTION_KEY."
        ),
    )


class EventPlatformSettings(BaseModel):
    """Event-platform (US-501) tunables.

    Env overrides use the ``HESABYAR_EVENTS__`` prefix, e.g.
    ``HESABYAR_EVENTS__INBOX_STALE_HOURS=12``.
    """

    inbox_stale_hours: int = Field(
        default=24,
        ge=1,
        description="Inbox events stuck in received/processing longer than this raise the staleness alert",
    )
    worker_sweep_cron: str = Field(
        default="* * * * *",
        description="Procrastinate cron for the periodic worker sweep (minute granularity)",
    )
    worker_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Max inbox/outbox events claimed per worker sweep",
    )
    max_attempts: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Max processing attempts before an event is dead-lettered",
    )
    idempotency_ttl_days: int = Field(
        default=30,
        ge=1,
        description="How long a completed idempotency record is kept for replays",
    )


class PackagingSettings(BaseModel):
    """Packaging & shipping (US-515) tunables.

    Env overrides use the ``HESABYAR_PACKAGING__`` prefix, e.g.
    ``HESABYAR_PACKAGING__SUPPLY_CHECK_CRON="0 7 * * *"``.
    """

    supply_check_cron: str = Field(
        default="30 6 * * *",
        description=(
            "Procrastinate cron for the daily supply check (reads the Tryton "
            "supply-wizard results and files packaging_reorder items)"
        ),
    )


class SettlementSettings(BaseModel):
    """Settlement engine (US-521) tunables.

    Env overrides use the ``HESABYAR_SETTLEMENT__`` prefix, e.g.
    ``HESABYAR_SETTLEMENT__TICK_CRON="0 7 * * *"``.
    """

    tick_cron: str = Field(
        default="0 6 * * *",
        description=(
            "Procrastinate cron for the daily settlement tick (W1: expected→due→overdue "
            "transitions + late-tranche escalation, every morning)"
        ),
    )


class FilestoreSettings(BaseModel):
    """Local filestore for uploaded/downloaded files (US-533).

    Cheque leaf scans downloaded from a site ``scan_url`` land under
    ``<directory>/cheques/``. Env override:
    ``HESABYAR_FILESTORE__DIRECTORY``. The default is relative to
    the process working directory (the repo root in dev); production
    installations MUST set an absolute path outside the checkout.
    """

    directory: str = Field(
        default="filestore",
        description="Directory holding downloaded files (cheque leaf scans)",
    )


class AppSettings(BaseSettings):
    """Application-level configuration loaded from environment variables."""

    env: Environment = Field(default=Environment.DEV, description="Application environment")
    database: DatabaseSettings = Field(
        default_factory=DatabaseSettings, description="Database settings"
    )
    tryton: TrytonSettings = Field(
        default_factory=TrytonSettings, description="Tryton engine connection settings"
    )
    auth: AuthSettings = Field(
        default_factory=AuthSettings, description="API-key authentication settings"
    )
    events: EventPlatformSettings = Field(
        default_factory=EventPlatformSettings, description="Event-platform settings"
    )
    packaging: PackagingSettings = Field(
        default_factory=PackagingSettings, description="Packaging & shipping settings"
    )
    settlement: SettlementSettings = Field(
        default_factory=SettlementSettings, description="Settlement engine settings"
    )
    filestore: FilestoreSettings = Field(
        default_factory=FilestoreSettings, description="Local filestore settings"
    )
    database_url: str | None = Field(
        default=None,
        validation_alias="HESABYAR_DATABASE_URL",
        description="Direct override for database URL via HESABYAR_DATABASE_URL",
    )

    model_config = SettingsConfigDict(
        env_prefix="HESABYAR_",
        env_nested_delimiter="__",
        extra="ignore",
        # Fields with a validation_alias (database_url) must also be
        # settable by field name, e.g. AppSettings(database_url="…").
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def _apply_database_url_override(self) -> Self:
        if self.database_url:
            self.database.url = self.database_url
        return self

    @model_validator(mode="after")
    def _reject_dev_pepper_in_production(self) -> Self:
        if self.env.is_production and self.auth.api_key_pepper == DEV_API_KEY_PEPPER:
            raise ValueError(
                "HESABYAR_AUTH__API_KEY_PEPPER must be set to a random value "
                "in production; the dev default is not allowed."
            )
        return self


@lru_cache
def get_settings() -> AppSettings:
    """Return cached application-level settings loaded from the environment."""
    return AppSettings()


# ==============================================================================
# Clock Abstraction
# ==============================================================================


class Clock(ABC):
    """Abstract clock provider decoupling domain and application logic from the system clock."""

    @abstractmethod
    def now_utc(self) -> datetime.datetime:
        """Return the current timezone-aware UTC datetime."""
        ...

    def today_gregorian(self) -> datetime.date:
        """Return today's canonical Gregorian date."""
        return self.now_utc().date()

    def today_fiscal(self) -> FiscalDate:
        """Return today's date as a FiscalDate."""
        return FiscalDate.from_gregorian(self.today_gregorian())


class SystemClock(Clock):
    """Live system clock reading current UTC time."""

    def now_utc(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC)


class FixedClock(Clock):
    """Deterministic, immutable or step-controllable clock for tests and historical replay."""

    __slots__ = ("_current_time",)

    def __init__(self, fixed_point: datetime.datetime | datetime.date | FiscalDate) -> None:
        if isinstance(fixed_point, FiscalDate):
            self._current_time = datetime.datetime.combine(
                fixed_point.gregorian_date, datetime.time.min, tzinfo=datetime.UTC
            )
        elif isinstance(fixed_point, datetime.datetime):
            if fixed_point.tzinfo is None:
                self._current_time = fixed_point.replace(tzinfo=datetime.UTC)
            else:
                self._current_time = fixed_point.astimezone(datetime.UTC)
        elif isinstance(fixed_point, datetime.date):
            self._current_time = datetime.datetime.combine(
                fixed_point, datetime.time.min, tzinfo=datetime.UTC
            )
        else:
            raise TypeError(
                f"FixedClock requires datetime, date, or FiscalDate, got {type(fixed_point).__name__}"
            )

    def now_utc(self) -> datetime.datetime:
        return self._current_time

    def advance(self, delta: datetime.timedelta) -> Self:
        """Advance the fixed clock by delta."""
        self._current_time += delta
        return self

    def set_time(self, new_time: datetime.datetime | datetime.date | FiscalDate) -> None:
        """Explicitly reset the fixed clock point."""
        if isinstance(new_time, FiscalDate):
            self._current_time = datetime.datetime.combine(
                new_time.gregorian_date, datetime.time.min, tzinfo=datetime.UTC
            )
        elif isinstance(new_time, datetime.datetime):
            if new_time.tzinfo is None:
                self._current_time = new_time.replace(tzinfo=datetime.UTC)
            else:
                self._current_time = new_time.astimezone(datetime.UTC)
        elif isinstance(new_time, datetime.date):
            self._current_time = datetime.datetime.combine(
                new_time, datetime.time.min, tzinfo=datetime.UTC
            )
        else:
            raise TypeError(f"Invalid time type: {type(new_time).__name__}")
