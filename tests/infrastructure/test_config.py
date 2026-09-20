"""Tests for infrastructure configuration and clock abstraction."""

import datetime

import pytest

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.config import (
    AppSettings,
    DatabaseSettings,
    Environment,
    FixedClock,
    SystemClock,
    get_settings,
)


class TestConfig:
    def test_default_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HESABYAR_DATABASE_URL", raising=False)
        monkeypatch.delenv("HESABYAR_DATABASE__URL", raising=False)
        settings = AppSettings()
        assert settings.env == Environment.DEV
        assert not settings.env.is_production
        assert "hesabyar" in settings.database.url
        assert settings.database.pool_size == 5
        assert isinstance(settings.database, DatabaseSettings)
        assert "postgresql+psycopg://" in settings.database.sqlalchemy_url

    def test_environment_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HESABYAR_DATABASE_URL", raising=False)
        monkeypatch.setenv("HESABYAR_ENV", "production")
        monkeypatch.setenv("HESABYAR_DATABASE__POOL_SIZE", "20")
        monkeypatch.setenv("HESABYAR_DATABASE__URL", "postgresql://user:pass@db:5432/prod")

        settings = AppSettings()
        assert settings.env == Environment.PRODUCTION
        assert settings.env.is_production
        assert settings.database.pool_size == 20
        assert settings.database.url == "postgresql://user:pass@db:5432/prod"
        assert settings.database.sqlalchemy_url == "postgresql+psycopg://user:pass@db:5432/prod"

    def test_direct_database_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "HESABYAR_DATABASE_URL",
            "postgresql+psycopg://custom:secret@db.internal:5432/hesabyar_custom",
        )
        # Clear cache for get_settings test
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.database.url == "postgresql+psycopg://custom:secret@db.internal:5432/hesabyar_custom"
        get_settings.cache_clear()


class TestClock:
    def test_system_clock(self) -> None:
        clock = SystemClock()
        now = clock.now_utc()
        assert now.tzinfo is not None
        today = clock.today_fiscal()
        assert isinstance(today, FiscalDate)
        assert today.gregorian_date == now.date()

    def test_fixed_clock(self) -> None:
        fixed_date = datetime.date(2026, 4, 4)
        clock = FixedClock(fixed_date)

        assert clock.today_gregorian() == fixed_date
        assert clock.today_fiscal() == FiscalDate.from_gregorian(fixed_date)
        assert clock.today_fiscal().jalali_year == 1405
        assert clock.today_fiscal().jalali_month == 1
        assert clock.today_fiscal().jalali_day == 15

        # Advance by 5 days
        clock.advance(datetime.timedelta(days=5))
        assert clock.today_gregorian() == datetime.date(2026, 4, 9)
        assert clock.today_fiscal().jalali_day == 20
