"""Unit tests for the Sayyad simulators (US-534, phased design §5 §1.3/§1.4).

The Fintech simulator answers the 18 documented scopes by NAME
only (field shapes UNVERIFIED, §9.2 #1–3 — nothing guessed); the
Ehrazino simulator answers the two VERIFIED shapes (cheque-color +
sayad-check). Every result is marked simulated, verdicts come only
from seeded fixtures (an unseeded id is ``not_found``, never an
invented verdict), and no result ever echoes a national id (§3.5).
"""

from __future__ import annotations

import pytest

from hesabyar.domain.cheques.sayyad import (
    FINNOTECH_SAYYAD_SCOPES,
    SayyadInquiryError,
    SayyadLiveNotReadyError,
    inquiry_type_for_scope,
)
from hesabyar.infrastructure.sayyad.simulator import (
    DeterministicEhrazinoSimulator,
    DeterministicFintechSayyadSimulator,
    EhrazinoColorScenario,
    EhrazinoLiveClient,
    EhrazinoSayadCheckScenario,
    FintechSayyadLiveClient,
    SayyadScenario,
    deterministic_reference,
)

SAYAD_ID = "1111222233334444"
NATIONAL_ID = "1234567890"


def test_all_eighteen_scopes_are_named() -> None:
    assert len(FINNOTECH_SAYYAD_SCOPES) == 18
    assert len(set(FINNOTECH_SAYYAD_SCOPES)) == 18
    assert all(scope.startswith("credit:") for scope in FINNOTECH_SAYYAD_SCOPES)
    assert sum(1 for scope in FINNOTECH_SAYYAD_SCOPES if ":sms-" in scope) == 8


def test_simulator_answers_every_scope_from_a_seed() -> None:
    sim = DeterministicFintechSayyadSimulator()
    sim.seed(
        SAYAD_ID,
        SayyadScenario(
            color_code=3,
            back_cheques_count=2,
            status="in_transit",
            items_matched=True,
            transfers=1,
            leaves_used=5,
            leaves_remaining=20,
            last_bounce_reason="insufficient_funds",
        ),
    )
    for scope in FINNOTECH_SAYYAD_SCOPES:
        result = sim.inquire(scope, sayad_id=SAYAD_ID, national_id=NATIONAL_ID)
        assert result.simulated is True
        assert result.result_code, scope
        assert result.inquiry_type == inquiry_type_for_scope(scope)
        assert NATIONAL_ID not in result.result_code + result.result_summary + result.reference


def test_simulator_verdicts_come_from_the_fixture() -> None:
    sim = DeterministicFintechSayyadSimulator()
    sim.seed(
        SAYAD_ID,
        SayyadScenario(color_code=5, back_cheques_count=12, status="in_transit"),
    )
    color = sim.inquire("credit:cheque-color-inquiry:get", sayad_id=SAYAD_ID)
    assert color.result_code == "color=5"
    assert color.color_code == 5
    back = sim.inquire("credit:back-cheques:get", sayad_id=SAYAD_ID)
    assert back.result_code == "back_cheques=12"
    assert back.back_cheques_count == 12
    status = sim.inquire("credit:cheque-status-inquiry:post", sayad_id=SAYAD_ID)
    assert status.result_code == "status=in_transit"
    # Sayyad in-transit is NOT accounting in_clearing — the summary says so.
    assert "NOT accounting in_clearing" in status.result_summary
    items = sim.inquire("credit:sayad-serial-inquiry:get", sayad_id=SAYAD_ID)
    assert items.items_matched is True


def test_simulator_never_invents_a_verdict_for_an_unseeded_id() -> None:
    sim = DeterministicFintechSayyadSimulator()
    result = sim.inquire("credit:cheque-color-inquiry:get", sayad_id=SAYAD_ID)
    assert result.result_code == "not_found"
    assert "no fixture seeded" in result.result_summary


def test_simulator_builtin_scenarios() -> None:
    sim = DeterministicFintechSayyadSimulator()
    clean = sim.inquire("credit:cheque-color-inquiry:get", sayad_id=SAYAD_ID, scenario="clean")
    assert clean.color_code == 1
    red = sim.inquire("credit:cheque-color-inquiry:get", sayad_id=SAYAD_ID, scenario="red")
    assert red.color_code == 5
    assert red.back_cheques_count is None  # colour scope carries no back-cheques count


def test_simulator_unknown_scope_fails_closed() -> None:
    sim = DeterministicFintechSayyadSimulator()
    with pytest.raises(SayyadInquiryError):
        sim.inquire("credit:not-a-scope:get", sayad_id=SAYAD_ID)


def test_simulator_reference_is_deterministic() -> None:
    first = deterministic_reference("credit:cheque-color-inquiry:get", SAYAD_ID)
    second = deterministic_reference("credit:cheque-color-inquiry:get", SAYAD_ID)
    assert first == second
    assert first.startswith("SIM-")
    assert (
        deterministic_reference("credit:back-cheques:get", SAYAD_ID) != first
    )


def test_ehrazino_color_verified_shape_and_cost() -> None:
    sim = DeterministicEhrazinoSimulator()
    sim.seed_color(NATIONAL_ID, EhrazinoColorScenario(color_code=2))
    result = sim.cheque_color(NATIONAL_ID)
    assert result.color_code == 2
    assert result.color_name == "yellow"
    assert result.cost_irr == 17_000  # 1,700 Toman
    assert result.simulated is True


def test_ehrazino_color_rejects_a_bad_national_code() -> None:
    sim = DeterministicEhrazinoSimulator()
    with pytest.raises(SayyadInquiryError):
        sim.cheque_color("123")
    with pytest.raises(SayyadInquiryError):
        sim.cheque_color("12345678901x")


def test_ehrazino_color_never_invents_a_verdict() -> None:
    sim = DeterministicEhrazinoSimulator()
    with pytest.raises(SayyadInquiryError, match="no colour fixture"):
        sim.cheque_color(NATIONAL_ID)


def test_ehrazino_sayad_check_verified_shape_and_cost() -> None:
    sim = DeterministicEhrazinoSimulator()
    sim.seed_sayad_check(
        SAYAD_ID,
        EhrazinoSayadCheckScenario(
            items_matched=True, serial_no="987654", series_no="12", branch_code="056"
        ),
    )
    result = sim.sayad_check(SAYAD_ID)
    assert result.items_matched is True
    assert result.serial_no == "987654"
    assert result.series_no == "12"
    assert result.cost_irr == 69_000  # 6,900 Toman
    assert result.simulated is True
    # The result carries no issuer name / IBAN (§3.5 — dropped at the boundary).
    assert not hasattr(result, "name")
    assert not hasattr(result, "iban")


def test_ehrazino_sayad_check_rejects_a_bad_sayad_id() -> None:
    sim = DeterministicEhrazinoSimulator()
    with pytest.raises(SayyadInquiryError):
        sim.sayad_check("123")


def test_live_clients_raise_not_ready_instead_of_guessing() -> None:
    finnotech = FintechSayyadLiveClient(
        base_url="https://api.example.invalid", client_id="x", client_secret="y"
    )
    assert finnotech.available_scopes() == FINNOTECH_SAYYAD_SCOPES
    with pytest.raises(SayyadLiveNotReadyError, match="UNVERIFIED"):
        finnotech.inquire("credit:cheque-color-inquiry:get", sayad_id=SAYAD_ID)
    ehrazino = EhrazinoLiveClient(base_url="https://api.example.invalid", api_key="x")
    with pytest.raises(SayyadLiveNotReadyError, match="UNVERIFIED"):
        ehrazino.cheque_color(NATIONAL_ID)
    with pytest.raises(SayyadLiveNotReadyError, match="UNVERIFIED"):
        ehrazino.sayad_check(SAYAD_ID)
