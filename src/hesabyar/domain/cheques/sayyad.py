"""Sayyad reconciliation — Layer 1 (Fintech, legal) + Layer 2 (Ehrazino, natural).

Phased design §5 §1.3/§1.4/§3.5 (US-534). Two tiers, one receipt model:

- **Legal tier — Fintech.** The 18 Sayyad scopes are named here by
  their documented scope strings ONLY (``credit:<name>:<verb>``).
  Their exact input/output field shapes, the ``ac-sayad-*`` auth
  model and the per-call tariff are UNVERIFIED (§9.2 #1–3) — NO field
  is guessed anywhere in this module. Until the Fintech contract is
  signed (۵ت remainder, B3), inquiries run against the deterministic
  simulator in :mod:`hesabyar.infrastructure.sayyad.simulator` and
  every result is marked ``simulated=True``. There is NO production
  Fintech call without credentials.
- **Natural tier — Ehrazino.** The integration shape was verified
  against Ehrazino's public API pages (2026-11-19) for the two calls
  this story uses — ``cheque-color`` (input ``nationalCode`` 10/11
  digits; output colour name + numeric code 1–5; 1,700 Toman) and
  ``sayad-check`` (input ``sayadId`` 16 digits; output branch code,
  check type, issue/expiry dates, serial/series numbers, bounced
  indicator; 6,900 Toman). The endpoint URL path and the auth header
  are still UNVERIFIED (the docs page could not be opened from this
  environment), and whether Ehrazino opens a business account for
  natural persons is UNVERIFIED (§9.2 #9) — so the Ehrazino API path
  is also simulator-backed until B3 verifies the transport, and the
  manual channel (SMS / CBI website / bank app) stays the natural
  tier's working path. Colour is issuer history only, NEVER a
  statement about a specific leaf.

**Never-store rule (§3.5, Fintech contract):** an inquiry result
carries only verdict/code/time/reference — never the raw response
and never a full national ID. The port methods accept a national id
only as a transient lookup key; results never echo it, the simulator
keys its fixtures by its hash, and nothing here logs it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from hesabyar.domain.cheques.models import InquiryChannel, InquiryType
from hesabyar.domain.common.errors import HesabYarError

#: The 18 Fintech Sayyad scopes (phased design §5 §1.3, from
#: finnotech.ir/allServices). Scope NAMES only — no input/output
#: field is claimed for any of them (§9.2 #1–3).
FINNOTECH_SAYYAD_SCOPES: tuple[str, ...] = (
    "credit:cheque-status-inquiry:post",
    "credit:sayad-serial-inquiry:get",
    "credit:sayad-transfers-chain-inquiry:post",
    "credit:cheque-color-inquiry:get",
    "credit:cheque-color-inquiry-legal:get",
    "credit:back-cheques:get",
    "credit:back-cheques-legal:get",
    "credit:chequebook-inquiry:get",
    "credit:ac-sayad-issue-cheque:post",
    "credit:ac-sayad-transfer-cheque:post",
    "credit:sms-sayad-accept-cheque:post",
    "credit:sms-sayad-transfer-cheque:post",
    "credit:sms-sayad-cancel-cheque:post",
    "credit:sms-cheque-color-inquiry:get",
    "credit:sms-cheque-status-inquiry:post",
    "credit:sms-sayad-transfers-chain-inquiry:post",
    "credit:sms-sayady-cheque-inquiry:get",
    "credit:sms-sayad-issuer-inquiry-cheque:post",
)

#: Scopes whose ``sms-`` prefix marks the SMS-mediated Sayyad path
#: (the same verdict, delivered by SMS instead of the API).
SMS_SCOPES: frozenset[str] = frozenset(
    scope for scope in FINNOTECH_SAYYAD_SCOPES if ":sms-" in scope
)

#: Fintech scope -> the structured receipt type it answers (§1.4).
SCOPE_INQUIRY_TYPES: dict[str, InquiryType] = {
    "credit:cheque-status-inquiry:post": InquiryType.LEAF_STATUS,
    "credit:sayad-serial-inquiry:get": InquiryType.ITEMS_MATCH,
    "credit:sayad-transfers-chain-inquiry:post": InquiryType.TRANSFER_CHAIN,
    "credit:cheque-color-inquiry:get": InquiryType.COLOR,
    "credit:cheque-color-inquiry-legal:get": InquiryType.COLOR,
    "credit:back-cheques:get": InquiryType.BACK_CHEQUES,
    "credit:back-cheques-legal:get": InquiryType.BACK_CHEQUES,
    "credit:chequebook-inquiry:get": InquiryType.ISSUER_HISTORY,
    "credit:ac-sayad-issue-cheque:post": InquiryType.LEAF_STATUS,
    "credit:ac-sayad-transfer-cheque:post": InquiryType.TRANSFER_CHAIN,
    "credit:sms-sayad-accept-cheque:post": InquiryType.LEAF_STATUS,
    "credit:sms-sayad-transfer-cheque:post": InquiryType.TRANSFER_CHAIN,
    "credit:sms-sayad-cancel-cheque:post": InquiryType.LEAF_STATUS,
    "credit:sms-cheque-color-inquiry:get": InquiryType.COLOR,
    "credit:sms-cheque-status-inquiry:post": InquiryType.LEAF_STATUS,
    "credit:sms-sayad-transfers-chain-inquiry:post": InquiryType.TRANSFER_CHAIN,
    "credit:sms-sayady-cheque-inquiry:get": InquiryType.ITEMS_MATCH,
    "credit:sms-sayad-issuer-inquiry-cheque:post": InquiryType.ISSUER_HISTORY,
}

#: Ehrazino cheque-color price: 1,700 Toman = 17,000 Rial (verified
#: 2026-11-19, ehrazino.com/api/banking/cheque-color + pricing page).
EHRAZINO_CHEQUE_COLOR_COST_IRR = 17_000

#: Ehrazino sayad-check price: 6,900 Toman = 69,000 Rial (verified
#: 2026-11-19, ehrazino.com/api/banking/sayad-check).
EHRAZINO_SAYAD_CHECK_COST_IRR = 69_000

#: Verified Ehrazino cheque-color shape (input/output field NAMES
#: only — endpoint path + auth header still UNVERIFIED).
EHRAZINO_CHEQUE_COLOR_SHAPE: dict[str, tuple[str, ...]] = {
    "input": ("nationalCode",),
    "output": ("cheque_color", "cheque_color_code"),
}

#: Verified Ehrazino sayad-check shape (input/output field NAMES
#: only — endpoint path + auth header still UNVERIFIED). ``name``
#: and ``iban`` are PII: the port drops them at the boundary and
#: returns only the storable verdict fields below.
EHRAZINO_SAYAD_CHECK_SHAPE: dict[str, tuple[str, ...]] = {
    "input": ("sayadId",),
    "output": (
        "branch_code",
        "check_type",
        "expiration_date",
        "iban",
        "issue_date",
        "name",
        "returned_cheques",
        "sayad_id",
        "serial_no",
        "series_no",
    ),
}

#: Receipt types that satisfy the MANDATORY receipt rule (§1.4): an
#: important transition — above all registered -> held — needs at
#: least one item/status receipt.
REQUIRED_RECEIPT_TYPES: frozenset[InquiryType] = frozenset(
    {InquiryType.ITEMS_MATCH, InquiryType.LEAF_STATUS}
)


class SayyadScope(StrEnum):
    """The Fintech Sayyad scopes as a typed enum (names only)."""

    CHEQUE_STATUS = "credit:cheque-status-inquiry:post"
    SAYAD_SERIAL = "credit:sayad-serial-inquiry:get"
    TRANSFERS_CHAIN = "credit:sayad-transfers-chain-inquiry:post"
    CHEQUE_COLOR = "credit:cheque-color-inquiry:get"
    CHEQUE_COLOR_LEGAL = "credit:cheque-color-inquiry-legal:get"
    BACK_CHEQUES = "credit:back-cheques:get"
    BACK_CHEQUES_LEGAL = "credit:back-cheques-legal:get"
    CHEQUEBOOK = "credit:chequebook-inquiry:get"
    AC_ISSUE_CHEQUE = "credit:ac-sayad-issue-cheque:post"
    AC_TRANSFER_CHEQUE = "credit:ac-sayad-transfer-cheque:post"
    SMS_ACCEPT_CHEQUE = "credit:sms-sayad-accept-cheque:post"
    SMS_TRANSFER_CHEQUE = "credit:sms-sayad-transfer-cheque:post"
    SMS_CANCEL_CHEQUE = "credit:sms-sayad-cancel-cheque:post"
    SMS_CHEQUE_COLOR = "credit:sms-cheque-color-inquiry:get"
    SMS_CHEQUE_STATUS = "credit:sms-cheque-status-inquiry:post"
    SMS_TRANSFERS_CHAIN = "credit:sms-sayad-transfers-chain-inquiry:post"
    SMS_SAYAD_CHEQUE = "credit:sms-sayady-cheque-inquiry:get"
    SMS_ISSUER_INQUIRY = "credit:sms-sayad-issuer-inquiry-cheque:post"


@dataclass(frozen=True, slots=True)
class SayyadInquiryResult:
    """One Sayyad inquiry verdict — storable fields ONLY (§3.5).

    No raw response, no national id, no issuer name: ``result_code``
    carries the verdict (e.g. ``color=3``, ``items_match=true``),
    ``result_summary`` a short human sentence, ``reference`` the
    tracking number. ``simulated`` marks simulator output so it can
    never be mistaken for an official response.
    """

    scope: str
    inquiry_type: InquiryType
    channel: InquiryChannel
    result_code: str
    result_summary: str
    reference: str
    cost_irr: int = 0
    simulated: bool = False
    # Structured verdict fields the service may apply (colour code,
    # item-match boolean, bounced count) — still verdicts, not PII.
    color_code: int | None = None
    items_matched: bool | None = None
    back_cheques_count: int | None = None
    in_transit_status: str | None = None
    requested_at: datetime.datetime | None = None
    responded_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if not self.result_code.strip():
            raise ValueError("result_code cannot be blank.")
        if self.color_code is not None and not (1 <= self.color_code <= 5):
            raise ValueError("color_code must be 1–5.")
        if self.cost_irr < 0:
            raise ValueError("cost_irr must be >= 0.")


@dataclass(frozen=True, slots=True)
class EhrazinoColorResult:
    """Verdict of an Ehrazino cheque-color inquiry (verified shape).

    Input ``nationalCode`` (10/11 digits) is a transient lookup key
    and is NOT carried on the result. Output: the colour name (display
    only) + the numeric code 1–5 (decision logic uses the code).
    """

    color_code: int
    color_name: str
    cost_irr: int = EHRAZINO_CHEQUE_COLOR_COST_IRR
    simulated: bool = False
    requested_at: datetime.datetime | None = None
    responded_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if not (1 <= self.color_code <= 5):
            raise ValueError("color_code must be 1–5.")


@dataclass(frozen=True, slots=True)
class EhrazinoSayadCheckResult:
    """Verdict of an Ehrazino sayad-check inquiry (verified shape).

    Only storable verdict fields: whether the registered items match
    the leaf, the leaf serial/series, branch code, check type and the
    bounced indicator. The issuer ``name`` and ``iban`` from the
    upstream payload are dropped at the boundary (§3.5) and never
    appear here.
    """

    sayad_id: str
    items_matched: bool
    serial_no: str | None = None
    series_no: str | None = None
    branch_code: str | None = None
    check_type: str | None = None
    returned_cheques: bool | None = None
    cost_irr: int = EHRAZINO_SAYAD_CHECK_COST_IRR
    simulated: bool = False
    requested_at: datetime.datetime | None = None
    responded_at: datetime.datetime | None = None


class SayyadInquiryError(HesabYarError):
    """A Sayyad inquiry could not run (unknown scope, no fixture, live not ready)."""


class SayyadLiveNotReadyError(SayyadInquiryError):
    """Live Sayyad connectivity is not available yet.

    Raised when code asks for a live Fintech/Ehrazino call before
    the transport is verified (Fintech field shapes §9.2 #1–3,
    Ehrazino endpoint/auth). Live wiring lands with the ۵ت
    remainder (B3); until then the simulator + manual fallback are
    the only paths.
    """


class FintechSayyadPort(Protocol):
    """Fintech Sayyad inquiries (legal tier) — simulator until B3."""

    def inquire(
        self,
        scope: str,
        *,
        sayad_id: str,
        national_id: str | None = None,
        scenario: str | None = None,
    ) -> SayyadInquiryResult:
        """Run one scope inquiry; returns the verdict only (§3.5)."""
        ...

    def available_scopes(self) -> tuple[str, ...]:
        """The scope names this port can answer (the 18, for the simulator)."""
        ...


class EhrazinoPort(Protocol):
    """Ehrazino inquiries (natural tier) — simulator until B3 verifies transport."""

    def cheque_color(
        self, national_id: str, *, scenario: str | None = None
    ) -> EhrazinoColorResult:
        """Issuer colour by national code (10/11 digits); history only, never a leaf verdict."""
        ...

    def sayad_check(
        self, sayad_id: str, *, scenario: str | None = None
    ) -> EhrazinoSayadCheckResult:
        """Leaf identity by 16-digit sayad_id (items-match verdict)."""
        ...


def has_required_receipt(receipt_types: list[InquiryType]) -> bool:
    """True when the receipts include an item/status receipt (§1.4 mandatory rule)."""
    return any(receipt_type in REQUIRED_RECEIPT_TYPES for receipt_type in receipt_types)


def inquiry_type_for_scope(scope: str) -> InquiryType:
    """Map a Fintech scope name to its receipt type (unknown scope fails closed)."""
    try:
        return SCOPE_INQUIRY_TYPES[scope]
    except KeyError as exc:
        raise SayyadInquiryError(
            f"Unknown Fintech Sayyad scope {scope!r}; no inquiry was run.",
            details={"scope": scope},
        ) from exc
