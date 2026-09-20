"""Unit tests for Iranian payroll, labor, and insurance skill tooling scripts.

Verifies:
- validate_sources.py fails closed on missing/invalid snapshots for VERIFIED sources.
- validate_parameters.py audits both 1404 and 1405 without errors.
- diff_annual_rules.py accurately computes year-over-year deltas.
- check_source_freshness.py correctly evaluates freshness and expiration with --as-of-date.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "iran-payroll-insurance"
SCRIPTS_DIR = SKILL_DIR / "scripts"
REFERENCES_DIR = SKILL_DIR / "references"

# Ensure scripts directory is on sys.path for direct imports
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_source_freshness  # noqa: E402
import diff_annual_rules  # noqa: E402
import validate_parameters  # noqa: E402
import validate_sources  # noqa: E402
from validate_sources import SourceRecord  # noqa: E402
from validate_sources import validate_sources as run_validate_sources  # noqa: E402


class TestValidateSourcesFailClosed:
    """Tests verifying fail-closed invariant in validate_sources.py."""

    def test_registered_source_status_file_passes(self):
        """The canonical source-status.md in the repository must pass validation."""
        records = validate_sources.parse_source_status_table(REFERENCES_DIR / "source-status.md")
        assert len(records) >= 10
        verified, err_count, errors = run_validate_sources(records, base_dir=SKILL_DIR)
        assert err_count == 0, f"Unexpected errors in source-status.md: {errors}"

    def test_verified_source_missing_snapshot_fails_closed(self, tmp_path: Path):
        """A source marked VERIFIED without an existing snapshot file MUST fail closed."""
        rec = SourceRecord(
            source_id="SRC-TEST-FAIL-001",
            authority="تست",
            document_number="سند-۱",
            title="تست سند بدون فایل",
            published_at="1404/01/01",
            effective_from="1404/01/01",
            effective_to="None",
            verification_status="VERIFIED",
            canonical_url="https://example.com/law",
            snapshot_path="snapshots/non_existent.pdf",
            sha256="a" * 64,
            supersedes_superseded_by="None",
            verified_by="Test Reviewer",
            verified_at="2026-01-01T00:00:00Z",
        )
        verified, err_count, errors = run_validate_sources([rec], base_dir=tmp_path)
        assert err_count > 0
        assert any("snapshot file not found on disk" in err for err in errors)

    def test_verified_source_empty_snapshot_fails_closed(self, tmp_path: Path):
        """A source marked VERIFIED with a 0-byte snapshot file MUST fail closed."""
        empty_file = tmp_path / "empty.pdf"
        empty_file.write_bytes(b"")

        rec = SourceRecord(
            source_id="SRC-TEST-EMPTY-001",
            authority="تست",
            document_number="سند-۲",
            title="تست سند خالی",
            published_at="1404/01/01",
            effective_from="1404/01/01",
            effective_to="None",
            verification_status="VERIFIED",
            canonical_url="https://example.com/law",
            snapshot_path="empty.pdf",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            supersedes_superseded_by="None",
            verified_by="Test Reviewer",
            verified_at="2026-01-01T00:00:00Z",
        )
        verified, err_count, errors = run_validate_sources([rec], base_dir=tmp_path)
        assert err_count > 0
        assert any("empty (0 bytes)" in err for err in errors)

    def test_verified_source_hash_mismatch_fails_closed(self, tmp_path: Path):
        """A source marked VERIFIED whose hash does not match the file MUST fail closed."""
        snap_file = tmp_path / "real.pdf"
        snap_file.write_bytes(b"Official Gazette Real Content")
        wrong_hash = "b" * 64

        rec = SourceRecord(
            source_id="SRC-TEST-MISMATCH-001",
            authority="تست",
            document_number="سند-۳",
            title="تست هش نامنطبق",
            published_at="1404/01/01",
            effective_from="1404/01/01",
            effective_to="None",
            verification_status="VERIFIED",
            canonical_url="https://example.com/law",
            snapshot_path="real.pdf",
            sha256=wrong_hash,
            supersedes_superseded_by="None",
            verified_by="Test Reviewer",
            verified_at="2026-01-01T00:00:00Z",
        )
        verified, err_count, errors = run_validate_sources([rec], base_dir=tmp_path)
        assert err_count > 0
        assert any("Hash mismatch" in err for err in errors)

    def test_verified_source_missing_metadata_fails_closed(self, tmp_path: Path):
        """A source marked VERIFIED without verified_by or verified_at MUST fail closed."""
        content = b"Legal Content"
        real_hash = hashlib.sha256(content).hexdigest()
        snap_file = tmp_path / "valid.pdf"
        snap_file.write_bytes(content)

        rec = SourceRecord(
            source_id="SRC-TEST-NOMETA-001",
            authority="تست",
            document_number="سند-۴",
            title="تست بدون متادیتا",
            published_at="1404/01/01",
            effective_from="1404/01/01",
            effective_to="None",
            verification_status="VERIFIED",
            canonical_url="https://example.com/law",
            snapshot_path="valid.pdf",
            sha256=real_hash,
            supersedes_superseded_by="None",
            verified_by="None",
            verified_at="None",
        )
        verified, err_count, errors = run_validate_sources([rec], base_dir=tmp_path)
        assert err_count > 0
        assert any("requires verifier metadata" in err for err in errors)

    def test_verified_source_valid_snapshot_passes(self, tmp_path: Path):
        """A source marked VERIFIED with existing non-empty snapshot, valid hash and metadata passes."""
        content = b"Official Gazette Law 1404 Ratified Text"
        real_hash = hashlib.sha256(content).hexdigest()
        snap_file = tmp_path / "valid.pdf"
        snap_file.write_bytes(content)

        rec = SourceRecord(
            source_id="SRC-TEST-VALID-001",
            authority="تست",
            document_number="سند-۵",
            title="تست معتبر با سند",
            published_at="1404/01/01",
            effective_from="1404/01/01",
            effective_to="None",
            verification_status="VERIFIED",
            canonical_url="https://example.com/law",
            snapshot_path="valid.pdf",
            sha256=real_hash,
            supersedes_superseded_by="None",
            verified_by="HesabYar Legal Committee",
            verified_at="2026-01-01T00:00:00Z",
        )
        verified, err_count, errors = run_validate_sources([rec], base_dir=tmp_path)
        assert err_count == 0
        assert verified == 1


class TestValidateParameters:
    """Tests for validate_parameters.py on annual definitions."""

    def test_validate_1404_parameters(self):
        """annual-1404.yaml must conform to architectural schema and have zero float values."""
        file_path = REFERENCES_DIR / "annual-1404.yaml"
        assert file_path.exists()
        with open(file_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        errors = validate_parameters.validate_annual_parameters(data)
        assert errors == [], f"Errors in annual-1404.yaml: {errors}"
        assert data["metadata"]["year"] == 1404
        assert data["parameters"]["daily_minimum_wage_irr"]["value"] == 3463656
        assert data["parameters"]["salary_tax"]["annual_exemption_ceiling_irr"]["value"] == 2880000000

    def test_validate_1405_parameters(self):
        """annual-1405.yaml must conform to architectural schema and have zero float values."""
        file_path = REFERENCES_DIR / "annual-1405.yaml"
        assert file_path.exists()
        with open(file_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        errors = validate_parameters.validate_annual_parameters(data)
        assert errors == [], f"Errors in annual-1405.yaml: {errors}"
        assert data["metadata"]["year"] == 1405
        assert data["parameters"]["daily_minimum_wage_irr"]["value"] == 5541850
        assert data["parameters"]["salary_tax"]["annual_exemption_ceiling_irr"]["value"] == 4800000000

    def test_reject_float_values_in_parameters(self):
        """Any float encounter must be rejected."""
        bad_data = {
            "metadata": {
                "schema_version": "1.0",
                "year": 1404,
                "effective_from": "2025-03-21",
                "effective_to": "2026-03-20",
                "source_authority": "Test",
                "verification_status": "CORROBORATED",
            },
            "parameters": {
                "daily_minimum_wage_irr": {"value": 3463656.50},  # Float prohibited!
                "other_levels": {
                    "percentage_increase": {"value": "0.32"},
                    "daily_fixed_addition_irr": {"value": 310535},
                },
                "monthly_housing_allowance_irr": {"value": 9000000},
                "monthly_grocery_allowance_irr": {"value": 22000000},
                "daily_seniority_base_irr": {"value": 94000},
                "monthly_child_allowance_per_child_irr": {"value": 10390968},
                "monthly_marriage_allowance_irr": {"value": 5000000},
                "social_security": {
                    "ceiling_multiplier": 7,
                    "employee_rate": "0.07",
                    "employer_rate": "0.20",
                    "unemployment_rate": "0.03",
                },
                "salary_tax": {
                    "annual_exemption_ceiling_irr": {"value": 2880000000},
                    "brackets": [{"tier": 1, "marginal_rate": "0.00"}],
                },
            },
        }
        errors = validate_parameters.validate_annual_parameters(bad_data)
        assert any("Prohibited float value encountered" in e for e in errors)


class TestDiffAnnualRules:
    """Tests for diff_annual_rules.py delta calculation."""

    def test_diff_1404_to_1405_detects_accurate_deltas(self):
        """Diffing 1404 and 1405 must calculate exact deltas."""
        with open(REFERENCES_DIR / "annual-1404.yaml", encoding="utf-8") as f:
            data_1404 = yaml.safe_load(f)
        with open(REFERENCES_DIR / "annual-1405.yaml", encoding="utf-8") as f:
            data_1405 = yaml.safe_load(f)

        report = diff_annual_rules.diff_annual_rules(data_1404, data_1405)

        # Minimum wage 3,463,656 -> 5,541,850 is +60.0%
        assert "3,463,656 IRR" in report
        assert "5,541,850 IRR" in report
        assert "+60.0%" in report

        # Tax ceiling 2,880,000,000 -> 4,800,000,000
        assert "2,880,000,000 IRR" in report
        assert "4,800,000,000 IRR" in report

        # Status transition
        assert "`CORROBORATED` ➔ `PENDING_UPDATE`" in report
        assert "FAIL-CLOSED DIRECTIVE ACTIVE" in report


class TestCheckSourceFreshness:
    """Tests for check_source_freshness.py date handling."""

    def test_freshness_as_of_specific_date(self):
        """Auditing sources as of 2026-09-20 correctly flags expired 1404 decrees."""
        records = validate_sources.parse_source_status_table(REFERENCES_DIR / "source-status.md")
        as_of = date(2026, 9, 20)

        active, pending, expired, warnings = check_source_freshness.audit_freshness(records, as_of)

        assert len(records) == 13
        assert len(pending) == 3
        assert any("SRC-SLC-WAGE-1405" in p for p in pending)

        # 1404 decrees expired on 1404/12/29 (2026-03-20)
        assert len(expired) == 3
        assert any("SRC-SLC-WAGE-1404" in e for e in expired)
        assert any("Superseded by SRC-SLC-WAGE-1405" in e for e in expired)

    def test_freshness_in_mid_1404(self):
        """Auditing sources as of 2025-06-01 (mid 1404) shows 1404 decrees as active."""
        records = validate_sources.parse_source_status_table(REFERENCES_DIR / "source-status.md")
        as_of = date(2025, 6, 1)

        active, pending, expired, warnings = check_source_freshness.audit_freshness(records, as_of)

        # In mid-1404, 1404 decrees are not expired
        assert "SRC-SLC-WAGE-1404" in active
        assert "SRC-CAB-HOUSING-1404" in active
        assert "SRC-BUDGET-1404-TAX" in active
