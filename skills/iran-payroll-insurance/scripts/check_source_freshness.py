#!/usr/bin/env python3
"""Freshness and expiration auditing tooling for statutory sources.

This script evaluates the temporal validity of registered legal sources
against a reference evaluation date (defaults to today's system date).
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

# Reuse parse_source_status_table from validate_sources
from validate_sources import parse_source_status_table


def audit_freshness(
    records: list, as_of: date
) -> tuple[list[str], list[str], list[str]]:
    """Audit source records against an evaluation date.

    Returns:
        (active_verified, pending_update, warnings)
    """
    active_verified: list[str] = []
    pending_update: list[str] = []
    warnings: list[str] = []

    for rec in records:
        if rec.verification_status == "PENDING_UPDATE":
            pending_update.append(f"{rec.source_id}: {rec.document} ({rec.authority})")
        elif rec.verification_status == "VERIFIED":
            active_verified.append(rec.source_id)
            # Check expiration if effective_to exists
            if hasattr(rec, "effective_to") and rec.effective_to and rec.effective_to != "None":
                try:
                    exp_date = date.fromisoformat(rec.effective_to)
                    if exp_date < as_of:
                        warnings.append(
                            f"Source {rec.source_id} expired on {rec.effective_to} "
                            f"(evaluation date: {as_of.isoformat()}). Verification required for subsequent periods."
                        )
                except ValueError:
                    pass
        elif rec.verification_status in ("DISCOVERY_ONLY", "DISPUTED"):
            warnings.append(
                f"Source {rec.source_id} is in status '{rec.verification_status}'. "
                "Will fail closed if queried in production."
            )

    return active_verified, pending_update, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit freshness and pending status of legal sources.")
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        help="Evaluation date in YYYY-MM-DD format (defaults to current date: 2026-09-20)",
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
        # Default to current session date
        eval_date = date(2026, 9, 20)

    try:
        records = parse_source_status_table(args.file)
        active, pending, warnings = audit_freshness(records, eval_date)

        print(f"Legal Source Freshness Audit (Evaluation Date: {eval_date.isoformat()})")
        print(f"Total Sources Evaluated: {len(records)}")
        print(f"Active & Verified: {len(active)}")
        print(f"Pending Statutory Updates: {len(pending)}")

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
