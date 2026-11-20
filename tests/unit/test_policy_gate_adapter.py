"""Unit tests for the registry-backed policy gate adapter (US-511).

``RegistryPolicyGate`` translates the pipeline's ``PolicyGatePort``
call into the US-503 ``PolicyGateService`` and back. These tests run
the REAL service over the in-memory registry fake, so they prove the
translation — not just the adapter's shape: the fail-closed defaults
(no installation, no company, no opt-in, no policy), the allow path
(opt-in + policy + cap + open period), and the channel layer.

No DB, no engine: the fiscal checker is the stub from the US-503
service tests.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from hesabyar.application.commands.events import normalize_event
from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
    PolicyGateService,
)
from hesabyar.application.commands.policy_gate import RegistryPolicyGate
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.models import NormalizedEvent, ResolvedMapping, StoreSettings
from hesabyar.domain.integrations.commands import (
    UpsertInstallationSettings,
    UpsertPolicyGateConfig,
)
from hesabyar.domain.integrations.models import AutoPostPolicy
from tests.event_fixtures import INSTALLATION_ID, raw_envelope
from tests.unit.test_integrations_services import (
    FOUR_TRANCHES,
    FakeIntegrationsRepository,
    StubFiscalChecker,
)

ORDER_TOTAL_RIAL = 280000
EVENT_DATE = datetime.date(2026, 11, 17)


def make_event(
    payload: dict[str, Any] | None = None,
    *,
    installation_id: str | None = INSTALLATION_ID,
) -> NormalizedEvent:
    """Normalize one order event the way the pipeline does."""
    event = CanonicalEvent.from_raw(
        raw_envelope(
            payload=payload if payload is not None else {"total_amount": 28000},
            external_entity_type="order",
            external_entity_id="order-1",
        )
    )
    return normalize_event(
        event,
        StoreSettings(source_system="hayat", source_store_id="store-1"),
        event_type=CanonicalEventType.SALES_ORDER_COMPLETED,
        installation_id=installation_id,
    )


def make_mapping(*, company: CompanyRef | None = CompanyRef(1)) -> ResolvedMapping:
    return ResolvedMapping(mapping_version="test-v1", company=company)


def make_gate(
    repo: FakeIntegrationsRepository, checker: StubFiscalChecker
) -> RegistryPolicyGate:
    return RegistryPolicyGate(PolicyGateService(repo, checker))


def enable_auto_post(
    repo: FakeIntegrationsRepository,
    *,
    cap: int | None = 1_000_000,
    allowed: bool = True,
    require_tieout: bool = False,
) -> None:
    """Opt the installation in + allow the order event type."""
    repo.save_installation_settings(
        UpsertInstallationSettings(
            installation_id=INSTALLATION_ID, auto_post_enabled=True
        )
    )
    repo.save_policy_config(
        UpsertPolicyGateConfig(
            installation_id=INSTALLATION_ID,
            event_type=CanonicalEventType.SALES_ORDER_COMPLETED.value,
            allowed=allowed,
            max_amount_irr=cap,
            require_tieout=require_tieout,
        )
    )


def make_channel_with_contract(
    repo: FakeIntegrationsRepository,
    *,
    contract_cap: int | None = 500_000,
    auto_post_policy: AutoPostPolicy = AutoPostPolicy.AUTO_POST_WITHIN_POLICY,
) -> None:
    """Create zarinpal, attach one active contract, and enable it.

    Channels are born disabled; the operator enables them after
    attaching a contract.
    """
    ChannelRegistryService(repo).create_channel(
        INSTALLATION_ID, "zarinpal", "زرین‌پال", "gateway"
    )
    GatewayContractService(repo).create_version(
        INSTALLATION_ID,
        "zarinpal",
        valid_from=datetime.date(2026, 11, 1),
        fee_percent=Decimal("2"),
        tranche_legs=FOUR_TRANCHES,
        auto_post_policy=auto_post_policy,
        auto_post_max_amount_irr=contract_cap,
    )
    ChannelRegistryService(repo).update_channel(
        INSTALLATION_ID, "zarinpal", enabled=True
    )


# ---------------------------------------------------------------------------
# Adapter-level fail-closed
# ---------------------------------------------------------------------------


def test_missing_installation_context_denies_without_touching_the_registry() -> None:
    repo = FakeIntegrationsRepository()
    checker = StubFiscalChecker(period_open=True)
    gate = make_gate(repo, checker)
    decision = gate.evaluate(
        make_event(installation_id=None), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL)
    )
    assert not decision.allowed
    assert decision.policy_version == RegistryPolicyGate.POLICY_VERSION_FALLBACK
    assert len(decision.gate_results) == 1
    assert decision.gate_results[0].name == "installation_context"
    assert not decision.gate_results[0].passed
    assert checker.calls == []


def test_missing_company_denies_without_touching_the_registry() -> None:
    repo = FakeIntegrationsRepository()
    checker = StubFiscalChecker(period_open=True)
    gate = make_gate(repo, checker)
    decision = gate.evaluate(
        make_event(), make_mapping(company=None), MoneyIRR(ORDER_TOTAL_RIAL)
    )
    assert not decision.allowed
    assert decision.gate_results[0].name == "company_context"
    assert checker.calls == []


# ---------------------------------------------------------------------------
# Draft is the default
# ---------------------------------------------------------------------------


def test_no_settings_and_no_policy_denies() -> None:
    repo = FakeIntegrationsRepository()
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(make_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    assert decision.policy_version == RegistryPolicyGate.POLICY_VERSION_FALLBACK
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["event_type"].passed
    assert "not enabled" in layers["event_type"].reason


def test_policy_row_with_allowed_false_denies() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo, allowed=False)
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(make_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["event_type"].passed


def test_amount_over_the_cap_denies() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo, cap=100)
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(make_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert layers["event_type"].passed
    assert not layers["amount_cap"].passed


def test_closed_period_denies() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo)
    gate = make_gate(repo, StubFiscalChecker(period_open=False))
    decision = gate.evaluate(make_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert layers["event_type"].passed
    assert layers["amount_cap"].passed
    assert not layers["period_lock"].passed


def test_tieout_required_policy_denies_without_a_tieout() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo, require_tieout=True)
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(make_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["tieout"].passed


# ---------------------------------------------------------------------------
# The allow path
# ---------------------------------------------------------------------------


def test_opt_in_plus_policy_plus_cap_plus_open_period_allows() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo)
    checker = StubFiscalChecker(period_open=True)
    gate = make_gate(repo, checker)
    decision = gate.evaluate(make_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert decision.allowed
    assert decision.policy_version.startswith("policy-gate:")
    assert all(result.passed for result in decision.gate_results)
    assert {result.name for result in decision.gate_results} == {
        "event_type",
        "amount_cap",
        "period_lock",
    }
    # The fiscal check runs for the event's own date, not today.
    assert checker.calls == [(CompanyRef(1), EVENT_DATE)]


# ---------------------------------------------------------------------------
# The channel layer (payment events carry payment_channel)
# ---------------------------------------------------------------------------


def _payment_event() -> NormalizedEvent:
    return make_event({"gross_amount": 28000, "payment_channel": "zarinpal"})


def test_payment_channel_missing_from_the_registry_denies() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo)
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(_payment_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["channel"].passed
    assert "not in the registry" in layers["channel"].reason


def test_disabled_channel_denies() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo)
    make_channel_with_contract(repo)
    ChannelRegistryService(repo).update_channel(
        INSTALLATION_ID, "zarinpal", enabled=False
    )
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(_payment_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["channel"].passed
    assert "disabled" in layers["channel"].reason


def test_channel_without_a_contract_denies() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo)
    ChannelRegistryService(repo).create_channel(
        INSTALLATION_ID, "zarinpal", "زرین‌پال", "gateway"
    )
    # Enabled but contract-less: the gate must reach the contract check.
    ChannelRegistryService(repo).update_channel(
        INSTALLATION_ID, "zarinpal", enabled=True
    )
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(_payment_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["channel"].passed
    assert "no active contract" in layers["channel"].reason


def test_channel_with_an_active_contract_allows_within_the_contract_cap() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo)
    make_channel_with_contract(repo, contract_cap=500_000)
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(_payment_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert decision.allowed


def test_the_stricter_contract_cap_wins_over_the_policy_cap() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo, cap=1_000_000)
    make_channel_with_contract(repo, contract_cap=100)
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(_payment_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["amount_cap"].passed


def test_a_draft_only_contract_denies_even_with_an_allowed_policy() -> None:
    repo = FakeIntegrationsRepository()
    enable_auto_post(repo)
    make_channel_with_contract(repo, auto_post_policy=AutoPostPolicy.DRAFT_ONLY)
    gate = make_gate(repo, StubFiscalChecker(period_open=True))
    decision = gate.evaluate(_payment_event(), make_mapping(), MoneyIRR(ORDER_TOTAL_RIAL))
    assert not decision.allowed
    layers = {result.name: result for result in decision.gate_results}
    assert not layers["amount_cap"].passed
    assert "draft-only" in layers["amount_cap"].reason
