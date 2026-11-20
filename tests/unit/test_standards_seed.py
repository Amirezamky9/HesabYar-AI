"""Unit tests for the standards corpus loader + DSL port (US-401).

No database is needed here: the corpus loader reads the repository
files and the converter produces closed-DSL formulas, which the golden
dataset (tests/standards_fixtures.py) exercises end to end.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from hesabyar.application.commands.standards import _rule_context
from hesabyar.domain.standards.dsl import evaluate_rule
from hesabyar.domain.standards.models import RuleOutcome, RuleSeverity
from hesabyar.infrastructure.persistence.postgres.seed_standards import (
    STD15_EFFECTIVE_FROM,
    STD15_EFFECTIVE_TO,
    STD43_EFFECTIVE_FROM,
    STD44_EFFECTIVE_FROM,
    load_corpus,
    repo_root,
    to_dsl_formula,
)
from tests.standards_fixtures import BALANCED_DATA

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_root_resolves() -> None:
    assert repo_root() == REPO_ROOT
    assert (REPO_ROOT / "metadata.json").is_file()
    assert (REPO_ROOT / "validators" / "rules.yaml").is_file()


def test_corpus_loads_35_standards_and_36_rules() -> None:
    corpus = load_corpus()
    assert len(corpus.standards) == 35
    assert len(corpus.rules) == 36
    assert corpus.ruleset_version == "1.0.0-legacy"
    codes = sorted(standard.number for standard in corpus.standards)
    assert codes == [
        1,
        2,
        4,
        5,
        8,
        10,
        11,
        12,
        13,
        15,
        16,
        17,
        18,
        20,
        22,
        24,
        25,
        26,
        27,
        28,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
        44,
    ]


def test_corpus_verification_statuses_follow_current_rules_1405() -> None:
    corpus = load_corpus()
    by_number = {standard.number: standard for standard in corpus.standards}
    assert by_number[43].verification_status.value == "VERIFIED_TEXT"
    assert by_number[44].verification_status.value == "VERIFIED_TEXT"
    assert by_number[15].verification_status.value == "BLOCKED_SOURCE_GAP"
    others = [standard for standard in corpus.standards if standard.number not in (15, 43, 44)]
    assert len(others) == 32
    assert {s.verification_status.value for s in others} == {"UNVERIFIED"}


def test_corpus_effective_dates_follow_current_rules_1405() -> None:
    corpus = load_corpus()
    by_number = {standard.number: standard for standard in corpus.standards}
    assert by_number[43].effective_from == STD43_EFFECTIVE_FROM
    assert by_number[43].effective_from == date(2025, 3, 21)  # 1404/01/01
    assert by_number[44].effective_from == STD44_EFFECTIVE_FROM
    assert by_number[44].effective_from == date(2026, 3, 21)  # 1405/01/01
    assert by_number[15].effective_from == STD15_EFFECTIVE_FROM
    assert by_number[15].effective_from == date(2001, 3, 21)  # 1380/01/01
    assert by_number[15].effective_to == STD15_EFFECTIVE_TO
    assert by_number[15].effective_to == date(2026, 3, 20)  # 1404/12/29
    assert by_number[1].effective_from is None
    assert by_number[1].effective_to is None


def test_corpus_revision_ids() -> None:
    corpus = load_corpus()
    by_number = {standard.number: standard for standard in corpus.standards}
    # Header years win where the text states one; Standard 43 is the
    # documented override (frontmatter 1397 is stale, text is the 1404 revision).
    assert by_number[1].revision_id == "1397"
    assert by_number[15].revision_id == "1399"
    assert by_number[20].revision_id == "1398"
    assert by_number[22].revision_id == "1400"
    assert by_number[43].revision_id == "1404"
    assert by_number[44].revision_id == "1404"


def test_corpus_snapshot_hashes_match_source_files() -> None:
    corpus = load_corpus()
    for standard in corpus.standards:
        path = REPO_ROOT / standard.snapshot_uri
        assert path.is_file(), standard.snapshot_uri
        assert hashlib.sha256(path.read_bytes()).hexdigest() == standard.sha256, standard.snapshot_uri


def test_corpus_rules_all_belong_to_standard_1() -> None:
    corpus = load_corpus()
    assert all(rule.verification_status.value == "UNVERIFIED" for rule in corpus.rules)
    assert {rule.ruleset_version for rule in corpus.rules} == {"1.0.0-legacy"}


def test_dsl_formulas_contain_no_float_literals() -> None:
    """The DSL rejects float literals (fail-closed), so the port must not emit any."""
    import ast

    corpus = load_corpus()
    for rule in corpus.rules:
        if rule.dsl_formula is None:
            continue
        tree = ast.parse(rule.dsl_formula, mode="eval")
        floats = [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, float)]
        assert floats == [], f"{rule.rule_id}: float literal in {rule.dsl_formula}"


def test_converter_equation_rule() -> None:
    formula = to_dsl_formula(
        {
            "id": "BS001",
            "formula": "total_assets - (total_liabilities + total_equity)",
            "expected": 0,
            "tolerance": 0.01,
        }
    )
    assert formula == (
        "(total_assets - (total_liabilities + total_equity) - 0) <= 1/100 "
        "and (total_assets - (total_liabilities + total_equity) - 0) >= -1/100"
    )


def test_converter_threshold_rules() -> None:
    assert to_dsl_formula({"id": "BS-W01", "formula": "a / b", "min": 1.0}) == "a / b >= 1"
    assert to_dsl_formula({"id": "BS-W03", "formula": "a / b", "max": 0.7}) == "a / b <= 7/10"


def test_converter_requirement_rule_has_no_formula() -> None:
    assert to_dsl_formula({"id": "CF005", "requirement": "x"}) is None


def test_converter_rejects_rule_without_assertion() -> None:
    with pytest.raises(ValueError, match="no expected/min/max"):
        to_dsl_formula({"id": "XX001", "formula": "a + b"})


def test_golden_data_passes_all_executable_rules() -> None:
    """Every executable ported rule PASSES on the balanced dataset.

    This is the golden test for the DSL port: it proves the converted
    formulas are mutually consistent under the per-category statement
    scoping (a flat-only reading contradicts IS007 vs CI002).
    """
    corpus = load_corpus()
    executable = [rule for rule in corpus.rules if rule.is_executable]
    assert len(executable) == 35
    failures: list[str] = []
    for rule in executable:
        assert rule.dsl_formula is not None
        result = evaluate_rule(
            rule.rule_id,
            rule.dsl_formula,
            _rule_context(BALANCED_DATA, rule.category),
            severity=RuleSeverity(rule.severity),
        )
        if result.outcome != RuleOutcome.PASS:
            failures.append(f"{rule.rule_id}: {result.outcome} ({result.message})")
    assert failures == []


def test_golden_data_unbalanced_balance_sheet_fails_bs001() -> None:
    data = {
        **BALANCED_DATA,
        "balance_sheet": {**BALANCED_DATA["balance_sheet"], "total_assets": 999},
    }
    corpus = load_corpus()
    rule = next(r for r in corpus.rules if r.rule_id == "BS001")
    assert rule.dsl_formula is not None
    result = evaluate_rule(
        rule.rule_id,
        rule.dsl_formula,
        _rule_context(data, rule.category),
        severity=RuleSeverity(rule.severity),
    )
    assert result.outcome == RuleOutcome.FAIL


def test_golden_data_missing_field_errors_fail_closed() -> None:
    """A rule whose data is missing yields ERROR, never PASS."""
    data = {"balance_sheet": {"total_assets": 1000}}
    corpus = load_corpus()
    rule = next(r for r in corpus.rules if r.rule_id == "BS001")
    assert rule.dsl_formula is not None
    result = evaluate_rule(
        rule.rule_id,
        rule.dsl_formula,
        _rule_context(data, rule.category),
        severity=RuleSeverity(rule.severity),
    )
    assert result.outcome == RuleOutcome.ERROR


def test_rule_context_merges_statement_slice_over_document() -> None:
    data = {
        "owners_of_parent": 1,
        "income_statement": {"owners_of_parent": 200},
        "comprehensive_income": {"owners_of_parent": 240},
    }
    assert _rule_context(data, "income_statement")["owners_of_parent"] == 200
    assert _rule_context(data, "comprehensive_income")["owners_of_parent"] == 240
    # Cross-checks see the whole document, dotted paths included.
    context = _rule_context(data, "cross_checks")
    assert context["income_statement"]["owners_of_parent"] == 200
    assert context["owners_of_parent"] == 1


def test_rule_context_missing_slice_leaves_document_unchanged() -> None:
    data = {"total_assets": 1000}
    assert _rule_context(data, "balance_sheet") == data
    assert _rule_context(data, "unknown_category") == data
