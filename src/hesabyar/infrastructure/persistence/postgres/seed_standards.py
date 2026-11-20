"""Seed the standards registry from the bundled corpus (US-401).

Reads the repository corpus — ``metadata.json`` (35 standards),
``standards/<slug>/source/standard.md`` (source text, hashed) and
``validators/rules.yaml`` (36 rules) — and upserts it into
``standard_sources`` / ``standard_revisions`` / ``standard_rules``.
The seed is idempotent: re-running it updates the same rows (keyed by
``standard_code``, ``(source_id, revision_id)`` and
``(revision_id, rule_id)``).

Verification labels (docs/CURRENT_RULES_1405.md §1):

- Standards 43 and 44 are VERIFIED_TEXT with their effective dates
  (1404/01/01 and 1405/01/01). Standard 43's bundled text is the 1404
  revision — its SKILL.md frontmatter still says 1397, which is stale,
  so the seed overrides the revision id to ``1404``.
- Standard 15 is BLOCKED_SOURCE_GAP: the bundled text is the pre-1404
  revision (amended 1399, effective 1380/01/01), and the revised-1404
  text applies to periods beginning 1405/01/01+. The bundled revision
  is recorded with ``effective_to`` 1404/12/29; the validation service
  refuses any run that would use it for 1405+ periods.
- Every other bundled revision is UNVERIFIED: the text is present, but
  no official source snapshot has been pinned and verified yet, so the
  registry must not present it as verified.

Rule port (validators/rules.yaml is the LEGACY file — its header
forbids production use of its ``eval()`` formulas):

- Equation rules (``formula`` + ``expected`` + ``tolerance``) become a
  closed-DSL boolean: ``(expr - expected) <= tol and
  (expr - expected) >= -tol``. The DSL forbids binary-float literals
  (``0.01`` evaluates to ERROR), so the tolerance is rendered as an
  exact integer fraction (``1/100``) built from integer division.
- Threshold rules (``formula`` + ``min``/``max``) become
  ``expr >= min`` / ``expr <= max`` with the bound rendered the same way.
- Requirement rules (no ``formula``, e.g. CF005 — a disclosure
  requirement) have no DSL formula and are seeded with
  ``is_executable`` False: a run reports them as not executed, never as
  a fabricated PASS.
- The four ``materiality_thresholds`` entries are ratios, not
  pass/fail rules, and are NOT seeded as rules (the corpus therefore
  seeds 36 rules, matching the story).

Run as a module to seed the configured database::

    python -m hesabyar.infrastructure.persistence.postgres.seed_standards
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import jdatetime
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.infrastructure.persistence.postgres.models import (
    StandardRevisionModel,
    StandardRuleModel,
    StandardSourceModel,
)

# The standards are issued by the Auditing Organization of Iran; the
# per-standard PDF URL in metadata.json is the codified-text mirror
# (thdorsan.com) the bundled text was taken from.
SOURCE_AUTHORITY = "Auditing Organization of Iran (سازمان حسابرسی)"

#: Evidence citation recorded on the VERIFIED_TEXT / BLOCKED_SOURCE_GAP rows.
VERIFIED_BY = "CURRENT_RULES_1405.md (HYA-RULES-1405-01)"

#: The 1405 baseline document's "As of" date (1405/06/29 = 2026-09-20, UTC).
VERIFIED_AT = datetime(2026, 9, 20, tzinfo=UTC)

# Effective dates from CURRENT_RULES_1405.md §2, converted with jdatetime
# (1404/01/01 = 2025-03-21, 1405/01/01 = 2026-03-21, 1380/01/01 =
# 2001-03-21, 1404/12/29 = 2026-03-20).
STD43_EFFECTIVE_FROM = jdatetime.date(1404, 1, 1).togregorian()
STD44_EFFECTIVE_FROM = jdatetime.date(1405, 1, 1).togregorian()
STD15_EFFECTIVE_FROM = jdatetime.date(1380, 1, 1).togregorian()
STD15_EFFECTIVE_TO = jdatetime.date(1404, 12, 29).togregorian()

#: Standard 15's bundled revision is blocked for periods beginning on/after this date.
STD15_BLOCKED_FROM = jdatetime.date(1405, 1, 1).togregorian()

#: Revision-year overrides where the SKILL.md frontmatter is stale:
#: Standard 43's bundled text is the 1404 revision (CURRENT_RULES_1405.md §2).
REVISION_ID_OVERRIDES: dict[str, str] = {"43": "1404"}

#: Header parenthetical marking the revision year inside standard.md.
_REVISION_HEADER_RE = re.compile(r"\((?:تجدیدنظرشده|اصلاح‌شده|اصلاح شده|تصویب‌شده|مصوب)\s*(\d{4})\)")
#: SKILL.md frontmatter ``revision:`` line.
_SKILL_REVISION_RE = re.compile(r"^revision:\s*(\S+)\s*$", re.MULTILINE)

#: YAML groups in rules.yaml that are validation rules (materiality
#: thresholds are ratios, not rules, and are excluded).
_RULE_CATEGORIES = (
    "balance_sheet",
    "income_statement",
    "comprehensive_income",
    "equity_changes",
    "cash_flow",
    "cross_checks",
)

#: Standard number whose revision carries the 36 ported rules.
RULES_STANDARD_CODE = "1"


@dataclass(frozen=True)
class SeedStandard:
    """One standard revision as loaded from the corpus files."""

    number: int
    slug: str
    title: str
    title_en: str | None
    ifrs_equivalent: str | None
    phase: int | None
    source_url: str | None
    revision_id: str
    sha256: str
    snapshot_uri: str
    verification_status: VerificationStatus
    effective_from: date | None
    effective_to: date | None
    verified_by: str | None
    verified_at: datetime | None
    notes: str | None


@dataclass(frozen=True)
class SeedRule:
    """One rule as loaded from validators/rules.yaml."""

    rule_id: str
    category: str
    description: str
    dsl_formula: str | None
    rule_type: str
    severity: str
    standard_refs: tuple[str, ...]
    is_executable: bool
    ruleset_version: str
    verification_status: VerificationStatus
    verified_by: str | None
    verified_at: datetime | None


@dataclass(frozen=True)
class CorpusData:
    """The whole seedable corpus."""

    standards: tuple[SeedStandard, ...]
    rules: tuple[SeedRule, ...]
    ruleset_version: str


def repo_root(start: Path | None = None) -> Path:
    """Return the repository root (five parents above this module)."""
    here = (start or Path(__file__)).resolve()
    return here.parents[5]


def _number_literal(value: int | float) -> str:
    """Render a YAML number as an exact DSL literal.

    The DSL forbids binary-float literals, so a non-integer value is
    rendered as an integer fraction built from integer division
    (``0.01`` -> ``1/100``, ``0.7`` -> ``7/10``); integers render bare.
    """
    frac = Fraction(str(value)).limit_denominator(10_000)
    if frac.denominator == 1:
        return str(frac.numerator)
    return f"{frac.numerator}/{frac.denominator}"


def to_dsl_formula(rule: dict[str, Any]) -> str | None:
    """Convert one legacy rules.yaml rule to a closed-DSL boolean formula.

    Returns None for rules with no formula (disclosure requirements),
    which are seeded as non-executable. Raises ValueError for a rule
    that has a formula but no assertion (expected/min/max) — such a
    rule must fail loudly at seed time, never silently.
    """
    formula = rule.get("formula")
    if formula is None:
        return None
    expr = " ".join(str(formula).split())
    expected = rule.get("expected")
    if expected is not None:
        tolerance = rule.get("tolerance")
        tol = _number_literal(tolerance if tolerance is not None else 0)
        exp = _number_literal(expected)
        return f"({expr} - {exp}) <= {tol} and ({expr} - {exp}) >= -{tol}"
    minimum = rule.get("min")
    if minimum is not None:
        return f"{expr} >= {_number_literal(minimum)}"
    maximum = rule.get("max")
    if maximum is not None:
        return f"{expr} <= {_number_literal(maximum)}"
    raise ValueError(f"Rule {rule.get('id')!r} has a formula but no expected/min/max assertion.")


def _rule_type(rule: dict[str, Any]) -> str:
    """Classify a legacy rule for the registry."""
    if rule.get("formula") is None:
        return "requirement"
    if rule.get("expected") is not None:
        return "equation"
    if rule.get("min") is not None or rule.get("max") is not None:
        return "threshold"
    raise ValueError(f"Rule {rule.get('id')!r} has a formula but no expected/min/max assertion.")


def _skill_revision(standard_dir: Path) -> str:
    """Read the ``revision:`` frontmatter value from a standard's SKILL.md."""
    skill_md = standard_dir / "SKILL.md"
    match = _SKILL_REVISION_RE.search(skill_md.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"No 'revision:' frontmatter in {skill_md}")
    return match.group(1)


def _header_revision_year(standard_md: Path) -> str | None:
    """Read the revision year from the standard text header, if present."""
    head = "\n".join(standard_md.read_text(encoding="utf-8").splitlines()[:15])
    match = _REVISION_HEADER_RE.search(head)
    return match.group(1) if match else None


def _revision_id(number: str, standard_dir: Path) -> str:
    """Resolve the bundled revision id for one standard.

    The standard text header wins when it states a revision year;
    otherwise the SKILL.md frontmatter value is used; Standard 43 is
    overridden to ``1404`` (its frontmatter value 1397 is stale — the
    bundled text is the 1404 revision per CURRENT_RULES_1405.md §2).
    """
    if number in REVISION_ID_OVERRIDES:
        return REVISION_ID_OVERRIDES[number]
    header_year = _header_revision_year(standard_dir / "source" / "standard.md")
    if header_year is not None:
        return header_year
    return _skill_revision(standard_dir)


def _verification(
    number: str,
) -> tuple[VerificationStatus, date | None, date | None, str | None, datetime | None, str | None]:
    """Return (status, effective_from, effective_to, verified_by, verified_at, notes).

    Pinned by docs/CURRENT_RULES_1405.md §2; everything else is
    UNVERIFIED with no effective dates.
    """
    if number == "43":
        return (
            VerificationStatus.VERIFIED_TEXT,
            STD43_EFFECTIVE_FROM,
            None,
            VERIFIED_BY,
            VERIFIED_AT,
            "Supersedes Standards 3, 9 and 29 for periods beginning 1404/01/01 (CURRENT_RULES_1405.md §2).",
        )
    if number == "44":
        return (
            VerificationStatus.VERIFIED_TEXT,
            STD44_EFFECTIVE_FROM,
            None,
            VERIFIED_BY,
            VERIFIED_AT,
            "Supersedes Standard 21 for periods beginning 1405/01/01 (CURRENT_RULES_1405.md §2).",
        )
    if number == "15":
        return (
            VerificationStatus.BLOCKED_SOURCE_GAP,
            STD15_EFFECTIVE_FROM,
            STD15_EFFECTIVE_TO,
            VERIFIED_BY,
            VERIFIED_AT,
            "Bundled text is the pre-1404 revision (amended 1399); the revised-1404 "
            "text applies to periods beginning 1405/01/01 and must be imported and "
            "VERIFIED before use (CURRENT_RULES_1405.md §2).",
        )
    return (VerificationStatus.UNVERIFIED, None, None, None, None, None)


def load_corpus(root: Path | None = None) -> CorpusData:
    """Load the seedable corpus from the repository files (no DB access)."""
    root = root or repo_root()
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    standards: list[SeedStandard] = []
    for entry in metadata["standards"]:
        number = str(entry["number"])
        slug = str(entry["slug"])
        standard_dir = root / "standards" / slug
        standard_md = standard_dir / "source" / "standard.md"
        sha256 = hashlib.sha256(standard_md.read_bytes()).hexdigest()
        status, effective_from, effective_to, verified_by, verified_at, notes = _verification(number)
        standards.append(
            SeedStandard(
                number=int(number),
                slug=slug,
                title=str(entry["title"]),
                title_en=entry.get("title_en"),
                ifrs_equivalent=entry.get("ifrs_equivalent"),
                phase=entry.get("phase"),
                source_url=entry.get("pdf_url"),
                revision_id=_revision_id(number, standard_dir),
                sha256=sha256,
                snapshot_uri=f"standards/{slug}/source/standard.md",
                verification_status=status,
                effective_from=effective_from,
                effective_to=effective_to,
                verified_by=verified_by,
                verified_at=verified_at,
                notes=notes,
            )
        )

    rules_doc = yaml.safe_load((root / "validators" / "rules.yaml").read_text(encoding="utf-8"))
    ruleset_version = str(rules_doc["version"])
    # All rules belong to Standard 1 (rules.yaml declares standard: 1).
    std1 = next(s for s in standards if s.number == int(RULES_STANDARD_CODE))
    rules: list[SeedRule] = []
    for category in _RULE_CATEGORIES:
        for rule in rules_doc.get(category) or []:
            formula = to_dsl_formula(rule)
            rules.append(
                SeedRule(
                    rule_id=str(rule["id"]),
                    category=category,
                    description=str(rule["description"]),
                    dsl_formula=formula,
                    rule_type=_rule_type(rule),
                    severity=str(rule["severity"]),
                    standard_refs=tuple(str(ref) for ref in rule.get("refs") or []),
                    is_executable=formula is not None,
                    ruleset_version=ruleset_version,
                    verification_status=std1.verification_status,
                    verified_by=std1.verified_by,
                    verified_at=std1.verified_at,
                )
            )
    return CorpusData(standards=tuple(standards), rules=tuple(rules), ruleset_version=ruleset_version)


def seed_standards(session: Session, root: Path | None = None) -> dict[str, int]:
    """Upsert the corpus into the registry. Returns created/updated counts.

    Idempotent: re-running with the same corpus updates the same rows.
    The caller owns the transaction (commit or roll back).
    """
    corpus = load_corpus(root)
    counts = {
        "sources_created": 0,
        "sources_updated": 0,
        "revisions_created": 0,
        "revisions_updated": 0,
        "rules_created": 0,
        "rules_updated": 0,
    }
    revision_rows: dict[str, StandardRevisionModel] = {}
    for seed in corpus.standards:
        code = str(seed.number)
        source = session.execute(
            select(StandardSourceModel).where(StandardSourceModel.standard_code == code)
        ).scalar_one_or_none()
        if source is None:
            source = StandardSourceModel(
                standard_code=code,
                title=seed.title,
                title_en=seed.title_en,
                ifrs_equivalent=seed.ifrs_equivalent,
                phase=seed.phase,
                slug=seed.slug,
                source_authority=SOURCE_AUTHORITY,
                source_url=seed.source_url,
            )
            session.add(source)
            counts["sources_created"] += 1
        else:
            source.title = seed.title
            source.title_en = seed.title_en
            source.ifrs_equivalent = seed.ifrs_equivalent
            source.phase = seed.phase
            source.slug = seed.slug
            source.source_authority = SOURCE_AUTHORITY
            source.source_url = seed.source_url
            counts["sources_updated"] += 1
        session.flush()

        revision = session.execute(
            select(StandardRevisionModel).where(
                StandardRevisionModel.source_id == source.id,
                StandardRevisionModel.revision_id == seed.revision_id,
            )
        ).scalar_one_or_none()
        if revision is None:
            revision = StandardRevisionModel(
                source_id=source.id,
                revision_id=seed.revision_id,
                title=seed.title,
                published_at=None,
                effective_from=seed.effective_from,
                effective_to=seed.effective_to,
                status="active",
                source_authority=SOURCE_AUTHORITY,
                source_url=seed.source_url,
                snapshot_uri=seed.snapshot_uri,
                sha256=seed.sha256,
                verification_status=seed.verification_status.value,
                verified_by=seed.verified_by,
                verified_at=seed.verified_at,
                notes=seed.notes,
            )
            session.add(revision)
            counts["revisions_created"] += 1
        else:
            revision.title = seed.title
            revision.effective_from = seed.effective_from
            revision.effective_to = seed.effective_to
            revision.source_authority = SOURCE_AUTHORITY
            revision.source_url = seed.source_url
            revision.snapshot_uri = seed.snapshot_uri
            revision.sha256 = seed.sha256
            revision.verification_status = seed.verification_status.value
            revision.verified_by = seed.verified_by
            revision.verified_at = seed.verified_at
            revision.notes = seed.notes
            counts["revisions_updated"] += 1
        session.flush()
        revision_rows[code] = revision

    std1_revision = revision_rows[RULES_STANDARD_CODE]
    for seed in corpus.rules:
        rule = session.execute(
            select(StandardRuleModel).where(
                StandardRuleModel.revision_id == std1_revision.id,
                StandardRuleModel.rule_id == seed.rule_id,
            )
        ).scalar_one_or_none()
        if rule is None:
            rule = StandardRuleModel(
                revision_id=std1_revision.id,
                rule_id=seed.rule_id,
                category=seed.category,
                description=seed.description,
                dsl_formula=seed.dsl_formula,
                rule_type=seed.rule_type,
                severity=seed.severity,
                standard_refs=list(seed.standard_refs),
                is_executable=seed.is_executable,
                ruleset_version=seed.ruleset_version,
                verification_status=seed.verification_status.value,
                verified_by=seed.verified_by,
                verified_at=seed.verified_at,
            )
            session.add(rule)
            counts["rules_created"] += 1
        else:
            rule.category = seed.category
            rule.description = seed.description
            rule.dsl_formula = seed.dsl_formula
            rule.rule_type = seed.rule_type
            rule.severity = seed.severity
            rule.standard_refs = list(seed.standard_refs)
            rule.is_executable = seed.is_executable
            rule.ruleset_version = seed.ruleset_version
            rule.verification_status = seed.verification_status.value
            rule.verified_by = seed.verified_by
            rule.verified_at = seed.verified_at
            counts["rules_updated"] += 1
    session.flush()
    return counts


def main(argv: list[str] | None = None) -> int:
    """Seed the configured database from the command line."""
    parser = argparse.ArgumentParser(description="Seed the HesabYar standards registry from the bundled corpus.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: derived from this file)",
    )
    args = parser.parse_args(argv)
    from hesabyar.infrastructure.config import get_settings

    settings = get_settings()
    engine = create_engine(settings.database.sqlalchemy_url)
    with Session(engine) as session:
        counts = seed_standards(session, args.repo_root)
        session.commit()
    print("Standards registry seeded:")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
