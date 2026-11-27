"""Pure domain tests for the integrations registries + policy gate (US-503).

DB-free: the envelope contract, tranche-leg validation, the
three-layer policy gate and the holiday calendar are all pure logic.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from hesabyar.domain.integrations.envelope import (
    SCHEMA_VERSION_1,
    CanonicalEventEnvelope,
)
from hesabyar.domain.integrations.holidays import FRIDAY, HolidayCalendar
from hesabyar.domain.integrations.models import (
    AutoPostPolicy,
    ContractStatus,
    GatewayContract,
    PolicyGateConfig,
    TrancheLeg,
    validate_tranche_legs,
)
from hesabyar.domain.integrations.policy_gate import evaluate_policy_gate


def _policy(**overrides: object) -> PolicyGateConfig:
    values: dict[str, object] = {
        "id": "policy-1",
        "installation_id": "inst-1",
        "event_type": "payment.captured",
        "allowed": True,
        "max_amount_irr": 10_000_000,
        "require_tieout": False,
    }
    values.update(overrides)
    return PolicyGateConfig(**values)  # type: ignore[arg-type]


def _contract(**overrides: object) -> GatewayContract:
    values: dict[str, object] = {
        "id": "contract-1",
        "channel_id": "channel-1",
        "channel_key": "zarinpal",
        "version": 1,
        "valid_from": datetime.date(2026, 1, 1),
        "fee_percent": Decimal("2"),
        "auto_post_policy": AutoPostPolicy.AUTO_POST_WITHIN_POLICY,
        "auto_post_max_amount_irr": 5_000_000,
        "status": ContractStatus.ACTIVE,
    }
    values.update(overrides)
    return GatewayContract(**values)  # type: ignore[arg-type]


def _evaluate(**overrides: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "event_type": "payment.captured",
        "amount_irr": 1_000_000,
        "policy": _policy(),
        "installation_auto_post_enabled": True,
        "contract": None,
        "period_open": True,
        "tieout_ok": None,
    }
    values.update(overrides)
    return evaluate_policy_gate(**values)  # type: ignore[arg-type]


# -- envelope -------------------------------------------------------------


class TestCanonicalEventEnvelope:
    def test_fifteen_spec_fields(self) -> None:
        """The envelope carries exactly the 15 spec §10.2 fields."""
        envelope = CanonicalEventEnvelope(
            id="evt-1",
            schema_version=1,
            source_system="hayat",
            source_store_id="store-1",
            source_event_id="order-42",
            occurred_at=datetime.datetime(2026, 11, 18, 10, 0, tzinfo=datetime.UTC),
            received_at=datetime.datetime(2026, 11, 18, 10, 0, 1, tzinfo=datetime.UTC),
            company_mapping="company-1",
            currency="IRR",
            external_entity_type="order",
            external_entity_id="42",
            correlation_id="corr-1",
            idempotency_key="idem-1",
            payload={"amount_irr": 1_000_000},
            payload_hash="aabbcc",
        )
        assert envelope.is_current_schema()
        assert envelope.schema_version == SCHEMA_VERSION_1
        assert envelope.currency == "IRR"
        assert envelope.payload["amount_irr"] == 1_000_000

    def test_old_schema_is_not_current(self) -> None:
        """A future schema version is not the current one."""
        envelope = CanonicalEventEnvelope(
            id="evt-1",
            schema_version=2,
            source_system="hayat",
            source_store_id="store-1",
            source_event_id="order-42",
            occurred_at=datetime.datetime(2026, 11, 18, 10, 0, tzinfo=datetime.UTC),
            received_at=datetime.datetime(2026, 11, 18, 10, 0, 1, tzinfo=datetime.UTC),
            company_mapping="company-1",
            currency="IRR",
            external_entity_type="order",
            external_entity_id="42",
            correlation_id=None,
            idempotency_key="idem-1",
            payload={},
            payload_hash="aabbcc",
        )
        assert not envelope.is_current_schema()


# -- tranche legs ---------------------------------------------------------


class TestValidateTrancheLegs:
    def test_valid_four_tranche_schedule(self) -> None:
        """The default BNPL model: 4 monthly tranches of 25%."""
        legs = (
            TrancheLeg(1, 5, Decimal("25")),
            TrancheLeg(2, 5, Decimal("25")),
            TrancheLeg(3, 5, Decimal("25")),
            TrancheLeg(4, 5, Decimal("25")),
        )
        assert validate_tranche_legs(legs) == []

    def test_valid_single_leg_lump_sum(self) -> None:
        """A lump-sum model is a single 100% leg."""
        assert validate_tranche_legs((TrancheLeg(2, 5, Decimal("100")),)) == []

    def test_empty_legs_rejected(self) -> None:
        problems = validate_tranche_legs(())
        assert len(problems) == 1
        assert "at least one leg" in problems[0]

    def test_percent_sum_must_be_100(self) -> None:
        problems = validate_tranche_legs(
            (TrancheLeg(1, 5, Decimal("30")), TrancheLeg(2, 5, Decimal("30")))
        )
        assert any("sum to exactly 100" in p for p in problems)

    def test_bad_month_offset_and_payout_day(self) -> None:
        problems = validate_tranche_legs(
            (TrancheLeg(-1, 32, Decimal("100")),),
        )
        assert any("month_offset" in p for p in problems)
        assert any("payout_day" in p for p in problems)

    def test_bad_percent(self) -> None:
        problems = validate_tranche_legs((TrancheLeg(1, 5, Decimal("0")),))
        assert any("percent" in p for p in problems)


class TestGatewayContractEffectiveDates:
    def test_is_effective_on_inclusive_both_ends(self) -> None:
        contract = _contract(
            valid_from=datetime.date(2026, 1, 1),
            valid_to=datetime.date(2026, 6, 30),
        )
        assert contract.is_effective_on(datetime.date(2026, 1, 1))
        assert contract.is_effective_on(datetime.date(2026, 6, 30))
        assert not contract.is_effective_on(datetime.date(2025, 12, 31))
        assert not contract.is_effective_on(datetime.date(2026, 7, 1))

    def test_open_ended_version(self) -> None:
        contract = _contract(valid_from=datetime.date(2026, 1, 1), valid_to=None)
        assert contract.is_effective_on(datetime.date(2030, 1, 1))


# -- policy gate ----------------------------------------------------------


class TestPolicyGate:
    def test_all_layers_pass_allows_auto_post(self) -> None:
        decision = _evaluate()
        assert decision.allowed
        assert decision.action == "auto_post"
        assert all(layer.passed for layer in decision.layers)
        assert decision.policy_config_id == "policy-1"
        assert decision.contract_id is None

    def test_installation_opt_in_off_denies(self) -> None:
        """Draft is the default: without the global opt-in nothing auto-posts."""
        decision = _evaluate(installation_auto_post_enabled=False)
        assert not decision.allowed
        assert decision.action == "draft"
        layer = next(layer for layer in decision.layers if layer.layer == "event_type")
        assert not layer.passed
        assert "not enabled for this installation" in layer.detail

    def test_missing_policy_denies(self) -> None:
        """No policy row for the event type → layer 1 denies."""
        decision = _evaluate(policy=None)
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "event_type")
        assert not layer.passed
        assert "No auto-post policy" in layer.detail

    def test_policy_not_allowed_denies(self) -> None:
        """An explicit allowed=False row denies."""
        decision = _evaluate(policy=_policy(allowed=False))
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "event_type")
        assert not layer.passed

    def test_missing_cap_denies(self) -> None:
        """No source recommends auto-post without a cap: no cap anywhere denies."""
        decision = _evaluate(policy=_policy(max_amount_irr=None))
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "amount_cap")
        assert not layer.passed
        assert "No auto-post amount cap" in layer.detail

    def test_over_cap_denies(self) -> None:
        """An amount above the policy cap never auto-posts."""
        decision = _evaluate(amount_irr=10_000_001)
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "amount_cap")
        assert not layer.passed
        assert "exceeds the auto-post cap" in layer.detail

    def test_at_cap_allows(self) -> None:
        """An amount exactly at the cap is within it."""
        decision = _evaluate(amount_irr=10_000_000)
        assert decision.allowed

    def test_contract_cap_is_the_stricter_one(self) -> None:
        """The effective cap is the min of the policy cap and the contract cap."""
        decision = _evaluate(amount_irr=6_000_000, contract=_contract())
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "amount_cap")
        assert "5000000" in layer.detail

    def test_contract_cap_alone_allows(self) -> None:
        """A contract cap can supply the cap when the policy has none."""
        decision = _evaluate(
            policy=_policy(max_amount_irr=None),
            amount_irr=4_000_000,
            contract=_contract(),
        )
        assert decision.allowed

    def test_draft_only_contract_denies(self) -> None:
        """A draft-only contract never auto-posts, whatever the policy says."""
        decision = _evaluate(contract=_contract(auto_post_policy=AutoPostPolicy.DRAFT_ONLY))
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "amount_cap")
        assert not layer.passed
        assert "draft-only" in layer.detail

    def test_closed_period_denies(self) -> None:
        """A closed (or unknown) fiscal period never auto-posts."""
        decision = _evaluate(period_open=False)
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "period_lock")
        assert not layer.passed
        assert "closed, locked, or unknown" in layer.detail

    def test_tieout_required_and_failed_denies(self) -> None:
        """A settlement event without tie-out never auto-posts."""
        decision = _evaluate(
            event_type="payment.settlement.received",
            policy=_policy(event_type="payment.settlement.received", require_tieout=True),
            tieout_ok=False,
        )
        assert not decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "tieout")
        assert not layer.passed

    def test_tieout_required_and_missing_denies(self) -> None:
        """A missing tie-out verdict is a failed tie-out (fail-closed)."""
        decision = _evaluate(
            event_type="payment.settlement.received",
            policy=_policy(event_type="payment.settlement.received", require_tieout=True),
            tieout_ok=None,
        )
        assert not decision.allowed

    def test_tieout_required_and_holding_allows(self) -> None:
        decision = _evaluate(
            event_type="payment.settlement.received",
            policy=_policy(event_type="payment.settlement.received", require_tieout=True),
            tieout_ok=True,
        )
        assert decision.allowed
        layer = next(layer for layer in decision.layers if layer.layer == "tieout")
        assert layer.passed

    def test_tieout_not_required_skips_layer(self) -> None:
        """Without require_tieout there is no tie-out layer at all."""
        decision = _evaluate()
        assert "tieout" not in {layer.layer for layer in decision.layers}

    def test_decision_records_policy_and_contract_versions(self) -> None:
        """The audit log needs the exact versions the decision used (§6.7)."""
        decision = _evaluate(contract=_contract())
        assert decision.policy_config_id == "policy-1"
        assert decision.contract_id == "contract-1"
        assert decision.contract_version == 1

    def test_reason_names_every_failed_layer(self) -> None:
        decision = _evaluate(
            installation_auto_post_enabled=False,
            period_open=False,
        )
        assert "not enabled" in decision.reason
        assert "closed, locked, or unknown" in decision.reason


# -- holiday calendar -----------------------------------------------------


class TestHolidayCalendar:
    def test_friday_is_the_default_weekly_holiday(self) -> None:
        """Friday (weekday 4) is the default weekly holiday in Iran."""
        assert FRIDAY == 4
        calendar = HolidayCalendar()
        # 2026-11-20 is a Friday.
        friday = datetime.date(2026, 11, 20)
        assert friday.weekday() == FRIDAY
        assert calendar.is_holiday(friday)
        assert not calendar.is_business_day(friday)

    def test_saturday_is_a_business_day_by_default(self) -> None:
        calendar = HolidayCalendar()
        saturday = datetime.date(2026, 11, 21)
        assert saturday.weekday() == 5
        assert calendar.is_business_day(saturday)

    def test_listed_holiday_is_a_holiday(self) -> None:
        """Nowruz 1405 (2026-03-21, a Saturday) on the list is a holiday."""
        nowruz = datetime.date(2026, 3, 21)
        calendar = HolidayCalendar(holidays=frozenset({nowruz}))
        assert calendar.is_holiday(nowruz)
        assert not calendar.is_business_day(nowruz)

    def test_weekly_holiday_is_configurable(self) -> None:
        """An installation may move its weekly holiday (e.g. Thursday)."""
        calendar = HolidayCalendar(weekly_holiday=3)  # Thursday
        thursday = datetime.date(2026, 11, 19)
        assert thursday.weekday() == 3
        assert calendar.is_holiday(thursday)
        assert calendar.is_business_day(datetime.date(2026, 11, 20))  # Friday

    def test_matching_window_uses_calendar_days(self) -> None:
        """Until a holiday source is verified, the window is ±N CALENDAR days."""
        calendar = HolidayCalendar(holidays=frozenset({datetime.date(2026, 11, 21)}))
        start, end = calendar.matching_window(datetime.date(2026, 11, 20), 2)
        assert start == datetime.date(2026, 11, 18)
        assert end == datetime.date(2026, 11, 22)
        # The listed holiday inside the window does not shrink it:
        # the window is calendar-based, not business-day-based.

    def test_add_business_days_skips_friday_and_holidays(self) -> None:
        """Day-lag arithmetic counts only business days."""
        # Wednesday 2026-11-18 + 2 business days = Sunday 2026-11-22
        # (Thursday 19th is day 1, Friday 20th is skipped, Saturday 21st
        # is a listed holiday and skipped, Sunday 22nd is day 2).
        calendar = HolidayCalendar(holidays=frozenset({datetime.date(2026, 11, 21)}))
        assert calendar.add_business_days(datetime.date(2026, 11, 18), 2) == datetime.date(
            2026, 11, 22
        )

    def test_add_business_days_zero_is_identity(self) -> None:
        calendar = HolidayCalendar()
        day = datetime.date(2026, 11, 18)
        assert calendar.add_business_days(day, 0) == day

    def test_add_business_days_rejects_negative(self) -> None:
        calendar = HolidayCalendar()
        with pytest.raises(ValueError, match="days must be >= 0"):
            calendar.add_business_days(datetime.date(2026, 11, 18), -1)

    def test_add_calendar_days(self) -> None:
        calendar = HolidayCalendar()
        assert calendar.add_calendar_days(datetime.date(2026, 11, 18), 5) == datetime.date(
            2026, 11, 23
        )
