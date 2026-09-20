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


class AppSettings(BaseSettings):
    """Application-level configuration loaded from environment variables."""

    env: Environment = Field(default=Environment.DEV, description="Application environment")
    database: DatabaseSettings = Field(
        default_factory=DatabaseSettings, description="Database settings"
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
    )

    @model_validator(mode="after")
    def _apply_database_url_override(self) -> Self:
        if self.database_url:
            self.database.url = self.database_url
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
