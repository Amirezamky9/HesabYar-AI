"""Unit tests for the deterministic Moadian simulator + invoice validator (US-404).

DB-free: the simulator is in-memory and the validator is pure, so
these run without the shared test database.
"""

from __future__ import annotations

import uuid

import pytest

from hesabyar.domain.moadian.models import (
    OfficialStatus,
    SimulationScenario,
    validate_invoice_payload,
)
from hesabyar.domain.moadian.ports import SimulatorSubmitResult
from hesabyar.infrastructure.moadian.simulator import (
    DeterministicMoadianSimulator,
    deterministic_reference_number,
)
from tests.moadian_fixtures import (
    copy_payload,
    taxid_for,
    valid_invoice_payload,
)

UID_1 = str(uuid.uuid3(uuid.NAMESPACE_URL, "hesabyar:test:uid-1"))
UID_2 = str(uuid.uuid3(uuid.NAMESPACE_URL, "hesabyar:test:uid-2"))


def _packet(uid: str, fiscal_id: str | None = "AA56CD") -> dict:
    return {
        "uid": uid,
        "packetType": "INVOICE.V01",
        "retry": False,
        "data": '{"header": {}}',
        "encryptionKeyId": None,
        "symmetrickey": None,
        "iv": None,
        "fiscalId": fiscal_id,
        "dataSignature": None,
    }


# ---------------------------------------------------------------------------
# Simulator: submit
# ---------------------------------------------------------------------------


def test_submit_returns_deterministic_receipt() -> None:
    simulator = DeterministicMoadianSimulator()
    result = simulator.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.ACCEPT)
    assert isinstance(result, SimulatorSubmitResult)
    assert result.accepted
    assert result.uid == UID_1
    assert result.reference_number == deterministic_reference_number(UID_1)
    assert result.error_code is None
    # A fresh instance derives the same receipt for the same uid.
    other = DeterministicMoadianSimulator()
    again = other.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.ACCEPT)
    assert again.reference_number == result.reference_number


def test_submit_duplicate_uid_refused_with_official_error_5005() -> None:
    simulator = DeterministicMoadianSimulator()
    first = simulator.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.ACCEPT)
    assert first.accepted
    second = simulator.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.ACCEPT)
    assert not second.accepted
    assert second.reference_number is None
    assert second.error_code == "5005"
    assert second.error_detail == "duplicate.request.uid"


def test_submit_malformed_uid_refused_with_official_error_5003() -> None:
    simulator = DeterministicMoadianSimulator()
    result = simulator.submit_packet(uid="not-a-uuid", packet=_packet("not-a-uuid"), scenario=SimulationScenario.ACCEPT)
    assert not result.accepted
    assert result.error_code == "5003"
    assert result.error_detail == "uid.format.is.not.valid"


def test_simulator_performs_no_network_calls() -> None:
    """The simulator module must not import any HTTP client (spec §12.3)."""
    import ast
    from pathlib import Path

    import hesabyar.infrastructure.moadian.simulator as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = imported & {"httpx", "requests", "urllib", "http", "socket"}
    assert not forbidden, f"simulator imports network modules: {sorted(forbidden)}"


# ---------------------------------------------------------------------------
# Simulator: inquiry lifecycle
# ---------------------------------------------------------------------------


def test_inquiry_pending_then_success_for_accept_scenario() -> None:
    simulator = DeterministicMoadianSimulator()
    submitted = simulator.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.ACCEPT)
    first = simulator.inquiry_by_uid([UID_1])[0]
    assert first.status == OfficialStatus.PENDING
    assert first.reference_number == submitted.reference_number
    assert first.packet_type == "INVOICE.V01"
    assert first.fiscal_id == "AA56CD"
    assert first.data is None
    second = simulator.inquiry_by_uid([UID_1])[0]
    assert second.status == OfficialStatus.SUCCESS
    assert second.data == {"simulated": True}
    # Terminal status is stable on further polls.
    third = simulator.inquiry_by_uid([UID_1])[0]
    assert third.status == OfficialStatus.SUCCESS


def test_inquiry_pending_then_failed_for_reject_scenario() -> None:
    simulator = DeterministicMoadianSimulator()
    simulator.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.REJECT)
    assert simulator.inquiry_by_uid([UID_1])[0].status == OfficialStatus.PENDING
    failed = simulator.inquiry_by_uid([UID_1])[0]
    assert failed.status == OfficialStatus.FAILED
    assert failed.data is not None
    assert failed.data["simulated"] is True
    errors = failed.data["error"]
    assert errors[0]["code"] == "SIMULATED_REJECTION"
    assert "simulated" in errors[0]["message"].lower()


def test_inquiry_unknown_uid_is_not_found() -> None:
    simulator = DeterministicMoadianSimulator()
    item = simulator.inquiry_by_uid([UID_1])[0]
    assert item.status == OfficialStatus.NOT_FOUND
    assert item.reference_number is None
    assert item.data is None


def test_inquiry_batch_reports_each_uid() -> None:
    simulator = DeterministicMoadianSimulator()
    simulator.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.ACCEPT)
    items = simulator.inquiry_by_uid([UID_1, UID_2])
    assert [item.uid for item in items] == [UID_1, UID_2]
    assert items[0].status == OfficialStatus.PENDING
    assert items[1].status == OfficialStatus.NOT_FOUND


def test_register_submitted_resumes_lifecycle_from_poll_count() -> None:
    simulator = DeterministicMoadianSimulator()
    reference = deterministic_reference_number(UID_1)
    # Zero prior polls: PENDING first, then SUCCESS.
    simulator.register_submitted(
        uid=UID_1,
        reference_number=reference,
        scenario=SimulationScenario.ACCEPT,
        fiscal_id="AA56CD",
        polls=0,
    )
    assert simulator.inquiry_by_uid([UID_1])[0].status == OfficialStatus.PENDING
    assert simulator.inquiry_by_uid([UID_1])[0].status == OfficialStatus.SUCCESS
    # One prior poll recorded: the next poll is already terminal.
    other = DeterministicMoadianSimulator()
    other.register_submitted(
        uid=UID_2,
        reference_number=deterministic_reference_number(UID_2),
        scenario=SimulationScenario.ACCEPT,
        polls=1,
    )
    assert other.inquiry_by_uid([UID_2])[0].status == OfficialStatus.SUCCESS


def test_reset_clears_registered_packets() -> None:
    simulator = DeterministicMoadianSimulator()
    simulator.submit_packet(uid=UID_1, packet=_packet(UID_1), scenario=SimulationScenario.ACCEPT)
    assert UID_1 in simulator.registered_uids
    simulator.reset()
    assert simulator.registered_uids == frozenset()
    assert simulator.inquiry_by_uid([UID_1])[0].status == OfficialStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Invoice payload validator (instruction Table 7)
# ---------------------------------------------------------------------------


def test_valid_payload_passes() -> None:
    assert validate_invoice_payload(valid_invoice_payload()) == []


def test_missing_header_or_body_fails() -> None:
    assert "header must be an object." in validate_invoice_payload({})
    assert "header must be an object." in validate_invoice_payload({"header": None, "body": []})
    payload = valid_invoice_payload()
    del payload["body"]
    assert "body must be a non-empty array of invoice lines." in validate_invoice_payload(payload)
    payload = valid_invoice_payload()
    payload["body"] = []
    assert "body must be a non-empty array of invoice lines." in validate_invoice_payload(payload)


def test_missing_required_header_fields_listed() -> None:
    payload = valid_invoice_payload()
    del payload["header"]["tins"]
    del payload["header"]["tbill"]
    payload["header"]["tvam"] = None
    problems = validate_invoice_payload(payload)
    assert "header.tins is required." in problems
    assert "header.tbill is required." in problems
    assert "header.tvam is required." in problems


def test_taxid_and_serial_lengths_enforced() -> None:
    payload = valid_invoice_payload(taxid="SHORT")
    assert "header.taxid must be a 22-character string." in validate_invoice_payload(payload)
    payload = valid_invoice_payload(serial="123")
    assert "header.inno must be a 10-character string." in validate_invoice_payload(payload)
    assert validate_invoice_payload(valid_invoice_payload(taxid=taxid_for(7))) == []


def test_enum_fields_restricted_to_table_7_values() -> None:
    payload = valid_invoice_payload(inty=9)
    assert "header.inty must be one of [1, 2, 3]." in validate_invoice_payload(payload)
    payload = valid_invoice_payload(inp=7)
    assert "header.inp must be one of [1, 2, 3, 4, 5, 6]." in validate_invoice_payload(payload)
    payload = valid_invoice_payload(ins=0)
    assert "header.ins must be one of [1, 2, 3, 4]." in validate_invoice_payload(payload)
    payload = valid_invoice_payload(tob=6)
    assert "header.tob must be one of [1, 2, 3, 4, 5]." in validate_invoice_payload(payload)
    payload = valid_invoice_payload(setm=4)
    assert "header.setm must be one of [1, 2, 3]." in validate_invoice_payload(payload)


def test_economic_code_accepts_10_or_14_digits() -> None:
    payload = valid_invoice_payload()
    payload["header"]["tins"] = 1234567890
    assert validate_invoice_payload(payload) == []
    payload = valid_invoice_payload()
    payload["header"]["tins"] = 12345678901
    assert "header.tins must be 10 or 14 digits." in validate_invoice_payload(payload)
    payload = valid_invoice_payload()
    payload["header"]["tins"] = "32652362589632"
    assert "header.tins must be an integer." in validate_invoice_payload(payload)


def test_corrective_invoice_requires_reference_taxid() -> None:
    payload = valid_invoice_payload(ins=2)
    assert "header.irtaxid is required when header.ins is 2, 3 or 4." in validate_invoice_payload(payload)
    payload = valid_invoice_payload(ins=3, irtaxid="SHORT")
    assert "header.irtaxid must be a 22-character string." in validate_invoice_payload(payload)
    payload = valid_invoice_payload(ins=4, irtaxid=taxid_for(99))
    assert validate_invoice_payload(payload) == []


def test_body_line_required_fields_and_types() -> None:
    payload = valid_invoice_payload()
    del payload["body"][0]["sstid"]
    del payload["body"][0]["tsstam"]
    payload["body"][0]["fee"] = "expensive"
    problems = validate_invoice_payload(payload)
    assert "body[0].sstid is required." in problems
    assert "body[0].tsstam is required." in problems
    assert "body[0].fee must be a number." in problems
    payload = valid_invoice_payload()
    payload["body"][0]["sstid"] = 123
    assert "body[0].sstid must be 13 digits." in validate_invoice_payload(payload)
    payload = valid_invoice_payload()
    payload["body"].append("not-an-object")
    assert "body[1] must be an object." in validate_invoice_payload(payload)


def test_payment_must_be_an_array_when_present() -> None:
    payload = valid_invoice_payload()
    payload["payment"] = {"iinn": 1}
    assert "payment must be an array when present." in validate_invoice_payload(payload)
    payload = valid_invoice_payload()
    payload["payment"] = []
    assert validate_invoice_payload(payload) == []


def test_validator_never_mutates_the_payload() -> None:
    payload = valid_invoice_payload()
    snapshot = copy_payload(payload)
    validate_invoice_payload(payload)
    assert payload == snapshot


@pytest.mark.parametrize(
    "field",
    ["taxid", "indatim", "indati2m", "inty", "inno", "inp", "ins", "tins", "tob"],
)
def test_each_required_header_field_is_required(field: str) -> None:
    payload = valid_invoice_payload()
    del payload["header"][field]
    assert f"header.{field} is required." in validate_invoice_payload(payload)
