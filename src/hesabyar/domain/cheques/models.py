"""Cheque registry models (US-531, Phase 5-4 ۵ث).

The registry is the HesabYar-side record of every cheque. The
accounting truth lives in Tryton's ``account_check_ir`` module: every
state transition that carries a journal happens there, in ONE Tryton
transaction (state + journal together — the golden rule of phased
design §5 §2). This module owns the shapes both sides agree on:

- :class:`Cheque` — the registry record (the 24 payload fields +
  lifecycle, storage, bounce, endorsement and link fields).
- :class:`ChequeSettings` — per-installation cheque policy (§9.1
  defaults: 7-day leaf-arrival deadline, bounce destination =
  receivables, red/brown = manager approval, draft journals).
- :class:`CustodyLogEntry` — append-only physical-leaf moves
  (a custody move is NEVER a journal).
- :class:`InquiryReceipt` — one structured Sayyad inquiry receipt
  (verdict/code/time only — never raw PII, §3.5).

US-531 scope is received commercial cheques (``kind=received``,
``purpose=payment``). ``kind=issued`` and ``purpose=collateral``
are carried on the model from day one (US-530 contract) but their
transitions belong to US-532; registering them (intake, no journal)
is purpose-agnostic and shared with US-533.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from hesabyar.domain.common.dates import FiscalDate


class ChequeKind(StrEnum):
    """Received (from a customer) or issued (to a supplier)."""

    RECEIVED = "received"
    ISSUED = "issued"


class ChequePurpose(StrEnum):
    """Commercial (on-balance-sheet) or collateral (memorandum only)."""

    PAYMENT = "payment"
    COLLATERAL = "collateral"


class ChequeState(StrEnum):
    """Accounting state of a cheque (§5 §2.1–§2.3).

    ``sayad_status`` (Sayyad registration) is a SEPARATE axis —
    never confuse the two. US-532 adds the issued states
    (``issued``, ``voided``) and ``called_in`` — a collateral
    cheque called in (received) or unexpectedly presented (issued),
    now commercial and following §2.1 / §2.3 from there.
    """

    REGISTERED = "registered"
    HELD = "held"
    IN_CLEARING = "in_clearing"
    CLEARED = "cleared"
    BOUNCED = "bounced"
    RETURNED = "returned"
    ENDORSED = "endorsed"
    EXPIRED_UNRECEIVED = "expired_unreceived"
    REJECTED_MISMATCH = "rejected_mismatch"
    ISSUED = "issued"
    #: Voided — by a site cancellation before ``held`` (US-533: a voided
    #: intake never revives; its ``sayad_id`` is reusable) or by voiding
    #: an issued leaf (US-532, سند ۱۳).
    VOIDED = "voided"
    CALLED_IN = "called_in"


#: States from which a cheque never moves again. ``called_in`` is
#: NOT terminal: a called-in received collateral cheque follows
#: §2.1 from there, and a called-in issued collateral cheque clears
#: once the accountant has posted its commercial journal.
TERMINAL_STATES: frozenset[ChequeState] = frozenset(
    {
        ChequeState.CLEARED,
        ChequeState.RETURNED,
        ChequeState.ENDORSED,
        ChequeState.VOIDED,
        ChequeState.EXPIRED_UNRECEIVED,
        ChequeState.REJECTED_MISMATCH,
    }
)

#: States excluded from the sayad_id uniqueness rule: a leaf that
#: expired unreceived, was rejected on mismatch, or was voided by a
#: site cancellation before held may be registered again with the
#: same sayad_id (a NEW record — the old one never revives, §2.1).
SAYAD_REUSABLE_STATES: frozenset[ChequeState] = frozenset(
    {ChequeState.VOIDED, ChequeState.EXPIRED_UNRECEIVED, ChequeState.REJECTED_MISMATCH}
)


class SayadStatus(StrEnum):
    """Sayyad registration status — separate from accounting ``state``."""

    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    CONFIRMED = "confirmed"
    TRANSFERRED = "transferred"


class StorageLocation(StrEnum):
    """Physical location of the paper leaf."""

    CASHBOX = "cashbox"
    VAULT = "vault"
    BANK = "bank"
    WITH_ENDORSEE = "with_endorsee"
    RETURNED_TO_ISSUER = "returned_to_issuer"


class BounceDestination(StrEnum):
    """Where a bounced cheque's claim goes (§9.1 #2)."""

    RECEIVABLES = "receivables"
    BOUNCED_RECEIVABLES = "bounced_receivables"


class RedBrownPolicy(StrEnum):
    """Installation policy for a red/brown issuer color (or high
    in-transit commitments): refuse outright, or hold for a manager's
    explicit approval."""

    REJECT = "reject"
    MANAGER_APPROVAL = "manager_approval"


class InstallmentLegStatus(StrEnum):
    """Effect of a cheque transition on its linked installment leg
    (۵ج consumes this; the two-way test with a real leg is deferred
    to ۵ج — see :func:`leg_status_effect` in state_machine)."""

    PENDING_CLEARANCE = "pending_clearance"
    PAID = "paid"
    UNPAID = "unpaid"


class InquiryChannel(StrEnum):
    """Channel a Sayyad inquiry receipt came through (§1.4)."""

    SMS = "sms"
    CBI_WEBSITE = "cbi_website"
    BANK_APP = "bank_app"
    PAYMENT_APP = "payment_app"
    FINNOTECH_API = "finnotech_api"
    EHRAZINO_API = "ehrazino_api"


class InquiryType(StrEnum):
    """What the inquiry asked about."""

    COLOR = "color"
    LEAF_STATUS = "leaf_status"
    ITEMS_MATCH = "items_match"
    ISSUER_HISTORY = "issuer_history"
    BACK_CHEQUES = "back_cheques"
    TRANSFER_CHAIN = "transfer_chain"


#: CBI issuer-colour scale, numeric code 1–5 (count-based; never
#: amount thresholds, §9.2 #13). 1 = white (no bounced cheques),
#: 5 = red. US-534 confirms the mapping against the CBI source; the
#: decision logic only depends on the ordering (4–5 = brown/red).
WHITE_COLOR_CODE = 1
BROWN_COLOR_CODE = 4
RED_COLOR_CODE = 5

#: Cost of one SMS inquiry receipt: 350 Toman = 3,500 Rial (§1.4).
SMS_INQUIRY_COST_IRR = 3_500

#: Ehrazino cheque-color price: 1,700 Toman = 17,000 Rial (verified
#: 2026-11-19; see domain.cheques.sayyad).
EHRAZINO_CHEQUE_COLOR_COST_IRR = 17_000

#: Ehrazino sayad-check price: 6,900 Toman = 69,000 Rial (verified
#: 2026-11-19; see domain.cheques.sayyad).
EHRAZINO_SAYAD_CHECK_COST_IRR = 69_000


@dataclass(frozen=True, slots=True)
class ChequeSettings:
    """Per-installation cheque policy (settings ← cheque module).

    Defaults are the §9.1 build defaults (US-530 documents them in
    ``docs/cheque-decisions.md``; this is the code that enforces
    them). ``auto_post`` is deliberately NOT here — it lives in the
    installation settings + per-event policy rows the F3/F4 gate
    reads, and defaults to draft.
    """

    installation_id: str
    leaf_arrival_deadline_days: int = 7
    # No hard-coded default (§9.1 #6 / docs/cheque-decisions.md): the
    # standard clearing duration is UNVERIFIED, so the setting stays
    # unset until the installation sets it; US-534 defines behaviour
    # while it is unset.
    clearing_window_days: int | None = None
    bounce_destination: BounceDestination = BounceDestination.RECEIVABLES
    red_brown_policy: RedBrownPolicy = RedBrownPolicy.MANAGER_APPROVAL
    max_in_transit_commitments_irr: int | None = None
    default_storage_location: StorageLocation = StorageLocation.VAULT

    def __post_init__(self) -> None:
        if self.leaf_arrival_deadline_days < 1:
            raise ValueError("leaf_arrival_deadline_days must be >= 1.")
        if self.clearing_window_days is not None and self.clearing_window_days < 1:
            raise ValueError("clearing_window_days must be >= 1.")
        if (
            self.max_in_transit_commitments_irr is not None
            and self.max_in_transit_commitments_irr < 0
        ):
            raise ValueError("max_in_transit_commitments_irr must be >= 0.")


@dataclass(frozen=True, slots=True)
class CustodyLogEntry:
    """One append-only physical-leaf move (read-only after writing).

    Origin/destination/time/actor/reason — never a journal.
    """

    cheque_id: str
    from_location: StorageLocation | None
    to_location: StorageLocation
    actor: str
    reason: str
    at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True, slots=True)
class InquiryReceipt:
    """One structured Sayyad inquiry receipt (§1.4).

    Only verdict/code/time/reference are stored — never the raw
    inquiry response and never full national IDs (§3.5).
    """

    cheque_id: str
    channel: InquiryChannel
    inquiry_type: InquiryType
    result_code: str
    result_summary: str
    reference: str
    recorded_by: str
    cost_irr: int = 0
    requested_at: datetime.datetime | None = None
    responded_at: datetime.datetime | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if not self.result_code.strip():
            raise ValueError("result_code cannot be blank.")
        if self.cost_irr < 0:
            raise ValueError("cost_irr must be >= 0.")


@dataclass(slots=True)
class Cheque:
    """One cheque in the HesabYar registry.

    Mutable by design: the application service applies transitions
    to it (state + move refs + timestamps) after the engine has
    committed the same transition atomically. The ``engine_id`` links
    the registry row to its ``account.check`` record; ``move_ids``
    maps transition name -> engine move id for the full audit trail
    (report 17).
    """

    id: str
    tenant_id: str
    installation_id: str
    company_engine_id: int
    kind: ChequeKind
    purpose: ChequePurpose
    sayad_id: str
    amount_irr: int
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
    source: str = "manual"
    state: ChequeState = ChequeState.REGISTERED
    sayad_status: SayadStatus = SayadStatus.UNREGISTERED
    sayad_unconfirmed: bool = False
    leaf_number: str | None = None
    issuer_branch: str | None = None
    issuer_account_number: str | None = None
    reason: str | None = None
    invoice_id: str | None = None
    installment_leg_id: str | None = None
    applied_amount_irr: int | None = None
    scan_url: str | None = None
    scan_ref: str | None = None
    #: Filestore ref of the downloaded leaf scan
    #: (``cheques/<sayad_id>-<hash>.<ext>``), set by US-533 when the
    #: ``scan_url`` download succeeds. The site's own ``scan_ref``
    #: is kept untouched — this is HesabYar's copy.
    scan_filestore_ref: str | None = None
    registered_by: str | None = None
    notes: str | None = None
    storage_location: StorageLocation | None = None
    engine_id: int | None = None
    move_ids: dict[str, int] = field(default_factory=dict)
    destination_bank_account_id: int | None = None
    destination_bank_name: str | None = None
    # -- clearing (US-534, §1.5) --------------------------------------------
    # The day the leaf was handed to the bank (the start of the
    # clearing date window). None for cheques deposited before US-534
    # recorded it — those can never auto-match (the window key cannot
    # be verified) and clear only by manual confirmation.
    deposit_date: FiscalDate | None = None
    # Confidence of the clearing match that cleared this cheque
    # ('exact' | 'probable' | None), and the statement line that did
    # it, for the clearing-matching-rate report (report 12).
    clearing_confidence: str | None = None
    clearing_statement_ref: str | None = None
    # Engine reconciliation id joining the deposit move's clearing leg
    # (سند ۲) with the clear move's clearing leg (سند ۳), when the
    # legs reconciled; None when reconciliation is still pending or
    # failed (a review item carries the follow-up).
    clearing_reconciliation_id: int | None = None
    # -- pre-acceptance (recorded at confirm-receipt) -------------------
    issuer_color_code: int | None = None
    issuer_color_inquired_at: datetime.datetime | None = None
    issuer_back_cheques: int | None = None
    issuer_in_transit_commitments_irr: int | None = None
    manager_approval_by: str | None = None
    manager_approval_at: datetime.datetime | None = None
    receipt_missing_warning: bool = False
    # -- bounce -----------------------------------------------------------
    bounce_reason: str | None = None
    bounce_date: FiscalDate | None = None
    bounce_certificate_reference: str | None = None
    dunning_notified_at: datetime.datetime | None = None
    # -- endorsement ------------------------------------------------------
    endorsee_name: str | None = None
    endorsee_type: str | None = None
    sayad_transfer_reference: str | None = None
    journal_pending_review: bool = False
    # -- installment-leg hook (۵ج consumes this) -------------------------
    installment_leg_status: InstallmentLegStatus | None = None
    # -- mismatch ---------------------------------------------------------
    mismatch_detail: str | None = None
    # -- issued / collateral (US-532) -------------------------------------
    #: The obligation this collateral cheque secures (installment
    #: plan / contract / invoice reference) — the report 6
    #: dimension. Optional at registration; the cheque is still
    #: memorandum-only without it.
    secured_obligation: str | None = None
    #: Engine checkbook id the issued leaf was drawn from (US-532).
    checkbook_engine_id: int | None = None
    #: Who approved this case's call-in (§9.1 #4, §5 §4) and when.
    call_in_approved_by: str | None = None
    call_in_approved_at: datetime.datetime | None = None
    #: Why an issued cheque was voided (سند ۱۳ audit trail).
    void_reason: str | None = None
    #: True when an issued collateral cheque was presented at the
    #: bank by the counterparty without notice (§2.3).
    unexpected_call_in: bool = False
    #: Bank-statement withdrawal reference matched by leaf number
    #: for an unexpected call-in.
    call_in_statement_reference: str | None = None
    # -- intake reminders (job idempotency) -------------------------------
    reminder_day3_sent_at: datetime.datetime | None = None
    reminder_final_sent_at: datetime.datetime | None = None
    #: When the stale-in-clearing review item was filed (US-533 daily
    #: job): filed once per cheque, never re-filed on later runs.
    stale_in_clearing_filed_at: datetime.datetime | None = None
    #: The §10.2 source event that created this intake
    #: (``source_event_id`` of the site webhook / polled change), so
    #: every auto-created record traces to its origin event (§6
    #: dual-entry traceability). None for manual entries.
    source_event_id: str | None = None
    created_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    updated_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def amount_toman(self) -> int:
        """Amount in Toman (display unit; storage is Rial)."""
        return self.amount_irr // 10

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def touch(self) -> None:
        self.updated_at = datetime.datetime.now(datetime.UTC)


@dataclass
class MonthlyMatchHistory:
    """One Jalali month of clearing-match outcomes (US-553, report 12).

    The clearing service records every matching run's newly decided
    lines here (``cheque_match_history`` table, one row per
    (installation, Jalali month)); the trend chart renders FROM
    these rows, never recomputed from raw match records at read
    time. ``match_rate`` is the share of lines the matcher decided
    (exact + probable) out of all lines it saw, as an exact
    decimal fraction (0–1); a month with no lines rates 0.

    NO BACKFILL: history starts with the first matching run after
    deploy. The pre-existing match records cannot seed it faithfully
    — an unmatched clearing line is indistinguishable from a generic
    reconcile unmatched line (both are ``target_type=MANUAL`` with
    an empty ``target_ref``), and suggested matches carry no
    decision timestamp. See docs/cash-forecast.md.
    """

    id: str
    tenant_id: str
    installation_id: str
    month_jalali: str  # Jalali "YYYY-MM"
    exact_count: int = 0
    probable_count: int = 0
    ambiguous_count: int = 0
    unmatched_count: int = 0
    created_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    updated_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )

    @property
    def total_lines(self) -> int:
        """All lines the matcher saw this month."""
        return (
            self.exact_count
            + self.probable_count
            + self.ambiguous_count
            + self.unmatched_count
        )

    @property
    def match_rate(self) -> Decimal:
        """Share of decided lines (exact + probable), 0–1, 4 places."""
        total = self.total_lines
        if total == 0:
            return Decimal("0")
        return (Decimal(self.exact_count + self.probable_count) / Decimal(total)).quantize(
            Decimal("0.0001")
        )

    def add_counts(
        self, *, exact: int, probable: int, ambiguous: int, unmatched: int
    ) -> MonthlyMatchHistory:
        """Return a copy with one run's counts added (the upsert-add)."""
        return MonthlyMatchHistory(
            id=self.id,
            tenant_id=self.tenant_id,
            installation_id=self.installation_id,
            month_jalali=self.month_jalali,
            exact_count=self.exact_count + exact,
            probable_count=self.probable_count + probable,
            ambiguous_count=self.ambiguous_count + ambiguous,
            unmatched_count=self.unmatched_count + unmatched,
            created_at=self.created_at,
            updated_at=datetime.datetime.now(datetime.UTC),
        )
