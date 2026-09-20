# -*- coding: utf-8 -*-
"""
موتور اعتبارسنجی صورت‌های مالی
بر اساس استاندارد حسابداری ۱ و قواعد cross-standard
"""
import logging
# ==================== تنظیم UTF-8 ====================
import ast
import io
import logging
import operator
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)

# مجبور کردن stdout/stderr به UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace"
    )


# ==================== انواع ====================
class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationResult:
    """نتیجه یک اعتبارسنجی"""
    rule_id: str
    description: str
    severity: Severity
    passed: bool
    message: str = ""
    expected: Any = None
    actual: Any = None
    diff: Any = None
    refs: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "severity": self.severity.value,
            "passed": self.passed,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
            "diff": self.diff,
            "refs": self.refs,
        }


@dataclass
class ValidationReport:
    """گزارش کامل اعتبارسنجی"""
    total_rules: int = 0
    passed: int = 0
    failed: int = 0
    errors: List[ValidationResult] = field(default_factory=list)
    warnings: List[ValidationResult] = field(default_factory=list)
    infos: List[ValidationResult] = field(default_factory=list)
    results: List[ValidationResult] = field(default_factory=list)
    
    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0
    
    def add(self, result: ValidationResult) -> None:
        self.total_rules += 1
        self.results.append(result)
        
        if result.passed:
            self.passed += 1
        else:
            self.failed += 1
            if result.severity == Severity.ERROR:
                self.errors.append(result)
            elif result.severity == Severity.WARNING:
                self.warnings.append(result)
            else:
                self.infos.append(result)
    
    def to_dict(self) -> Dict:
        return {
            "summary": {
                "total_rules": self.total_rules,
                "passed": self.passed,
                "failed": self.failed,
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "infos": len(self.infos),
                "is_valid": self.is_valid,
            },
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "infos": [i.to_dict() for i in self.infos],
        }
    
    def to_markdown(self) -> str:
        """تولید گزارش Markdown"""
        lines = ["# گزارش اعتبارسنجی صورت‌های مالی", ""]
        
        # خلاصه
        lines.append("## 📊 خلاصه")
        lines.append("")
        lines.append(f"- **کل قواعد:** {self.total_rules}")
        lines.append(f"- **موفق:** {self.passed} ✅")
        lines.append(f"- **ناموفق:** {self.failed} ❌")
        lines.append(f"- **خطاها:** {len(self.errors)}")
        lines.append(f"- **هشدارها:** {len(self.warnings)}")
        lines.append(f"- **وضعیت کلی:** {'✅ معتبر' if self.is_valid else '❌ نامعتبر'}")
        lines.append("")
        
        # خطاها
        if self.errors:
            lines.append("## ❌ خطاها")
            lines.append("")
            for err in self.errors:
                lines.append(f"### {err.rule_id}: {err.description}")
                lines.append(f"- **پیام:** {err.message}")
                if err.expected is not None:
                    lines.append(f"- **مقدار مورد انتظار:** {err.expected}")
                if err.actual is not None:
                    lines.append(f"- **مقدار واقعی:** {err.actual}")
                if err.diff is not None:
                    lines.append(f"- **اختلاف:** {err.diff}")
                lines.append("")
        
        # هشدارها
        if self.warnings:
            lines.append("## ⚠️ هشدارها")
            lines.append("")
            for w in self.warnings:
                lines.append(f"- **{w.rule_id}:** {w.message}")
            lines.append("")
        
        return "\n".join(lines)


class UnsafeFormulaError(Exception):
    """Raised when a formula contains disallowed AST constructs or malicious code."""


_SAFE_AST_NODES: frozenset[type] = frozenset({
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Compare,
    ast.Load,
    # Allowed operators
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
})

_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
}

_CMP_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

_UNARY_OPS: dict[type, Any] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _validate_ast_tree(node: ast.AST) -> None:
    if type(node) not in _SAFE_AST_NODES:
        raise UnsafeFormulaError(
            f"Disallowed AST node: {type(node).__name__}. "
            "Only arithmetic and safe comparisons are permitted."
        )
    for child in ast.iter_child_nodes(node):
        _validate_ast_tree(child)


def _safe_div(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError("Division by zero in rule formula")
    return a / b


def _eval_ast_node(node: ast.AST, context: Dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body, context)

    if isinstance(node, ast.Constant):
        val = node.value
        if isinstance(val, bool):
            raise UnsafeFormulaError("Boolean constants are not permitted in arithmetic expressions")
        if isinstance(val, (int, float, Decimal)):
            return float(val)
        raise UnsafeFormulaError(f"Unsupported constant type: {type(val).__name__}")

    if isinstance(node, ast.Name):
        name = node.id
        if name.startswith("_"):
            raise UnsafeFormulaError(f"Access to private/dunder identifier forbidden: {name}")
        if name not in context:
            raise KeyError(f"Field not found in context: {name!r}")
        val = context[name]
        if isinstance(val, (int, float, Decimal)):
            return float(val)
        return val

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise UnsafeFormulaError(f"Access to private/dunder attribute forbidden: {node.attr}")
        target = _eval_ast_node(node.value, context)
        if isinstance(target, dict):
            if node.attr not in target:
                raise KeyError(f"Key {node.attr!r} not found in dict")
            val = target[node.attr]
            if isinstance(val, (int, float, Decimal)):
                return float(val)
            return val
        if hasattr(target, node.attr):
            val = getattr(target, node.attr)
            if isinstance(val, (int, float, Decimal)):
                return float(val)
            return val
        raise KeyError(f"Attribute {node.attr!r} not found on target")

    if isinstance(node, ast.BinOp):
        left = _eval_ast_node(node.left, context)
        right = _eval_ast_node(node.right, context)
        if not isinstance(left, (int, float, Decimal)) or not isinstance(right, (int, float, Decimal)):
            raise TypeError(f"Arithmetic operands must be numbers, got {type(left).__name__} and {type(right).__name__}")
        if isinstance(node.op, ast.Div):
            return _safe_div(float(left), float(right))
        op_func = _BIN_OPS.get(type(node.op))
        if op_func is None:
            raise UnsafeFormulaError(f"Unsupported binary operator: {type(node.op).__name__}")
        return float(op_func(left, right))

    if isinstance(node, ast.UnaryOp):
        operand = _eval_ast_node(node.operand, context)
        if not isinstance(operand, (int, float, Decimal)):
            raise TypeError(f"Unary operand must be a number, got {type(operand).__name__}")
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            raise UnsafeFormulaError(f"Unsupported unary operator: {type(node.op).__name__}")
        return float(op_func(operand))

    if isinstance(node, ast.Compare):
        left = _eval_ast_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_ast_node(comparator, context)
            cmp_func = _CMP_OPS.get(type(op))
            if cmp_func is None:
                raise UnsafeFormulaError(f"Unsupported comparison: {type(op).__name__}")
            if not cmp_func(left, right):
                return False
            left = right
        return True

    raise UnsafeFormulaError(f"Cannot evaluate AST node: {type(node).__name__}")


# ==================== موتور اعتبارسنجی ====================
class FinancialValidator:
    """موتور اعتبارسنجی صورت‌های مالی"""

    def __init__(self, rules_path: Optional[Path] = None):
        """
        Args:
            rules_path: مسیر فایل قواعد YAML
        """
        if rules_path is None:
            rules_path = Path(__file__).parent.parent / "validators" / "rules.yaml"

        self.rules_path = rules_path
        self.rules = self._load_rules()
        self.tolerance = 0.01  # تلورانس پیش‌فرض
        self._last_eval_error: Optional[str] = None
    
    def _load_rules(self) -> Dict:
        """بارگذاری قواعد از YAML"""
        if not self.rules_path.exists():
            logger.warning(f"Rules file not found: {self.rules_path}")
            return {}
        
        with open(self.rules_path, "r", encoding="utf-8") as f:
            rules = yaml.safe_load(f)
        
        logger.info(f"Loaded rules from {self.rules_path}")
        return rules or {}
    
    def _safe_get(self, data: Any, path: str, default: Any = 0) -> Any:
        """
        دریافت امن مقدار از ساختار تودرتو
        پشتیبانی از: obj.attr, obj['key'], obj[0]
        """
        if data is None:
            return default

        parts = path.replace("[", ".").replace("]", "").split(".")
        current = data

        for part in parts:
            if not part or part.startswith("_"):
                return default

            # اگه دیکشنری بود
            if isinstance(current, dict):
                current = current.get(part, default)
            # اگه آبجکت با attribute بود
            elif hasattr(current, part):
                current = getattr(current, part, default)
            else:
                return default

            if current is None:
                return default

        return current

    def _expand_sum(self, formula: str, context: Dict) -> str:
        """جایگزینی sum(...) با مقدار"""
        def replace_sum(match: re.Match) -> str:
            items_key = match.group(1).strip()
            if not all(part.isidentifier() for part in items_key.split(".")):
                raise UnsafeFormulaError(f"Invalid sum target: {items_key}")
            items = self._safe_get(context, items_key, default=[])
            if isinstance(items, list):
                total = 0.0
                for item in items:
                    if isinstance(item, dict):
                        total += float(item.get("amount", 0))
                    elif isinstance(item, (int, float, Decimal)):
                        total += float(item)
                return str(total)
            return "0"

        return re.sub(r"sum\(([^)]+)\)", replace_sum, formula)

    def _evaluate_formula_ast(self, formula: str, context: Dict) -> float:
        """
        ارزیابی امن یک فرمول محاسباتی با تحلیل AST.
        هیچ‌گونه eval()، exec() یا ایمپورت مجاز نیست.
        """
        formula = " ".join(formula.split())
        formula = self._expand_sum(formula, context)
        try:
            tree = ast.parse(formula, mode="eval")
        except SyntaxError as exc:
            raise UnsafeFormulaError(f"Formula syntax error: {exc}") from exc

        _validate_ast_tree(tree)
        val = _eval_ast_node(tree.body, context)
        if isinstance(val, bool):
            raise UnsafeFormulaError("Formula must evaluate to a numeric value, not boolean")
        if isinstance(val, (int, float, Decimal)):
            return float(val)
        raise UnsafeFormulaError(f"Formula evaluated to non-numeric type: {type(val).__name__}")

    def _evaluate_formula(self, formula: str, context: Dict) -> Optional[float]:
        """
        ارزیابی یک فرمول با موتور امن AST (بدون eval).

        پشتیبانی از:
        - a + b - c
        - a * b / c
        - sum(items)
        - (a + b) - (c + d)
        - مسیرهای تودرتو: x.y - z
        """
        try:
            val = self._evaluate_formula_ast(formula, context)
            self._last_eval_error = None
            return val
        except Exception as e:
            self._last_eval_error = str(e)
            logger.debug(f"Formula evaluation failed: {formula} - {e}")
            return None

    def _validate_rule(self, rule: Dict, context: Dict, category: str) -> ValidationResult:
        """اعتبارسنجی یک قاعده"""
        rule_id = rule.get("id", "UNKNOWN")
        description = rule.get("description", "")
        severity = Severity(rule.get("severity", "error"))
        refs = rule.get("refs", [])
        message = rule.get("message", "")

        # بررسی applies_to
        applies_to = rule.get("applies_to")
        if applies_to:
            method = context.get("classification_method") or context.get("method")
            if method != applies_to:
                return ValidationResult(
                    rule_id=rule_id,
                    description=description,
                    severity=severity,
                    passed=True,
                    message=f"Skipped (applies to {applies_to})",
                    refs=refs,
                )

        # بررسی requirement (بدون فرمول)
        requirement = rule.get("requirement")
        if requirement:
            value = self._safe_get(context, requirement, default=None)
            passed = bool(value)
            return ValidationResult(
                rule_id=rule_id,
                description=description,
                severity=severity,
                passed=passed,
                message=message if not passed else "",
                expected=True,
                actual=value,
                refs=refs,
            )

        # ارزیابی فرمول
        formula = rule.get("formula")
        if not formula:
            if severity == Severity.ERROR:
                return ValidationResult(
                    rule_id=rule_id,
                    description=description,
                    severity=severity,
                    passed=False,
                    message="Mandatory rule missing executable formula or requirement",
                    refs=refs,
                )
            return ValidationResult(
                rule_id=rule_id,
                description=description,
                severity=severity,
                passed=True,
                message="No formula",
                refs=refs,
            )

        # پاکسازی فرمول (خطوط چندگانه)
        formula = " ".join(formula.split())

        result = self._evaluate_formula(formula, context)

        if result is None:
            err_detail = getattr(self, "_last_eval_error", None) or f"could not evaluate: {formula[:50]}"
            if severity == Severity.ERROR:
                return ValidationResult(
                    rule_id=rule_id,
                    description=description,
                    severity=severity,
                    passed=False,
                    message=f"Formula evaluation error: {err_detail}",
                    refs=refs,
                )
            return ValidationResult(
                rule_id=rule_id,
                description=description,
                severity=severity,
                passed=True,
                message=f"Could not evaluate: {formula[:50]}",
                refs=refs,
            )

        # بررسی expected
        expected = rule.get("expected", 0)
        tolerance = rule.get("tolerance", self.tolerance)

        if "expected" in rule:
            passed = abs(result - expected) <= tolerance
        elif "min" in rule:
            passed = result >= rule["min"]
        elif "max" in rule:
            passed = result <= rule["max"]
        else:
            passed = abs(result) <= tolerance

        return ValidationResult(
            rule_id=rule_id,
            description=description,
            severity=severity,
            passed=passed,
            message="" if passed else message.format(diff=result, value=result),
            expected=expected if "expected" in rule else rule.get("min") or rule.get("max"),
            actual=result,
            diff=result if not passed else None,
            refs=refs,
        )
    
    def validate_category(self, category: str, context: Dict) -> List[ValidationResult]:
        """اعتبارسنجی یک دسته از قواعد"""
        rules = self.rules.get(category, [])
        results = []
        
        for rule in rules:
            try:
                result = self._validate_rule(rule, context, category)
                results.append(result)
            except Exception as e:
                logger.error(f"Rule {rule.get('id')} failed: {e}")
                results.append(ValidationResult(
                    rule_id=rule.get("id", "UNKNOWN"),
                    description=rule.get("description", ""),
                    severity=Severity.ERROR,
                    passed=False,
                    message=f"Rule execution error: {e}",
                ))
        
        return results
    
    def validate_balance_sheet(self, bs: Dict) -> List[ValidationResult]:
        """اعتبارسنجی ترازنامه"""
        return self.validate_category("balance_sheet", bs)
    
    def validate_income_statement(self, is_: Dict) -> List[ValidationResult]:
        """اعتبارسنجی صورت سود و زیان"""
        return self.validate_category("income_statement", is_)
    
    def validate_comprehensive_income(self, ci: Dict) -> List[ValidationResult]:
        """اعتبارسنجی صورت سود و زیان جامع"""
        return self.validate_category("comprehensive_income", ci)
    
    def validate_equity_changes(self, ec: Dict) -> List[ValidationResult]:
        """اعتبارسنجی صورت تغییرات در حقوق مالکانه"""
        return self.validate_category("equity_changes", ec)
    
    def validate_cash_flow(self, cf: Dict, balance_sheet: Optional[Dict] = None) -> List[ValidationResult]:
        """اعتبارسنجی صورت جریان‌های نقدی"""
        context = dict(cf)
        if balance_sheet is not None and "balance_sheet" not in context:
            context["balance_sheet"] = balance_sheet
        return self.validate_category("cash_flow", context)

    def validate_cross_checks(self, context: Dict) -> List[ValidationResult]:
        """اعتبارسنجی بین صورت‌ها"""
        return self.validate_category("cross_checks", context)

    def validate_all(
        self,
        balance_sheet: Dict,
        income_statement: Dict,
        comprehensive_income: Optional[Dict] = None,
        equity_changes: Optional[Dict] = None,
        cash_flow: Optional[Dict] = None,
    ) -> ValidationReport:
        """اعتبارسنجی جامع همه صورت‌ها"""
        report = ValidationReport()

        # ۱. ترازنامه
        for r in self.validate_balance_sheet(balance_sheet):
            report.add(r)

        # ۲. صورت سود و زیان
        for r in self.validate_income_statement(income_statement):
            report.add(r)

        # ۳. صورت سود و زیان جامع
        if comprehensive_income:
            for r in self.validate_comprehensive_income(comprehensive_income):
                report.add(r)

        # ۴. صورت تغییرات در حقوق مالکانه
        if equity_changes:
            for r in self.validate_equity_changes(equity_changes):
                report.add(r)

        # ۵. صورت جریان‌های نقدی
        if cash_flow:
            for r in self.validate_cash_flow(cash_flow, balance_sheet=balance_sheet):
                report.add(r)
        
        # ۶. Cross-checks
        cross_context = {
            "balance_sheet": balance_sheet,
            "income_statement": income_statement,
            "comprehensive_income": comprehensive_income or {},
            "equity_changes": equity_changes or {},
            "cash_flow": cash_flow or {},
        }
        for r in self.validate_cross_checks(cross_context):
            report.add(r)
        
        return report


# ==================== CLI ====================
def main():
    import argparse
    import json
    
    parser = argparse.ArgumentParser(description="اعتبارسنجی صورت‌های مالی")
    parser.add_argument("input", help="فایل JSON با داده‌های صورت‌ها")
    parser.add_argument("--output", "-o", help="فایل خروجی گزارش")
    parser.add_argument("--format", "-f", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--verbose", "-v", action="store_true")
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )
    
    # بارگذاری داده‌ها
    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # اعتبارسنجی
    validator = FinancialValidator()
    report = validator.validate_all(
        balance_sheet=data.get("balance_sheet", {}),
        income_statement=data.get("income_statement", {}),
        comprehensive_income=data.get("comprehensive_income"),
        equity_changes=data.get("equity_changes"),
        cash_flow=data.get("cash_flow"),
    )
    
    # خروجی
    if args.format == "json":
        output = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    else:
        output = report.to_markdown()
    
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report saved: {args.output}")
    else:
        print(output)
    
    # کد خروج
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())