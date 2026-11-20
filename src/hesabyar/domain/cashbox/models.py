"""Cash-box domain models (US-516, Phase 5 wave 2, stream K2).

Phased design §6 (هـ.۲) + §1.3 (5چ): the Iranian three-step cash-box
method — (1) supervised count, (2) signed minutes as the supporting
document (اسناد مثبته), (3) shortage/surplus journal. Tryton has no
counting session, so the session is a HesabYar-side entity; the cash
daybook itself is Tryton ``account_statement`` (one Statement Journal
per cash box, each with its own cash account).

Money is whole Iranian rials (``int`` — ``MoneyIRR`` digits=0) unless
a field name says otherwise. Persian labels live in the frontend
(``features/cash-box``) — this layer stays English.

Extension points (links that activate as their source modules land —
phased design §1.3 K2): cheques (5ث), bank deposit (5ت B1/B2), COD
(5ب S1/P1), cash installment (5ج). A session carries optional link
references (``cheque_refs`` / ``deposit_ref`` / ``order_refs`` /
``installment_refs``) that stay empty until those modules exist; the
counting core never depends on them. The proposed canonical events
(``cashbox.count.completed``, ``cashbox.shortage.recorded``,
``cashbox.overage.recorded``, ``cashbox.receipt``, ``cashbox.payment``)
are NOT emitted yet — new event names beyond US-501's nine need ADR
approval first (phased design §8.4); the session completion files its
review item directly instead.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum, unique

from hesabyar.domain.accounting.commands import JournalDraftLine
from hesabyar.domain.accounting.refs import AccountRef
from hesabyar.domain.common.money import MoneyIRR

__all__ = [
    "CASH_SHORTAGE_OVERAGE_ACCOUNT_CODE",
    "COUNT_SCHEDULES",
    "DEFAULT_COUNT_REMINDER_DAYS",
    "DEFAULT_SHORTAGE_SETTLEMENT_HOURS",
    "MISC_CREDITORS_OVERAGE_ACCOUNT_CODE",
    "PETTY_CASH_ACCOUNT_CODE",
    "STORE_CASH_ACCOUNT_CODE",
    "TEMPORARY_DEBTORS_SHORTAGE_ACCOUNT_CODE",
    "CashBox",
    "CashBoxKind",
    "CashBoxReport",
    "CashBoxReportLine",
    "CashBoxSettings",
    "CountSession",
    "CountSessionStatus",
    "DenominationCount",
    "VariancePolicy",
    "variance_account_code",
    "variance_journal_lines",
]

#: HesabYar chart-extension account codes (``hesabyar_setup``
#: ``account_chart_en.xml``). Policy (a) — the default — books both
#: shortage and overage against one temporary current-asset account;
#: policy (b) — the banking policy — books a shortage as a temporary
#: receivable from the cashier and an overage as a miscellaneous
#: creditor. All three are ``party_required=False``: the cashier is
#: named in the counting session and the signed minutes, not as a
#: Tryton party.
CASH_SHORTAGE_OVERAGE_ACCOUNT_CODE = "1.2.6"
TEMPORARY_DEBTORS_SHORTAGE_ACCOUNT_CODE = "1.2.7"
MISC_CREDITORS_OVERAGE_ACCOUNT_CODE = "2.1.6"

#: Default cash accounts for the two built-in box kinds. A box may use
#: any postable, non-party-required cash account — these are only the
#: provisioning defaults (the registry stores the account the box's
#: Statement Journal actually uses).
STORE_CASH_ACCOUNT_CODE = "1.1.1"
PETTY_CASH_ACCOUNT_CODE = "1.1.5"

#: Count schedules (phased design هـ.۲.۳ ``CASHBOX_COUNT_SCHEDULE``):
#: daily / weekly / period-end. The user has not answered how often
#: counting happens yet, so the schedule is configuration, not a rule.
COUNT_SCHEDULES: tuple[str, ...] = ("daily", "weekly", "period_end")

#: Default "cash box uncounted for N days" alert threshold (phased
#: design هـ.۲.۳ ``CASHBOX_COUNT_REMINDER_DAYS``). A starting value,
#: not a verified business rule — the settings endpoint exists so an
#: admin can set the real one per installation.
DEFAULT_COUNT_REMINDER_DAYS = 7

#: Default shortage-settlement deadline under policy (b) (phased
#: design هـ.۲.۳ ``CASHBOX_SHORTAGE_SETTLEMENT_HOURS``): the banking
#: policy settles a shortage within 24 hours.
DEFAULT_SHORTAGE_SETTLEMENT_HOURS = 24


@unique
class CashBoxKind(StrEnum):
    """Kind of cash box (phased design هـ.۲.۱).

    ``store`` is the shop's cash box; ``petty_cash`` is the
    تنخواه‌گردان (a separate box with its own ceiling, modelled with
    the same Statement Journal — no separate Tryton 8.2 petty-cash
    module was found); ``other`` covers any further box.
    """

    STORE = "store"
    PETTY_CASH = "petty_cash"
    OTHER = "other"


@unique
class VariancePolicy(StrEnum):
    """Shortage/surplus journal policy, selectable per installation
    (phased design هـ.۲.۲ / هـ.۲.۳ ``CASHBOX_VARIANCE_POLICY``).

    ``expense`` is policy (a) — the HesabYar default; ``receivable``
    is policy (b) — the banking policy, where the cashier owes the
    shortage.
    """

    EXPENSE = "expense"
    RECEIVABLE_FROM_CASHIER = "receivable_from_cashier"


@unique
class CountSessionStatus(StrEnum):
    """Lifecycle of a counting session (phased design هـ.۲.۲).

    ``in_progress`` → ``awaiting_supervisor`` → ``awaiting_manager``
    → ``archived``. A recount reopens an ``awaiting_manager`` session
    back to ``in_progress`` (the manager asked for it from the review
    queue); an archived session is terminal.
    """

    IN_PROGRESS = "in_progress"
    AWAITING_SUPERVISOR = "awaiting_supervisor"
    AWAITING_MANAGER = "awaiting_manager"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class DenominationCount:
    """One denomination line of a counted amount (optional breakdown).

    ``denomination_irr`` is the banknote/coin value in rials,
    ``count`` how many of them were counted. When a breakdown is
    given, its total must equal the counted amount.
    """

    denomination_irr: int
    count: int

    @property
    def total_irr(self) -> int:
        """Value of this denomination line in rials."""
        return self.denomination_irr * self.count


@dataclass(frozen=True)
class CashBox:
    """One cash box in an installation's registry.

    A box is the HesabYar-side record for one Tryton Statement
    Journal: ``statement_journal_id`` is the engine id of the
    ``account.statement.journal`` (the daybook), ``cash_account_id``
    / ``cash_account_code`` the journal's cash account. ``company_id``
    is the HesabYar ``engine_companies`` row id (canonical within the
    tenant — the router resolves the API company id to the row).
    ``engine_company_id`` is the Tryton company id, denormalized at
    registration (same as on the counting session) so the live
    balance lookup needs no second row resolution — the get-by-id
    route takes no ``company`` parameter. ``ceiling_irr`` is the
    petty-cash ceiling (required for ``petty_cash`` boxes, ignored
    otherwise).
    """

    id: str
    tenant_id: str
    company_id: str
    name_fa: str
    kind: CashBoxKind
    statement_journal_id: int
    cash_account_id: int
    cash_account_code: str
    engine_company_id: int = 0
    ceiling_irr: int | None = None
    active: bool = True
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class CashBoxSettings:
    """One installation's cash-box policy settings (phased design هـ.۲.۳).

    One row per tenant; the defaults apply until an admin configures
    them. ``material_variance_threshold_irr`` is the "material
    variance" threshold (above it: a critical review item + senior
    management scrutiny); None means every variance is treated the
    same (warning severity). ``supervisor_required`` is the internal
    control that the supervisor differs from the cashier — default
    True, and disabling it is a deliberate installation choice.
    """

    tenant_id: str
    variance_policy: VariancePolicy = VariancePolicy.EXPENSE
    material_variance_threshold_irr: int | None = None
    count_reminder_days: int = DEFAULT_COUNT_REMINDER_DAYS
    count_schedule: str = "daily"
    supervisor_required: bool = True
    shortage_settlement_hours: int = DEFAULT_SHORTAGE_SETTLEMENT_HOURS
    updated_at: datetime.datetime | None = None


@dataclass(frozen=True)
class CountSession:
    """One supervised cash-counting session (HesabYar-side entity).

    ``book_balance_irr`` is the Tryton book balance of the box's cash
    account, snapshotted read-only when the session starts — it is
    never entered by hand. ``counted_amount_irr`` is the physical
    count (None until entered); ``variance_irr`` is computed
    (counted − book): negative is a shortage, positive an overage.

    Signatures: the supervisor's attendance (``witnessed_by``) is
    required in EVERY session; the signed minutes (cashier +
    supervisor + manager) are required ONLY when the variance is
    non-zero (phased design هـ.۲.۲ / part-2-5 §7.4). The minutes are
    the supporting document (اسناد مثبته) for the variance journal
    and are prepared BEFORE any accounting entry is issued.

    ``review_item_id`` links the ``cashbox_variance`` review item
    (only when the variance is non-zero); ``draft_journal_id`` the
    engine id of the DRAFT variance journal (posted only after the
    manager approves the review item — a cash shortage is NEVER
    auto-posted); ``posted_journal_id`` the same move once posted.
    """

    id: str
    tenant_id: str
    company_id: str
    cash_box_id: str
    cashier: str
    supervisor: str
    status: CountSessionStatus
    book_balance_irr: int
    created_at: datetime.datetime
    # The engine company id, denormalized from ``company_id`` (the
    # HesabYar ``engine_companies`` row id) at session start: the
    # variance journal is created in the engine when the minutes
    # complete, and the service needs the engine id without a second
    # row lookup. Engine company ids are stable, so the copy cannot
    # go stale.
    engine_company_id: int = 0
    counted_amount_irr: int | None = None
    denominations: tuple[DenominationCount, ...] = ()
    minutes_text: str | None = None
    witnessed_by: str | None = None
    witnessed_at: datetime.datetime | None = None
    minutes_cashier_signed_by: str | None = None
    minutes_cashier_signed_at: datetime.datetime | None = None
    minutes_supervisor_signed_by: str | None = None
    minutes_supervisor_signed_at: datetime.datetime | None = None
    minutes_manager_signed_by: str | None = None
    minutes_manager_signed_at: datetime.datetime | None = None
    review_item_id: str | None = None
    draft_journal_id: int | None = None
    posted_journal_id: int | None = None
    notes: str | None = None
    updated_at: datetime.datetime | None = None
    # -- link extension points (empty until the source modules land) --
    cheque_refs: tuple[str, ...] = ()
    deposit_ref: str | None = None
    order_refs: tuple[str, ...] = ()
    installment_refs: tuple[str, ...] = ()

    @property
    def variance_irr(self) -> int | None:
        """Counted minus book (None until the count is entered)."""
        if self.counted_amount_irr is None:
            return None
        return self.counted_amount_irr - self.book_balance_irr

    @property
    def is_shortage(self) -> bool:
        """True when the count found less cash than the books."""
        variance = self.variance_irr
        return variance is not None and variance < 0

    @property
    def is_overage(self) -> bool:
        """True when the count found more cash than the books."""
        variance = self.variance_irr
        return variance is not None and variance > 0

    @property
    def is_balanced(self) -> bool:
        """True when the count matches the books exactly."""
        return self.variance_irr == 0

    @property
    def minutes_required(self) -> bool:
        """Signed minutes are required exactly when the variance is non-zero."""
        variance = self.variance_irr
        return variance is not None and variance != 0

    @property
    def minutes_signed(self) -> bool:
        """True when cashier + supervisor + manager have all signed the minutes."""
        return (
            self.minutes_cashier_signed_at is not None
            and self.minutes_supervisor_signed_at is not None
            and self.minutes_manager_signed_at is not None
        )

    @property
    def supervisor_attended(self) -> bool:
        """True when the supervisor's attendance is recorded."""
        return self.witnessed_by is not None

    @property
    def denominations_total_irr(self) -> int:
        """Total of the denomination breakdown (0 when none was given)."""
        return sum(line.total_irr for line in self.denominations)

    @property
    def is_terminal(self) -> bool:
        """True once archived — no further action may be performed on it."""
        return self.status is CountSessionStatus.ARCHIVED


def variance_account_code(policy: VariancePolicy, variance_irr: int) -> str:
    """Chart code for a variance under a policy (phased design هـ.۲.۲).

    Policy (a) uses the single shortage/overage account for both
    directions; policy (b) uses the temporary-debtors account for a
    shortage and the miscellaneous-creditors account for an overage.
    A zero variance has no account — callers never journal it.
    """
    if variance_irr == 0:
        raise ValueError("A zero variance has no variance account.")
    if policy is VariancePolicy.EXPENSE:
        return CASH_SHORTAGE_OVERAGE_ACCOUNT_CODE
    if variance_irr < 0:
        return TEMPORARY_DEBTORS_SHORTAGE_ACCOUNT_CODE
    return MISC_CREDITORS_OVERAGE_ACCOUNT_CODE


def variance_journal_lines(
    *,
    variance_irr: int,
    cash_account: AccountRef,
    variance_account: AccountRef,
) -> tuple[JournalDraftLine, ...]:
    """The two balanced lines of a shortage/overage journal.

    Shortage (variance < 0): debit the variance account, credit the
    cash box. Overage (variance > 0): debit the cash box, credit the
    variance account. The journal is ONLY for the variance amount
    (phased design هـ.۲.۲: "سند کسری/اضافی فقط به میزان تفاوت صادر
    می‌شود"). A zero variance has no journal — callers never call
    this with one.
    """
    if variance_irr == 0:
        raise ValueError("A zero variance has no variance journal.")
    amount = MoneyIRR(abs(variance_irr))
    zero = MoneyIRR.zero()
    if variance_irr < 0:
        return (
            JournalDraftLine(account=variance_account, debit=amount, credit=zero),
            JournalDraftLine(account=cash_account, debit=zero, credit=amount),
        )
    return (
        JournalDraftLine(account=cash_account, debit=amount, credit=zero),
        JournalDraftLine(account=variance_account, debit=zero, credit=amount),
    )


@dataclass(frozen=True)
class CashBoxReportLine:
    """One cash box's figures in a cash-box variance report.

    ``opening_book_balance_irr`` / ``closing_book_balance_irr`` are
    the Tryton book balances of the box's cash account at the period
    edges (posted moves only); ``receipts_irr`` / ``payments_irr``
    are the posted debits/credits on that account inside the period
    (from the trial balance — never from a partial read).
    ``shortage_irr`` / ``overage_irr`` are the session variances in
    the period (absolute values). ``recorded_sales_irr`` is the total
    of posted customer invoices in the period — the sales side of the
    comparison; the store cash-sales link (5ب S1) is a documented
    extension point, so until it lands the figure is Tryton invoices
    only and is labelled as such. ``possible_unrecorded_sales_irr``
    is the net overage (surplus cash suggests unrecorded sales —
    an indicator, not proof).
    """

    cash_box_id: str
    cash_box_name_fa: str
    cash_account_code: str
    opening_book_balance_irr: int
    closing_book_balance_irr: int
    receipts_irr: int
    payments_irr: int
    session_count: int
    shortage_irr: int
    overage_irr: int
    recorded_sales_irr: int

    @property
    def net_variance_irr(self) -> int:
        """Overage minus shortage (signed): positive means surplus cash."""
        return self.overage_irr - self.shortage_irr

    @property
    def possible_unrecorded_sales_irr(self) -> int:
        """Net overage, floored at zero — the unrecorded-sales indicator."""
        return max(0, self.net_variance_irr)


@dataclass(frozen=True)
class CashBoxReport:
    """A cash-box vs sales variance report (spec §11.1 provenance).

    ``source`` names the engine reads the figures came from;
    ``generated_at`` is the generation timestamp; ``company_id`` /
    ``date_from`` / ``date_to`` fix the company and period;
    ``report_version`` / ``calculation_version`` are the report and
    calculation versions, so a regenerated report is reproducible.
    ``sales_source`` states honestly what the sales side is
    (``tryton_invoices`` until the 5ب store link lands).
    """

    company_id: str
    date_from: datetime.date
    date_to: datetime.date
    lines: tuple[CashBoxReportLine, ...]
    generated_at: datetime.datetime
    source: str = "account.move.line + account.invoice (Tryton REST)"
    sales_source: str = "tryton_invoices"
    report_version: str = "1.0"
    calculation_version: str = "1.0"

    @property
    def total_shortage_irr(self) -> int:
        """Total shortage across the report's boxes."""
        return sum(line.shortage_irr for line in self.lines)

    @property
    def total_overage_irr(self) -> int:
        """Total overage across the report's boxes."""
        return sum(line.overage_irr for line in self.lines)

    @property
    def total_possible_unrecorded_sales_irr(self) -> int:
        """Total unrecorded-sales indicator across the report's boxes."""
        return sum(line.possible_unrecorded_sales_irr for line in self.lines)
