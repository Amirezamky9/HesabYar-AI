#!/usr/bin/env python3
"""Freshness and expiration auditing tooling for statutory sources.

This script evaluates the temporal validity of registered legal sources
against a reference evaluation date (defaults to current system date).
It identifies:
- Expired or superseded statutory decrees.
- Pending annual decrees (e.g. 1405 housing allowance or wage circulars).
- Potential fail-closed calculation triggers in production runs.

THIS SCRIPT CONTAINS VALIDATION/SUPPORT TOOLING ONLY.
NO BUSINESS CALCULATION ENGINE IS CONTAINED HERE.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import jdatetime

# Reuse parse_source_status_table and SourceRecord from validate_sources
from validate_sources import SourceRecord, parse_source_status_table


def parse_date_flexible(val: str) -> date | None:
    """Parse date from either ISO format (YYYY-MM-DD) or Jalali format (YYYY/MM/DD)."""
    val = val.strip()
    if val in ("None", "", "-", "Pending"):
        return None

    # Check ISO format YYYY-MM-DD
    if "-" in val:
        try:
            return date.fromisoformat(val)
        except ValueError:
            pass

    # Check Jalali format YYYY/MM/DD
    if "/" in val:
        parts = val.split("/")
        if len(parts) == 3:
            try:
                jy, jm, jd = int(parts[0]), int(parts[1]), int(parts[2])
                return jdatetime.date(jy, jm, jd).togregorian()
            except (ValueError, TypeError):
                pass

    return None


def audit_freshness(
    records: list[SourceRecord], as_of: date
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Audit source records against an evaluation date.

    Returns:
        (active_sources, pending_update, expired_or_superseded, warnings)
    """
    active_sources: list[str] = []
    pending_update: list[str] = []
    expired_or_superseded: list[str] = []
    warnings: list[str] = []

    records_by_id = {r.source_id: r for r in records}

    for rec in records:
        if rec.verification_status == "PENDING_UPDATE":
            pending_update.append(f"{rec.source_id}: {rec.title} ({rec.authority})")

        # Check effective_to expiration
        exp_date = parse_date_flexible(rec.effective_to)
        is_expired = False
        if exp_date and as_of > exp_date:
            is_expired = True

        # Check supersession
        superseded_info = ""
        if "superseded_by:" in rec.supersedes_superseded_by:
            target_id = rec.supersedes_superseded_by.split("superseded_by:")[1].strip()
            target_rec = records_by_id.get(target_id)
            target_status = target_rec.verification_status if target_rec else "UNKNOWN"
            superseded_info = f"Superseded by {target_id} (status: {target_status})"

        if is_expired or superseded_info:
            note_parts = []
            if is_expired:
                note_parts.append(f"Expired on {rec.effective_to} (Gregorian: {exp_date})")
            if superseded_info:
                note_parts.append(superseded_info)
            desc = f"{rec.source_id}: {rec.title} -> " + " | ".join(note_parts)
            expired_or_superseded.append(desc)

            if is_expired and not superseded_info:
                warnings.append(
                    f"Source {rec.source_id} expired on {rec.effective_to} with no superseding decree declared."
                )

        if rec.verification_status in ("VERIFIED", "CORROBORATED"):
            if not is_expired:
                active_sources.append(rec.source_id)

        elif rec.verification_status in ("DISCOVERY_ONLY", "DISPUTED"):
            warnings.append(
                f"Source {rec.source_id} is in status '{rec.verification_status}'. "
                "Calculations will fail closed if queried in production."
            )

    return active_sources, pending_update, expired_or_superseded, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit freshness and pending status of legal sources.")
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        help="Evaluation date in YYYY-MM-DD format (defaults to current date)",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(__file__).parent.parent / "references" / "source-status.md",
        help="Path to source-status.md",
    )
    args = parser.parse_args()

    if args.as_of_date:
        try:
            eval_date = date.fromisoformat(args.as_of_date)
        except ValueError:
            print(f"ERROR: Invalid date format for --as-of-date: {args.as_of_date}", file=sys.stderr)
            return 1
    else:
        # Default to current date for interactive use
        eval_date = date.today()

    try:
        records = parse_source_status_table(args.file)
        active, pending, expired, warnings = audit_freshness(records, eval_date)

        print(f"Legal Source Freshness Audit (Evaluation Date: {eval_date.isoformat()})")
        print(f"Total Sources Evaluated: {len(records)}")
        print(f"Active Valid Sources: {len(active)}")
        print(f"Expired or Superseded: {len(expired)}")
        print(f"Pending Statutory Updates: {len(pending)}")

        if expired:
            print("\n--- EXPIRED OR SUPERSEDED SOURCES ---")
            for e in expired:
                print(f"  📜 {e}")

        if pending:
            print("\n--- PENDING STATUTORY DECREES ---")
            for p in pending:
                print(f"  ⏳ {p}")

        if warnings:
            print("\n--- WARNINGS & ADVISORIES ---")
            for w in warnings:
                print(f"  ⚠️  {w}")

        print("\nAUDIT SUMMARY:")
        if pending:
            print("  STATUS: ADVISORY - Some annual cycles are awaiting official gazette publication.")
            print("  OPERATIONAL DIRECTIVE: Automated production runs for pending cycles will fail closed.")
        else:
            print("  STATUS: ALL SOURCES FRESH AND VERIFIED.")

        return 0

    except Exception as exc:
        print(f"ERROR: Freshness audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
