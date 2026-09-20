"""Safe typed expression evaluator for accounting validation rules.

This module implements a CLOSED rule DSL as required by ARCHITECTURE.md §9.3:
- NO eval(), exec(), or dynamic Python imports.
- Formulas are parsed via Python's ast module and restricted to a safe subset.
- Allowed AST node types: numbers, names (field references), binary arithmetic
  (+, -, *, /), unary negation, comparisons (==, !=, <, <=, >, >=), boolean
  operators (and, or), attribute access on Mapping/dict objects, and unary sum().
- Any disallowed construct (arbitrary function calls, arbitrary getattr/descriptor
  traversal, imports, etc.) yields RuleOutcome.ERROR — fail-closed.
- An unknown field or division by zero yields RuleOutcome.ERROR, never PASS.
- All financial calculations strictly use Decimal and int — binary float is forbidden.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from hesabyar.domain.standards.models import (
    RuleId,
    RuleOutcome,
    RuleResult,
    RuleSeverity,
)

# Allowed binary operators — maps AST op types to callables.
_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
}

# Allowed comparison operators.
_CMP_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# Allowed unary operators.
_UNARY_OPS: dict[type, Any] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# Allowed boolean operators.
_BOOL_OPS: dict[type, str] = {
    ast.And: "and",
    ast.Or: "or",
}

# AST node types that are safe to evaluate — anything else is rejected.
_SAFE_NODE_TYPES = frozenset({
    ast.Module,
    ast.Expression,
    ast.Expr,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.BoolOp,
    ast.Name,
    ast.Constant,
    ast.Load,
    ast.Attribute,
    ast.Call,
    # Operators (not evaluable themselves, but present in the tree).
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.And,
    ast.Or,
})


class UnsafeFormulaError(Exception):
    """Raised when a formula contains disallowed AST constructs."""


def _validate_ast(node: ast.AST) -> None:
    """Walk the AST and reject any node type not in the safe set."""
    if type(node) not in _SAFE_NODE_TYPES:
        raise UnsafeFormulaError(
            f"Disallowed AST node: {type(node).__name__}. "
            "Only arithmetic, comparisons, and boolean logic are permitted."
        )
    if isinstance(node, ast.Name):
        if node.id.startswith("_"):
            raise UnsafeFormulaError(f"Access to private/dunder identifier forbidden: {node.id}")
    elif isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise UnsafeFormulaError(f"Access to private/dunder attribute forbidden: {node.attr}")
    elif isinstance(node, ast.Call):
        if not (
            isinstance(node.func, ast.Name)
            and node.func.id == "sum"
            and len(node.args) == 1
            and not node.keywords
        ):
            raise UnsafeFormulaError(
                "Only unary sum(...) calls are permitted; arbitrary function calls are forbidden."
            )
    for child in ast.iter_child_nodes(node):
        _validate_ast(child)


def _safe_div(a: Decimal, b: Decimal) -> Decimal:
    """Division with zero-guard — raises ZeroDivisionError instead of silently producing Infinity."""
    if b == 0:
        raise ZeroDivisionError("Division by zero in rule formula")
    return a / b


def _to_decimal(value: Any) -> Decimal:
    """Coerce a numeric value to Decimal for safe arithmetic.

    Rejects bool, float, and non-numeric types.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("Boolean values are not valid in arithmetic expressions")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise TypeError(
            "Float values are forbidden in financial calculations. Use int or Decimal."
        )
    raise TypeError(f"Cannot coerce {type(value).__name__} to Decimal")


def _eval_node(node: ast.AST, context: Mapping[str, Any]) -> Any:
    """Recursively evaluate a validated AST node against the given context."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)

    if isinstance(node, ast.Constant):
        val = node.value
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return _to_decimal(val)
        raise UnsafeFormulaError(f"Unsupported constant type: {type(val).__name__}")

    if isinstance(node, ast.Name):
        name = node.id
        if name.startswith("_"):
            raise UnsafeFormulaError(f"Access to private/dunder identifier forbidden: {name}")
        if name not in context:
            raise KeyError(f"Unknown field: {name!r}")
        val = context[name]
        if isinstance(val, int) and not isinstance(val, bool):
            return Decimal(val)
        if isinstance(val, float):
            raise TypeError("Float values are forbidden in financial calculations. Use int or Decimal.")
        return val

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise UnsafeFormulaError(f"Access to private/dunder attribute forbidden: {node.attr}")
        target = _eval_node(node.value, context)
        if not isinstance(target, Mapping):
            raise UnsafeFormulaError(
                f"Attribute access only allowed on Mapping/dict structures, got {type(target).__name__}"
            )
        if node.attr not in target:
            raise KeyError(f"Field {node.attr!r} not found in {type(target).__name__}")
        val = target[node.attr]
        if isinstance(val, int) and not isinstance(val, bool):
            return Decimal(val)
        if isinstance(val, float):
            raise TypeError("Float values are forbidden in financial calculations. Use int or Decimal.")
        return val

    if isinstance(node, ast.Call):
        items = _eval_node(node.args[0], context)
        if not isinstance(items, (list, tuple)):
            raise TypeError(f"sum() argument must be a list or tuple, got {type(items).__name__}")
        total = Decimal(0)
        for item in items:
            if isinstance(item, Mapping):
                if "amount" not in item:
                    raise KeyError("Missing 'amount' key in item dict for sum()")
                amt = item["amount"]
                total += _to_decimal(amt)
            elif isinstance(item, (int, Decimal)):
                total += _to_decimal(item)
            elif isinstance(item, float):
                raise TypeError("Float values are forbidden in financial calculations. Use int or Decimal.")
            else:
                raise TypeError(f"Cannot sum item of type {type(item).__name__}")
        return total

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        if not isinstance(left, Decimal) or not isinstance(right, Decimal):
            raise TypeError(
                f"Arithmetic operands must be numeric/Decimal, got {type(left).__name__} and {type(right).__name__}"
            )
        if isinstance(node.op, ast.Div):
            return _safe_div(left, right)
        op_func = _BIN_OPS.get(type(node.op))
        if op_func is None:
            raise UnsafeFormulaError(f"Unsupported binary operator: {type(node.op).__name__}")
        return op_func(left, right)

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, context)
        if not isinstance(operand, Decimal):
            raise TypeError(f"Unary operand must be numeric/Decimal, got {type(operand).__name__}")
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            raise UnsafeFormulaError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op_func(operand)

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, context)
            cmp_func = _CMP_OPS.get(type(op))
            if cmp_func is None:
                raise UnsafeFormulaError(f"Unsupported comparison: {type(op).__name__}")
            if not cmp_func(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for v in node.values:
                res = _eval_node(v, context)
                if not isinstance(res, bool):
                    raise TypeError(f"Boolean operator requires boolean operands, got {type(res).__name__}")
                if not res:
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for v in node.values:
                res = _eval_node(v, context)
                if not isinstance(res, bool):
                    raise TypeError(f"Boolean operator requires boolean operands, got {type(res).__name__}")
                if res:
                    return True
            return False

    raise UnsafeFormulaError(f"Cannot evaluate node type: {type(node).__name__}")


def evaluate_expression(
    expr: str,
    context: Mapping[str, Any],
) -> Decimal | int | bool:
    """Parse and evaluate an expression string against the given context.

    Evaluates arithmetic, comparisons, and boolean logic using ONLY Decimal and int.
    Nested fields may be accessed via dot notation on Mapping/dict objects.
    Arbitrary getattr traversal is strictly forbidden.

    Returns Decimal, int, or bool.
    Raises UnsafeFormulaError for disallowed AST constructs or code injection attempts.
    Raises KeyError for missing fields or dictionary keys.
    Raises ZeroDivisionError for division by zero.
    Raises TypeError for float inputs or invalid operand types.
    """
    clean_expr = " ".join(expr.split())
    try:
        tree = ast.parse(clean_expr, mode="eval")
    except SyntaxError as exc:
        raise UnsafeFormulaError(f"Formula syntax error: {exc}") from exc

    _validate_ast(tree)
    result = _eval_node(tree, context)

    if isinstance(result, (Decimal, int, bool)):
        return result

    raise UnsafeFormulaError(
        f"Expression must evaluate to Decimal, int, or bool, got {type(result).__name__}"
    )


def evaluate_formula(
    formula: str,
    context: Mapping[str, Any],
) -> bool:
    """Parse and evaluate a formula string against the given field context.

    Returns True if the formula assertion holds, False otherwise.
    Raises UnsafeFormulaError for disallowed constructs or non-boolean results.
    Raises KeyError for missing fields.
    Raises ZeroDivisionError for division by zero.
    Raises TypeError for invalid operand types.
    """
    result = evaluate_expression(formula, context)

    if isinstance(result, bool):
        return result

    raise UnsafeFormulaError(
        f"Formula must evaluate to a boolean, got {type(result).__name__}"
    )


def evaluate_rule(
    rule_id: str,
    formula: str,
    context: Mapping[str, Any],
    severity: RuleSeverity = RuleSeverity.ERROR,
) -> RuleResult:
    """Evaluate a single validation rule. Fail-closed: any error yields ERROR, never PASS.

    This is the primary entry point for the standards validator.
    """
    rid = RuleId(rule_id)
    try:
        result = evaluate_formula(formula, context)
    except (UnsafeFormulaError, KeyError, ZeroDivisionError, TypeError, InvalidOperation) as exc:
        # Fail-closed: evaluation failure is always ERROR.
        return RuleResult(
            rule_id=rid,
            outcome=RuleOutcome.ERROR,
            severity=severity,
            message=f"Rule evaluation error: {exc}",
        )

    if result:
        return RuleResult(
            rule_id=rid,
            outcome=RuleOutcome.PASS,
            severity=severity,
            message="Rule assertion holds",
        )
    else:
        return RuleResult(
            rule_id=rid,
            outcome=RuleOutcome.FAIL,
            severity=severity,
            message="Rule assertion does not hold",
        )


__all__ = [
    "UnsafeFormulaError",
    "evaluate_expression",
    "evaluate_formula",
    "evaluate_rule",
]
