"""Installment payload validation + schedule generation (US-540 + US-543).

Fail-closed validation for plan / receipt / contract payloads:
every problem is collected into ``field_errors`` and raised as one
:class:`InstallmentValidationError` (typed 422 at the REST layer).
Nothing is guessed — a missing or malformed field is an error, never
a default silently substituted.

Schedule generation (terms → legs): equal split of the total
principal across the legs, with the integer-division remainder on
the LAST leg (deterministic, documented, and the reason the last
leg may be a few Rial larger than the others). Due dates are the
first due date plus ``(sequence - 1) * interval_days`` — Gregorian
day arithmetic on stored dates, Jalali only at the boundary.

US-543 (BNPL) additions are marked with ``# -- US-543 (BNPL) --``
so the US-542 merge (cheque receipts, same file) stays mechanical:
plan ``kind`` + ``gateway_channel_key``, and the receipt's
``gateway`` + ``gateway_reference`` for ``payment_method == bnpl``.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NoReturn

import jdatetime

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.errors import (
    InstallmentLinkageError,
    InstallmentPaymentMethodError,
    InstallmentValidationError,
)
from hesabyar.domain.installments.finance import (
    MAX_DUNNING_OFFSET_DAYS,
    MAX_DUNNING_OFFSETS,
)
from hesabyar.domain.installments.models import (
    RECEIPT_METHODS,
    RECEIPT_METHODS_US540,
    EarlySettlementFeePolicy,
    EarlySettlementIncomePolicy,
    LateFeeBasis,
    PaymentMethod,
    PlanKind,
    SettlementType,
)

#: A plan must have at least one leg and at most ten years of
#: monthly legs. The cap is a sanity bound against a typo'd
#: ``leg_count`` (e.g. 1000) creating a thousand rows.
MAX_LEGS = 120

#: Jalali YYYY-MM-DD, ASCII digits after normalization.
_JALALI_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PERSIAN_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789")


def normalize_digits(value: str) -> str:
    """Normalize Persian and Arabic numerals to ASCII standard digits."""
    return value.translate(_PERSIAN_ARABIC_DIGITS)


def to_jalali_string(day: datetime.date) -> str:
    """Render a Gregorian date as Jalali YYYY-MM-DD."""
    return jdatetime.date.fromgregorian(date=day).strftime("%Y-%m-%d")


def parse_jalali_date(raw: str, field_name: str, errors: dict[str, str]) -> FiscalDate | None:
    """Parse one Jalali YYYY-MM-DD date, collecting the problem on failure.

    Returns None (and records ``field_name`` in ``errors``) when the
    value is blank, malformed, or not a real Jalali date — the caller
    raises once all fields are checked.
    """
    if not isinstance(raw, str) or not raw.strip():
        errors[field_name] = "must be a non-empty Jalali YYYY-MM-DD date."
        return None
    cleaned = normalize_digits(raw.strip())
    if not _JALALI_RE.match(cleaned):
        errors[field_name] = f"must be a Jalali YYYY-MM-DD date, got {raw!r}."
        return None
    try:
        return FiscalDate.from_jalali_str(cleaned)
    except Exception:  # noqa: BLE001 - any parse failure is a field error
        errors[field_name] = f"is not a valid Jalali date: {raw!r}."
        return None


def _fail(errors: dict[str, str]) -> NoReturn:
    raise InstallmentValidationError(
        "Installment payload validation failed.", field_errors=errors
    )


@dataclass(frozen=True, slots=True)
class ExplicitLegInput:
    """One caller-supplied leg (due date + principal in Rial)."""

    due_date: FiscalDate
    principal: MoneyIRR


@dataclass(frozen=True, slots=True)
class GeneratedLeg:
    """One generated leg: sequence + due date + principal (Rial)."""

    sequence: int
    due_date: FiscalDate
    principal: MoneyIRR


def generate_schedule(
    total_principal: MoneyIRR,
    leg_count: int,
    interval_days: int,
    first_due_date: FiscalDate,
) -> list[GeneratedLeg]:
    """Generate legs from multi-stage payment terms (equal split).

    ``total // leg_count`` per leg, the integer remainder on the
    LAST leg; due dates ``first_due_date + (i * interval_days)``.
    The leg principals always sum to exactly ``total_principal``.
    """
    base = total_principal.amount // leg_count
    remainder = total_principal.amount - base * leg_count
    legs: list[GeneratedLeg] = []
    for index in range(leg_count):
        principal = base + (remainder if index == leg_count - 1 else 0)
        due = first_due_date.gregorian_date + datetime.timedelta(days=index * interval_days)
        legs.append(
            GeneratedLeg(
                sequence=index + 1,
                due_date=FiscalDate.from_gregorian(due),
                principal=MoneyIRR(principal),
            )
        )
    return legs


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    """A validated plan-creation payload, ready for the service."""

    invoice_id: str
    contract_id: str | None
    contract_code: str | None
    customer_ref: str | None
    total_principal: MoneyIRR
    interval_days: int | None
    first_due_date: FiscalDate | None
    legs: tuple[GeneratedLeg, ...]
    auto_post: bool
    #: US-543 (BNPL): plan kind (``internal`` by default) + the
    #: pinned gateway channel key for BNPL plans.
    kind: PlanKind = PlanKind.INTERNAL
    gateway_channel_key: str | None = None


def validate_plan_payload(payload: dict[str, object]) -> ValidatedPlan:
    """Validate a plan-creation payload (terms-based OR explicit legs).

    Exactly one of the two schedule shapes must be present: either
    ``total_principal_toman`` + ``leg_count`` + ``interval_days`` +
    ``first_due_date`` (terms-based generation), or ``legs`` (an
    explicit list of ``{due_date, principal_toman}``). Supplying
    both, or neither, is a 422. Toman→Rial ×10 happens here, once,
    at the boundary.

    US-543 (BNPL): ``kind`` is ``internal`` (default) or ``bnpl``.
    A BNPL plan REQUIRES ``gateway_channel_key`` (the sale goes
    through exactly one gateway) and FORBIDS ``contract_id`` (no
    late fee / early settlement on a BNPL plan — the gateway owns
    collection). An internal plan forbids ``gateway_channel_key``.
    """
    errors: dict[str, str] = {}

    invoice_id = payload.get("invoice_id")
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        errors["invoice_id"] = "is required and must be a non-empty string."
        invoice_id = ""
    else:
        invoice_id = invoice_id.strip()

    contract_id = payload.get("contract_id")
    if contract_id is not None and (not isinstance(contract_id, str) or not contract_id.strip()):
        errors["contract_id"] = "must be a non-empty string when present."
        contract_id = None
    elif isinstance(contract_id, str):
        contract_id = contract_id.strip()

    contract_code = payload.get("contract_code")
    if contract_code is not None and (not isinstance(contract_code, str) or not contract_code.strip()):
        errors["contract_code"] = "must be a non-empty string when present."
        contract_code = None
    elif isinstance(contract_code, str):
        contract_code = contract_code.strip()
    if contract_id is not None and contract_code is not None:
        errors["contract_code"] = (
            "supply either 'contract_id' (one version) or 'contract_code' "
            "(the version in force is selected), not both."
        )

    customer_ref = payload.get("customer_ref")
    if customer_ref is not None and (not isinstance(customer_ref, str) or not customer_ref.strip()):
        errors["customer_ref"] = "must be a non-empty string when present."
        customer_ref = None
    elif isinstance(customer_ref, str):
        customer_ref = customer_ref.strip()

    auto_post = payload.get("auto_post", False)
    if not isinstance(auto_post, bool):
        errors["auto_post"] = "must be a boolean."
        auto_post = False

    # -- US-543 (BNPL): plan kind + pinned gateway channel -----------
    kind_raw = payload.get("kind", "internal")
    kind: PlanKind = PlanKind.INTERNAL
    if not isinstance(kind_raw, str):
        errors["kind"] = "must be a string: 'internal' or 'bnpl'."
    else:
        try:
            kind = PlanKind(kind_raw.strip().lower())
        except ValueError:
            errors["kind"] = "must be 'internal' or 'bnpl'."
    gateway_channel_key = payload.get("gateway_channel_key")
    if gateway_channel_key is not None and (
        not isinstance(gateway_channel_key, str) or not gateway_channel_key.strip()
    ):
        errors["gateway_channel_key"] = "must be a non-empty string when present."
        gateway_channel_key = None
    elif isinstance(gateway_channel_key, str):
        gateway_channel_key = gateway_channel_key.strip()
    if kind is PlanKind.BNPL:
        if gateway_channel_key is None:
            errors["gateway_channel_key"] = (
                "is required for a BNPL plan (the sale goes through exactly one gateway)."
            )
        if contract_id is not None:
            errors["contract_id"] = (
                "is not valid on a BNPL plan (no late fee / early settlement — "
                "the gateway owns collection)."
            )
    elif gateway_channel_key is not None:
        errors["gateway_channel_key"] = (
            "is only valid on a BNPL plan (kind 'bnpl')."
        )
    # -- end US-543 (BNPL) --------------------------------------------

    legs_raw = payload.get("legs")
    terms_present = any(
        payload.get(key) is not None
        for key in ("total_principal_toman", "leg_count", "interval_days", "first_due_date")
    )
    if legs_raw is not None and terms_present:
        errors["legs"] = (
            "supply either explicit 'legs' or the payment terms "
            "(total_principal_toman, leg_count, interval_days, first_due_date), not both."
        )
    if legs_raw is None and not terms_present:
        errors["legs"] = (
            "supply either explicit 'legs' or the payment terms "
            "(total_principal_toman, leg_count, interval_days, first_due_date)."
        )
    if errors:
        _fail(errors)

    if legs_raw is not None:
        return _validate_explicit_legs(
            legs_raw,
            invoice_id,
            contract_id,
            contract_code,
            customer_ref,
            auto_post,
            errors,
            kind=kind,
            gateway_channel_key=gateway_channel_key,
        )
    return _validate_terms_plan(
        payload,
        invoice_id,
        contract_id,
        contract_code,
        customer_ref,
        auto_post,
        errors,
        kind=kind,
        gateway_channel_key=gateway_channel_key,
    )


def _validate_explicit_legs(
    legs_raw: object,
    invoice_id: str,
    contract_id: str | None,
    contract_code: str | None,
    customer_ref: str | None,
    auto_post: bool,
    errors: dict[str, str],
    *,
    kind: PlanKind,
    gateway_channel_key: str | None,
) -> ValidatedPlan:
    if not isinstance(legs_raw, list) or not legs_raw:
        errors["legs"] = "must be a non-empty list of {due_date, principal_toman}."
        _fail(errors)
    if len(legs_raw) > MAX_LEGS:
        errors["legs"] = f"must have at most {MAX_LEGS} legs."
        _fail(errors)
    legs: list[GeneratedLeg] = []
    total = 0
    for index, raw in enumerate(legs_raw):
        field = f"legs[{index}]"
        if not isinstance(raw, dict):
            errors[field] = "must be an object with due_date and principal_toman."
            continue
        due = parse_jalali_date(str(raw.get("due_date", "")), f"{field}.due_date", errors)
        principal_toman = raw.get("principal_toman")
        if (
            isinstance(principal_toman, bool)
            or not isinstance(principal_toman, int)
            or principal_toman <= 0
        ):
            errors[f"{field}.principal_toman"] = "must be a positive integer (Toman)."
            continue
        if due is None:
            continue
        principal = MoneyIRR(principal_toman * 10)
        total += principal.amount
        legs.append(GeneratedLeg(sequence=index + 1, due_date=due, principal=principal))
    if errors:
        _fail(errors)
    return ValidatedPlan(
        invoice_id=invoice_id,
        contract_id=contract_id,
        contract_code=contract_code,
        customer_ref=customer_ref,
        total_principal=MoneyIRR(total),
        interval_days=None,
        first_due_date=None,
        legs=tuple(legs),
        auto_post=auto_post,
        kind=kind,
        gateway_channel_key=gateway_channel_key,
    )


def _validate_terms_plan(
    payload: dict[str, object],
    invoice_id: str,
    contract_id: str | None,
    contract_code: str | None,
    customer_ref: str | None,
    auto_post: bool,
    errors: dict[str, str],
    *,
    kind: PlanKind,
    gateway_channel_key: str | None,
) -> ValidatedPlan:
    total_toman = payload.get("total_principal_toman")
    if isinstance(total_toman, bool) or not isinstance(total_toman, int) or total_toman <= 0:
        errors["total_principal_toman"] = "must be a positive integer (Toman)."
    leg_count = payload.get("leg_count")
    if isinstance(leg_count, bool) or not isinstance(leg_count, int) or not (1 <= leg_count <= MAX_LEGS):
        errors["leg_count"] = f"must be an integer between 1 and {MAX_LEGS}."
    interval_days = payload.get("interval_days")
    if isinstance(interval_days, bool) or not isinstance(interval_days, int) or interval_days < 1:
        errors["interval_days"] = "must be a positive integer (days between legs)."
    first_due = parse_jalali_date(
        str(payload.get("first_due_date", "")), "first_due_date", errors
    )
    if errors:
        _fail(errors)
    assert isinstance(total_toman, int)
    assert isinstance(leg_count, int)
    assert isinstance(interval_days, int)
    assert first_due is not None
    total = MoneyIRR(total_toman * 10)
    legs = generate_schedule(total, leg_count, interval_days, first_due)
    return ValidatedPlan(
        invoice_id=invoice_id,
        contract_id=contract_id,
        contract_code=contract_code,
        customer_ref=customer_ref,
        total_principal=total,
        interval_days=interval_days,
        first_due_date=first_due,
        legs=tuple(legs),
        auto_post=auto_post,
        kind=kind,
        gateway_channel_key=gateway_channel_key,
    )


@dataclass(frozen=True, slots=True)
class ValidatedReceipt:
    """A validated receipt payload — the linkage guard already passed."""

    invoice_id: str
    journal_move_id: int
    amount: MoneyIRR
    received_date: FiscalDate
    payment_method: PaymentMethod
    reference: str | None
    notes: str | None
    #: The cheque backing the receipt (US-542) — set only when
    #: ``payment_method`` is ``cheque``.
    cheque_id: str | None = None
    #: US-543 (BNPL): the gateway channel key + the gateway's
    #: transaction id — required for BNPL receipts, forbidden for
    #: every other method (fail-closed: no silent ignoring).
    gateway_channel_key: str | None = None
    gateway_reference: str | None = None


def _check_linkage(payload: dict[str, object]) -> tuple[str, int]:
    """THE LINKAGE GUARD: a receipt/settlement needs BOTH refs.

    Shared by receipts and early settlements (a settlement IS a
    receipt): the original invoice ref AND the accounting-document
    (journal move) ref. Runs BEFORE any other validation — a
    payload without both is rejected with
    :class:`InstallmentLinkageError` (typed 422), not a generic
    field error. The invoice-vs-plan match is checked by the
    service (it owns the plan); the presence check lives here so
    every entry point enforces it identically.

    Cheque receipts (US-542) MUST name their cheque (``cheque_id``
    is required when ``payment_method`` is ``cheque``) and a
    ``cheque_id`` with any other method is a contradictory payload
    (422) — the link is only meaningful for a cheque receipt. The
    cheque itself (existence, receivable state, leg link, amount)
    is checked by the service, which owns the cheque repository.
    """
    missing: list[str] = []
    invoice_id = payload.get("invoice_id")
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        missing.append("invoice_id")
    journal_move_id = payload.get("journal_move_id")
    if (
        isinstance(journal_move_id, bool)
        or not isinstance(journal_move_id, int)
        or journal_move_id <= 0
    ):
        missing.append("journal_move_id")
    if missing:
        raise InstallmentLinkageError(missing=tuple(sorted(missing)))
    assert isinstance(invoice_id, str)
    assert isinstance(journal_move_id, int)
    return invoice_id.strip(), journal_move_id


def validate_receipt_payload(payload: dict[str, object]) -> ValidatedReceipt:
    """Validate a receipt payload, enforcing THE LINKAGE GUARD first.

    The guard runs BEFORE any other validation: a receipt without
    ``invoice_id`` or without ``journal_move_id`` is rejected with
    :class:`InstallmentLinkageError` (typed 422), not a generic
    field error — the missing link is the finding, and it must be
    unmistakable. The invoice-vs-plan match is checked by the
    service (it owns the plan); the presence check lives here so
    every entry point enforces it identically.
    """
    invoice_id, journal_move_id = _check_linkage(payload)

    errors: dict[str, str] = {}
    amount_toman = payload.get("amount_toman")
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int) or amount_toman <= 0:
        errors["amount_toman"] = "must be a positive integer (Toman)."
    received_raw = payload.get("received_date")
    received_date: FiscalDate | None = None
    if received_raw is None:
        received_date = FiscalDate.from_gregorian(datetime.date.today())
    elif isinstance(received_raw, str):
        received_date = parse_jalali_date(received_raw, "received_date", errors)
    else:
        errors["received_date"] = "must be a Jalali YYYY-MM-DD date string."
    method_raw = payload.get("payment_method", "cash")
    method: PaymentMethod | None = None
    if not isinstance(method_raw, str):
        errors["payment_method"] = "must be a string."
    else:
        try:
            method = PaymentMethod(method_raw.strip().lower())
        except ValueError:
            errors["payment_method"] = (
                f"must be one of {', '.join(m.value for m in PaymentMethod)}."
            )
    reference = payload.get("reference")
    if reference is not None and (not isinstance(reference, str) or not reference.strip()):
        errors["reference"] = "must be a non-empty string when present."
        reference = None
    elif isinstance(reference, str):
        reference = reference.strip()
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        errors["notes"] = "must be a string when present."
        notes = None
    # -- US-542 (cheque): cheque_id ----------------------------------
    cheque_id = payload.get("cheque_id")
    if cheque_id is not None and (not isinstance(cheque_id, str) or not cheque_id.strip()):
        errors["cheque_id"] = "must be a non-empty string when present."
        cheque_id = None
    elif isinstance(cheque_id, str):
        cheque_id = cheque_id.strip()
    # -- US-543 (BNPL): gateway + gateway_reference ----------------
    # Required for BNPL receipts (they identify the gateway payment
    # the settlement engine creates); forbidden for every other
    # method — a cash receipt naming a gateway is a caller mistake,
    # never silently ignored.
    gateway_raw = payload.get("gateway")
    gateway_channel_key: str | None = None
    if gateway_raw is not None:
        if not isinstance(gateway_raw, str) or not gateway_raw.strip():
            errors["gateway"] = "must be a non-empty string when present."
        else:
            gateway_channel_key = gateway_raw.strip()
    gateway_reference_raw = payload.get("gateway_reference")
    gateway_reference: str | None = None
    if gateway_reference_raw is not None:
        if not isinstance(gateway_reference_raw, str) or not gateway_reference_raw.strip():
            errors["gateway_reference"] = "must be a non-empty string when present."
        else:
            gateway_reference = gateway_reference_raw.strip()
    if method is PaymentMethod.BNPL:
        if gateway_channel_key is None:
            errors["gateway"] = (
                "is required for a BNPL receipt (the gateway channel key, e.g. 'snapppay')."
            )
        if gateway_reference is None:
            errors["gateway_reference"] = (
                "is required for a BNPL receipt (the gateway's transaction id)."
            )
    elif gateway_channel_key is not None or gateway_reference is not None:
        errors["gateway"] = (
            "'gateway' and 'gateway_reference' are only valid for BNPL receipts."
        )
    # -- end US-543 (BNPL) ------------------------------------------
    if errors:
        _fail(errors)
    assert isinstance(amount_toman, int)
    assert received_date is not None
    assert method is not None
    if method not in RECEIPT_METHODS:
        # Unreachable while PaymentMethod and RECEIPT_METHODS agree,
        # but fail closed if a future method is added to the enum
        # without enabling it here.
        reason = "this payment method is not enabled for installment receipts"
        raise InstallmentPaymentMethodError(method.value, reason=reason)
    if method is PaymentMethod.CHEQUE and cheque_id is None:
        errors["cheque_id"] = "is required when payment_method is 'cheque'."
        _fail(errors)
    if method is not PaymentMethod.CHEQUE and cheque_id is not None:
        errors["cheque_id"] = "is only allowed when payment_method is 'cheque'."
        _fail(errors)
    return ValidatedReceipt(
        invoice_id=invoice_id,
        journal_move_id=journal_move_id,
        amount=MoneyIRR(amount_toman * 10),
        received_date=received_date,
        payment_method=method,
        reference=reference,
        notes=notes,
        cheque_id=cheque_id,
        gateway_channel_key=gateway_channel_key,
        gateway_reference=gateway_reference,
    )


@dataclass(frozen=True, slots=True)
class ValidatedSettlement:
    """A validated early-settlement payload — the linkage guard already passed."""

    invoice_id: str
    journal_move_id: int
    amount: MoneyIRR
    settlement_type: SettlementType
    settlement_date: FiscalDate
    payment_method: PaymentMethod
    reference: str | None
    notes: str | None


def validate_settlement_payload(payload: dict[str, object]) -> ValidatedSettlement:
    """Validate an early-settlement payload, enforcing THE LINKAGE GUARD first.

    A settlement IS a receipt (it closes legs and records money),
    so the guard applies identically: ``invoice_id`` AND
    ``journal_move_id`` are both mandatory, checked before anything
    else. ``settlement_type`` is ``full`` (pay the whole payoff
    quote, close every open leg) or ``partial`` (pay part of the
    schedule; the advance it creates re-amortises the rest). The
    amount-vs-payoff check lives in the service (it owns the
    quote). The payment method is cash / bank_transfer / other
    only: a cheque settlement cannot exist — a settlement receipt
    allocates across ALL open legs, while a cheque receipt is
    single-leg (US-542), and settlement recognises income + fees
    at settlement time while cheque money is collected only on
    clear. A BNPL settlement cannot exist either — settlements
    are internal-plan only (US-543).
    """
    invoice_id, journal_move_id = _check_linkage(payload)

    errors: dict[str, str] = {}
    amount_toman = payload.get("amount_toman")
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int) or amount_toman <= 0:
        errors["amount_toman"] = "must be a positive integer (Toman)."
    type_raw = payload.get("settlement_type", "full")
    settlement_type: SettlementType | None = None
    if not isinstance(type_raw, str):
        errors["settlement_type"] = "must be a string."
    else:
        try:
            settlement_type = SettlementType(type_raw.strip().lower())
        except ValueError:
            errors["settlement_type"] = "must be 'full' or 'partial'."
    date_raw = payload.get("settlement_date")
    settlement_date: FiscalDate | None = None
    if date_raw is None:
        settlement_date = FiscalDate.from_gregorian(datetime.date.today())
    elif isinstance(date_raw, str):
        settlement_date = parse_jalali_date(date_raw, "settlement_date", errors)
    else:
        errors["settlement_date"] = "must be a Jalali YYYY-MM-DD date string."
    method_raw = payload.get("payment_method", "cash")
    method: PaymentMethod | None = None
    if not isinstance(method_raw, str):
        errors["payment_method"] = "must be a string."
    else:
        try:
            method = PaymentMethod(method_raw.strip().lower())
        except ValueError:
            errors["payment_method"] = (
                f"must be one of {', '.join(m.value for m in PaymentMethod)}."
            )
    reference = payload.get("reference")
    if reference is not None and (not isinstance(reference, str) or not reference.strip()):
        errors["reference"] = "must be a non-empty string when present."
        reference = None
    elif isinstance(reference, str):
        reference = reference.strip()
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        errors["notes"] = "must be a string when present."
        notes = None
    if errors:
        _fail(errors)
    assert isinstance(amount_toman, int)
    assert settlement_type is not None
    assert settlement_date is not None
    assert method is not None
    if method not in RECEIPT_METHODS_US540:
        # cash / bank_transfer / other only: a cheque settlement
        # cannot allocate across all open legs (cheque receipts are
        # single-leg) and settles income before collection; a BNPL
        # settlement cannot exist on an internal plan.
        reason = (
            "early settlements accept cash, bank_transfer or other "
            "only — not cheque (single-leg, collected on clear) "
            "and not bnpl (internal plans only)"
        )
        raise InstallmentPaymentMethodError(method.value, reason=reason)
    return ValidatedSettlement(
        invoice_id=invoice_id,
        journal_move_id=journal_move_id,
        amount=MoneyIRR(amount_toman * 10),
        settlement_type=settlement_type,
        settlement_date=settlement_date,
        payment_method=method,
        reference=reference,
        notes=notes,
    )


@dataclass(frozen=True, slots=True)
class ValidatedContract:
    """A validated sale-contract payload, ready for the service."""

    code: str
    name: str
    version: int
    effective_from: FiscalDate
    late_fee_basis: LateFeeBasis
    late_fee_rate: Decimal
    late_fee_grace_days: int
    penalty_account_engine_id: int | None
    penalty_account_code: str | None
    finance_income_rate: Decimal
    dunning_offsets: tuple[int, ...]
    receivable_account_engine_id: int | None
    receivable_account_code: str | None
    early_settlement_fee_policy: EarlySettlementFeePolicy
    early_settlement_income_policy: EarlySettlementIncomePolicy
    early_settlement_discount_rate: Decimal
    supersedes_contract_id: str | None


def _parse_decimal_field(
    raw: object, field_name: str, errors: dict[str, str]
) -> Decimal:
    """Parse one exact-decimal field (string or int — never a float)."""
    rate = Decimal("0")
    if isinstance(raw, float):
        errors[field_name] = "must be a decimal string or integer, never a float."
    elif isinstance(raw, int) and not isinstance(raw, bool):
        rate = Decimal(raw)
    elif isinstance(raw, str):
        try:
            rate = Decimal(raw.strip())
        except InvalidOperation:
            errors[field_name] = f"is not a valid decimal: {raw!r}."
    else:
        errors[field_name] = "must be a decimal string or integer."
    if not rate.is_finite():
        errors[field_name] = "must be a finite decimal."
    elif rate < 0:
        errors[field_name] = "must be >= 0."
    return rate


def validate_contract_payload(payload: dict[str, object]) -> ValidatedContract:
    """Validate a sale-contract payload.

    The late-fee rule is user-entered: ``late_fee_basis`` +
    ``late_fee_rate`` (+ ``late_fee_grace_days``). A rate is
    required unless the basis is ``none``; a rate with basis
    ``none`` is refused (a contract must not carry a meaningless
    rate). ``late_fee_rate`` arrives as a JSON number or a decimal
    string — floats are rejected (money-shaped values are never
    floats), and the value is kept as an exact :class:`Decimal`.

    US-541 adds the finance terms: ``finance_income_rate`` (the
    fraction of principal charged as finance income over the whole
    plan), ``dunning_offsets`` (a strictly increasing list of day
    offsets, at most ten), ``receivable_account_engine_id`` /
    ``receivable_account_code`` (the debit side of the late-fee
    journal), and ``early_settlement_discount_rate`` (required
    between 0 and 1 when the income policy is ``discounted``, 0
    otherwise).
    """
    errors: dict[str, str] = {}

    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        errors["code"] = "is required and must be a non-empty string."
        code = ""
    else:
        code = code.strip()
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        errors["name"] = "is required and must be a non-empty string."
        name = ""
    else:
        name = name.strip()
    version = payload.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        errors["version"] = "must be an integer >= 1."
        version = 1
    effective_from = parse_jalali_date(
        str(payload.get("effective_from", "")), "effective_from", errors
    )

    basis_raw = payload.get("late_fee_basis", "none")
    basis: LateFeeBasis | None = None
    if not isinstance(basis_raw, str):
        errors["late_fee_basis"] = "must be a string."
    else:
        try:
            basis = LateFeeBasis(basis_raw.strip().lower())
        except ValueError:
            errors["late_fee_basis"] = (
                f"must be one of {', '.join(b.value for b in LateFeeBasis)}."
            )

    rate_raw = payload.get("late_fee_rate", "0")
    rate = Decimal("0")
    if isinstance(rate_raw, float):
        errors["late_fee_rate"] = "must be a decimal string or integer, never a float."
    elif isinstance(rate_raw, int) and not isinstance(rate_raw, bool):
        rate = Decimal(rate_raw)
    elif isinstance(rate_raw, str):
        try:
            rate = Decimal(rate_raw.strip())
        except InvalidOperation:
            errors["late_fee_rate"] = f"is not a valid decimal: {rate_raw!r}."
    else:
        errors["late_fee_rate"] = "must be a decimal string or integer."
    if not rate.is_finite():
        errors["late_fee_rate"] = "must be a finite decimal."
    elif rate < 0:
        errors["late_fee_rate"] = "must be >= 0."
    if basis is LateFeeBasis.NONE and rate != 0:
        errors["late_fee_rate"] = "must be 0 when late_fee_basis is 'none'."
    if basis is not None and basis is not LateFeeBasis.NONE and rate == 0:
        errors["late_fee_rate"] = (
            f"must be > 0 when late_fee_basis is {basis.value!r}."
        )

    grace_days = payload.get("late_fee_grace_days", 0)
    if isinstance(grace_days, bool) or not isinstance(grace_days, int) or grace_days < 0:
        errors["late_fee_grace_days"] = "must be an integer >= 0."
        grace_days = 0

    penalty_engine_id = payload.get("penalty_account_engine_id")
    if penalty_engine_id is not None and (
        isinstance(penalty_engine_id, bool)
        or not isinstance(penalty_engine_id, int)
        or penalty_engine_id <= 0
    ):
        errors["penalty_account_engine_id"] = "must be a positive integer when present."
        penalty_engine_id = None
    penalty_code = payload.get("penalty_account_code")
    if penalty_code is not None and (not isinstance(penalty_code, str) or not penalty_code.strip()):
        errors["penalty_account_code"] = "must be a non-empty string when present."
        penalty_code = None
    elif isinstance(penalty_code, str):
        penalty_code = penalty_code.strip()

    # US-541 finance terms: the income rate, the dunning schedule,
    # and the receivable account (the debit side of the late-fee
    # journal). All user-entered, all optional (absent = none).
    finance_rate = _parse_decimal_field(
        payload.get("finance_income_rate", "0"), "finance_income_rate", errors
    )

    offsets_raw = payload.get("dunning_offsets", [])
    offsets: tuple[int, ...] = ()
    if not isinstance(offsets_raw, list):
        errors["dunning_offsets"] = "must be a list of day offsets (integers)."
    elif len(offsets_raw) > MAX_DUNNING_OFFSETS:
        errors["dunning_offsets"] = f"must have at most {MAX_DUNNING_OFFSETS} offsets."
    else:
        parsed_offsets: list[int] = []
        offsets_ok = True
        for index, raw_offset in enumerate(offsets_raw):
            if (
                isinstance(raw_offset, bool)
                or not isinstance(raw_offset, int)
                or not (1 <= raw_offset <= MAX_DUNNING_OFFSET_DAYS)
            ):
                errors[f"dunning_offsets[{index}]"] = (
                    f"must be an integer between 1 and {MAX_DUNNING_OFFSET_DAYS}."
                )
                offsets_ok = False
            else:
                parsed_offsets.append(raw_offset)
        if offsets_ok and parsed_offsets != sorted(set(parsed_offsets)):
            errors["dunning_offsets"] = "must be strictly increasing."
        elif offsets_ok:
            offsets = tuple(parsed_offsets)

    receivable_engine_id = payload.get("receivable_account_engine_id")
    if receivable_engine_id is not None and (
        isinstance(receivable_engine_id, bool)
        or not isinstance(receivable_engine_id, int)
        or receivable_engine_id <= 0
    ):
        errors["receivable_account_engine_id"] = "must be a positive integer when present."
        receivable_engine_id = None
    receivable_code = payload.get("receivable_account_code")
    if receivable_code is not None and (not isinstance(receivable_code, str) or not receivable_code.strip()):
        errors["receivable_account_code"] = "must be a non-empty string when present."
        receivable_code = None
    elif isinstance(receivable_code, str):
        receivable_code = receivable_code.strip()

    fee_policy_raw = payload.get("early_settlement_fee_policy", "waived")
    fee_policy: EarlySettlementFeePolicy | None = None
    if not isinstance(fee_policy_raw, str):
        errors["early_settlement_fee_policy"] = "must be a string."
    else:
        try:
            fee_policy = EarlySettlementFeePolicy(fee_policy_raw.strip().lower())
        except ValueError:
            errors["early_settlement_fee_policy"] = "must be 'waived' or 'kept'."

    income_policy_raw = payload.get("early_settlement_income_policy", "waived")
    income_policy: EarlySettlementIncomePolicy | None = None
    if not isinstance(income_policy_raw, str):
        errors["early_settlement_income_policy"] = "must be a string."
    else:
        try:
            income_policy = EarlySettlementIncomePolicy(income_policy_raw.strip().lower())
        except ValueError:
            errors["early_settlement_income_policy"] = (
                "must be 'waived', 'discounted' or 'full'."
            )

    discount_rate = _parse_decimal_field(
        payload.get("early_settlement_discount_rate", "0"),
        "early_settlement_discount_rate",
        errors,
    )
    if income_policy is EarlySettlementIncomePolicy.DISCOUNTED:
        if not (Decimal("0") < discount_rate < Decimal("1")):
            errors["early_settlement_discount_rate"] = (
                "must be between 0 and 1 when early_settlement_income_policy "
                "is 'discounted'."
            )
    elif discount_rate != 0:
        errors["early_settlement_discount_rate"] = (
            "must be 0 unless early_settlement_income_policy is 'discounted'."
        )

    supersedes = payload.get("supersedes_contract_id")
    if supersedes is not None and (not isinstance(supersedes, str) or not supersedes.strip()):
        errors["supersedes_contract_id"] = "must be a non-empty string when present."
        supersedes = None
    elif isinstance(supersedes, str):
        supersedes = supersedes.strip()

    if errors:
        _fail(errors)
    assert effective_from is not None
    assert basis is not None
    assert fee_policy is not None
    assert income_policy is not None
    return ValidatedContract(
        code=code,
        name=name,
        version=version,
        effective_from=effective_from,
        late_fee_basis=basis,
        late_fee_rate=rate,
        late_fee_grace_days=grace_days,
        penalty_account_engine_id=penalty_engine_id,
        penalty_account_code=penalty_code,
        finance_income_rate=finance_rate,
        dunning_offsets=offsets,
        receivable_account_engine_id=receivable_engine_id,
        receivable_account_code=receivable_code,
        early_settlement_fee_policy=fee_policy,
        early_settlement_income_policy=income_policy,
        early_settlement_discount_rate=discount_rate,
        supersedes_contract_id=supersedes,
    )
