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


# Dev-only database URL for the local test install. It carries a
# well-known password; production MUST override it (enforced by
# AppSettings validation, US-701).
DEV_DATABASE_URL = "postgresql://hesabyar:hesabyar_pass@127.0.0.1:15445/hesabyar_test"


class DatabaseSettings(BaseModel):
    """PostgreSQL 16+ database configuration."""

    url: str = Field(
        default=DEV_DATABASE_URL,
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


# Dev-only Tryton application key for the local test database. It is
# a well-known value (documented in docs/dev-setup.md); production
# MUST override it (enforced by AppSettings validation, US-701).
DEV_TRYTON_API_KEY = "hesabyar-dev-rest-key"


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
        default=DEV_TRYTON_API_KEY,
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
    webhook_max_event_age_hours: int = Field(
        default=168,
        ge=0,
        description=(
            "Webhook replay window (US-701): a NEW webhook event whose "
            "occurred_at is older than this is rejected (422 EVENT_TOO_OLD) "
            "— a captured signed payload for an event that was never "
            "delivered cannot be replayed arbitrarily late. Re-deliveries "
            "of already-stored events still replay as duplicates, and the "
            "polling/sync path (ingest_verified) never applies this check "
            "— backfills are legitimately old. 0 disables the check."
        ),
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


class SchedulerSettings(BaseModel):
    """Scheduler + maintenance jobs (US-560) tunables.

    Env overrides use the ``HESABYAR_SCHEDULER__`` prefix, e.g.
    ``HESABYAR_SCHEDULER__STORE_SYNC_CRON="*/30 * * * *"``. Crons are
    UTC (Procrastinate periodic tasks are UTC); the registry's
    ``time_of_day`` values are Tehran wall time (UTC+3:30, no DST).
    """

    dunning_sweep_cron: str = Field(
        default="30 4 * * *",
        description=(
            "Procrastinate cron for the daily dunning sweep "
            "(08:00 Tehran; W1 puts the US-541 sweep on schedule)"
        ),
    )
    store_sync_cron: str = Field(
        default="*/15 * * * *",
        description=(
            "Procrastinate cron for reconciliation polling "
            "(every 15 minutes; W1 puts the US-512/US-533 sync on schedule)"
        ),
    )
    staleness_cron: str = Field(
        default="0 * * * *",
        description=(
            "Procrastinate cron for the inbox staleness alert (hourly; "
            "W1 puts the US-501 staleness check on schedule)"
        ),
    )
    recurring_generation_cron: str = Field(
        default="0 4 * * *",
        description=(
            "Procrastinate cron for the recurring generator "
            "(daily 07:30 Tehran; W3 puts the US-563 generation on schedule)"
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


class BackupSettings(BaseModel):
    """S3 backups + restore drills (US-703, spec §15.4 / §17 phase 7).

    Env overrides use the ``HESABYAR_BACKUP__`` prefix, e.g.
    ``HESABYAR_BACKUP__ENDPOINT_URL``.

    The S3 destination decision is UNVERIFIED (phased design §1.3
    phase 7: Iran-hosted S3-compatible vs S3 with non-Iranian
    egress): NOTHING here names a provider or defaults an endpoint,
    region or bucket — the operator configures all of them, and the
    backup job fails closed (recorded failure + review item) until
    they do. ``local_directory`` is the dev/CI backend (a plain
    directory on disk); exactly one backend may be configured.

    ``tryton_database_url`` has NO default and cannot be derived:
    HesabYar otherwise never knows the Tryton database's DSN (it
    talks REST). The operator supplies it — it is the backup
    procedure's DSN, used ONLY by pg_dump/pg_restore in the
    operations layer, never by application code (see
    docs/backup-restore.md for the §19.2 operational exception).
    """

    endpoint_url: str | None = Field(
        default=None,
        description=(
            "S3-compatible endpoint URL (e.g. https://s3.example.com), "
            "no trailing slash. No default — the destination decision "
            "is unverified; the operator configures it."
        ),
    )
    bucket: str | None = Field(
        default=None,
        description="S3-compatible bucket name (required with endpoint_url).",
    )
    region: str | None = Field(
        default=None,
        description=(
            "S3 signing region (required with endpoint_url; SigV4 needs "
            "one — no region is guessed)."
        ),
    )
    access_key_id: str | None = Field(
        default=None,
        description="S3 access key id (required with endpoint_url).",
    )
    secret_access_key: str | None = Field(
        default=None,
        description="S3 secret access key (required with endpoint_url; never logged).",
    )
    local_directory: str | None = Field(
        default=None,
        description=(
            "Local directory backend (dev/CI): backups are files under "
            "this directory. Mutually exclusive with endpoint_url."
        ),
    )
    key_prefix: str = Field(
        default="hesabyar-backups",
        description=(
            "Object-key prefix (one per installation — spec §15.1: "
            "independent backup prefix per store)."
        ),
    )
    encryption_key: str | None = Field(
        default=None,
        description=(
            "Fernet key (URL-safe base64, 32 bytes) encrypting backup "
            "archives at rest. No dev default: when unset, backups fail "
            "closed. Generate with Fernet.generate_key() and set via "
            "HESABYAR_BACKUP__ENCRYPTION_KEY. Rotation is a manual "
            "procedure (docs/backup-restore.md) — no tooling, the same "
            "as the credential vault (threat model A6)."
        ),
    )
    hesabyar_database_url: str | None = Field(
        default=None,
        description=(
            "PostgreSQL DSN pg_dump uses for the HesabYar database. "
            "Defaults to the application's database URL when unset."
        ),
    )
    tryton_database_url: str | None = Field(
        default=None,
        description=(
            "PostgreSQL DSN pg_dump/pg_restore use for the Tryton "
            "database. No default — the operator supplies it. Used "
            "ONLY by the backup/restore operations layer."
        ),
    )
    hesabyar_admin_database_url: str | None = Field(
        default=None,
        description=(
            "Maintenance-DB DSN (a role with CREATEDB) the restore "
            "drill uses to create/drop HesabYar scratch databases. "
            "Defaults to hesabyar_database_url with the database "
            "name replaced by 'postgres'."
        ),
    )
    tryton_admin_database_url: str | None = Field(
        default=None,
        description=(
            "Maintenance-DB DSN (a role with CREATEDB) the restore "
            "drill uses to create/drop Tryton scratch databases. "
            "Defaults to tryton_database_url with the database "
            "name replaced by 'postgres'."
        ),
    )
    retention_days: int = Field(
        default=30,
        ge=1,
        description="Daily backups are kept at least this long.",
    )
    monthly_keep_count: int = Field(
        default=12,
        ge=0,
        description=(
            "How many monthly restore points (newest backup per Jalali "
            "month) pruning keeps past the daily window. 0 keeps none."
        ),
    )
    backup_cron: str = Field(
        default="30 22 * * *",
        description=(
            "Procrastinate cron for the daily backup job "
            "(02:00 Tehran; backups run on Friday too)."
        ),
    )
    restore_drill_cron: str = Field(
        default="30 23 * * *",
        description=(
            "Procrastinate cron for the restore drill (03:00 Tehran, "
            "fired daily — the registry's Jalali-monthly gate runs it "
            "on the 1st of each month)."
        ),
    )
    pg_dump_path: str = Field(
        default="pg_dump",
        description="pg_dump binary (override when it is not on PATH).",
    )
    pg_restore_path: str = Field(
        default="pg_restore",
        description="pg_restore binary (override when it is not on PATH).",
    )
    command_timeout_seconds: int = Field(
        default=1800,
        ge=60,
        description="Timeout for one pg_dump / pg_restore run.",
    )
    max_dump_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        description=(
            "Fail-closed size guard: a dump larger than this is refused "
            "rather than held in memory for Fernet encryption (Fernet "
            "encrypts whole buffers; streaming encryption is future work)."
        ),
    )
    max_external_refs_checked: int = Field(
        default=200,
        ge=1,
        description=(
            "How many external-id-map rows the drill's cross-database "
            "orphan check verifies against the scratch engine (bounds "
            "the REST calls; the rest are reported as unchecked)."
        ),
    )

    @model_validator(mode="after")
    def _validate_backend(self) -> Self:
        if self.endpoint_url is not None and self.local_directory is not None:
            raise ValueError(
                "Configure exactly one backup backend: HESABYAR_BACKUP__ENDPOINT_URL "
                "(S3-compatible) or HESABYAR_BACKUP__LOCAL_DIRECTORY (local files), "
                "not both."
            )
        if self.endpoint_url is not None:
            missing = [
                name
                for name, value in (
                    ("bucket", self.bucket),
                    ("region", self.region),
                    ("access_key_id", self.access_key_id),
                    ("secret_access_key", self.secret_access_key),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "HESABYAR_BACKUP__ENDPOINT_URL is set but these S3 settings "
                    f"are missing: {', '.join(missing)}."
                )
        return self


class McpSettings(BaseModel):
    """MCP server settings (US-601, spec §13.2/§13.8).

    Env overrides use the ``HESABYAR_MCP__`` prefix, e.g.
    ``HESABYAR_MCP__PORT=8002``. List values use JSON syntax, e.g.
    ``HESABYAR_MCP__ALLOWED_HOSTS='["mcp.example.com:443"]'``.

    ``public_url`` is the server's own public Streamable HTTP URL: it is
    the RFC 8707 resource/audience the API-key token verifier binds
    tokens to (``AccessToken.resource``) and the issuer URL for the
    self-issued API-key credentials. It MUST match the URL clients use
    to reach the server, or token verification rejects every request.
    """

    host: str = Field(
        default="127.0.0.1",
        description="Bind address for the Streamable HTTP transport",
    )
    port: int = Field(
        default=8002,
        ge=1,
        le=65535,
        description="Port for the Streamable HTTP transport (8000 is Tryton, 8001 is REST)",
    )
    path: str = Field(
        default="/mcp",
        description="Streamable HTTP endpoint path",
    )
    public_url: str = Field(
        default="http://127.0.0.1:8002/mcp",
        description=(
            "Public Streamable HTTP URL of this server (resource/audience "
            "for token binding). Override in production with the real URL."
        ),
    )
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:8002", "localhost:8002"],
        description=(
            "Host header values accepted by the DNS-rebinding protection "
            "(SDK TransportSecuritySettings). Production must list the real host."
        ),
    )
    allowed_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Origin header values accepted by the DNS-rebinding protection. "
            "An absent Origin is always allowed (non-browser clients); a "
            "present Origin must be listed here."
        ),
    )
    max_request_body_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1024,
        description="Maximum Streamable HTTP request body size in bytes (SDK default 4 MiB)",
    )
    rate_limit_per_minute: int = Field(
        default=120,
        ge=1,
        description="Streamable HTTP requests allowed per API-key prefix per minute",
    )
    session_idle_timeout_seconds: float = Field(
        default=1800.0,
        ge=60.0,
        description="Streamable HTTP session idle timeout in seconds (SDK default 1800)",
    )
    max_sessions: int = Field(
        default=10000,
        ge=1,
        description="Maximum concurrent Streamable HTTP sessions (SDK default 10000)",
    )
    stdio_api_key: str | None = Field(
        default=None,
        description=(
            "API key the stdio transport authenticates once at startup "
            "(env HESABYAR_MCP__STDIO_API_KEY). The stdio process has no "
            "per-request auth, so it runs as this one key's grant. Never "
            "log or print this value."
        ),
    )
    tls_certfile: str | None = Field(
        default=None,
        description="TLS certificate file for Streamable HTTP (production: serve behind TLS)",
    )
    tls_keyfile: str | None = Field(
        default=None,
        description="TLS private-key file for Streamable HTTP",
    )


class MonitoringSettings(BaseModel):
    """Monitoring + alerting (US-704) tunables.

    Env overrides use the ``HESABYAR_MONITORING__`` prefix, e.g.
    ``HESABYAR_MONITORING__TOKEN_WARNING_DAYS=7``. The inbox-stale
    threshold is NOT duplicated here: the rules read
    ``events.inbox_stale_hours`` (one source of truth, shared with
    the US-501 staleness job).
    """

    worker_lag_minutes: int = Field(
        default=10,
        ge=1,
        description=(
            "A worker whose last recorded sweep is older than this "
            "raises the worker-lag alert (the sweep runs every minute)"
        ),
    )
    token_warning_days: int = Field(
        default=14,
        ge=1,
        description="Tokens expiring within this many days raise a warning alert",
    )
    token_critical_days: int = Field(
        default=3,
        ge=0,
        description="Tokens expiring within this many days raise a critical alert",
    )
    webhook_failures_24h: int = Field(
        default=5,
        ge=1,
        description="Failed webhook attempts in 24h at or above this raise the alert",
    )
    error_rate_5xx: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="5xx rate (server errors / total requests) at or above this alerts",
    )
    error_rate_min_requests: int = Field(
        default=100,
        ge=1,
        description="Minimum requests before the 5xx-rate rule can fire (no alert on 1/2)",
    )
    backup_max_age_hours: int = Field(
        default=26,
        ge=1,
        description=(
            "A database with no successful backup within this long "
            "raises the stale-backup alert (daily backups + 2h slack)"
        ),
    )
    evaluation_cron: str = Field(
        default="*/15 * * * *",
        description="Procrastinate cron for the monitoring evaluation job (UTC)",
    )
    mcp_probe_timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        description="Timeout for the MCP /health probe (same default as the other health checks)",
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
    scheduler: SchedulerSettings = Field(
        default_factory=SchedulerSettings, description="Scheduler + maintenance jobs settings"
    )
    filestore: FilestoreSettings = Field(
        default_factory=FilestoreSettings, description="Local filestore settings"
    )
    mcp: McpSettings = Field(
        default_factory=McpSettings, description="MCP server settings"
    )
    backup: BackupSettings = Field(
        default_factory=BackupSettings, description="Backup + restore-drill settings"
    )
    monitoring: MonitoringSettings = Field(
        default_factory=MonitoringSettings,
        description="Monitoring + alerting settings",
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
    def _reject_dev_defaults_in_production(self) -> Self:
        # US-701: the guard used to cover only the pepper. A production
        # deploy that forgot the Tryton key or the database URL would
        # silently run against the well-known dev credentials (the dev
        # Tryton key is a published value; the dev DB URL carries a
        # well-known password), so all three dev defaults are refused.
        if not self.env.is_production:
            return self
        if self.auth.api_key_pepper == DEV_API_KEY_PEPPER:
            raise ValueError(
                "HESABYAR_AUTH__API_KEY_PEPPER must be set to a random value "
                "in production; the dev default is not allowed."
            )
        if self.tryton.api_key == DEV_TRYTON_API_KEY:
            raise ValueError(
                "HESABYAR_TRYTON__API_KEY must be set to the production "
                "Tryton application key in production; the dev default "
                "is not allowed."
            )
        if self.database.url == DEV_DATABASE_URL:
            raise ValueError(
                "HESABYAR_DATABASE__URL must be set to the production "
                "database in production; the dev default is not allowed."
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
