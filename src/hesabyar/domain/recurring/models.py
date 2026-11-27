"""Recurring template + occurrence domain models (US-563, Phase 5-7 ۵خ W3).

A **template** is a standing instruction: "for this company, on
this Jalali schedule, create this invoice / journal". Each firing
creates one **occurrence** — a draft document in the engine (auto-
post only with the template's explicit opt-in AND the policy
gate's blessing) plus a HesabYar-side occurrence row that links
the document to its template and occurrence number.

The snapshot rule (the acceptance criterion that editing a
template must not rewrite already-generated drafts): every
occurrence stores the payload it was generated from. The engine
document is created from that snapshot, and a later template edit
only changes FUTURE occurrences — the snapshot (and the draft it
made) is never touched. The schedule shape is likewise immutable
once the template has an occurrence (see
:class:`RecurringSchedule.shape`), so an occurrence number never
changes meaning.

Money is Rial only (integer IRR, ``MoneyIRR`` digits=0); the
payloads validate through the same accounting command invariants
as the live draft flows (US-202/US-204) — a template that could
never produce a valid draft is refused at creation, not at its
first firing.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum, unique
from typing import Any

from hesabyar.domain.accounting.commands import InvoiceDraftLine, JournalDraftLine
from hesabyar.domain.accounting.refs import AccountRef, PartyRef
from hesabyar.domain.common.errors import (
    InvalidInvoiceError,
    InvalidJournalError,
    UnbalancedJournalError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.recurring.errors import InvalidRecurringTemplateError
from hesabyar.domain.recurring.schedules import (
    OccurrenceDate,
    RecurringSchedule,
)

#: The policy-gate event type for recurring generation. Auto-post
#: needs a policy row for this type (plus the installation opt-in
#: and an open fiscal period) — see docs/recurring.md.
RECURRING_GATE_EVENT_TYPE = "recurring.generate"


@unique
class TemplateKind(StrEnum):
    """What a template generates: an invoice draft or a journal draft."""

    INVOICE = "invoice"
    JOURNAL = "journal"


@unique
class TemplateStatus(StrEnum):
    """A template's lifecycle: active, paused by the operator, or done."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"  # its end condition was reached


@unique
class OccurrenceStatus(StrEnum):
    """What happened to one scheduled occurrence."""

    PENDING = "pending"  # claimed, generation in flight (or interrupted)
    DRAFT = "draft"  # generated as a draft (or post failed after the draft landed)
    POSTED = "posted"  # generated and auto-posted through the policy gate
    FAILED = "failed"  # draft creation failed; the next run retries it


def _invalid(message: str, details: dict[str, Any] | None = None) -> InvalidRecurringTemplateError:
    return InvalidRecurringTemplateError(message, details=details)


def _require_engine_id(raw: Any, field_name: str) -> int:
    """Parse a stored engine id (fail closed on bad input)."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise _invalid(
            f"{field_name} must be a positive engine id, got {raw!r}.",
            details={field_name: raw},
        )
    return raw


def _optional_engine_id(raw: Any, field_name: str) -> int | None:
    if raw is None:
        return None
    return _require_engine_id(raw, field_name)


def _require_amount(raw: Any, field_name: str) -> int:
    """Parse a stored integer Rial amount (fail closed on bad input)."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise _invalid(
            f"{field_name} must be a non-negative integer Rial amount, got {raw!r}.",
            details={field_name: raw},
        )
    return raw


def _parse_quantity(raw: Any) -> Decimal:
    """Parse a stored quantity (serialized as a decimal string)."""
    try:
        quantity = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise _invalid(
            f"quantity must be a decimal, got {raw!r}.",
            details={"quantity": raw},
        ) from exc
    if not quantity.is_finite() or quantity <= 0:
        raise _invalid(
            f"quantity must be a positive finite decimal, got {raw!r}.",
            details={"quantity": raw},
        )
    return quantity


@dataclass(frozen=True)
class InvoiceTemplateLine:
    """One priced line of an invoice template (mirrors ``InvoiceDraftLine``)."""

    account_engine_id: int
    unit_price_irr: int
    quantity: Decimal = Decimal(1)
    description: str = ""

    def __post_init__(self) -> None:
        try:
            InvoiceDraftLine(
                account=AccountRef(self.account_engine_id),
                unit_price=MoneyIRR(self.unit_price_irr),
                quantity=self.quantity,
                description=self.description,
            )
        except InvalidInvoiceError as exc:
            raise _invalid(
                f"Invalid invoice template line: {exc.message}",
                details=dict(exc.details),
            ) from exc

    def to_line(self) -> InvoiceDraftLine:
        """Build the accounting command line for one occurrence."""
        return InvoiceDraftLine(
            account=AccountRef(self.account_engine_id),
            unit_price=MoneyIRR(self.unit_price_irr),
            quantity=self.quantity,
            description=self.description,
        )

    @property
    def amount_irr(self) -> int:
        """The line amount in integer IRR (quantity x unit price)."""
        return int(self.quantity * self.unit_price_irr)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_engine_id": self.account_engine_id,
            "unit_price_irr": self.unit_price_irr,
            "quantity": str(self.quantity),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvoiceTemplateLine:
        if not isinstance(data, dict):
            raise _invalid(f"An invoice line must be an object, got {type(data).__name__}.")
        return cls(
            account_engine_id=_require_engine_id(data.get("account_engine_id"), "account_engine_id"),
            unit_price_irr=_require_amount(data.get("unit_price_irr"), "unit_price_irr"),
            quantity=_parse_quantity(data.get("quantity", "1")),
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True)
class InvoiceTemplatePayload:
    """The invoice a template generates, once per occurrence.

    The invariants are ``CreateInvoiceDraft``'s (US-204): at least
    one priced line, a total greater than zero. The engine resolves
    omitted journal / account / address exactly as for a manual
    draft (the adapter's resolution rules apply at generation time).
    """

    invoice_type: str  # 'out' (customer) | 'in' (supplier)
    party_engine_id: int
    lines: tuple[InvoiceTemplateLine, ...]
    description: str = ""
    reference: str = ""
    journal_engine_id: int | None = None
    account_engine_id: int | None = None
    invoice_address_engine_id: int | None = None

    def __post_init__(self) -> None:
        if self.invoice_type not in ("out", "in"):
            raise _invalid(
                f"Invoice type must be 'out' (customer) or 'in' (supplier), "
                f"got {self.invoice_type!r}.",
            )
        if self.party_engine_id <= 0:
            raise _invalid(
                f"party_engine_id must be a positive engine id, got {self.party_engine_id}.",
                details={"party_engine_id": self.party_engine_id},
            )
        if not self.lines:
            raise _invalid(
                "An invoice template needs at least one line.",
                details={"line_count": 0},
            )
        if self.total_amount_irr <= 0:
            raise _invalid("An invoice template total must be greater than zero.")

    @property
    def total_amount_irr(self) -> int:
        """The invoice total in integer IRR (sum of the line amounts)."""
        return sum(line.amount_irr for line in self.lines)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "invoice_type": self.invoice_type,
            "party_engine_id": self.party_engine_id,
            "lines": [line.to_dict() for line in self.lines],
            "description": self.description,
            "reference": self.reference,
        }
        if self.journal_engine_id is not None:
            data["journal_engine_id"] = self.journal_engine_id
        if self.account_engine_id is not None:
            data["account_engine_id"] = self.account_engine_id
        if self.invoice_address_engine_id is not None:
            data["invoice_address_engine_id"] = self.invoice_address_engine_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvoiceTemplatePayload:
        if not isinstance(data, dict):
            raise _invalid(f"An invoice payload must be an object, got {type(data).__name__}.")
        raw_lines = data.get("lines")
        if not isinstance(raw_lines, list):
            raise _invalid(
                f"An invoice payload needs a lines list, got {type(raw_lines).__name__}.",
            )
        return cls(
            invoice_type=str(data.get("invoice_type", "")),
            party_engine_id=_require_engine_id(data.get("party_engine_id"), "party_engine_id"),
            lines=tuple(InvoiceTemplateLine.from_dict(item) for item in raw_lines),
            description=str(data.get("description", "")),
            reference=str(data.get("reference", "")),
            journal_engine_id=_optional_engine_id(
                data.get("journal_engine_id"), "journal_engine_id"
            ),
            account_engine_id=_optional_engine_id(
                data.get("account_engine_id"), "account_engine_id"
            ),
            invoice_address_engine_id=_optional_engine_id(
                data.get("invoice_address_engine_id"), "invoice_address_engine_id"
            ),
        )


@dataclass(frozen=True)
class JournalTemplateLine:
    """One line of a journal template (mirrors ``JournalDraftLine``)."""

    account_engine_id: int
    debit_irr: int = 0
    credit_irr: int = 0
    description: str = ""
    party_engine_id: int | None = None

    def __post_init__(self) -> None:
        try:
            JournalDraftLine(
                account=AccountRef(self.account_engine_id),
                debit=MoneyIRR(self.debit_irr),
                credit=MoneyIRR(self.credit_irr),
                description=self.description,
                party=(
                    PartyRef(self.party_engine_id)
                    if self.party_engine_id is not None
                    else None
                ),
            )
        except InvalidJournalError as exc:
            raise _invalid(
                f"Invalid journal template line: {exc.message}",
                details=dict(exc.details),
            ) from exc

    def to_line(self) -> JournalDraftLine:
        """Build the accounting command line for one occurrence."""
        return JournalDraftLine(
            account=AccountRef(self.account_engine_id),
            debit=MoneyIRR(self.debit_irr),
            credit=MoneyIRR(self.credit_irr),
            description=self.description,
            party=(
                PartyRef(self.party_engine_id) if self.party_engine_id is not None else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "account_engine_id": self.account_engine_id,
            "debit_irr": self.debit_irr,
            "credit_irr": self.credit_irr,
            "description": self.description,
        }
        if self.party_engine_id is not None:
            data["party_engine_id"] = self.party_engine_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournalTemplateLine:
        if not isinstance(data, dict):
            raise _invalid(f"A journal line must be an object, got {type(data).__name__}.")
        return cls(
            account_engine_id=_require_engine_id(data.get("account_engine_id"), "account_engine_id"),
            debit_irr=_require_amount(data.get("debit_irr", 0), "debit_irr"),
            credit_irr=_require_amount(data.get("credit_irr", 0), "credit_irr"),
            description=str(data.get("description", "")),
            party_engine_id=_optional_engine_id(
                data.get("party_engine_id"), "party_engine_id"
            ),
        )


@dataclass(frozen=True)
class JournalTemplatePayload:
    """The journal a template generates, once per occurrence.

    The invariants are ``CreateJournalDraft``'s §8.2 (US-202): at
    least two lines, one-sided lines, balanced, total > 0.
    """

    lines: tuple[JournalTemplateLine, ...]
    description: str = ""
    journal_engine_id: int | None = None

    def __post_init__(self) -> None:
        if len(self.lines) < 2:
            raise _invalid(
                "A journal template needs at least two lines.",
                details={"line_count": len(self.lines)},
            )
        total_debit = sum(line.debit_irr for line in self.lines)
        total_credit = sum(line.credit_irr for line in self.lines)
        if total_debit == 0 and total_credit == 0:
            raise _invalid("A journal template total must be greater than zero.")
        if total_debit != total_credit:
            raise UnbalancedJournalError(
                f"Journal template is unbalanced: debit {total_debit} IRR "
                f"!= credit {total_credit} IRR.",
                details={
                    "total_debit_irr": total_debit,
                    "total_credit_irr": total_credit,
                },
            )

    @property
    def total_amount_irr(self) -> int:
        """The journal total in integer IRR (debit == credit)."""
        return sum(line.debit_irr for line in self.lines)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "lines": [line.to_dict() for line in self.lines],
            "description": self.description,
        }
        if self.journal_engine_id is not None:
            data["journal_engine_id"] = self.journal_engine_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournalTemplatePayload:
        if not isinstance(data, dict):
            raise _invalid(f"A journal payload must be an object, got {type(data).__name__}.")
        raw_lines = data.get("lines")
        if not isinstance(raw_lines, list):
            raise _invalid(
                f"A journal payload needs a lines list, got {type(raw_lines).__name__}.",
            )
        return cls(
            lines=tuple(JournalTemplateLine.from_dict(item) for item in raw_lines),
            description=str(data.get("description", "")),
            journal_engine_id=_optional_engine_id(
                data.get("journal_engine_id"), "journal_engine_id"
            ),
        )


#: A template's payload, by kind.
TemplatePayload = InvoiceTemplatePayload | JournalTemplatePayload


def payload_from_dict(kind: TemplateKind, data: dict[str, Any]) -> TemplatePayload:
    """Parse a stored payload dict for the template's kind (fail closed)."""
    if kind is TemplateKind.INVOICE:
        return InvoiceTemplatePayload.from_dict(data)
    return JournalTemplatePayload.from_dict(data)


@dataclass(frozen=True)
class RecurringTemplate:
    """One recurring template (a row in ``recurring_templates``).

    ``company_id`` is the HesabYar ``engine_companies`` row id (the
    API-facing id); ``company_engine_id`` is the Tryton company id
    the generator drafts into. ``installation_id`` is the
    installation that owns the template — the policy gate evaluates
    auto-post against ITS registry policy. ``total_amount_irr`` is
    the payload's total at save time (the preview's amount); the
    occurrence rows carry their own snapshot totals.
    """

    id: str
    tenant_id: str
    installation_id: str
    company_id: str
    company_engine_id: int
    kind: TemplateKind
    name: str
    status: TemplateStatus
    schedule: RecurringSchedule
    payload: TemplatePayload
    auto_post: bool
    total_amount_irr: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise _invalid("A recurring template needs a name.")
        if self.company_engine_id <= 0:
            raise _invalid(
                f"company_engine_id must be a positive engine id, got {self.company_engine_id}.",
                details={"company_engine_id": self.company_engine_id},
            )
        expected = (
            InvoiceTemplatePayload
            if self.kind is TemplateKind.INVOICE
            else JournalTemplatePayload
        )
        if not isinstance(self.payload, expected):
            raise _invalid(
                f"A {self.kind.value} template needs a {expected.__name__}, "
                f"got {type(self.payload).__name__}.",
            )
        if self.total_amount_irr != self.payload.total_amount_irr:
            raise _invalid(
                f"total_amount_irr {self.total_amount_irr} does not match "
                f"the payload total {self.payload.total_amount_irr}.",
                details={
                    "total_amount_irr": self.total_amount_irr,
                    "payload_total_irr": self.payload.total_amount_irr,
                },
            )


@dataclass(frozen=True)
class OccurrencePreview:
    """One previewed occurrence: date + amount + clamp report."""

    occurrence_number: int
    scheduled_date: datetime.date
    jalali_date: str
    total_amount_irr: int
    clamped: bool
    requested_jalali_day: int
    actual_jalali_day: int

    @classmethod
    def from_date(cls, candidate: OccurrenceDate, total_amount_irr: int) -> OccurrencePreview:
        """Build a preview row from a sequence candidate + the amount."""
        return cls(
            occurrence_number=candidate.occurrence_number,
            scheduled_date=candidate.scheduled_date,
            jalali_date=candidate.jalali_date,
            total_amount_irr=total_amount_irr,
            clamped=candidate.clamped,
            requested_jalali_day=candidate.requested_jalali_day,
            actual_jalali_day=candidate.actual_jalali_day,
        )


def snapshot_for_template(template: RecurringTemplate) -> dict[str, Any]:
    """The snapshot an occurrence records: payload + schedule + total.

    The snapshot is frozen at claim time and immutable once the
    document exists (draft/posted) — an edit never rewrites it. A
    FAILED occurrence refreshes its snapshot on retry (nothing was
    generated yet, so there is no history to preserve — and fixing
    the template is exactly how a failed occurrence heals).
    """
    return {
        "kind": template.kind.value,
        "schedule": template.schedule.to_dict(),
        "payload": template.payload.to_dict(),
        "total_amount_irr": template.total_amount_irr,
    }


@dataclass(frozen=True)
class RecurringOccurrence:
    """One generated (or failed) occurrence (a row in ``recurring_occurrences``).

    ``snapshot`` is the template payload + schedule the occurrence
    was generated from — the evidence that an edit never rewrote
    history. ``gate_decision`` is the policy-gate verdict when
    auto-post was attempted (None when the template never opted
    in). ``error`` carries the draft-creation failure (status
    ``failed`` — retried next run) or a post failure that left a
    draft behind (status ``draft`` — NOT retried: the draft exists,
    an accountant posts it by hand).
    """

    id: str
    template_id: str
    occurrence_number: int
    scheduled_date: datetime.date
    jalali_date: str
    clamped: bool
    requested_jalali_day: int
    actual_jalali_day: int
    status: OccurrenceStatus
    total_amount_irr: int
    snapshot: dict[str, Any] = field(default_factory=dict)
    engine_document_id: int | None = None
    gate_decision: dict[str, Any] | None = None
    error: str | None = None
    generated_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    @classmethod
    def new_pending(
        cls,
        *,
        id: str,
        template: RecurringTemplate,
        candidate: OccurrenceDate,
        now: datetime.datetime,
    ) -> RecurringOccurrence:
        """Claim a due occurrence: pending, with the template's snapshot frozen."""
        return cls(
            id=id,
            template_id=template.id,
            occurrence_number=candidate.occurrence_number,
            scheduled_date=candidate.scheduled_date,
            jalali_date=candidate.jalali_date,
            clamped=candidate.clamped,
            requested_jalali_day=candidate.requested_jalali_day,
            actual_jalali_day=candidate.actual_jalali_day,
            status=OccurrenceStatus.PENDING,
            total_amount_irr=template.total_amount_irr,
            snapshot=snapshot_for_template(template),
            created_at=now,
            updated_at=now,
        )

    def snapshot_payload(self) -> TemplatePayload:
        """The payload this occurrence was generated from (never the live template)."""
        raw_kind = self.snapshot.get("kind", "")
        try:
            kind = TemplateKind(str(raw_kind))
        except ValueError as exc:
            raise _invalid(
                f"Occurrence {self.id} has an unknown snapshot kind {raw_kind!r}.",
                details={"occurrence_id": self.id, "kind": raw_kind},
            ) from exc
        raw_payload = self.snapshot.get("payload")
        if not isinstance(raw_payload, dict):
            raise _invalid(
                f"Occurrence {self.id} has no payload snapshot.",
                details={"occurrence_id": self.id},
            )
        return payload_from_dict(kind, raw_payload)


def occurrence_note(template: RecurringTemplate, occurrence_number: int) -> str:
    """The marker appended to a generated document's description.

    The occurrence row is the durable link (template + number); the
    marker makes the same link visible to an accountant reading the
    draft inside Tryton, where the HesabYar row does not exist.
    """
    return f"[{template.name} — تکرار {occurrence_number}]"
