"""Unit tests for the backup domain models (US-703).

Covers: manifest JSON round-trip + fail-closed parsing, object-key
layout, the retention policy (daily window + Jalali-monthly keeps),
and the drill/backup report verdicts. No I/O anywhere.
"""

from __future__ import annotations

import datetime
import json

import pytest

from hesabyar.domain.backup.errors import BackupError
from hesabyar.domain.backup.models import (
    MANIFEST_VERSION,
    BackupManifest,
    BackupRunReport,
    BackupTarget,
    DrillCheckResult,
    DrillCheckStatus,
    DrillReport,
    RetentionPolicy,
    manifest_key_for,
    object_key_for,
)
from hesabyar.domain.common.dates import FiscalDate

NOW = datetime.datetime(2026, 11, 21, 22, 30, tzinfo=datetime.UTC)


def _manifest(
    *,
    target: BackupTarget = BackupTarget.HESABYAR_DB,
    created_at: datetime.datetime = NOW,
    object_key: str = "hesabyar-backups/hesabyar/2026-11-21/223000-hesabyar.dump.enc",
) -> BackupManifest:
    return BackupManifest(
        target=target,
        object_key=object_key,
        database="hesabyar_test",
        created_at=created_at,
        sha256="a" * 64,
        plaintext_sha256="b" * 64,
        size_bytes=1234,
        plaintext_size_bytes=1200,
        encryption_key_id="a1b2c3d4",
        retention_expires_at=created_at + datetime.timedelta(days=30),
        pg_dump_version="16.4",
    )


# ---------------------------------------------------------------------------
# Object keys + manifests
# ---------------------------------------------------------------------------


def test_object_key_layout_is_date_partitioned() -> None:
    assert (
        object_key_for("hesabyar-backups", BackupTarget.HESABYAR_DB, NOW)
        == "hesabyar-backups/hesabyar/2026-11-21/223000-hesabyar.dump.enc"
    )
    assert (
        object_key_for("prefix", BackupTarget.TRYTON_DB, NOW)
        == "prefix/tryton/2026-11-21/223000-tryton.dump.enc"
    )
    assert (
        manifest_key_for("prefix/tryton/2026-11-21/223000-tryton.dump.enc")
        == "prefix/tryton/2026-11-21/223000-tryton.dump.enc.manifest.json"
    )


def test_manifest_json_round_trip() -> None:
    manifest = _manifest()
    restored = BackupManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert restored == manifest
    assert restored.to_dict()["version"] == MANIFEST_VERSION


def test_manifest_rejects_unknown_version() -> None:
    data = _manifest().to_dict()
    data["version"] = 99
    with pytest.raises(BackupError, match="Unsupported backup manifest version"):
        BackupManifest.from_dict(data)


def test_manifest_rejects_bad_target_and_naive_timestamps() -> None:
    data = _manifest().to_dict()
    data["target"] = "postgres"
    with pytest.raises(BackupError):
        BackupManifest.from_dict(data)

    data = _manifest().to_dict()
    data["created_at"] = "2026-11-21T22:30:00"  # naive — no timezone
    with pytest.raises(BackupError, match="must carry a timezone"):
        BackupManifest.from_dict(data)


def test_manifest_rejects_missing_and_negative_fields() -> None:
    data = _manifest().to_dict()
    del data["sha256"]
    with pytest.raises(BackupError):
        BackupManifest.from_dict(data)

    data = _manifest().to_dict()
    data["size_bytes"] = -1
    with pytest.raises(BackupError, match="must be >= 0"):
        BackupManifest.from_dict(data)

    with pytest.raises(BackupError, match="must be an object"):
        BackupManifest.from_dict("not-a-dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Retention policy
# ---------------------------------------------------------------------------


def _months_back(year: int, month: int, steps: int) -> tuple[int, int]:
    """The Jalali (year, month) ``steps`` months before (year, month)."""
    total = year * 12 + (month - 1) - steps
    return total // 12, total % 12 + 1


def test_retention_keeps_recent_backups() -> None:
    policy = RetentionPolicy(daily_retention_days=30, monthly_keep_count=12)
    recent = [
        _manifest(created_at=NOW - datetime.timedelta(days=days), object_key=f"recent-{days}")
        for days in (0, 1, 29)
    ]
    assert policy.select_expired(recent, now=NOW) == []


def test_retention_expires_everything_old_when_no_monthly_keep() -> None:
    policy = RetentionPolicy(daily_retention_days=30, monthly_keep_count=0)
    old = [
        _manifest(created_at=NOW - datetime.timedelta(days=days), object_key=f"old-{days}")
        for days in (31, 60, 400)
    ]
    assert policy.select_expired(old, now=NOW) == sorted(old, key=lambda m: m.object_key)


def test_retention_keeps_newest_per_month_for_the_newest_months() -> None:
    policy = RetentionPolicy(daily_retention_days=30, monthly_keep_count=12)
    fiscal = FiscalDate(NOW.date())
    # One backup (day 15) per Jalali month, months M-2 .. M-15: all
    # older than 30 days, 14 distinct months.
    manifests: list[BackupManifest] = []
    for steps in range(2, 16):
        year, month = _months_back(fiscal.jalali_year, fiscal.jalali_month, steps)
        day = FiscalDate.from_jalali(year, month, 15).gregorian_date
        manifests.append(
            _manifest(
                created_at=datetime.datetime.combine(day, datetime.time(2, 0), tzinfo=datetime.UTC),
                object_key=f"month-{year}-{month:02d}",
            )
        )
    expired = policy.select_expired(manifests, now=NOW)
    # 14 months, 12 kept -> the 2 OLDEST months expire.
    assert len(expired) == 2
    assert {m.object_key for m in expired} == {
        f"month-{y}-{m:02d}"
        for y, m in (
            _months_back(fiscal.jalali_year, fiscal.jalali_month, 15),
            _months_back(fiscal.jalali_year, fiscal.jalali_month, 14),
        )
    }


def test_retention_keeps_only_the_newest_backup_within_a_month() -> None:
    policy = RetentionPolicy(daily_retention_days=30, monthly_keep_count=12)
    fiscal = FiscalDate((NOW - datetime.timedelta(days=60)).date())
    year, month = fiscal.jalali_year, fiscal.jalali_month
    day1 = FiscalDate.from_jalali(year, month, 1).gregorian_date
    day2 = FiscalDate.from_jalali(year, month, 2).gregorian_date
    older = _manifest(
        created_at=datetime.datetime.combine(day1, datetime.time(2, 0), tzinfo=datetime.UTC),
        object_key="same-month-older",
    )
    newer = _manifest(
        created_at=datetime.datetime.combine(day2, datetime.time(2, 0), tzinfo=datetime.UTC),
        object_key="same-month-newer",
    )
    expired = policy.select_expired([older, newer], now=NOW)
    assert expired == [older]


# ---------------------------------------------------------------------------
# Drill + run reports
# ---------------------------------------------------------------------------


def _report(*statuses: DrillCheckStatus) -> DrillReport:
    return DrillReport(
        started_at=NOW,
        finished_at=NOW + datetime.timedelta(seconds=90),
        checks=tuple(
            DrillCheckResult(name=f"check-{i}", status=status)
            for i, status in enumerate(statuses)
        ),
    )


def test_drill_report_verdicts() -> None:
    assert _report(DrillCheckStatus.PASS, DrillCheckStatus.PASS).passed is True
    assert _report(DrillCheckStatus.PASS, DrillCheckStatus.PASS).fully_verified is True
    skipped = _report(DrillCheckStatus.PASS, DrillCheckStatus.SKIPPED)
    assert skipped.passed is True  # nothing failed
    assert skipped.fully_verified is False  # ...but not fully verified
    failed = _report(DrillCheckStatus.PASS, DrillCheckStatus.FAIL)
    assert failed.passed is False
    assert failed.fully_verified is False
    assert [c.name for c in failed.failed_checks()] == ["check-1"]
    assert [c.name for c in skipped.skipped_checks()] == ["check-1"]


def test_drill_report_summary_is_json_safe() -> None:
    report = _report(DrillCheckStatus.PASS, DrillCheckStatus.FAIL)
    summary = report.summary()
    json.dumps(summary)  # must not raise
    assert summary["passed"] is False
    assert summary["failed"] == ["check-1"]
    assert summary["duration_seconds"] == 90.0


def test_backup_run_report_summary() -> None:
    manifest = _manifest()
    ok = BackupRunReport(manifests={BackupTarget.HESABYAR_DB: manifest})
    assert ok.ok is True
    summary = ok.summary()
    json.dumps(summary)
    assert summary["backups"]["hesabyar"]["object_key"] == manifest.object_key
    failed = BackupRunReport(
        manifests={BackupTarget.HESABYAR_DB: manifest},
        failures={BackupTarget.TRYTON_DB: "pg_dump exploded"},
    )
    assert failed.ok is False
    assert failed.summary()["failures"] == {"tryton": "pg_dump exploded"}
