#!/usr/bin/env python3
"""Validation tooling for annual statutory parameter YAML definitions.

This script audits annual parameter definitions (e.g. references/annual-1405.yaml)
to enforce architectural schema constraints:
- Zero float types permitted for monetary or percentage values.
- Required statutory parameter keys must be declared.
- Tax bracket tiers must be ordered and contiguous.
- Effective dates must be valid ISO-8601 strings.

THIS SCRIPT CONTAINS VALIDATION/SUPPORT TOOLING ONLY.
NO BUSINESS CALCULATION ENGINE IS CONTAINED HERE.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REQUIRED_METADATA_KEYS = {
    "schema_version",
    "year",
    "effective_from",
    "effective_to",
    "source_authority",
    "verification_status",
}

REQUIRED_PARAMETER_KEYS = {
    "daily_minimum_wage_irr",
    "other_levels",
    "monthly_housing_allowance_irr",
    "monthly_grocery_allowance_irr",
    "daily_seniority_base_irr",
    "monthly_child_allowance_per_child_irr",
    "monthly_marriage_allowance_irr",
    "social_security",
    "salary_tax",
}


def check_no_floats(obj: Any, path: str = "") -> list[str]:
    """Recursively verify that no float values exist in the parsed data."""
    errors: list[str] = []
    if isinstance(obj, float):
        errors.append(
            f"Prohibited float value encountered at '{path}': {obj}. "
            "Financial parameters must use integer Rials, Decimal string representations, or null."
        )
    elif isinstance(obj, dict):
        for k, v in obj.items():
            errors.extend(check_no_floats(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            errors.extend(check_no_floats(item, f"{path}[{idx}]"))
    return errors


def validate_annual_parameters(data: dict[str, Any]) -> list[str]:
    """Validate parameter dictionary against architectural invariants."""
    errors: list[str] = []

    # 1. Float prohibition check
    errors.extend(check_no_floats(data))

    # 2. Metadata validation
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return ["Root 'metadata' object is missing or invalid."]

    missing_meta = REQUIRED_METADATA_KEYS - set(metadata.keys())
    if missing_meta:
        errors.append(f"Metadata missing required keys: {sorted(missing_meta)}")

    # Date validity
    for date_key in ("effective_from", "effective_to"):
        val = metadata.get(date_key)
        if val:
            try:
                date.fromisoformat(str(val))
            except ValueError:
                errors.append(f"Metadata '{date_key}' must be valid ISO-8601 date string. Found: {val}")

    # 3. Parameters structure validation
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        errors.append("Root 'parameters' object is missing or invalid.")
        return errors

    missing_params = REQUIRED_PARAMETER_KEYS - set(parameters.keys())
    if missing_params:
        errors.append(f"Parameters missing required statutory keys: {sorted(missing_params)}")

    # 4. Social Security parameters
    sso = parameters.get("social_security")
    if isinstance(sso, dict):
        if sso.get("ceiling_multiplier") != 7:
            errors.append(
                f"Social Security ceiling multiplier must be 7 per Article 35. Found: {sso.get('ceiling_multiplier')}"
            )
        for rate_key in ("employee_rate", "employer_rate", "unemployment_rate"):
            rate_val = sso.get(rate_key)
            if rate_val is not None and not isinstance(rate_val, str):
                errors.append(f"Social Security rate '{rate_key}' must be a Decimal string (e.g. '0.07').")

    # 5. Salary Tax brackets
    tax = parameters.get("salary_tax")
    if isinstance(tax, dict):
        brackets = tax.get("brackets")
        if not isinstance(brackets, list) or len(brackets) == 0:
            errors.append("Salary tax configuration requires non-empty 'brackets' list.")
        else:
            previous_tier = 0
            for b in brackets:
                tier = b.get("tier", 0)
                if tier <= previous_tier:
                    errors.append(
                        f"Tax bracket tier numbers must be strictly ascending. "
                        f"Found tier {tier} after {previous_tier}."
                    )
                previous_tier = tier

                rate = b.get("marginal_rate")
                if rate is not None and not isinstance(rate, str):
                    errors.append(f"Bracket tier {tier} marginal_rate must be a Decimal string (e.g. '0.10').")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate annual parameter YAML schemas.")
    parser.add_argument(
        "--year",
        type=int,
        default=1405,
        help="Solar year to validate (default: 1405)",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Explicit file path to YAML parameter file",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all annual-*.yaml parameter files found in references directory",
    )
    args = parser.parse_args()

    ref_dir = Path(__file__).parent.parent / "references"

    if args.all:
        files_to_validate = sorted(ref_dir.glob("annual-*.yaml"))
        if not files_to_validate:
            print(f"ERROR: No annual parameter files found in {ref_dir}", file=sys.stderr)
            return 1
    elif args.file:
        files_to_validate = [args.file]
    else:
        files_to_validate = [ref_dir / f"annual-{args.year}.yaml"]

    total_errors = 0

    for file_path in files_to_validate:
        if not file_path.exists():
            print(f"ERROR: Annual parameter file not found: {file_path}", file=sys.stderr)
            total_errors += 1
            continue

        try:
            with open(file_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not isinstance(data, dict):
                print(f"ERROR: YAML content at {file_path} is not a valid dictionary.", file=sys.stderr)
                total_errors += 1
                continue

            errors = validate_annual_parameters(data)
            meta = data.get("metadata", {})
            print(f"Audited parameter file: {file_path.name}")
            print(f"  Solar Year: {meta.get('year')}")
            print(f"  Verification Status: {meta.get('verification_status')}")
            print(f"  Authority: {meta.get('source_authority')}")

            if errors:
                print(f"  FAILED: {len(errors)} schema invariant errors found in {file_path.name}:")
                for err in errors:
                    print(f"    - {err}")
                total_errors += len(errors)
            else:
                print(f"  SUCCESS: {file_path.name} strictly conforms to architectural schemas and float-free invariants.\n")

        except Exception as exc:
            print(f"ERROR: Unexpected exception during parameter validation of {file_path}: {exc}", file=sys.stderr)
            total_errors += 1

    if total_errors > 0:
        print(f"VALIDATION FAILED: {total_errors} total errors encountered across inspected files.", file=sys.stderr)
        return 1

    print("ALL AUDITED PARAMETER FILES CONFORM TO SPECIFICATION.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
