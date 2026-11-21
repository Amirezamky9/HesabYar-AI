"""Tests for infrastructure configuration and clock abstraction."""

import datetime

import pytest
from pydantic import ValidationError

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.config import (
    DEV_API_KEY_PEPPER,
    DEV_DATABASE_URL,
    DEV_TRYTON_API_KEY,
    AppSettings,
    BackupSettings,
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
        monkeypatch.delenv("HESABYAR_AUTH__API_KEY_PEPPER", raising=False)
        settings = AppSettings()
        assert settings.env == Environment.DEV
        assert not settings.env.is_production
        assert "hesabyar" in settings.database.url
        assert settings.database.pool_size == 5
        assert isinstance(settings.database, DatabaseSettings)
        assert "postgresql+psycopg://" in settings.database.sqlalchemy_url
        assert settings.auth.api_key_pepper == DEV_API_KEY_PEPPER

    def test_environment_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HESABYAR_DATABASE_URL", raising=False)
        monkeypatch.setenv("HESABYAR_ENV", "production")
        monkeypatch.setenv("HESABYAR_DATABASE__POOL_SIZE", "20")
        monkeypatch.setenv("HESABYAR_DATABASE__URL", "postgresql://user:pass@db:5432/prod")
        monkeypatch.setenv("HESABYAR_AUTH__API_KEY_PEPPER", "production-pepper-value")
        # US-701: production also refuses the dev Tryton key, so the
        # override must set it too.
        monkeypatch.setenv("HESABYAR_TRYTON__API_KEY", "production-tryton-key")

        settings = AppSettings()
        assert settings.env == Environment.PRODUCTION
        assert settings.env.is_production
        assert settings.database.pool_size == 20
        assert settings.database.url == "postgresql://user:pass@db:5432/prod"
        assert settings.database.sqlalchemy_url == "postgresql+psycopg://user:pass@db:5432/prod"
        assert settings.auth.api_key_pepper == "production-pepper-value"

    def test_production_refuses_dev_pepper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HESABYAR_DATABASE_URL", raising=False)
        monkeypatch.delenv("HESABYAR_AUTH__API_KEY_PEPPER", raising=False)
        monkeypatch.setenv("HESABYAR_ENV", "production")
        with pytest.raises(ValidationError, match="API_KEY_PEPPER"):
            AppSettings()

    # -- US-701: the production guard covers every dev default --------

    def test_production_refuses_dev_tryton_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HESABYAR_DATABASE_URL", raising=False)
        monkeypatch.setenv("HESABYAR_ENV", "production")
        monkeypatch.setenv("HESABYAR_AUTH__API_KEY_PEPPER", "production-pepper-value")
        monkeypatch.setenv("HESABYAR_DATABASE__URL", "postgresql://user:pass@db:5432/prod")
        monkeypatch.delenv("HESABYAR_TRYTON__API_KEY", raising=False)
        with pytest.raises(ValidationError, match="HESABYAR_TRYTON__API_KEY"):
            AppSettings()

    def test_production_refuses_dev_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HESABYAR_DATABASE_URL", raising=False)
        monkeypatch.setenv("HESABYAR_ENV", "production")
        monkeypatch.setenv("HESABYAR_AUTH__API_KEY_PEPPER", "production-pepper-value")
        monkeypatch.setenv("HESABYAR_TRYTON__API_KEY", "production-tryton-key")
        monkeypatch.delenv("HESABYAR_DATABASE__URL", raising=False)
        with pytest.raises(ValidationError, match="HESABYAR_DATABASE__URL"):
            AppSettings()

    def test_dev_defaults_are_the_named_constants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The dev defaults are the values the guard compares against."""
        monkeypatch.delenv("HESABYAR_DATABASE_URL", raising=False)
        monkeypatch.delenv("HESABYAR_DATABASE__URL", raising=False)
        monkeypatch.delenv("HESABYAR_AUTH__API_KEY_PEPPER", raising=False)
        monkeypatch.delenv("HESABYAR_TRYTON__API_KEY", raising=False)
        settings = AppSettings()
        assert settings.auth.api_key_pepper == DEV_API_KEY_PEPPER
        assert settings.tryton.api_key == DEV_TRYTON_API_KEY
        assert settings.database.url == DEV_DATABASE_URL

    def test_webhook_replay_window_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The webhook replay window defaults to 7 days (168h)."""
        monkeypatch.delenv("HESABYAR_EVENTS__WEBHOOK_MAX_EVENT_AGE_HOURS", raising=False)
        settings = AppSettings()
        assert settings.events.webhook_max_event_age_hours == 168

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


class TestBackupSettings:
    """US-703: the S3 destination is unverified — no provider, endpoint
    or region is ever defaulted; exactly one backend may be set."""

    def test_backup_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "HESABYAR_BACKUP__ENDPOINT_URL",
            "HESABYAR_BACKUP__LOCAL_DIRECTORY",
            "HESABYAR_BACKUP__RETENTION_DAYS",
            "HESABYAR_BACKUP__ENCRYPTION_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        settings = AppSettings()
        backup = settings.backup
        assert isinstance(backup, BackupSettings)
        assert backup.endpoint_url is None
        assert backup.bucket is None
        assert backup.region is None
        assert backup.access_key_id is None
        assert backup.secret_access_key is None
        assert backup.local_directory is None
        assert backup.encryption_key is None
        assert backup.key_prefix == "hesabyar-backups"
        assert backup.retention_days == 30
        assert backup.monthly_keep_count == 12
        assert backup.backup_cron == "30 22 * * *"
        assert backup.restore_drill_cron == "30 23 * * *"
        assert backup.tryton_database_url is None
        assert backup.max_dump_bytes == 512 * 1024 * 1024

    def test_backup_environment_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HESABYAR_BACKUP__LOCAL_DIRECTORY", "/var/backups/hesabyar")
        monkeypatch.setenv("HESABYAR_BACKUP__RETENTION_DAYS", "7")
        monkeypatch.setenv("HESABYAR_BACKUP__MONTHLY_KEEP_COUNT", "6")
        monkeypatch.setenv("HESABYAR_BACKUP__KEY_PREFIX", "store-1")
        settings = AppSettings()
        assert settings.backup.local_directory == "/var/backups/hesabyar"
        assert settings.backup.retention_days == 7
        assert settings.backup.monthly_keep_count == 6
        assert settings.backup.key_prefix == "store-1"

    def test_backup_rejects_both_backends(self) -> None:
        with pytest.raises(ValidationError, match="exactly one backup backend"):
            BackupSettings(
                endpoint_url="https://s3.example.com", local_directory="/tmp/backups"
            )

    def test_backup_rejects_partial_s3(self) -> None:
        with pytest.raises(ValidationError, match="missing: bucket, region"):
            BackupSettings(endpoint_url="https://s3.example.com")
        with pytest.raises(
            ValidationError, match="missing: access_key_id, secret_access_key"
        ):
            BackupSettings(
                endpoint_url="https://s3.example.com",
                bucket="hesabyar-backups",
                region="us-east-1",
            )

    def test_backup_accepts_a_complete_s3_config(self) -> None:
        settings = BackupSettings(
            endpoint_url="https://s3.example.com",
            bucket="hesabyar-backups",
            region="us-east-1",
            access_key_id="AKIAEXAMPLE",
            secret_access_key="secret",
        )
        assert settings.endpoint_url == "https://s3.example.com"
        assert settings.bucket == "hesabyar-backups"

    def test_backup_accepts_a_local_directory(self) -> None:
        settings = BackupSettings(local_directory="/var/backups/hesabyar")
        assert settings.local_directory == "/var/backups/hesabyar"


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
