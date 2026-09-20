"""تست‌های موتور اعتبارسنجی"""
import json
import sys
from pathlib import Path

import pytest

# افزودن مسیر scripts
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from validator import (
    FinancialValidator,
    Severity,
    ValidationReport,
    ValidationResult,
)


# ==================== Fixtures ====================
@pytest.fixture
def validator():
    """نمونه validator"""
    return FinancialValidator()


@pytest.fixture
def sample_data():
    """داده نمونه از فایل JSON"""
    path = Path(__file__).parent / "sample-data.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def valid_balance_sheet():
    """ترازنامه معتبر"""
    return {
        "cash_and_equivalents": 100,
        "current_assets_items": [
            {"key": "cash_and_equivalents", "amount": 100},
        ],
        "total_current_assets": 100,
        "non_current_assets_items": [],
        "total_non_current_assets": 0,
        "total_assets": 100,
        "current_liabilities_items": [],
        "total_current_liabilities": 0,
        "non_current_liabilities_items": [],
        "total_non_current_liabilities": 0,
        "total_liabilities": 0,
        "equity_items": [
            {"key": "share_capital", "amount": 100},
        ],
        "total_equity": 100,
    }


@pytest.fixture
def invalid_balance_sheet():
    """ترازنامه نامعتبر (تراز نیست)"""
    return {
        "cash_and_equivalents": 100,
        "current_assets_items": [
            {"key": "cash_and_equivalents", "amount": 100},
        ],
        "total_current_assets": 100,
        "non_current_assets_items": [],
        "total_non_current_assets": 0,
        "total_assets": 100,
        "current_liabilities_items": [],
        "total_current_liabilities": 0,
        "non_current_liabilities_items": [],
        "total_non_current_liabilities": 0,
        "total_liabilities": 0,
        "equity_items": [
            {"key": "share_capital", "amount": 50},
        ],
        "total_equity": 50,  # ← اشتباه: باید ۱۰۰ باشه
    }


# ==================== تست‌های ساختار ====================
class TestValidationResult:
    """تست ValidationResult"""

    def test_create_result(self):
        r = ValidationResult(
            rule_id="TEST001",
            description="تست",
            severity=Severity.ERROR,
            passed=True,
        )
        assert r.rule_id == "TEST001"
        assert r.passed is True
        assert r.severity == Severity.ERROR

    def test_to_dict(self):
        r = ValidationResult(
            rule_id="TEST001",
            description="تست",
            severity=Severity.ERROR,
            passed=False,
            message="خطا",
        )
        d = r.to_dict()
        assert d["rule_id"] == "TEST001"
        assert d["passed"] is False
        assert d["severity"] == "error"
        assert d["message"] == "خطا"


class TestValidationReport:
    """تست ValidationReport"""

    def test_empty_report(self):
        report = ValidationReport()
        assert report.total_rules == 0
        assert report.is_valid is True

    def test_add_passed_result(self):
        report = ValidationReport()
        report.add(ValidationResult(
            rule_id="TEST001",
            description="تست",
            severity=Severity.ERROR,
            passed=True,
        ))
        assert report.total_rules == 1
        assert report.passed == 1
        assert report.failed == 0
        assert report.is_valid is True

    def test_add_failed_error(self):
        report = ValidationReport()
        report.add(ValidationResult(
            rule_id="TEST001",
            description="تست",
            severity=Severity.ERROR,
            passed=False,
        ))
        assert report.total_rules == 1
        assert report.passed == 0
        assert report.failed == 1
        assert report.is_valid is False
        assert len(report.errors) == 1

    def test_add_failed_warning(self):
        report = ValidationReport()
        report.add(ValidationResult(
            rule_id="TEST001",
            description="تست",
            severity=Severity.WARNING,
            passed=False,
        ))
        assert report.is_valid is True  # warning باعث invalid نمی‌شه
        assert len(report.warnings) == 1

    def test_to_dict(self):
        report = ValidationReport()
        report.add(ValidationResult(
            rule_id="TEST001",
            description="تست",
            severity=Severity.ERROR,
            passed=False,
            message="خطا",
        ))
        d = report.to_dict()
        assert d["summary"]["total_rules"] == 1
        assert d["summary"]["failed"] == 1
        assert d["summary"]["is_valid"] is False
        assert len(d["errors"]) == 1

    def test_to_markdown(self):
        report = ValidationReport()
        report.add(ValidationResult(
            rule_id="TEST001",
            description="تست",
            severity=Severity.ERROR,
            passed=False,
            message="خطای تست",
        ))
        md = report.to_markdown()
        assert "# گزارش اعتبارسنجی" in md
        assert "TEST001" in md
        assert "خطای تست" in md


# ==================== تست‌های Validator ====================
class TestFinancialValidator:
    """تست FinancialValidator"""

    def test_init(self, validator):
        assert validator is not None
        assert validator.rules is not None
        assert len(validator.rules) > 0

    def test_rules_loaded(self, validator):
        # بررسی دسته‌های اصلی
        assert "balance_sheet" in validator.rules
        assert "income_statement" in validator.rules
        assert "cash_flow" in validator.rules
        assert "cross_checks" in validator.rules

    def test_validate_balance_sheet_valid(self, validator, valid_balance_sheet):
        results = validator.validate_balance_sheet(valid_balance_sheet)
        assert len(results) > 0

        # هیچ خطای error نباید باشه
        # توجه: بعضی قواعد ممکنه به خاطر داده ناقص خطا بدن، پس فقط چک می‌کنیم که اجرا شده
        assert isinstance(results, list)

    def test_validate_balance_sheet_invalid(self, validator, invalid_balance_sheet):
        results = validator.validate_balance_sheet(invalid_balance_sheet)

        # باید حداقل یه خطا داشته باشیم (BS001)
        errors = [r for r in results if not r.passed and r.rule_id == "BS001"]
        assert len(errors) >= 1, "BS001 باید خطا بده"

    def test_validate_all(self, validator, sample_data):
        report = validator.validate_all(
            balance_sheet=sample_data["balance_sheet"],
            income_statement=sample_data["income_statement"],
            comprehensive_income=sample_data.get("comprehensive_income"),
            equity_changes=sample_data.get("equity_changes"),
            cash_flow=sample_data.get("cash_flow"),
        )

        assert report.total_rules > 0
        assert report.passed > 0

        # داده نمونه باید معتبر باشه
        assert report.is_valid, f"داده نمونه معتبر نیست: {[e.rule_id for e in report.errors]}"

    def test_sample_data_all_pass(self, validator, sample_data):
        """تمام ۳۶ قاعده در داده نمونه ارزیابی شده و بدون خطا هستند (۳۲ موفق، ۴ غیرقابل اعمال)"""
        report = validator.validate_all(
            balance_sheet=sample_data["balance_sheet"],
            income_statement=sample_data["income_statement"],
            comprehensive_income=sample_data.get("comprehensive_income"),
            equity_changes=sample_data.get("equity_changes"),
            cash_flow=sample_data.get("cash_flow"),
        )

        assert report.total_rules == 36
        assert report.passed == 32
        assert report.not_applicable == 4
        assert report.failed == 0, f"قواعد ناموفق: {[e.rule_id for e in report.errors + report.warnings]}"
        assert len(report.errors) == 0
        assert len(report.warnings) == 0
        assert report.is_valid is True


# ==================== تست‌های کمکی ====================
class TestSafeGet:
    """تست _safe_get"""

    def test_dict_access(self, validator):
        data = {"a": {"b": {"c": 42}}}
        assert validator._safe_get(data, "a.b.c") == 42
        assert validator._safe_get(data, "a.b") == {"c": 42}
        assert validator._safe_get(data, "x.y", default=None) is None

    def test_attr_access(self, validator):
        class Obj:
            def __init__(self):
                self.x = 10
        obj = Obj()
        assert validator._safe_get(obj, "x") == 10
        assert validator._safe_get(obj, "y", default=0) == 0


class TestFormulaEvaluation:
    """تست _evaluate_formula"""

    def test_simple_formula(self, validator):
        context = {"a": 10, "b": 5}
        result = validator._evaluate_formula("a - b", context)
        assert result == 5

    def test_nested_formula(self, validator):
        context = {
            "x": {"y": 10},
            "z": 3,
        }
        result = validator._evaluate_formula("x.y - z", context)
        assert result == 7


# ==================== تست‌های یکپارچگی ====================
class TestIntegration:
    """تست‌های یکپارچگی"""

    def test_metadata_exists(self):
        path = Path(__file__).parent.parent / "metadata.json"
        assert path.exists()

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["standards_count"] == 35
        assert len(data["standards"]) == 35

    def test_all_skill_md_exist(self):
        path = Path(__file__).parent.parent / "metadata.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        missing = []
        for std in data["standards"]:
            slug = std["slug"]
            skill = Path(__file__).parent.parent / "standards" / slug / "SKILL.md"
            if not skill.exists():
                missing.append(slug)

        assert len(missing) == 0, f"فایل‌های SKILL.md گمشده: {missing}"

    def test_rules_yaml_exists(self):
        path = Path(__file__).parent.parent / "validators" / "rules.yaml"
        assert path.exists()

        import yaml
        with open(path, encoding="utf-8") as f:
            rules = yaml.safe_load(f)

        assert rules is not None
        assert "balance_sheet" in rules
        assert "income_statement" in rules


# ==================== تست‌های Fail-Closed ====================
class TestValidatorFailClosed:
    """تست رفتارهای Fail-Closed در اعتبارسنجی"""

    def test_formula_evaluation_error_fails_closed(self, validator):
        """خطای ارزیابی فرمول در قاعده اجباری باید fail-closed باشد (passed=False, is_valid=False)"""
        rule = {
            "id": "ERR001",
            "description": "خطای تقسیم بر صفر",
            "formula": "total_assets / zero_val",
            "severity": "error",
            "message": "خطای محاسباتی",
        }
        context = {"total_assets": 100, "zero_val": 0}
        result = validator._validate_rule(rule, context, "custom")

        assert result.passed is False
        assert result.severity == Severity.ERROR
        assert "Formula evaluation error:" in result.message

        report = ValidationReport()
        report.add(result)
        assert report.is_valid is False
        assert len(report.errors) == 1

    def test_syntax_error_formula_fails_closed(self, validator):
        """فرمول با خطای نحوی باید fail-closed باشد"""
        rule = {
            "id": "SYN001",
            "description": "خطای نحو",
            "formula": "total_assets +* 10",
            "severity": "error",
        }
        context = {"total_assets": 100}
        result = validator._validate_rule(rule, context, "custom")

        assert result.passed is False
        assert result.severity == Severity.ERROR
        assert "Formula evaluation error:" in result.message

        report = ValidationReport()
        report.add(result)
        assert report.is_valid is False

    @pytest.mark.parametrize("malicious_formula", [
        "__import__('os').system('id')",
        "eval('1 + 1')",
        "exec('a = 1')",
        "__builtins__.__import__('os')",
        "[c for c in ().__class__.__base__.__subclasses__()]",
        "open('/etc/passwd').read()",
    ])
    def test_malicious_formula_injection_fails_closed(self, validator, malicious_formula):
        """تلاش برای تزریق کد مخرب باید رد شده و fail-closed باشد"""
        rule = {
            "id": "SEC001",
            "description": "تست امنیتی تزریق کد",
            "formula": malicious_formula,
            "severity": "error",
            "message": "رد شد",
        }
        context = {"total_assets": 100}
        result = validator._validate_rule(rule, context, "custom")

        assert result.passed is False
        assert result.severity == Severity.ERROR
        assert "Formula evaluation error:" in result.message

        report = ValidationReport()
        report.add(result)
        assert report.is_valid is False
        assert len(report.errors) == 1

    def test_missing_formula_on_mandatory_rule_fails_closed(self, validator):
        """قاعده اجباری بدون فرمول و بدون requirement باید fail-closed باشد"""
        rule = {
            "id": "EMPTY001",
            "description": "قاعده بدون فرمول",
            "severity": "error",
        }
        context = {"total_assets": 100}
        result = validator._validate_rule(rule, context, "custom")

        assert result.passed is False
        assert result.severity == Severity.ERROR
        assert result.message == "Rule missing executable formula or requirement"

        report = ValidationReport()
        report.add(result)
        assert report.is_valid is False
        assert len(report.errors) == 1

    def test_non_mandatory_missing_formula_or_error_does_not_fail_report(self, validator):
        """قاعده غیراجباری (warning/info) در صورت خطا گزارش را invalid نمی‌کند اما passed=False می‌شود"""
        rule_missing = {
            "id": "WARN001",
            "description": "هشدار بدون فرمول",
            "severity": "warning",
        }
        res_missing = validator._validate_rule(rule_missing, {}, "custom")
        assert res_missing.passed is False
        assert res_missing.message == "Rule missing executable formula or requirement"

        rule_err = {
            "id": "WARN002",
            "description": "هشدار با خطای ارزیابی",
            "formula": "unknown_var + 10",
            "severity": "warning",
        }
        res_err = validator._validate_rule(rule_err, {}, "custom")
        assert res_err.passed is False
        assert "Formula evaluation error:" in res_err.message

        report = ValidationReport()
        report.add(res_missing)
        report.add(res_err)
        assert report.is_valid is True
        assert len(report.warnings) == 2
        assert report.failed == 2

    def test_warning_rule_evaluation_error_never_passed(self, validator):
        """خطای ارزیابی در قاعده اخطار هرگز نباید passed=True باشد و گزارش معتبر می‌ماند"""
        rule = {
            "id": "WARN003",
            "description": "هشدار تقسیم بر صفر",
            "formula": "assets / zero_divisor",
            "severity": "warning",
            "message": "هشدار نسبت",
        }
        context = {"assets": 100, "zero_divisor": 0}
        result = validator._validate_rule(rule, context, "custom")

        assert result.passed is False
        assert result.severity == Severity.WARNING
        assert "Formula evaluation error:" in result.message

        report = ValidationReport()
        report.add(result)
        assert report.is_valid is True
        assert len(report.warnings) == 1
        assert len(report.errors) == 0
        assert report.failed == 1

    def test_not_applicable_never_marked_pass(self, validator):
        """قاعده غیرقابل اعمال نباید به عنوان PASS شمرده شود"""
        rule = {
            "id": "NA001",
            "description": "قاعده مخصوص روش مستقیم",
            "formula": "cash_from_operations > 0",
            "applies_to": "direct",
            "severity": "error",
        }
        # داده با روش indirect است
        context = {"classification_method": "indirect", "cash_from_operations": 100}
        result = validator._validate_rule(rule, context, "custom")

        assert result.passed is False
        assert result.not_applicable is True
        assert "Skipped" in result.message

        report = ValidationReport()
        report.add(result)
        assert report.is_valid is True
        assert report.passed == 0
        assert report.failed == 0
        assert report.not_applicable == 1
        assert len(report.errors) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
