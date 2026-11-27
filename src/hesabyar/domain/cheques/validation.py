"""24-field cheque payload validation (US-531, §5 §1.2 / part-2-4 §3.1).

The manual-entry form takes the SAME 24 fields with the SAME
validations as the site payload (14 mandatory + 10 optional,
``installment_leg_id`` being the 10th optional field from batch 1).
Validation is fail-closed at the boundary: an unknown field, a bad
format, or a violated rule rejects the whole payload — nothing is
silently dropped or coerced.

Money: the payload carries Toman with an explicit unit
(``amount_currency`` ``"IRT"``); the boundary converts ×10 to Rial
(``"IRR"`` arrives already in Rial and is not converted). Dates are
Jalali ``YYYY-MM-DD`` and are stored as Gregorian
:class:`FiscalDate`.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping
from dataclasses import dataclass

import jdatetime

from hesabyar.domain.cheques.errors import ChequeValidationError
from hesabyar.domain.cheques.models import Cheque, ChequeKind, ChequePurpose
from hesabyar.domain.common.dates import FiscalDate

_SAYAD_ID_RE = re.compile(r"^\d{16}$")
_NATIONAL_ID_RE = re.compile(r"^\d{10}$|^\d{11}$")
_BANK_CODE_RE = re.compile(r"^\d{3}(\s+\S.*)?$")
_JALALI_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MANDATORY_FIELDS: frozenset[str] = frozenset(
    {
        "sayad_id",
        "amount_toman",
        "amount_currency",
        "issue_date",
        "due_date",
        "issuer_name",
        "issuer_national_id",
        "issuer_bank",
        "payee_name",
        "payee_national_id",
        "purpose",
        "order_id",
        "customer_ref",
        "sayad_registered",
    }
)

OPTIONAL_FIELDS: frozenset[str] = frozenset(
    {
        "leaf_number",
        "issuer_branch",
        "issuer_account_number",
        "reason",
        "invoice_id",
        "installment_leg_id",
        "scan_url",
        "scan_ref",
        "registered_by",
        "notes",
    }
)

KNOWN_FIELDS: frozenset[str] = MANDATORY_FIELDS | OPTIONAL_FIELDS


@dataclass(frozen=True, slots=True)
class ChequeRegistration:
    """A validated, normalized cheque registration (intake, no journal).

    Amounts are Rial ints; dates are Gregorian :class:`FiscalDate`
    converted from the Jalali payload strings.
    """

    kind: ChequeKind
    purpose: ChequePurpose
    sayad_id: str
    amount_irr: int
    amount_toman: int
    issue_date: FiscalDate
    due_date: FiscalDate
    issuer_name: str
    issuer_national_id: str
    issuer_bank: str
    payee_name: str
    payee_national_id: str
    order_id: str
    customer_ref: str
    sayad_registered: bool
    leaf_number: str | None = None
    issuer_branch: str | None = None
    issuer_account_number: str | None = None
    reason: str | None = None
    invoice_id: str | None = None
    installment_leg_id: str | None = None
    scan_url: str | None = None
    scan_ref: str | None = None
    registered_by: str | None = None
    notes: str | None = None


def _fail(field_errors: dict[str, str]) -> None:
    raise ChequeValidationError(
        "Cheque payload failed validation.",
        field_errors=field_errors,
    )


def _parse_jalali(raw: object, field_name: str, errors: dict[str, str]) -> FiscalDate | None:
    if not isinstance(raw, str) or not _JALALI_DATE_RE.match(raw):
        errors[field_name] = "Must be a Jalali date in YYYY-MM-DD format."
        return None
    try:
        year, month, day = (int(part) for part in raw.split("-"))
        return FiscalDate.from_jalali(jdatetime.date(year, month, day))
    except ValueError as exc:
        errors[field_name] = f"Invalid Jalali date: {exc}."
        return None


def validate_registration_payload(
    payload: Mapping[str, object],
    *,
    kind: ChequeKind = ChequeKind.RECEIVED,
) -> ChequeRegistration:
    """Validate a 24-field payload and return its normalized form.

    Raises :class:`ChequeValidationError` carrying every field error
    at once (the caller reports them all; nothing is partially
    applied).
    """
    errors: dict[str, str] = {}

    unknown = sorted(set(payload) - KNOWN_FIELDS)
    if unknown:
        errors["__unknown__"] = f"Unknown field(s): {', '.join(unknown)}."
    for name in sorted(MANDATORY_FIELDS - set(payload)):
        errors[name] = "This field is mandatory."

    def _str(name: str) -> str | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            errors[name] = "Must be a non-blank string."
            return None
        return value.strip()

    sayad_id = _str("sayad_id")
    if sayad_id is not None and not _SAYAD_ID_RE.match(sayad_id):
        errors["sayad_id"] = "Must be exactly 16 digits, no spaces or dashes."

    amount_toman: int | None = None
    raw_amount = payload.get("amount_toman")
    if raw_amount is None:
        errors["amount_toman"] = "This field is mandatory."
    elif isinstance(raw_amount, bool) or not isinstance(raw_amount, int):
        errors["amount_toman"] = "Must be an integer number of Toman."
    elif raw_amount <= 0:
        errors["amount_toman"] = "Must be greater than 0."
    else:
        amount_toman = raw_amount

    amount_currency = _str("amount_currency")
    if amount_currency is not None and amount_currency not in ("IRT", "IRR"):
        errors["amount_currency"] = 'Must be "IRT" (Toman) or "IRR" (Rial).'

    issue_date = _parse_jalali(payload.get("issue_date"), "issue_date", errors)
    due_date = _parse_jalali(payload.get("due_date"), "due_date", errors)
    if issue_date is not None and due_date is not None and due_date.date < issue_date.date:
        errors["due_date"] = "Due date must be on or after the issue date."

    issuer_name = _str("issuer_name")
    issuer_national_id = _str("issuer_national_id")
    if issuer_national_id is not None and not _NATIONAL_ID_RE.match(issuer_national_id):
        errors["issuer_national_id"] = "Must be 10 digits (natural person) or 11 digits (legal entity)."

    issuer_bank = _str("issuer_bank")
    if issuer_bank is not None and not _BANK_CODE_RE.match(issuer_bank):
        errors["issuer_bank"] = "Must start with the 3-digit bank code, e.g. '056' or '056 سامان'."

    payee_name = _str("payee_name")
    payee_national_id = _str("payee_national_id")
    if payee_national_id is not None and not _NATIONAL_ID_RE.match(payee_national_id):
        errors["payee_national_id"] = "Must be 10 digits (natural person) or 11 digits (legal entity)."

    purpose_raw = _str("purpose")
    purpose: ChequePurpose | None = None
    if purpose_raw is not None:
        try:
            purpose = ChequePurpose(purpose_raw)
        except ValueError:
            errors["purpose"] = 'Must be "payment" (commercial) or "collateral".'

    order_id = _str("order_id")
    customer_ref = _str("customer_ref")

    sayad_registered = payload.get("sayad_registered")
    if sayad_registered is not None and not isinstance(sayad_registered, bool):
        errors["sayad_registered"] = "Must be a boolean."

    leaf_number = _str("leaf_number")
    issuer_branch = _str("issuer_branch")
    issuer_account_number = _str("issuer_account_number")
    reason = _str("reason")
    invoice_id = _str("invoice_id")
    installment_leg_id = _str("installment_leg_id")
    scan_url = _str("scan_url")
    scan_ref = _str("scan_ref")
    registered_by = _str("registered_by")
    notes = _str("notes")

    if errors:
        _fail(errors)

    assert sayad_id is not None
    assert amount_toman is not None
    assert amount_currency is not None
    assert issue_date is not None
    assert due_date is not None
    assert issuer_name is not None
    assert issuer_national_id is not None
    assert issuer_bank is not None
    assert payee_name is not None
    assert payee_national_id is not None
    assert purpose is not None
    assert order_id is not None
    assert customer_ref is not None
    assert isinstance(sayad_registered, bool)

    # Toman -> Rial x10 at the connector boundary; IRR is already Rial.
    amount_irr = amount_toman * 10 if amount_currency == "IRT" else amount_toman
    return ChequeRegistration(
        kind=kind,
        purpose=purpose,
        sayad_id=sayad_id,
        amount_irr=amount_irr,
        amount_toman=amount_toman,
        issue_date=issue_date,
        due_date=due_date,
        issuer_name=issuer_name,
        issuer_national_id=issuer_national_id,
        issuer_bank=issuer_bank,
        payee_name=payee_name,
        payee_national_id=payee_national_id,
        order_id=order_id,
        customer_ref=customer_ref,
        sayad_registered=sayad_registered,
        leaf_number=leaf_number,
        issuer_branch=issuer_branch,
        issuer_account_number=issuer_account_number,
        reason=reason,
        invoice_id=invoice_id,
        installment_leg_id=installment_leg_id,
        scan_url=scan_url,
        scan_ref=scan_ref,
        registered_by=registered_by,
        notes=notes,
    )


def same_cheque_claim(registration: ChequeRegistration, cheque: Cheque) -> bool:
    """True when a new registration is the SAME claim as an existing cheque.

    Dual-entry convergence (US-533, §6): a site-registered cheque
    later entered manually — or re-registered by the site — is ONE
    record, not two. The comparison covers the business-identifying
    subset: sayad_id, amount (Rial), issue/due dates, issuer and
    payee national ids, purpose, kind, order and customer refs.
    Optional fields (leaf number, branch, account number, reason,
    invoice/leg links, scan, notes) are compared only when BOTH
    sides carry a value — a manual entry that omits what the site
    sent is still the same claim; a value that CONTRADICTS the
    stored one is a different payload and goes to the review queue
    as a ``duplicate_sayad_id`` conflict (§10.4).
    """
    if registration.sayad_id != cheque.sayad_id:
        return False
    if registration.amount_irr != cheque.amount_irr:
        return False
    if registration.issue_date != cheque.issue_date or registration.due_date != cheque.due_date:
        return False
    if registration.issuer_national_id != cheque.issuer_national_id:
        return False
    if registration.payee_national_id != cheque.payee_national_id:
        return False
    if registration.purpose != cheque.purpose or registration.kind != cheque.kind:
        return False
    if registration.order_id != cheque.order_id:
        return False
    if registration.customer_ref != cheque.customer_ref:
        return False
    for new_value, old_value in (
        (registration.leaf_number, cheque.leaf_number),
        (registration.issuer_branch, cheque.issuer_branch),
        (registration.issuer_account_number, cheque.issuer_account_number),
        (registration.invoice_id, cheque.invoice_id),
        (registration.installment_leg_id, cheque.installment_leg_id),
    ):
        if new_value is not None and old_value is not None and new_value != old_value:
            return False
    return True


def parse_jalali_date(raw: str) -> FiscalDate:
    """Parse one Jalali YYYY-MM-DD date, raising ChequeValidationError."""
    errors: dict[str, str] = {}
    parsed = _parse_jalali(raw, "date", errors)
    if parsed is None:
        _fail(errors)
    assert parsed is not None
    return parsed


def today_jalali() -> str:
    """Today's date as a Jalali YYYY-MM-DD string (for API responses)."""
    return jdatetime.date.today().strftime("%Y-%m-%d")


def to_jalali_string(day: datetime.date) -> str:
    """Render a Gregorian date as Jalali YYYY-MM-DD."""
    return jdatetime.date.fromgregorian(date=day).strftime("%Y-%m-%d")
