# -*- coding: utf-8 -*-
"""
موتور اعتبارسنجی صورت‌های مالی
بر اساس استاندارد حسابداری ۱ و قواعد cross-standard
"""
import logging
# ==================== تنظیم UTF-8 ====================
import sys
import io
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
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
            if not part:
                continue
            
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
    
    def _evaluate_formula(self, formula: str, context: Dict) -> Optional[float]:
        """
        ارزیابی یک فرمول ساده
        
        پشتیبانی از:
        - a + b - c
        - a * b / c
        - sum(items)
        - (a + b) - (c + d)
        """
        try:
            # جایگزینی sum(...) با مقادیر
            formula = self._expand_sum(formula, context)
            
            # جایگزینی متغیرها با مقادیر
            for key, value in context.items():
                if isinstance(value, (int, float)):
                    formula = formula.replace(key, str(value))
                elif isinstance(value, dict):
                    # مسیرهای تودرتو
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, (int, float)):
                            full_key = f"{key}.{sub_key}"
                            formula = formula.replace(full_key, str(sub_value))
            
            # ارزیابی
            result = eval(formula, {"__builtins__": {}}, {})
            return float(result)
        except Exception as e:
            logger.debug(f"Formula evaluation failed: {formula} - {e}")
            return None
    
    def _expand_sum(self, formula: str, context: Dict) -> str:
        """جایگزینی sum(...) با مقدار"""
        import re
        
        def replace_sum(match):
            items_key = match.group(1).strip()
            items = context.get(items_key, [])
            if isinstance(items, list):
                total = 0
                for item in items:
                    if isinstance(item, dict):
                        total += item.get("amount", 0)
                    elif isinstance(item, (int, float)):
                        total += item
                return str(total)
            return "0"
        
        return re.sub(r"sum\(([^)]+)\)", replace_sum, formula)
    
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
    
    def validate_cash_flow(self, cf: Dict) -> List[ValidationResult]:
        """اعتبارسنجی صورت جریان‌های نقدی"""
        return self.validate_category("cash_flow", cf)
    
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
            for r in self.validate_cash_flow(cash_flow):
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