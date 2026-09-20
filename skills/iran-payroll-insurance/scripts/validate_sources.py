#!/usr/bin/env python3
"""Validation tooling for legal source records and verification statuses.

This script audits skills/iran-payroll-insurance/references/source-status.md
to verify that all statutory authorities, gazette decrees, and court rulings
conform to the normative metadata schema and cryptographic hash rules.

THIS SCRIPT CONTAINS VALIDATION/SUPPORT TOOLING ONLY.
NO BUSINESS CALCULATION ENGINE IS CONTAINED HERE.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VALID_STATUSES = {
    "VERIFIED",
    "CORROBORATED",
    "DISCOVERY_ONLY",
    "DISPUTED",
    "RETIRED",
    "PENDING_UPDATE",
}

HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    authority: str
    document: str
    published_at: str
    effective_from: str
    verification_status: str
    snapshot_path: str
    sha256: str


def parse_source_status_table(file_path: Path) -> list[SourceRecord]:
    """Parse the markdown table in source-status.md into structured records."""
    if not file_path.exists():
        raise FileNotFoundError(f"Source status file not found: {file_path}")

    records: list[SourceRecord] = []
    in_table = False

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|"):
                continue

            parts = [p.strip().strip("`") for p in line.split("|")[1:-1]]
            if not parts:
                continue

            # Header check
            if "Source ID" in parts[0]:
                in_table = True
                continue

            # Separator check
            if set(parts[0]) <= {"-", ":"}:
                continue

            if in_table and len(parts) >= 8:
                records.append(
                    SourceRecord(
                        source_id=parts[0],
                        authority=parts[1],
                        document=parts[2],
                        published_at=parts[3],
                        effective_from=parts[4],
                        verification_status=parts[5],
                        snapshot_path=parts[6],
                        sha256=parts[7],
                    )
                )

    return records


def validate_sources(records: list[SourceRecord], base_dir: Path | None = None) -> tuple[int, int, list[str]]:
    """Validate source status entries against architectural invariants.

    Returns:
        (verified_count, errors_count, error_messages)
    """
    errors: list[str] = []
    verified_count = 0

    if not records:
        return 0, 1, ["No source records discovered in table."]

    seen_ids: set[str] = set()

    for rec in records:
        # Unique ID check
        if rec.source_id in seen_ids:
            errors.append(f"Duplicate Source ID: {rec.source_id}")
        seen_ids.add(rec.source_id)

        # Status validity check
        if rec.verification_status not in VALID_STATUSES:
            errors.append(
                f"Source {rec.source_id} has invalid status '{rec.verification_status}'. "
                f"Allowed: {sorted(VALID_STATUSES)}"
            )

        # Verified status constraints
        if rec.verification_status == "VERIFIED":
            verified_count += 1
            if rec.sha256 == "None" or not HEX_64_PATTERN.match(rec.sha256):
                errors.append(
                    f"VERIFIED source {rec.source_id} requires a valid 64-character hex SHA-256 hash. "
                    f"Found: '{rec.sha256}'"
                )

            if rec.snapshot_path == "None" or not rec.snapshot_path:
                errors.append(f"VERIFIED source {rec.source_id} requires a valid snapshot_path.")
            elif base_dir and (base_dir / rec.snapshot_path).exists():
                computed = hashlib.sha256((base_dir / rec.snapshot_path).read_bytes()).hexdigest()
                if computed.lower() != rec.sha256.lower():
                    errors.append(
                        f"Hash mismatch for {rec.source_id} at {rec.snapshot_path}: "
                        f"expected {rec.sha256}, computed {computed}"
                    )

    return verified_count, len(errors), errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate legal source registry records.")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(__file__).parent.parent / "references" / "source-status.md",
        help="Path to source-status.md",
    )
    args = parser.parse_args()

    try:
        records = parse_source_status_table(args.file)
        verified, err_count, errors = validate_sources(records, base_dir=args.file.parent.parent)

        print(f"Parsed {len(records)} legal source records from {args.file.name}")
        print(f"Verified sources: {verified}")

        pending = [r.source_id for r in records if r.verification_status == "PENDING_UPDATE"]
        if pending:
            print(f"Pending update (expected for draft cycles): {len(pending)} ({', '.join(pending)})")

        if err_count > 0:
            print(f"\nFAILED: {err_count} schema/integrity errors encountered:")
            for err in errors:
                print(f"  - {err}")
            return 1

        print("\nSUCCESS: All registered source records conform to architectural specifications.")
        return 0

    except Exception as exc:
        print(f"ERROR: Failed to validate source records: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
