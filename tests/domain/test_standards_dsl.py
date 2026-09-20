"""Tests for the safe typed standards validator DSL.

Covers:
- Valid formula evaluation (balanced equation, arithmetic)
- Invalid formula detection (equation doesn't hold)
- Syntax/type errors yield ERROR, never PASS (fail-closed)
- Code injection regression tests (__import__, eval, exec, attribute access, etc.)
- Division by zero handling
- Missing field handling
- Boolean logic (and/or)
- Comparison operators
- ValidationReport aggregation
"""

from __future__ import annotations

from decimal import Decimal

from hesabyar.domain.standards.dsl import (
    evaluate_expression,
    evaluate_formula,
    evaluate_rule,
)
from hesabyar.domain.standards.models import (
    RuleOutcome,
    RuleSeverity,
    ValidationReport,
)

# ==============================================================================
# Valid formula evaluation
# ==============================================================================


class TestValidFormulas:
    def test_balanced_accounting_equation(self) -> None:
        """assets == liabilities + equity must pass when balanced."""
        context = {"assets": 1_000_000, "liabilities": 600_000, "equity": 400_000}
        assert evaluate_formula("assets == liabilities + equity", context) is True

    def test_unbalanced_accounting_equation(self) -> None:
        """assets == liabilities + equity must fail when unbalanced."""
        context = {"assets": 1_000_000, "liabilities": 600_000, "equity": 300_000}
        assert evaluate_formula("assets == liabilities + equity", context) is False

    def test_subtraction(self) -> None:
        context = {"net_income": 200, "revenue": 500, "expenses": 300}
        assert evaluate_formula("net_income == revenue - expenses", context) is True

    def test_multiplication(self) -> None:
        context = {"total": 1000, "quantity": 10, "price": 100}
        assert evaluate_formula("total == quantity * price", context) is True

    def test_division(self) -> None:
        context = {"ratio": 5, "numerator": 100, "denominator": 20}
        assert evaluate_formula("ratio == numerator / denominator", context) is True

    def test_nested_arithmetic(self) -> None:
        context = {"result": 350, "a": 100, "b": 200, "c": 50}
        assert evaluate_formula("result == a + b + c", context) is True

    def test_unary_negation(self) -> None:
        context = {"loss": -100, "profit": 100}
        assert evaluate_formula("loss == -profit", context) is True

    def test_comparison_less_than(self) -> None:
        context = {"debt_ratio": 40, "max_ratio": 60}
        assert evaluate_formula("debt_ratio < max_ratio", context) is True

    def test_comparison_greater_equal(self) -> None:
        context = {"current_ratio": 150, "minimum": 100}
        assert evaluate_formula("current_ratio >= minimum", context) is True

    def test_boolean_and(self) -> None:
        context = {"assets": 1000, "liabilities": 400, "equity": 600, "cash": 200, "min_cash": 100}
        assert evaluate_formula(
            "assets == liabilities + equity and cash >= min_cash", context
        ) is True

    def test_boolean_or(self) -> None:
        context = {"ratio": 80, "min_a": 90, "min_b": 70}
        assert evaluate_formula("ratio >= min_a or ratio >= min_b", context) is True

    def test_integer_constants_in_formula(self) -> None:
        context = {"total": 100}
        assert evaluate_formula("total == 100", context) is True

    def test_chained_comparison(self) -> None:
        context = {"ratio": 50}
        assert evaluate_formula("0 <= ratio <= 100", context) is True


# ==============================================================================
# Invalid formula detection (formula evaluates but assertion doesn't hold)
# ==============================================================================


class TestInvalidFormulas:
    def test_unbalanced_equation_fails(self) -> None:
        context = {"assets": 500, "liabilities": 200, "equity": 100}
        assert evaluate_formula("assets == liabilities + equity", context) is False

    def test_ratio_exceeds_threshold(self) -> None:
        context = {"debt_ratio": 90, "max_ratio": 60}
        assert evaluate_formula("debt_ratio < max_ratio", context) is False


# ==============================================================================
# Fail-closed: errors yield ERROR, never PASS
# ==============================================================================


class TestFailClosed:
    def test_missing_field_yields_error(self) -> None:
        result = evaluate_rule("BS001", "assets == liabilities + equity", {"assets": 100})
        assert result.outcome == RuleOutcome.ERROR
        assert result.severity == RuleSeverity.ERROR
        assert "Unknown field" in result.message

    def test_syntax_error_yields_error(self) -> None:
        result = evaluate_rule("BS002", "assets ==== liabilities", {"assets": 100, "liabilities": 100})
        assert result.outcome == RuleOutcome.ERROR
        assert "syntax" in result.message.lower() or "error" in result.message.lower()

    def test_division_by_zero_yields_error(self) -> None:
        result = evaluate_rule("BS003", "ratio == a / b", {"ratio": 0, "a": 100, "b": 0})
        assert result.outcome == RuleOutcome.ERROR
        assert "zero" in result.message.lower()

    def test_float_value_yields_error(self) -> None:
        """Float values are forbidden in financial calculations (ARCHITECTURE.md §6)."""
        result = evaluate_rule("BS004", "total == amount", {"total": 100.5, "amount": 100.5})
        assert result.outcome == RuleOutcome.ERROR
        assert "float" in result.message.lower() or "Float" in result.message

    def test_mandatory_rule_error_never_passes(self) -> None:
        """A mandatory rule (severity=ERROR) that cannot be evaluated MUST yield ERROR."""
        result = evaluate_rule(
            "MANDATORY001",
            "assets == liabilities + equity",
            {},  # Empty context — all fields missing.
            severity=RuleSeverity.ERROR,
        )
        assert result.outcome == RuleOutcome.ERROR
        assert result.outcome != RuleOutcome.PASS


# ==============================================================================
# Security: code injection regression tests
# ==============================================================================


class TestCodeInjectionPrevention:
    """Prove that the DSL is safe against arbitrary code execution."""

    def test_import_rejected(self) -> None:
        result = evaluate_rule("SEC001", "__import__('os').system('id')", {})
        assert result.outcome == RuleOutcome.ERROR

    def test_eval_string_rejected(self) -> None:
        result = evaluate_rule("SEC002", "eval('1+1')", {})
        assert result.outcome == RuleOutcome.ERROR

    def test_exec_rejected(self) -> None:
        result = evaluate_rule("SEC003", "exec('x=1')", {})
        assert result.outcome == RuleOutcome.ERROR

    def test_attribute_access_rejected(self) -> None:
        result = evaluate_rule("SEC004", "x.__class__.__bases__", {"x": 1})
        assert result.outcome == RuleOutcome.ERROR

    def test_subscript_rejected(self) -> None:
        result = evaluate_rule("SEC005", "data[0]", {"data": [1, 2, 3]})
        assert result.outcome == RuleOutcome.ERROR

    def test_lambda_rejected(self) -> None:
        result = evaluate_rule("SEC006", "(lambda: 1)()", {})
        assert result.outcome == RuleOutcome.ERROR

    def test_comprehension_rejected(self) -> None:
        result = evaluate_rule("SEC007", "[x for x in range(10)]", {})
        assert result.outcome == RuleOutcome.ERROR

    def test_walrus_operator_rejected(self) -> None:
        result = evaluate_rule("SEC008", "(x := 42) == 42", {"x": 0})
        assert result.outcome == RuleOutcome.ERROR

    def test_fstring_rejected(self) -> None:
        """f-strings are JoinedStr AST nodes — disallowed."""
        result = evaluate_rule("SEC009", "f'{__import__(\"os\")}'", {})
        assert result.outcome == RuleOutcome.ERROR

    def test_dunder_name_in_context_rejected(self) -> None:
        """Even if __builtins__ is in context, calls are still blocked."""
        result = evaluate_rule("SEC010", "__builtins__", {"__builtins__": {}})
        # This succeeds in reading the name but the result is a dict, not bool,
        # so evaluate_formula raises UnsafeFormulaError → ERROR.
        assert result.outcome == RuleOutcome.ERROR

    def test_nested_call_rejected(self) -> None:
        result = evaluate_rule("SEC011", "int('42') == 42", {})
        assert result.outcome == RuleOutcome.ERROR


# ==============================================================================
# evaluate_rule integration tests
# ==============================================================================


class TestEvaluateRule:
    def test_passing_rule(self) -> None:
        result = evaluate_rule(
            "BS001",
            "assets == liabilities + equity",
            {"assets": 1000, "liabilities": 400, "equity": 600},
        )
        assert result.outcome == RuleOutcome.PASS
        assert result.rule_id == "BS001"
        assert result.is_pass is True

    def test_failing_rule(self) -> None:
        result = evaluate_rule(
            "BS001",
            "assets == liabilities + equity",
            {"assets": 1000, "liabilities": 400, "equity": 500},
        )
        assert result.outcome == RuleOutcome.FAIL
        assert result.is_failure is True

    def test_warning_severity_rule(self) -> None:
        result = evaluate_rule(
            "W001",
            "ratio < 100",
            {"ratio": 150},
            severity=RuleSeverity.WARNING,
        )
        assert result.outcome == RuleOutcome.FAIL
        assert result.severity == RuleSeverity.WARNING


# ==============================================================================
# ValidationReport aggregation tests
# ==============================================================================


class TestValidationReport:
    def test_empty_report_is_valid(self) -> None:
        report = ValidationReport()
        assert report.is_valid is True
        assert report.total == 0

    def test_all_passing(self) -> None:
        report = ValidationReport()
        report.add(evaluate_rule("R1", "a == 1", {"a": 1}))
        report.add(evaluate_rule("R2", "b == 2", {"b": 2}))
        assert report.is_valid is True
        assert report.passed == 2
        assert report.failed == 0
        assert report.errors == 0

    def test_error_severity_failure_invalidates_report(self) -> None:
        report = ValidationReport()
        report.add(evaluate_rule("R1", "a == 1", {"a": 1}))
        report.add(evaluate_rule("R2", "a == 99", {"a": 1}, severity=RuleSeverity.ERROR))
        assert report.is_valid is False
        assert report.failed == 1

    def test_warning_severity_failure_does_not_invalidate(self) -> None:
        report = ValidationReport()
        report.add(evaluate_rule("R1", "a == 1", {"a": 1}))
        report.add(evaluate_rule("W1", "a == 99", {"a": 1}, severity=RuleSeverity.WARNING))
        assert report.is_valid is True  # Warning failures don't invalidate.

    def test_error_outcome_invalidates_report(self) -> None:
        report = ValidationReport()
        report.add(evaluate_rule("R1", "a == missing_field", {"a": 1}))
        assert report.is_valid is False
        assert report.errors == 1


# ==============================================================================
# Decimal precision preservation & Zero float policy
# ==============================================================================


class TestDecimalPrecision:
    def test_decimal_precision_preservation(self) -> None:
        """0.1 + 0.2 == 0.3 must hold with Decimal (which fails with binary float)."""
        context = {
            "a": Decimal("0.1"),
            "b": Decimal("0.2"),
            "c": Decimal("0.3"),
        }
        assert evaluate_formula("a + b == c", context) is True
        assert evaluate_expression("a + b", context) == Decimal("0.3")

    def test_decimal_fractional_precision(self) -> None:
        """Verify high-precision decimals are preserved without float truncation."""
        context = {
            "val1": Decimal("1000000000.123456789"),
            "val2": Decimal("2000000000.987654321"),
            "expected": Decimal("3000000001.111111110"),
        }
        assert evaluate_formula("val1 + val2 == expected", context) is True

    def test_decimal_division_exactness(self) -> None:
        context = {
            "num": Decimal("1"),
            "den": Decimal("4"),
            "expected": Decimal("0.25"),
        }
        assert evaluate_formula("num / den == expected", context) is True
        assert evaluate_expression("num / den", context) == Decimal("0.25")


# ==============================================================================
# Nested field traversal via dot notation (Mapping/dict only)
# ==============================================================================


class TestNestedFieldTraversal:
    def test_nested_dict_access_single_level(self) -> None:
        context = {
            "balance_sheet": {
                "cash_and_equivalents": Decimal("500"),
                "total_assets": Decimal("500"),
            }
        }
        assert evaluate_formula("balance_sheet.cash_and_equivalents == balance_sheet.total_assets", context) is True
        assert evaluate_expression("balance_sheet.cash_and_equivalents", context) == Decimal("500")

    def test_nested_dict_access_multi_level(self) -> None:
        context = {
            "equity_changes": {
                "closing_balance": {
                    "total_equity": Decimal("1000"),
                }
            },
            "target": Decimal("1000"),
        }
        assert evaluate_formula("equity_changes.closing_balance.total_equity == target", context) is True
        assert evaluate_expression("equity_changes.closing_balance.total_equity", context) == Decimal("1000")

    def test_nested_dict_sum(self) -> None:
        context = {
            "statement": {
                "items": [
                    {"amount": Decimal("100")},
                    {"amount": Decimal("200")},
                ]
            },
            "total": Decimal("300"),
        }
        assert evaluate_formula("sum(statement.items) == total", context) is True


# ==============================================================================
# Strict attribute access restriction: non-dict traversal rejected fail-closed
# ==============================================================================


class TestStrictAttributeAccessRestriction:
    def test_attribute_access_on_primitive_rejected(self) -> None:
        """Accessing attributes on integer or primitive fails closed."""
        result = evaluate_rule("SEC012", "x.__class__ == 1", {"x": 10})
        assert result.outcome == RuleOutcome.ERROR

    def test_attribute_access_on_custom_object_rejected(self) -> None:
        """Arbitrary getattr on custom class instance is strictly forbidden."""
        class MockAccount:
            balance = Decimal("100")

        context = {"account": MockAccount()}
        result = evaluate_rule("SEC013", "account.balance == 100", context)
        assert result.outcome == RuleOutcome.ERROR
        assert "Mapping/dict" in result.message or "Attribute" in result.message

    def test_attribute_access_on_builtin_object_rejected(self) -> None:
        """Accessing attributes on built-in objects (e.g. string/list methods) is forbidden."""
        context = {"s": "hello"}
        result = evaluate_rule("SEC014", "s.upper == 1", context)
        assert result.outcome == RuleOutcome.ERROR

    def test_private_attribute_access_on_dict_rejected(self) -> None:
        """Accessing private or dunder attributes on dict is forbidden."""
        context = {"d": {"key": 1}}
        result = evaluate_rule("SEC015", "d.__class__ == 1", context)
        assert result.outcome == RuleOutcome.ERROR

    def test_missing_nested_key_fails_closed(self) -> None:
        context = {"balance_sheet": {"cash": 100}}
        result = evaluate_rule("BS010", "balance_sheet.missing_field == 100", context)
        assert result.outcome == RuleOutcome.ERROR
        assert "not found" in result.message.lower() or "missing" in result.message.lower()

