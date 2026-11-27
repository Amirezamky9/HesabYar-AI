"""Three-way mismatch matrix (US-533, §5 §1.2 + §2.1).

When the physical leaf arrives, three sources must agree on the
cheque's amount and due date:

- the SITE registration (what the customer registered on the store),
- the LEAF itself (what is written on the paper),
- SAYYAD (what the issuer registered in the CBI system).

Pure functions — no I/O. A reading is ``None`` when that source
has not (yet) supplied the value; a missing reading is not a
mismatch, it is an unknown — the caller asks the operator to supply
it. A mismatch on ANY compared field means NEVER confirm: the
service files a ``three_way_mismatch`` review item and rejects the
intake (``registered -> rejected_mismatch``), it never confirms
receipt on a mismatch.

The same matrix shape also serves the other cheque exception
subtypes the unified review queue files (US-533): the subtype
names are the contract with the queue — see
:mod:`hesabyar.application.commands.cheques`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import StrEnum


class MismatchField(StrEnum):
    """Which cheque field the three sources disagree on."""

    AMOUNT = "amount"
    DUE_DATE = "due_date"


@dataclass(frozen=True, slots=True)
class SourceReading:
    """One source's reading of a cheque's amount and due date.

    ``amount_irr`` is whole Rial; ``due_date`` is a Gregorian date.
    Either may be None when the source has not supplied it.
    """

    amount_irr: int | None = None
    due_date: datetime.date | None = None


@dataclass(frozen=True, slots=True)
class ThreeWayMismatch:
    """The outcome of comparing site vs leaf vs Sayyad."""

    mismatched_fields: tuple[MismatchField, ...] = ()
    #: Per-field, which sources disagreed (only the fields that
    #: mismatched appear here).
    detail: dict[MismatchField, str] = field(default_factory=dict)

    @property
    def has_mismatch(self) -> bool:
        return bool(self.mismatched_fields)

    def describe(self) -> str:
        """A one-line human-readable description for the review item."""
        if not self.has_mismatch:
            return "Site, leaf and Sayyad agree on amount and due date."
        parts = []
        for field_name in self.mismatched_fields:
            parts.append(f"{field_name.value}: {self.detail[field_name]}")
        return "Three-way mismatch — " + "; ".join(parts)


def evaluate_three_way_mismatch(
    *,
    site: SourceReading,
    leaf: SourceReading,
    sayad: SourceReading,
) -> ThreeWayMismatch:
    """Compare the three readings on amount and due date.

    A field mismatches when at least two supplied readings disagree.
    A field supplied by only one source (or none) cannot mismatch —
    it is unknown, not wrong.
    """
    mismatched: list[MismatchField] = []
    detail: dict[MismatchField, str] = {}
    for field_name, attr in (
        (MismatchField.AMOUNT, "amount_irr"),
        (MismatchField.DUE_DATE, "due_date"),
    ):
        values = {
            source: getattr(reading, attr)
            for source, reading in (
                ("site", site),
                ("leaf", leaf),
                ("sayad", sayad),
            )
            if getattr(reading, attr) is not None
        }
        distinct = set(values.values())
        if len(distinct) > 1:
            mismatched.append(field_name)
            rendered = ", ".join(f"{name}={value}" for name, value in sorted(values.items()))
            detail[field_name] = rendered
    return ThreeWayMismatch(mismatched_fields=tuple(mismatched), detail=detail)


#: Cheque exception subtypes filed to the unified review queue
#: (US-533, §5 §6/§7). One subtype per exception kind so the queue
#: can filter and count them; the filing sites live in
#: :class:`ChequeCommandService
#: <hesabyar.application.commands.cheques.ChequeCommandService>`.
CHEQUE_REVIEW_SUBTYPES: tuple[str, ...] = (
    "three_way_mismatch",
    "duplicate_sayad_id",
    "cancellation_after_held",
    "stale_in_clearing",
    "never_arrived_cheque",
    "ambiguous_matching",
)
