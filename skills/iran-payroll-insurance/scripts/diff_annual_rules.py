#!/usr/bin/env python3
"""Delta analysis tooling between consecutive solar year parameter files.

This script compares two annual statutory parameter definitions (e.g. 1404 vs 1405)
and produces a structured differential report:
- Status transitions (e.g. VERIFIED -> PENDING_UPDATE).
- Nominal Rial value changes and percentage increases.
- Tax bracket tier threshold adjustments.

THIS SCRIPT CONTAINS VALIDATION/SUPPORT TOOLING ONLY.
NO BUSINESS CALCULATION ENGINE IS CONTAINED HERE.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, encoding="utf-8") as f:
        content = yaml.safe_load(f)
    if not isinstance(content, dict):
        raise ValueError(f"Content in {path} must be a dictionary.")
    return content


def format_irr(value: int | None) -> str:
    if value is None:
        return "PENDING (None)"
    return f"{value:,} IRR"


def diff_annual_rules(data_a: dict[str, Any], data_b: dict[str, Any]) -> str:
    meta_a = data_a.get("metadata", {})
    meta_b = data_b.get("metadata", {})
    params_a = data_a.get("parameters", {})
    params_b = data_b.get("parameters", {})

    lines: list[str] = [
        f"# Statutory Parameter Differential: Solar Year {meta_a.get('year')} ➔ {meta_b.get('year')}",
        "",
        "## 1. Metadata & Authority Comparison",
        f"- **Base Year ({meta_a.get('year')}):** Status: `{meta_a.get('verification_status')}`",
        f"  Authority: {meta_a.get('source_authority')}",
        f"- **Target Year ({meta_b.get('year')}):** Status: `{meta_b.get('verification_status')}`",
        f"  Authority: {meta_b.get('source_authority')}",
        "",
        "## 2. Core Labor & Wage Parameters",
        "| Parameter Name | Base Value | Target Value | Delta / Note |",
        "|---|---|---|---|",
    ]

    # Minimum wage
    mw_a = params_a.get("daily_minimum_wage_irr", {}).get("value")
    mw_b = params_b.get("daily_minimum_wage_irr", {}).get("value")
    delta_str = "Pending Enactment"
    if isinstance(mw_a, int) and isinstance(mw_b, int):
        diff_pct = (Decimal(mw_b - mw_a) / Decimal(mw_a) * 100).quantize(Decimal("0.1"))
        delta_str = f"+{diff_pct}% (+{mw_b - mw_a:,} IRR)"
    lines.append(f"| Daily Minimum Wage (مزد روزانه) | {format_irr(mw_a)} | {format_irr(mw_b)} | {delta_str} |")

    # Housing allowance
    ha_a = params_a.get("monthly_housing_allowance_irr", {}).get("value")
    ha_b = params_b.get("monthly_housing_allowance_irr", {}).get("value")
    lines.append(
        f"| Monthly Housing Allowance (حق مسکن) | {format_irr(ha_a)} | {format_irr(ha_b)} | "
        "Pending Cabinet Decree if Target is None |"
    )

    # Grocery allowance
    ga_a = params_a.get("monthly_grocery_allowance_irr", {}).get("value")
    ga_b = params_b.get("monthly_grocery_allowance_irr", {}).get("value")
    lines.append(
        f"| Monthly Grocery Allowance (بن کارگری) | {format_irr(ga_a)} | {format_irr(ga_b)} | "
        "Supreme Labor Council Decree |"
    )

    # Seniority daily base
    sb_a = params_a.get("daily_seniority_base_irr", {}).get("value")
    sb_b = params_b.get("daily_seniority_base_irr", {}).get("value")
    lines.append(
        f"| Daily Seniority Base (پایه سنوات روزانه) | {format_irr(sb_a)} | {format_irr(sb_b)} | "
        "Per day after 1 year service |"
    )

    # Marriage allowance
    ma_a = params_a.get("monthly_marriage_allowance_irr", {}).get("value")
    ma_b = params_b.get("monthly_marriage_allowance_irr", {}).get("value")
    lines.append(
        f"| Monthly Marriage Allowance (حق تاهل) | {format_irr(ma_a)} | {format_irr(ma_b)} | "
        "Married employees |"
    )

    lines.extend([
        "",
        "## 3. Direct Tax Law Article 84 Exemption Comparison",
        "| Exemption Level | Base Year | Target Year | Note |",
        "|---|---|---|---|",
    ])

    tax_a = params_a.get("salary_tax", {})
    tax_b = params_b.get("salary_tax", {})

    ann_a = tax_a.get("annual_exemption_ceiling_irr", {}).get("value")
    ann_b = tax_b.get("annual_exemption_ceiling_irr", {}).get("value")
    lines.append(f"| Annual Ceiling (ماده ۸۴) | {format_irr(ann_a)} | {format_irr(ann_b)} | Budget Law Enactment |")

    mon_a = tax_a.get("monthly_exemption_ceiling_irr", {}).get("value")
    mon_b = tax_b.get("monthly_exemption_ceiling_irr", {}).get("value")
    lines.append(f"| Monthly Prorated Exemption | {format_irr(mon_a)} | {format_irr(mon_b)} | Annual Ceiling / 12 |")

    lines.extend([
        "",
        "## 4. Architectural Compliance Verdict",
        f"- Status Transition: `{meta_a.get('verification_status')}` ➔ `{meta_b.get('verification_status')}`",
    ])

    if meta_b.get("verification_status") != "VERIFIED":
        lines.append(
            "- **FAIL-CLOSED DIRECTIVE ACTIVE:** Target year parameters are NOT fully verified. "
            "Production calculations for target year must fail closed with `PAYROLL_RULE_UNVERIFIED`."
        )
    else:
        lines.append("- **PRODUCTION AUTHORIZED:** Target year parameters are marked VERIFIED.")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff two annual parameter YAML definitions.")
    parser.add_argument("--from-year", type=int, default=1404, help="Base solar year (default: 1404)")
    parser.add_argument("--to-year", type=int, default=1405, help="Target solar year (default: 1405)")
    parser.add_argument("--from-file", type=Path, default=None, help="Explicit base YAML file path")
    parser.add_argument("--to-file", type=Path, default=None, help="Explicit target YAML file path")
    args = parser.parse_args()

    ref_dir = Path(__file__).parent.parent / "references"
    from_file = args.from_file or (ref_dir / f"annual-{args.from_year}.yaml")
    to_file = args.to_file or (ref_dir / f"annual-{args.to_year}.yaml")

    try:
        data_a = load_yaml(from_file)
        data_b = load_yaml(to_file)

        report = diff_annual_rules(data_a, data_b)
        print(report)
        return 0

    except Exception as exc:
        print(f"ERROR: Failed to generate annual rule differential: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
