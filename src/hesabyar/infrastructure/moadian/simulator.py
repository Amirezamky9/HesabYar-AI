"""Deterministic, fixture-based Moadian simulator (spec §12.3, US-404).

Mimics the Moadian sandbox lifecycle — submit → accepted into the
queue → status polling — with no network calls and no real Moadian
endpoint. Every output is a pure function of the inputs plus the
simulator's in-memory registry, so rehearsals are reproducible:

- ``submit_packet`` echoes the packet's uid and issues a deterministic
  receipt (``referenceNumber``): ``uuid5`` over the uid, so the same
  uid always gets the same receipt. A duplicate uid is refused with
  the instruction's own transport error 5005
  (``duplicate.request.uid``); a malformed uid with 5003
  (``uid.format.is.not.valid``).
- ``inquiry_by_uid`` returns ``PENDING`` on the first poll of a
  packet (the instruction: a ``PENDING`` request "هنوز درخواست پردازش
  نشده است" — has not been processed yet) and the scenario's terminal
  status from the second poll: ``SUCCESS`` for the ``accept``
  scenario, ``FAILED`` with simulated error evidence for ``reject``.
  An unknown uid reports ``NOT_FOUND``.
- The simulated receipt and error evidence are clearly marked
  simulated in the stored payloads; nothing here can be mistaken for
  an official Moadian response.

The simulator holds state only in memory: a fresh instance starts
empty (``reset()`` clears it explicitly). Tests and the
reconciliation worker share one module-level instance per process;
the REST layer builds one per request through the dependency in
``interfaces.rest.dependencies`` — in-memory state therefore does
NOT survive across requests. That is intentional for this phase: the
database is the durable record (submission status, reference number,
status events), and the simulator is re-seeded from it on each
reconciliation pass (see ``MoadianReconciliationWorker``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from hesabyar.domain.moadian.models import (
    INVOICE_PACKET_TYPE,
    OfficialStatus,
    SimulationScenario,
)
from hesabyar.domain.moadian.ports import (
    SimulatorInquiryItem,
    SimulatorSubmitResult,
)

#: Namespace for deterministic receipt ids (uuid5 over the uid).
_RECEIPT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

#: Namespace for the simulated error evidence marker.
_SIMULATED_ERROR_CODE = "SIMULATED_REJECTION"

#: Polls before the terminal status is reported (first poll: PENDING).
_POLLS_BEFORE_TERMINAL = 2


@dataclass
class _RegisteredPacket:
    """One packet the simulator has accepted into its queue."""

    uid: str
    reference_number: str
    scenario: SimulationScenario
    packet_type: str
    fiscal_id: str | None
    polls: int = 0
    data: dict[str, Any] = field(default_factory=dict)


def deterministic_reference_number(uid: str) -> str:
    """Derive the simulated receipt id for a uid (uuid5, stable)."""
    return str(uuid.uuid5(_RECEIPT_NAMESPACE, f"moadian-sim:receipt:{uid}"))


class DeterministicMoadianSimulator:
    """In-memory, deterministic stand-in for the Moadian API.

    No network, no signing, no encryption: the packet's ``data`` is
    stored verbatim as the submitted evidence and never transformed.
    """

    def __init__(self) -> None:
        self._packets: dict[str, _RegisteredPacket] = {}

    def reset(self) -> None:
        """Clear all registered packets (test isolation)."""
        self._packets.clear()

    @property
    def registered_uids(self) -> frozenset[str]:
        """Uids currently registered (introspection for tests)."""
        return frozenset(self._packets)

    def submit_packet(
        self,
        *,
        uid: str,
        packet: dict[str, Any],
        scenario: SimulationScenario,
    ) -> SimulatorSubmitResult:
        """Register one packet and issue its (simulated) receipt.

        A malformed uid fails with the instruction's error 5003; a
        uid already in the queue fails with error 5005 (the
        instruction: "ارسال درخواست با uid تکراری در packet").
        """
        try:
            uuid.UUID(uid)
        except ValueError:
            return SimulatorSubmitResult(
                uid=uid,
                reference_number=None,
                error_code="5003",
                error_detail="uid.format.is.not.valid",
            )
        if uid in self._packets:
            return SimulatorSubmitResult(
                uid=uid,
                reference_number=None,
                error_code="5005",
                error_detail="duplicate.request.uid",
            )
        reference_number = deterministic_reference_number(uid)
        packet_type = packet.get("packetType")
        fiscal_id = packet.get("fiscalId")
        self._packets[uid] = _RegisteredPacket(
            uid=uid,
            reference_number=reference_number,
            scenario=scenario,
            packet_type=packet_type if isinstance(packet_type, str) else INVOICE_PACKET_TYPE,
            fiscal_id=fiscal_id if isinstance(fiscal_id, str) else None,
            data=dict(packet),
        )
        return SimulatorSubmitResult(uid=uid, reference_number=reference_number)

    def register_submitted(
        self,
        *,
        uid: str,
        reference_number: str,
        scenario: SimulationScenario,
        packet_type: str = INVOICE_PACKET_TYPE,
        fiscal_id: str | None = None,
        polls: int = 0,
    ) -> None:
        """Re-seed a packet that was submitted in an earlier process.

        The reconciliation worker calls this for submissions whose
        status is ``submitted`` (their receipt lives in the database)
        so a fresh simulator instance can still poll them. ``polls``
        is the number of inquiries already recorded for the
        submission (counted from its persisted status events), so the
        lifecycle resumes where it left off instead of restarting.
        """
        if uid in self._packets:
            return
        self._packets[uid] = _RegisteredPacket(
            uid=uid,
            reference_number=reference_number,
            scenario=scenario,
            packet_type=packet_type,
            fiscal_id=fiscal_id,
            polls=polls,
        )

    def inquiry_by_uid(self, uids: list[str]) -> list[SimulatorInquiryItem]:
        """Report the simulated status of each uid (Table 10 shape)."""
        items: list[SimulatorInquiryItem] = []
        for uid in uids:
            registered = self._packets.get(uid)
            if registered is None:
                items.append(
                    SimulatorInquiryItem(
                        uid=uid,
                        reference_number=None,
                        status=OfficialStatus.NOT_FOUND,
                        data=None,
                        packet_type=None,
                        fiscal_id=None,
                    )
                )
                continue
            registered.polls += 1
            if registered.polls < _POLLS_BEFORE_TERMINAL:
                items.append(
                    SimulatorInquiryItem(
                        uid=uid,
                        reference_number=registered.reference_number,
                        status=OfficialStatus.PENDING,
                        data=None,
                        packet_type=registered.packet_type,
                        fiscal_id=registered.fiscal_id,
                    )
                )
            elif registered.scenario == SimulationScenario.REJECT:
                items.append(
                    SimulatorInquiryItem(
                        uid=uid,
                        reference_number=registered.reference_number,
                        status=OfficialStatus.FAILED,
                        data={
                            "simulated": True,
                            "error": [
                                {
                                    "code": _SIMULATED_ERROR_CODE,
                                    "message": (
                                        "Simulator fixture: the invoice was rejected "
                                        "(scenario 'reject'). This is simulated "
                                        "evidence, not an official Moadian response."
                                    ),
                                }
                            ],
                        },
                        packet_type=registered.packet_type,
                        fiscal_id=registered.fiscal_id,
                    )
                )
            else:
                items.append(
                    SimulatorInquiryItem(
                        uid=uid,
                        reference_number=registered.reference_number,
                        status=OfficialStatus.SUCCESS,
                        data={"simulated": True},
                        packet_type=registered.packet_type,
                        fiscal_id=registered.fiscal_id,
                    )
                )
        return items
