#!/usr/bin/env python3
"""Validation tooling for legal source records and verification statuses.

This script audits skills/iran-payroll-insurance/references/source-status.md
to verify that all statutory authorities, gazette decrees, and court rulings
conform to the normative metadata schema and cryptographic hash rules.

FAIL-CLOSED INTEGRITY DIRECTIVE:
If any source is marked VERIFIED:
  - The snapshot file MUST physically exist on disk and be non-empty.
  - The computed SHA-256 of the snapshot file MUST match the registered hash.
  - The canonical_url MUST be present and valid (http/https).
  - The verifier metadata (verified_by, verified_at) MUST be present.
  - The effective dates MUST be present and valid.
Any violation immediately fails with exit code 1.

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
URL_PATTERN = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    authority: str
    document_number: str
    title: str
    published_at: str
    effective_from: str
    effective_to: str
    verification_status: str
    canonical_url: str
    snapshot_path: str
    sha256: str
    supersedes_superseded_by: str
    verified_by: str
    verified_at: str


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

            if in_table and len(parts) >= 14:
                records.append(
                    SourceRecord(
                        source_id=parts[0],
                        authority=parts[1],
                        document_number=parts[2],
                        title=parts[3],
                        published_at=parts[4],
                        effective_from=parts[5],
                        effective_to=parts[6],
                        verification_status=parts[7],
                        canonical_url=parts[8],
                        snapshot_path=parts[9],
                        sha256=parts[10],
                        supersedes_superseded_by=parts[11],
                        verified_by=parts[12],
                        verified_at=parts[13],
                    )
                )
            elif in_table and len(parts) >= 8:
                # Fallback for legacy 8-column table if encountered
                records.append(
                    SourceRecord(
                        source_id=parts[0],
                        authority=parts[1],
                        document_number=parts[2],
                        title=parts[2],
                        published_at=parts[3],
                        effective_from=parts[4],
                        effective_to="None",
                        verification_status=parts[5],
                        canonical_url="None",
                        snapshot_path=parts[6],
                        sha256=parts[7],
                        supersedes_superseded_by="None",
                        verified_by="None",
                        verified_at="None",
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

        # Fail-closed checks for VERIFIED sources
        if rec.verification_status == "VERIFIED":
            verified_count += 1

            # 1. snapshot_path must exist and not be None/empty
            if rec.snapshot_path in ("None", "", "-"):
                errors.append(
                    f"FAIL CLOSED: VERIFIED source {rec.source_id} requires a valid snapshot_path on disk."
                )
            else:
                snapshot_file = (base_dir / rec.snapshot_path) if base_dir else Path(rec.snapshot_path)
                if not snapshot_file.exists() or not snapshot_file.is_file():
                    errors.append(
                        f"FAIL CLOSED: VERIFIED source {rec.source_id} snapshot file not found on disk: "
                        f"{snapshot_file}"
                    )
                elif snapshot_file.stat().st_size == 0:
                    errors.append(
                        f"FAIL CLOSED: VERIFIED source {rec.source_id} snapshot file is empty (0 bytes): "
                        f"{snapshot_file}"
                    )
                else:
                    computed = hashlib.sha256(snapshot_file.read_bytes()).hexdigest()
                    if computed.lower() != rec.sha256.lower():
                        errors.append(
                            f"FAIL CLOSED: Hash mismatch for {rec.source_id} at {rec.snapshot_path}: "
                            f"expected '{rec.sha256}', computed '{computed}'"
                        )

            # 2. SHA-256 format check
            if rec.sha256 in ("None", "", "-") or not HEX_64_PATTERN.match(rec.sha256):
                errors.append(
                    f"FAIL CLOSED: VERIFIED source {rec.source_id} requires a valid 64-character hex SHA-256 hash. "
                    f"Found: '{rec.sha256}'"
                )

            # 3. Canonical URL check
            if rec.canonical_url in ("None", "", "-") or not URL_PATTERN.match(rec.canonical_url):
                errors.append(
                    f"FAIL CLOSED: VERIFIED source {rec.source_id} requires a valid canonical_url. "
                    f"Found: '{rec.canonical_url}'"
                )

            # 4. Verifier metadata check
            if rec.verified_by in ("None", "", "-"):
                errors.append(
                    f"FAIL CLOSED: VERIFIED source {rec.source_id} requires verifier metadata in 'verified_by'."
                )
            if rec.verified_at in ("None", "", "-"):
                errors.append(
                    f"FAIL CLOSED: VERIFIED source {rec.source_id} requires verification timestamp in 'verified_at'."
                )

            # 5. Effective date check
            if rec.effective_from in ("None", "", "-"):
                errors.append(
                    f"FAIL CLOSED: VERIFIED source {rec.source_id} requires a valid effective_from date."
                )

        else:
            # Non-verified sources: forbid fabricated hashes
            if rec.sha256 not in ("None", "", "-") and HEX_64_PATTERN.match(rec.sha256):
                # If a hash is provided for non-verified without snapshot, reject it
                snapshot_file = (base_dir / rec.snapshot_path) if base_dir and rec.snapshot_path not in ("None", "") else None
                if snapshot_file is None or not snapshot_file.exists():
                    errors.append(
                        f"Non-VERIFIED source {rec.source_id} has a pinned SHA-256 hash '{rec.sha256}' "
                        f"without an actual snapshot file on disk. Status must be CORROBORATED or DISCOVERY_ONLY "
                        "with sha256: None until physical snapshot is acquired."
                    )

            # Check canonical URL if CORROBORATED
            if rec.verification_status == "CORROBORATED":
                if rec.canonical_url in ("None", "", "-") or not URL_PATTERN.match(rec.canonical_url):
                    errors.append(
                        f"CORROBORATED source {rec.source_id} requires a valid canonical_url. "
                        f"Found: '{rec.canonical_url}'"
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
        base_dir = args.file.parent.parent
        verified, err_count, errors = validate_sources(records, base_dir=base_dir)

        print(f"Parsed {len(records)} legal source records from {args.file.name}")
        print(f"Verified sources (with disk snapshot & hash match): {verified}")

        corroborated = [r.source_id for r in records if r.verification_status == "CORROBORATED"]
        if corroborated:
            print(f"Corroborated sources (canonical URL confirmed): {len(corroborated)} ({', '.join(corroborated)})")

        pending = [r.source_id for r in records if r.verification_status == "PENDING_UPDATE"]
        if pending:
            print(f"Pending update (annual draft cycles): {len(pending)} ({', '.join(pending)})")

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
