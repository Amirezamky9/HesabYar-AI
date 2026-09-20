"""Comprehensive unit and property tests for HesabYar domain common primitives."""

import datetime
from decimal import Decimal
from uuid import UUID

import jdatetime
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from hesabyar.domain.common.context import (
    ActorContext,
    Scope,
    TenantContext,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    DomainError,
    HesabYarError,
    InvalidDateError,
    InvalidIdentifierError,
    InvalidMoneyError,
    InvariantViolationError,
    TenantMismatchError,
    UnauthorizedScopeError,
)
from hesabyar.domain.common.ids import (
    AccountId,
    ActorId,
    EntityId,
    FiscalYearId,
    InvoiceId,
    PostingPeriodId,
    TafsiliId,
    TenantId,
    VoucherId,
)
from hesabyar.domain.common.money import DecimalRoundingPolicy, MoneyIRR
from hesabyar.domain.common.result import Failure, Result, Success

# ==============================================================================
# 1. MoneyIRR and DecimalRoundingPolicy Tests
# ==============================================================================


class TestMoneyIRR:
    def test_valid_creation(self) -> None:
        m = MoneyIRR(100_000)
        assert m.amount == 100_000
        assert m.is_positive
        assert not m.is_zero
        assert not m.is_negative

    def test_zero_money(self) -> None:
        zero = MoneyIRR.zero()
        assert zero.amount == 0
        assert zero.is_zero
        assert not zero.is_positive
        assert not zero.is_negative

    def test_signed_negative_money_allowed(self) -> None:
        neg = MoneyIRR(-500)
        assert neg.amount == -500
        assert neg.is_negative
        assert not neg.is_positive
        assert not neg.is_zero

    def test_float_strictly_forbidden(self) -> None:
        with pytest.raises(InvalidMoneyError, match="Float constructor is strictly forbidden"):
            MoneyIRR(100.5)  # type: ignore[arg-type]

        with pytest.raises(InvalidMoneyError, match="Float constructor is strictly forbidden"):
            MoneyIRR.from_int(100.0)  # type: ignore[arg-type]

    def test_bool_rejected(self) -> None:
        with pytest.raises(InvalidMoneyError, match="must be a Python int"):
            MoneyIRR(True)  # type: ignore[arg-type]

    def test_equal_instances_behave_identically(self) -> None:
        m1 = MoneyIRR(50_000)
        m2 = MoneyIRR(50_000)
        assert m1 == m2
        assert hash(m1) == hash(m2)
        assert repr(m1) == repr(m2)

        other = MoneyIRR(10_000)
        assert m1.add(other) == m2.add(other)
        assert m1.sub(other) == m2.sub(other)
        assert m1.mul_decimal(Decimal("0.10")) == m2.mul_decimal(Decimal("0.10"))

        neg1 = MoneyIRR(-50_000)
        neg2 = MoneyIRR(-50_000)
        assert neg1 == neg2
        assert hash(neg1) == hash(neg2)

    def test_addition(self) -> None:
        m1 = MoneyIRR(10_000)
        m2 = MoneyIRR(25_000)
        res = m1.add(m2)
        assert res.amount == 35_000

        # Operator overload
        res_op = m1 + m2
        assert res_op == res

        # Adding non-MoneyIRR raises
        with pytest.raises(InvalidMoneyError):
            m1.add(500)  # type: ignore[arg-type]

    def test_subtraction_allowing_negative_result(self) -> None:
        m1 = MoneyIRR(30_000)
        m2 = MoneyIRR(10_000)
        res = m1.sub(m2)
        assert res.amount == 20_000

        res_op = m1 - m2
        assert res_op == res

        # Underflow produces signed negative MoneyIRR
        underflow = m2.sub(m1)
        assert underflow.amount == -20_000
        assert underflow.is_negative

    def test_negation_operator(self) -> None:
        m = MoneyIRR(5_000)
        neg = -m
        assert neg.amount == -5_000
        assert -neg == m

    def test_multiplication_by_decimal_rates(self) -> None:
        # 10% VAT on 1,000,000 Rials
        price = MoneyIRR(1_000_000)
        vat_rate = Decimal("0.10")
        vat = price.mul_decimal(vat_rate, DecimalRoundingPolicy.HALF_UP)
        assert vat.amount == 100_000

        # Rounding edge cases: 105 * 0.09 = 9.45
        m = MoneyIRR(105)
        rate = Decimal("0.09")
        # HALF_UP -> 9.45 rounds to 9
        assert m.mul_decimal(rate, DecimalRoundingPolicy.HALF_UP).amount == 9
        # UP -> 10
        assert m.mul_decimal(rate, DecimalRoundingPolicy.UP).amount == 10
        # DOWN -> 9
        assert m.mul_decimal(rate, DecimalRoundingPolicy.DOWN).amount == 9
        # CEILING -> 10
        assert m.mul_decimal(rate, DecimalRoundingPolicy.CEILING).amount == 10
        # FLOOR -> 9
        assert m.mul_decimal(rate, DecimalRoundingPolicy.FLOOR).amount == 9

        # 105 * 0.095 = 9.975 -> rounds to 10
        rate2 = Decimal("0.095")
        assert m.mul_decimal(rate2, DecimalRoundingPolicy.HALF_UP).amount == 10

    def test_mul_decimal_rejects_non_finite_decimals(self) -> None:
        m = MoneyIRR(10_000)
        with pytest.raises(InvalidMoneyError, match="non-finite Decimal rate"):
            m.mul_decimal(Decimal("NaN"))

        with pytest.raises(InvalidMoneyError, match="non-finite Decimal rate"):
            m.mul_decimal(Decimal("Infinity"))

        with pytest.raises(InvalidMoneyError, match="non-finite Decimal rate"):
            m.mul_decimal(Decimal("-Infinity"))

    def test_mul_decimal_float_rate_forbidden(self) -> None:
        m = MoneyIRR(1000)
        with pytest.raises(InvalidMoneyError, match="Float rate is strictly forbidden"):
            m.mul_decimal(0.09)  # type: ignore[arg-type]

    def test_formatting_with_rial_sign_and_thousands_separator(self) -> None:
        m = MoneyIRR(12_345_678)
        assert m.to_formatted_string() == "12,345,678 ریال"
        assert m.to_formatted_string(show_symbol=False) == "12,345,678"
        assert m.to_formatted_string(currency_symbol="IRR") == "12,345,678 IRR"
        assert str(m) == "12,345,678 ریال"

    def test_toman_presentation_conversion(self) -> None:
        m = MoneyIRR(1_000_000)
        assert m.to_toman() == Decimal(100000)

    def test_comparisons_and_hash(self) -> None:
        m1 = MoneyIRR(100)
        m2 = MoneyIRR(100)
        m3 = MoneyIRR(200)
        assert m1 == m2
        assert m1 != m3
        assert m1 < m3
        assert m1 <= m2
        assert m3 > m1
        assert m3 >= m2
        assert hash(m1) == hash(m2)
        assert int(m1) == 100

    def test_pydantic_integration(self) -> None:
        class InvoiceLine(BaseModel):
            price: MoneyIRR

        line = InvoiceLine(price=50_000)  # type: ignore[arg-type]
        assert line.price == MoneyIRR(50_000)
        assert line.model_dump()["price"] == 50_000

        # Reject float in pydantic
        with pytest.raises(ValidationError):
            InvoiceLine(price=50.5)  # type: ignore[arg-type]


# ==============================================================================
# 2. FiscalDate and Date Conversions Tests
# ==============================================================================


class TestFiscalDate:
    def test_canonical_gregorian_wrapping(self) -> None:
        gd = datetime.date(2026, 4, 4)
        fd = FiscalDate(gd)
        assert fd.gregorian_date == gd
        assert fd.date == gd
        assert fd.gregorian_year == 2026
        assert fd.gregorian_month == 4
        assert fd.gregorian_day == 4

    def test_datetime_rejected_without_silent_truncation(self) -> None:
        dt = datetime.datetime(2026, 4, 4, 12, 0, 0)
        with pytest.raises(InvalidDateError, match="not datetime.datetime"):
            FiscalDate(dt)

        with pytest.raises(InvalidDateError, match="not datetime.datetime"):
            FiscalDate.from_gregorian(dt)

    def test_jalali_conversion(self) -> None:
        # 2026-04-04 is 1405-01-15 in Solar Hijri
        gd = datetime.date(2026, 4, 4)
        fd = FiscalDate.from_gregorian(gd)
        assert fd.jalali_year == 1405
        assert fd.jalali_month == 1
        assert fd.jalali_day == 15
        assert fd.to_jalali_str() == "1405/01/15"

    def test_from_jalali_constructors(self) -> None:
        # From integer year, month, day
        fd1 = FiscalDate.from_jalali(1405, 1, 15)
        assert fd1.gregorian_date == datetime.date(2026, 4, 4)
        assert fd1.jalali_year == 1405
        assert fd1.jalali_month == 1
        assert fd1.jalali_day == 15

        # From jdatetime.date instance
        jd = jdatetime.date(1405, 1, 15)
        fd2 = FiscalDate.from_jalali(jd)
        assert fd2 == fd1

    def test_from_jalali_str(self) -> None:
        fd1 = FiscalDate.from_jalali_str("1405/01/15")
        fd2 = FiscalDate.from_jalali_str("1405-01-15")
        assert fd1 == fd2
        assert fd1.gregorian_date == datetime.date(2026, 4, 4)

    def test_persian_numerals_normalization(self) -> None:
        persian_str = "۱۴۰۵/۰۱/۱۵"
        fd = FiscalDate.from_jalali_str(persian_str)
        assert fd.jalali_year == 1405
        assert fd.jalali_month == 1
        assert fd.jalali_day == 15

    def test_invalid_jalali_date_rejected(self) -> None:
        # Month 13 is invalid
        with pytest.raises(InvalidDateError):
            FiscalDate.from_jalali(1405, 13, 1)

        # Day 32 is invalid
        with pytest.raises(InvalidDateError):
            FiscalDate.from_jalali(1405, 1, 32)

        # Malformed strings
        with pytest.raises(InvalidDateError):
            FiscalDate.from_jalali_str("not-a-date")

    def test_ambiguous_date_rejected(self) -> None:
        # Gregorian date formatted with slashes must not be silently treated as Jalali
        with pytest.raises(InvalidDateError, match="outside valid Solar Hijri range"):
            FiscalDate.from_jalali_str("2026/04/04")

        with pytest.raises(InvalidDateError, match="Ambiguous date format"):
            FiscalDate.from_iso_str("2026/04/04")

    def test_from_iso_str(self) -> None:
        fd = FiscalDate.from_iso_str("2026-04-04")
        assert fd.gregorian_date == datetime.date(2026, 4, 4)

        with pytest.raises(InvalidDateError):
            FiscalDate.from_iso_str("invalid")

    def test_protocol_and_clock_independence(self) -> None:
        # Ensure Moadian epoch ms and system clock today() are absent from domain primitive
        assert not hasattr(FiscalDate, "to_moadian_epoch_ms")
        assert not hasattr(FiscalDate, "from_moadian_epoch_ms")
        assert not hasattr(FiscalDate, "today")

    def test_date_arithmetic_and_comparisons(self) -> None:
        fd1 = FiscalDate.from_jalali(1405, 1, 1)
        delta = datetime.timedelta(days=10)
        fd2 = fd1 + delta
        assert fd2.jalali_day == 11
        assert (fd2 - fd1) == delta
        assert (fd2 - delta) == fd1

        assert fd1 < fd2
        assert fd1 <= fd2
        assert fd2 > fd1
        assert fd2 >= fd1
        assert fd1 != fd2

    def test_pydantic_integration(self) -> None:
        class FiscalPeriod(BaseModel):
            as_of: FiscalDate

        fp1 = FiscalPeriod(as_of="1405/01/15")  # type: ignore[arg-type]
        assert fp1.as_of.jalali_year == 1405
        assert fp1.model_dump()["as_of"] == "2026-04-04"

        # Ambiguous string rejected in Pydantic
        with pytest.raises(ValidationError):
            FiscalPeriod(as_of="2026/04/04")  # type: ignore[arg-type]


# ==============================================================================
# 3. Strongly-Typed Identifier (EntityId) Tests
# ==============================================================================


class TestEntityIds:
    def test_creation_and_string_representation(self) -> None:
        tid = TenantId("org-123")
        assert tid.value == "org-123"
        assert str(tid) == "org-123"
        assert repr(tid) == "TenantId('org-123')"

    def test_uuid_generation(self) -> None:
        vid = VoucherId.generate()
        assert isinstance(vid, VoucherId)
        parsed_uuid = UUID(vid.value)
        assert str(parsed_uuid) == vid.value

    def test_distinct_types_are_not_equal(self) -> None:
        raw = "common-id-value"
        tid = TenantId(raw)
        aid = ActorId(raw)
        assert tid.value == aid.value
        assert tid != aid
        assert hash(tid) != hash(aid)

    def test_cross_type_construction_rejected(self) -> None:
        tid = TenantId("t-1")
        with pytest.raises(InvalidIdentifierError, match="distinct identifier type"):
            ActorId(tid)  # type: ignore[arg-type]

    def test_same_type_copy_construction_allowed(self) -> None:
        tid1 = TenantId("t-1")
        tid2 = TenantId(tid1)
        assert tid1 == tid2

    def test_empty_or_blank_rejected(self) -> None:
        with pytest.raises(InvalidIdentifierError, match="cannot be empty or blank"):
            TenantId("")

        with pytest.raises(InvalidIdentifierError, match="cannot be empty or blank"):
            TenantId("   ")

    def test_all_entity_subclasses(self) -> None:
        assert isinstance(TenantId("t-1"), EntityId)
        assert isinstance(ActorId("a-1"), EntityId)
        assert isinstance(VoucherId("v-1"), EntityId)
        assert isinstance(AccountId("acc-1"), EntityId)
        assert isinstance(TafsiliId("taf-1"), EntityId)
        assert isinstance(FiscalYearId("fy-1"), EntityId)
        assert isinstance(PostingPeriodId("pp-1"), EntityId)
        assert isinstance(InvoiceId("inv-1"), EntityId)

    def test_pydantic_integration(self) -> None:
        class EntityHolder(BaseModel):
            tenant_id: TenantId
            voucher_id: VoucherId

        raw_data = {"tenant_id": "tenant-abc", "voucher_id": "v-999"}
        holder = EntityHolder.model_validate(raw_data)
        assert holder.tenant_id == TenantId("tenant-abc")
        assert holder.voucher_id == VoucherId("v-999")
        assert holder.model_dump() == raw_data


# ==============================================================================
# 4. Error Hierarchy Tests
# ==============================================================================


class TestErrors:
    def test_hierarchy(self) -> None:
        assert issubclass(DomainError, HesabYarError)
        assert issubclass(InvariantViolationError, DomainError)
        assert issubclass(TenantMismatchError, DomainError)
        assert issubclass(InvalidMoneyError, DomainError)
        assert issubclass(InvalidDateError, DomainError)
        assert issubclass(InvalidIdentifierError, DomainError)
        assert issubclass(UnauthorizedScopeError, DomainError)

    def test_error_attributes(self) -> None:
        err = InvariantViolationError("Voucher unbalanced", code="UNBALANCED", details={"diff": 100})
        assert err.message == "Voucher unbalanced"
        assert err.code == "UNBALANCED"
        assert err.details == {"diff": 100}
        assert "InvariantViolationError" in repr(err)


# ==============================================================================
# 5. Functional Result Monad Tests
# ==============================================================================


class TestResult:
    def test_success_lifecycle(self) -> None:
        res: Result[int, str] = Success(42)
        assert res.is_success is True
        assert res.is_failure is False
        assert res.value == 42
        assert bool(res) is True
        assert res.unwrap() == 42
        assert res.unwrap_or(0) == 42

        # Accessing error on Success raises ValueError
        with pytest.raises(ValueError, match="Cannot access .error"):
            _ = res.error

    def test_failure_lifecycle(self) -> None:
        res: Result[int, str] = Failure("something went wrong")
        assert res.is_success is False
        assert res.is_failure is True
        assert res.error == "something went wrong"
        assert bool(res) is False
        assert res.unwrap_or(999) == 999

        # Accessing value on Failure raises ValueError
        with pytest.raises(ValueError, match="Cannot access .value"):
            _ = res.value

    def test_unwrap_contract_with_exception(self) -> None:
        # If Failure contains an Exception, re-raise that exact exception
        exact_err = InvariantViolationError("ledger invariant breached", code="BREACH")
        failed: Result[int, Exception] = Failure(exact_err)
        with pytest.raises(InvariantViolationError) as exc_info:
            failed.unwrap()
        assert exc_info.value is exact_err

    def test_unwrap_contract_with_non_exception_error(self) -> None:
        # If Failure contains a non-exception error value, raise ValueError(str(self.error))
        failed: Result[int, str] = Failure("domain failure code 403")
        with pytest.raises(ValueError) as exc_info:
            failed.unwrap()
        assert str(exc_info.value) == "domain failure code 403"

    def test_map_and_bind(self) -> None:
        # Success map
        s: Result[int, str] = Success(10)
        mapped = s.map(lambda x: x * 2)
        assert mapped == Success(20)

        # Success bind
        bound = s.bind(lambda x: Success(f"val-{x}"))
        assert bound == Success("val-10")

        # Failure map skips fn
        f: Result[int, str] = Failure("err")
        assert f.map(lambda x: x * 2) == Failure("err")

        # Failure bind skips fn
        assert f.bind(lambda x: Success("ok")) == Failure("err")

        # Failure map_err
        mapped_err = f.map_err(lambda e: f"wrapped: {e}")
        assert mapped_err == Failure("wrapped: err")

    def test_factory_methods(self) -> None:
        assert Result.ok("hello") == Success("hello")
        assert Result.fail("oops") == Failure("oops")


# ==============================================================================
# 6. Tenant and Actor Context Tests
# ==============================================================================


class TestContext:
    def test_tenant_context_basics(self) -> None:
        tc = TenantContext(TenantId("t-1"), name="Acme Corp")
        assert tc.tenant_id == TenantId("t-1")
        assert tc.name == "Acme Corp"
        assert tc.is_active is True
        assert tc.matches(TenantId("t-1"))
        assert not tc.matches(TenantId("t-2"))

        # require_matching_tenant succeeds on match
        tc.require_matching_tenant(TenantId("t-1"))

        # require_matching_tenant raises TenantMismatchError on mismatch
        with pytest.raises(TenantMismatchError, match="does not match target entity tenant"):
            tc.require_matching_tenant(TenantId("t-2"))

    def test_tenant_blank_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be blank"):
            TenantContext(TenantId("t-1"), name="  ")

    def test_tenant_equality_includes_is_active(self) -> None:
        t_active = TenantContext("t-1", "Acme", is_active=True)
        t_active_same = TenantContext("t-1", "Acme", is_active=True)
        t_inactive = TenantContext("t-1", "Acme", is_active=False)

        assert t_active == t_active_same
        assert t_active != t_inactive
        assert hash(t_active) != hash(t_inactive)

    def test_tenant_require_active(self) -> None:
        t_active = TenantContext("t-1", "Acme", is_active=True)
        t_active.require_active()  # does not raise

        t_inactive = TenantContext("t-1", "Acme", is_active=False)
        with pytest.raises(InvariantViolationError, match="is inactive and cannot perform operations"):
            t_inactive.require_active()

    def test_actor_context_and_scopes(self) -> None:
        actor = ActorContext(
            actor_id=ActorId("u-1"),
            tenant_id=TenantId("t-1"),
            scopes={Scope.READ, Scope.LEDGER_DRAFT},
        )
        assert actor.actor_id == ActorId("u-1")
        assert actor.tenant_id == TenantId("t-1")
        assert actor.has_scope(Scope.READ)
        assert actor.has_scope(Scope.LEDGER_DRAFT)
        assert not actor.has_scope(Scope.LEDGER_POST)
        assert not actor.is_admin

        # require_scope succeeds on granted scope
        actor.require_scope(Scope.READ)

        # require_scope raises UnauthorizedScopeError on missing scope
        with pytest.raises(UnauthorizedScopeError, match="lacks required scope"):
            actor.require_scope(Scope.LEDGER_POST)

    def test_admin_scope_does_not_imply_operational_scopes(self) -> None:
        """ADMIN scope MUST NOT act as a wildcard granting operational capabilities."""
        admin_actor = ActorContext(
            actor_id=ActorId("admin-1"),
            tenant_id=TenantId("t-1"),
            scopes={Scope.ADMIN},
        )
        assert admin_actor.is_admin
        assert admin_actor.has_scope(Scope.ADMIN)

        # Negative authorization tests: ADMIN does NOT grant operational scopes
        assert not admin_actor.has_scope(Scope.LEDGER_POST)
        assert not admin_actor.has_scope(Scope.LEDGER_DRAFT)
        assert not admin_actor.has_scope(Scope.TAX_SUBMIT)
        assert not admin_actor.has_scope(Scope.TAX_DRAFT)
        assert not admin_actor.has_scope(Scope.READ)

        # Operational scopes must raise UnauthorizedScopeError
        with pytest.raises(UnauthorizedScopeError, match="lacks required scope"):
            admin_actor.require_scope(Scope.LEDGER_POST)

        with pytest.raises(UnauthorizedScopeError, match="lacks required scope"):
            admin_actor.require_scope(Scope.TAX_SUBMIT)

    def test_unknown_scope_rejected_on_construction(self) -> None:
        with pytest.raises(UnauthorizedScopeError, match="Unknown or invalid scope"):
            ActorContext(
                actor_id=ActorId("u-1"),
                tenant_id=TenantId("t-1"),
                scopes={"hesabyar.invalid.typo_scope"},
            )

    def test_actor_tenant_isolation_check(self) -> None:
        actor = ActorContext(
            actor_id=ActorId("u-1"),
            tenant_id=TenantId("tenant-a"),
            scopes={Scope.READ},
        )
        actor.require_matching_tenant(TenantId("tenant-a"))

        with pytest.raises(TenantMismatchError, match="cannot access entity owned by tenant"):
            actor.require_matching_tenant(TenantId("tenant-b"))

    def test_system_admin_factory_is_not_unrestricted_superuser(self) -> None:
        sys_admin = ActorContext.system_admin("tenant-xyz")
        assert sys_admin.actor_id == ActorId("system")
        assert sys_admin.tenant_id == TenantId("tenant-xyz")
        assert sys_admin.is_admin
        # Does not have operational scopes by default
        assert not sys_admin.has_scope(Scope.LEDGER_POST)
        assert not sys_admin.has_scope(Scope.TAX_SUBMIT)


# ==============================================================================
# 7. Hypothesis Property-Based Invariant Tests
# ==============================================================================


class TestPropertyInvariants:
    @given(
        a=st.integers(min_value=-1_000_000_000, max_value=1_000_000_000),
        b=st.integers(min_value=-1_000_000_000, max_value=1_000_000_000),
    )
    def test_signed_money_addition_commutativity_and_associativity(self, a: int, b: int) -> None:
        m1 = MoneyIRR(a)
        m2 = MoneyIRR(b)
        assert (m1 + m2).amount == a + b
        assert (m1 + m2) == (m2 + m1)

    @given(
        year=st.integers(min_value=1380, max_value=1420),
        month=st.integers(min_value=1, max_value=12),
        day=st.integers(min_value=1, max_value=29),  # 1-29 are valid in all Jalali months
    )
    def test_jalali_gregorian_roundtrip_invariant(self, year: int, month: int, day: int) -> None:
        fd = FiscalDate.from_jalali(year, month, day)
        assert fd.jalali_year == year
        assert fd.jalali_month == month
        assert fd.jalali_day == day
        # Re-creating from Gregorian date matches
        fd_from_greg = FiscalDate.from_gregorian(fd.gregorian_date)
        assert fd_from_greg == fd
