"""Checkbook page data (US-532, §5 §2.3 + §7.5 report 8).

The checkbook page shows, per checkbook, the leaves used / voided /
remaining (computed by the engine's ``account.checkbook`` from the
issued cheques linked to it) plus the Fintech chequebook-inquiry.
The Fintech inquiry is a SIMULATOR until the ۵ت remainder (B3)
wires live connectivity: the simulator returns the engine's own
leaf counts with its source marked, and NEVER invents Sayyad data
(the Fintech scope field shapes are UNVERIFIED, §9.2 #1–3 — no
field is guessed here). When B3 lands, the simulator is replaced
by the real inquiry and this module keeps the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from hesabyar.domain.accounting.value_objects import CheckbookEngineView


@dataclass(frozen=True, slots=True)
class ChequebookInquiry:
    """The result of a chequebook inquiry (simulator until B3 live).

    ``source`` is always ``"simulator"`` until live Fintech
    connectivity exists; ``sayad_registered_leaves`` is None because
    the simulator cannot know Sayyad's view — the engine's leaf
    counts are the only ground truth it reports.
    """

    checkbook_engine_id: int
    source: str = "simulator"
    leaves_registered_in_sayad: int | None = None
    note: str = (
        "Fintech chequebook-inquiry is a SIMULATOR until the ۵ت "
        "remainder (B3) wires live connectivity — leaf counts below "
        "come from the engine registry, not from Sayyad."
    )


@dataclass(frozen=True, slots=True)
class CheckbookStatus:
    """One checkbook with its leaf counts + inquiry (report 8 data)."""

    checkbook: CheckbookEngineView
    inquiry: ChequebookInquiry

    @property
    def total_leaves(self) -> int:
        return self.checkbook.total_leaves

    @property
    def used_leaves(self) -> int:
        return self.checkbook.used_leaves

    @property
    def voided_leaves(self) -> int:
        return self.checkbook.voided_leaves

    @property
    def remaining_leaves(self) -> int:
        return self.checkbook.remaining_leaves


def simulate_chequebook_inquiry(
    checkbook: CheckbookEngineView,
) -> ChequebookInquiry:
    """Simulate the Fintech chequebook-inquiry for one checkbook.

    Pure function: it reports the engine's leaf counts as the only
    ground truth and marks itself a simulator. It never invents a
    Sayyad leaf count — ``leaves_registered_in_sayad`` stays None
    until B3 wires the real scope.
    """
    return ChequebookInquiry(checkbook_engine_id=checkbook.ref.engine_id)


def checkbook_status(checkbook: CheckbookEngineView) -> CheckbookStatus:
    """Build the checkbook page row for one engine checkbook."""
    return CheckbookStatus(
        checkbook=checkbook,
        inquiry=simulate_chequebook_inquiry(checkbook),
    )
