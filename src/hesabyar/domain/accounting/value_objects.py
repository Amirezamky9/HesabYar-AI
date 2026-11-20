"""Engine-neutral accounting views and validation results.

Views are read-only snapshots of engine data, expressed purely in domain
types (MoneyIRR, FiscalDate, refs). They never carry raw engine records.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from hesabyar.domain.accounting.refs import (
    AccountRef,
    AnalyticAccountRef,
    BankStatementRef,
    CheckbookEngineRef,
    ChequeEngineRef,
    CompanyRef,
    FiscalPeriodRef,
    FiscalYearRef,
    InventoryLineRef,
    InventoryRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    MoveLineRef,
    OrderPointRef,
    OutShipmentRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PostedJournalRef,
    ProductRef,
    PurchaseRequestRef,
    ReconciliationRef,
    SaleRef,
    StatementJournalRef,
    StockLocationRef,
    StockMoveRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR


@dataclass(frozen=True, slots=True)
class CompanyView:
    """A company known to the accounting engine.

    ``national_id`` is the company's national id (10 digits for a
    natural person, 11 for a legal entity) — read from the company
    party's ``code``, the same convention the cheque intake uses
    for issuer parties (``party.code`` = national id). None when
    the installation has not recorded it; the cheque payee check
    (US-533) fails closed to the review queue in that case rather
    than guessing.
    """

    ref: CompanyRef
    name: str
    currency_code: str
    national_id: str | None = None


@dataclass(frozen=True, slots=True)
class AccountView:
    """A chart-of-accounts entry (group / kol / moein)."""

    ref: AccountRef
    company: CompanyRef
    code: str
    name: str
    type_name: str | None
    parent: AccountRef | None
    closed: bool
    party_required: bool
    active: bool

    @property
    def is_postable(self) -> bool:
        """True when the account may receive journal lines (a leaf, open, active account)."""
        return self.active and not self.closed and self.type_name is not None


@dataclass(frozen=True, slots=True)
class FiscalYearView:
    """A fiscal year of a company."""

    ref: FiscalYearRef
    company: CompanyRef
    name: str
    start_date: FiscalDate
    end_date: FiscalDate
    state: str  # 'open' | 'closed' | 'locked'

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True, slots=True)
class FiscalPeriodView:
    """An accounting period of a fiscal year.

    ``ref`` / ``fiscal_year`` / ``period_type`` are None / "standard" when
    the view was built from a partial record (e.g. the period embedded in a
    journal read); ``list_periods`` always fills them in.
    """

    name: str
    start_date: FiscalDate
    end_date: FiscalDate
    state: str  # 'open' | 'closed' | 'locked'
    ref: FiscalPeriodRef | None = None
    fiscal_year: FiscalYearRef | None = None
    period_type: str = "standard"  # 'standard' | 'adjustment'

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def contains(self, day: FiscalDate) -> bool:
        return self.start_date <= day <= self.end_date


@dataclass(frozen=True, slots=True)
class CompanySetupStatus:
    """Read-only setup state of a company, for the dashboard wizard.

    Derived purely from port reads (chart, fiscal years, periods) — nothing
    here is invented. ``chart_template`` is None because the port does not
    expose account templates yet — not because the engine lacks the data:
    every wizard-created account records its template
    (``account.account.template``; the chart root's template is the
    installed chart template, e.g. "Universal Chart of Accounts"), and a
    future adapter story can derive the template name from it. See
    docs/setup-wizard.md §5.
    """

    company: CompanyRef
    chart_installed: bool
    account_count: int
    chart_template: str | None
    fiscal_year_present: bool
    fiscal_year_count: int
    open_fiscal_year: FiscalYearView | None
    open_period_present: bool
    open_period_count: int

    @property
    def ready(self) -> bool:
        """True when the company can take journals: chart + fiscal year + open period."""
        return self.chart_installed and self.fiscal_year_present and self.open_period_present


@dataclass(frozen=True, slots=True)
class JournalLineView:
    """One line of a journal draft, as read back from the engine."""

    account: AccountRef
    debit: MoneyIRR
    credit: MoneyIRR
    state: str  # engine line state: 'draft' (unbalanced move) | 'valid' (balanced move)
    description: str = ""


@dataclass(frozen=True, slots=True)
class JournalDraftView:
    """A journal draft (unposted move) as read back from the engine."""

    ref: JournalDraftRef
    company: CompanyRef
    journal: JournalRef
    period: FiscalPeriodView
    date: FiscalDate
    state: str  # 'draft' | 'posted'
    description: str
    lines: tuple[JournalLineView, ...] = ()

    @property
    def total_debit(self) -> MoneyIRR:
        total = MoneyIRR.zero()
        for line in self.lines:
            total = total.add(line.debit)
        return total

    @property
    def total_credit(self) -> MoneyIRR:
        total = MoneyIRR.zero()
        for line in self.lines:
            total = total.add(line.credit)
        return total

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit


@dataclass(frozen=True, slots=True)
class PostedJournalView:
    """A posted journal (a posted move) as read back from the engine.

    ``number`` is the engine-assigned reference (Tryton move number) and
    ``post_date`` the date the engine posted it. ``origin`` is the journal
    this one reverses, when this journal is itself a reversal.
    """

    ref: PostedJournalRef
    company: CompanyRef
    journal: JournalRef
    period: FiscalPeriodView
    date: FiscalDate
    post_date: FiscalDate | None
    number: str
    description: str
    origin: PostedJournalRef | None = None
    lines: tuple[JournalLineView, ...] = ()

    @property
    def total_debit(self) -> MoneyIRR:
        total = MoneyIRR.zero()
        for line in self.lines:
            total = total.add(line.debit)
        return total

    @property
    def total_credit(self) -> MoneyIRR:
        total = MoneyIRR.zero()
        for line in self.lines:
            total = total.add(line.credit)
        return total

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit

    @property
    def is_reversal(self) -> bool:
        """True when this journal reverses another posted journal."""
        return self.origin is not None


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One problem found while validating a journal draft."""

    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceLineView:
    """One line of an invoice, as read back from the engine.

    ``quantity`` is a Decimal (quantities multiply into money, and spec
    §8.3 forbids float arithmetic in financial paths). ``line_type`` is
    the engine's line kind: ``'line'`` (priced), ``'subtotal'``,
    ``'title'`` or ``'comment'`` (informational, no amount).
    """

    account: AccountRef
    quantity: Decimal
    unit_price: MoneyIRR
    amount: MoneyIRR
    line_type: str = "line"
    description: str = ""


@dataclass(frozen=True, slots=True)
class InvoiceView:
    """An invoice as read back from the engine (any state).

    ``number`` is None until the engine assigns it (on validation for
    supplier invoices, on posting for customer invoices). ``invoice_date``
    is None when the draft has no date yet (the engine sets it on posting
    for customer invoices). ``party_name`` is resolved by the adapter so
    callers need no second call per invoice. Amounts are the engine's own
    computed totals (untaxed + tax = total); ``validate_invoice_draft``
    cross-checks them against the lines.
    """

    ref: InvoiceRef
    company: CompanyRef
    invoice_type: str  # 'out' (customer) | 'in' (supplier)
    party: PartyRef
    party_name: str
    number: str | None
    invoice_date: FiscalDate | None
    accounting_date: FiscalDate | None
    state: str  # 'draft' | 'validated' | 'posted' | 'paid' | 'cancelled'
    description: str
    reference: str
    journal: JournalRef | None
    account: AccountRef
    currency_code: str
    untaxed_amount: MoneyIRR
    tax_amount: MoneyIRR
    total_amount: MoneyIRR
    lines: tuple[InvoiceLineView, ...] = ()
    # Still unpaid on the invoice (engine Function field). Reduced by
    # every non-failed payment linked to the invoice's lines-to-pay --
    # including Draft payments (docs/tryton-spikes-502.md spike 3).
    # None when the read did not request it.
    amount_to_pay: MoneyIRR | None = None
    # The posted accounting move of this invoice (None until posted).
    # The settlement engine reads the invoice's receivable lines
    # (lines_to_pay) through it.
    move: PostedJournalRef | None = None

    @property
    def is_customer_invoice(self) -> bool:
        return self.invoice_type == "out"

    @property
    def is_draft(self) -> bool:
        return self.state == "draft"


@dataclass(frozen=True, slots=True)
class MoveLineView:
    """One move line as read back from the engine (US-522).

    The settlement engine reads receivable lines by account + party +
    maturity date to gather a tranche's reconcilable lines, and reads
    a move's lines to find the clearing leg a payment created.
    ``reconciliation`` is None while the line is unreconciled.
    """

    ref: MoveLineRef
    move: JournalDraftRef
    account: AccountRef
    debit: MoneyIRR
    credit: MoneyIRR
    state: str  # engine line state: 'draft' (unbalanced move) | 'valid' (balanced move)
    party: PartyRef | None = None
    maturity_date: FiscalDate | None = None
    reconciliation: ReconciliationRef | None = None
    description: str = ""

    @property
    def balance(self) -> MoneyIRR:
        """Signed line amount (debit − credit)."""
        return self.debit.sub(self.credit)


@dataclass(frozen=True, slots=True)
class PaymentJournalView:
    """A payment journal as read back from the engine (US-522).

    ``clearing_account`` / ``clearing_journal`` are the clearing
    module's configuration: when both are set, ``succeed`` creates
    the clearing move (Dr clearing / Cr receivable). When either is
    missing, ``succeed`` silently creates NO move — the settlement
    engine refuses to process on such a journal (fail-closed,
    docs/tryton-spikes-520.md §1).
    """

    ref: PaymentJournalRef
    company: CompanyRef
    name: str
    process_method: str
    clearing_account: AccountRef | None = None
    clearing_journal: JournalRef | None = None

    @property
    def is_clearing_configured(self) -> bool:
        """True when succeed on this journal creates a clearing move."""
        return self.clearing_account is not None and self.clearing_journal is not None


@dataclass(frozen=True, slots=True)
class ReconciliationView:
    """A move-line reconciliation as read back from the engine (US-522)."""

    ref: ReconciliationRef
    number: str
    lines: tuple[MoveLineRef, ...] = ()


@dataclass(frozen=True)
class InvoiceTotalView:
    """One invoice's date and total, nothing else (US-516).

    The cash-box report sums recorded sales from posted customer
    invoices; it needs no lines, party names, or currency codes, so
    it reads totals through ``list_invoice_totals`` instead of the
    full ``list_invoices`` (which fetches all three). ``invoice_date``
    is None for an undated invoice — the report skips those.
    """

    ref: InvoiceRef
    invoice_date: FiscalDate | None
    total_amount: MoneyIRR


@dataclass(frozen=True, slots=True)
class PaymentView:
    """A payment as read back from the engine (any state).

    ``number`` is assigned by the engine from the company payment sequence
    at creation. ``journal_name`` is the payment journal's name (the
    engine's ``account.payment.journal``, e.g. a manual cash/bank
    journal). The payment's linked move line / origin invoice is not
    exposed yet: that mapping lands when invoice-payment linking is
    contract-verified in a later story.
    """

    ref: PaymentRef
    company: CompanyRef
    number: str
    kind: str  # 'receivable' | 'payable'
    party: PartyRef
    party_name: str
    date: FiscalDate
    amount: MoneyIRR
    state: str  # 'draft' | 'submitted' | 'approved' | 'processing' | 'succeeded' | 'failed'
    reference: str
    journal_name: str
    currency_code: str
    # The receivable/payable move line this payment settles, when linked
    # (an invoice's line-to-pay). None for unlinked payments.
    move_line: MoveLineRef | None = None
    # The clearing move the clearing module created when this payment
    # succeeded (Dr clearing / Cr receivable, dated to the clearing
    # date). None until the payment succeeds on a clearing-configured
    # journal. The move is created DRAFT — the caller posts it.
    clearing_move: JournalDraftRef | None = None

    @property
    def is_draft(self) -> bool:
        return self.state == "draft"


@dataclass(frozen=True, slots=True)
class SaleLineView:
    """One line of a sale order, as read back from the engine."""

    product: ProductRef
    product_name: str
    quantity: Decimal
    unit_price: MoneyIRR
    amount: MoneyIRR
    description: str = ""


@dataclass(frozen=True, slots=True)
class SaleView:
    """A sale order as read back from the engine (any state).

    ``number`` is None until the engine assigns it (on quotation).
    ``sale_date`` is None until confirmation. ``invoice_state`` /
    ``shipment_state`` are the engine's progress states (``'none'`` /
    ``'waiting'`` / ``'exception'`` / ``'paid'`` / ``'done'`` ...).
    ``invoices`` lists the invoices the sale produced (empty until the
    sale is processed with an invoice method that creates one).
    """

    ref: SaleRef
    company: CompanyRef
    party: PartyRef
    party_name: str
    number: str | None
    sale_date: FiscalDate | None
    state: str  # 'draft' | 'quotation' | 'confirmed' | 'processing' | 'done' | 'cancelled'
    invoice_method: str  # 'manual' | 'order' | 'shipment'
    shipment_method: str  # 'manual' | 'order' | 'invoice'
    invoice_state: str
    shipment_state: str
    currency_code: str
    untaxed_amount: MoneyIRR
    total_amount: MoneyIRR
    description: str
    reference: str
    lines: tuple[SaleLineView, ...] = ()
    invoices: tuple[InvoiceRef, ...] = ()

    @property
    def is_draft(self) -> bool:
        return self.state == "draft"


@dataclass(frozen=True, slots=True)
class BankStatementLineView:
    """One line of a bank/cash statement, as read back from the engine.

    ``amount`` is signed: positive for money in, negative for money out.
    ``related_invoice`` is the invoice the line is matched to, when the
    engine links one (``account.invoice`` via the line's ``related_to``).
    ``id`` is the engine line id — the reconcile service needs it to
    write match results back onto the line.
    """

    id: int
    date: FiscalDate
    amount: MoneyIRR
    account: AccountRef
    description: str = ""
    number: str | None = None
    party: PartyRef | None = None
    party_name: str | None = None
    related_invoice: InvoiceRef | None = None


@dataclass(frozen=True, slots=True)
class BankStatementView:
    """A bank/cash statement as read back from the engine (any state).

    Which balance fields are present depends on the statement journal's
    validation type: ``'balance'`` journals carry start/end balances,
    ``'amount'`` journals carry a total amount, ``'number_of_lines'``
    journals carry a line count; the others are None. ``balance`` is the
    engine-computed end-minus-start balance (None when the journal does
    not validate by balance). ``currency_code`` is the statement
    journal's currency.
    """

    ref: BankStatementRef
    company: CompanyRef
    name: str
    journal_name: str
    journal_id: int
    date: FiscalDate
    state: str  # 'draft' | 'validated' | 'posted' | 'cancelled'
    validation: str  # 'balance' | 'amount' | 'number_of_lines'
    currency_code: str
    start_balance: MoneyIRR | None
    end_balance: MoneyIRR | None
    balance: MoneyIRR | None
    total_amount: MoneyIRR | None
    number_of_lines: int | None
    lines: tuple[BankStatementLineView, ...] = ()

    @property
    def is_draft(self) -> bool:
        return self.state == "draft"


@dataclass(frozen=True, slots=True)
class AccountBalanceView:
    """One account's balance over posted moves up to a date (US-516).

    The balance is the cumulative debit minus credit of every posted
    move line on the account dated on or before ``as_of`` — draft
    moves never contribute. ``balance`` is signed: a debit balance is
    positive, a credit balance negative. A cash account's balance is
    the book balance a counting session snapshots (read-only).
    """

    account: AccountRef
    code: str
    name: str
    as_of: FiscalDate
    total_debit: MoneyIRR
    total_credit: MoneyIRR
    line_count: int = 0
    source_endpoint: str = "account.move.line (Tryton REST)"

    @property
    def balance(self) -> MoneyIRR:
        return MoneyIRR(self.total_debit.amount - self.total_credit.amount)


@dataclass(frozen=True, slots=True)
class StatementJournalView:
    """A statement journal (``account.statement.journal``) as read back.

    ``account`` is the bank/cash account the journal posts to — the
    destination account of every line on the journal's statements.
    ``bank_account_number`` is the Shaba (IBAN) when the journal is
    linked to a bank account, else None (US-514 import form).
    ``account_code``/``account_name`` are the account's code/name
    (US-516 cash-box registration); ``journal`` is the posting
    journal (``account.journal``) the statement journal books to;
    ``active`` gates cash-box registration. US-514 and US-516 each
    defined this view with a different shape; the merge unifies them
    (``id`` stays available as a property for the bank call sites).
    """

    ref: StatementJournalRef
    company: CompanyRef
    name: str
    account: AccountRef
    currency_code: str
    validation: str  # 'balance' | 'amount' | 'number_of_lines'
    journal: JournalRef | None = None
    account_code: str = ""
    account_name: str = ""
    bank_account_number: str | None = None
    active: bool = True

    @property
    def id(self) -> int:
        """Engine id of the statement journal (``ref.engine_id``)."""
        return self.ref.engine_id


@dataclass(frozen=True, slots=True)
class ReportFreshness:
    """§11.1 freshness metadata attached to every financial report (US-550).

    Every report response carries this block, so a reader can tell
    where every figure came from and reproduce the read:

    - ``source`` names the engine read (e.g.
      ``"tryton:account.move.line (Tryton REST)"``);
    - ``source_timestamp`` is when the source data was read — for
      these live reports the read happens at generation time, so it
      equals ``generated_at`` (there is no older snapshot: the engine
      is read live on every request, never from a HesabYar cache);
    - ``generated_at`` is when the response was generated (UTC);
    - ``company_id`` / ``period_start`` / ``period_end`` scope the
      figures (a point-in-time report sets both period bounds to its
      as-of date);
    - ``report_version`` versions the report definition,
      ``calculation_version`` the calculation rule that produced the
      figures (see ``domain/reports/statements.py``);
    - ``freshness_indicator`` is always ``"live"`` for engine reads;
    - ``reproducibility`` carries the exact read parameters (engine,
      engine version, source endpoint, company, period, filters) so
      the same read can be repeated.
    """

    source: str
    source_timestamp: datetime.datetime
    generated_at: datetime.datetime
    company_id: str
    period_start: FiscalDate | None
    period_end: FiscalDate | None
    report_version: str
    calculation_version: str
    freshness_indicator: str = "live"
    reproducibility: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TrialBalanceLineView:
    """One account's totals over posted moves in a trial-balance period.

    ``balance`` is debit minus credit (signed): a debit balance is
    positive, a credit balance negative. Zero-balance accounts are
    omitted by the adapter — a trial balance lists only accounts with
    posted activity in the period.
    """

    account: AccountRef
    code: str
    name: str
    total_debit: MoneyIRR
    total_credit: MoneyIRR

    @property
    def balance(self) -> MoneyIRR:
        return MoneyIRR(self.total_debit.amount - self.total_credit.amount)


@dataclass(frozen=True, slots=True)
class TrialBalanceView:
    """A trial balance read from POSTED moves only, for one company and date range.

    The figures come from the engine's posted moves — never from drafts,
    never from a paginated partial read (the adapter pages through the
    full line set). ``source_endpoint`` names the engine read the figures
    came from, so every figure carries its provenance; ``as_of_date`` is
    the period end. Posted moves are balanced by the engine, so
    ``total_debit == total_credit``; ``is_balanced`` is asserted by the
    report service before any validation run is built on the figures.
    """

    company: CompanyRef
    date_from: FiscalDate
    date_to: FiscalDate
    lines: tuple[TrialBalanceLineView, ...] = ()
    total_debit: MoneyIRR = MoneyIRR(0)
    total_credit: MoneyIRR = MoneyIRR(0)
    move_count: int = 0
    line_count: int = 0
    source_endpoint: str = "account.move.line (Tryton REST)"
    freshness: ReportFreshness | None = None

    @property
    def as_of_date(self) -> FiscalDate:
        return self.date_to

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """The outcome of validating a journal draft against the §8.3 invariants."""

    valid: bool
    issues: tuple[ValidationIssue, ...] = field(default_factory=tuple)

    @classmethod
    def ok(cls) -> ValidationSummary:
        return cls(valid=True, issues=())

    @classmethod
    def failed(cls, issues: list[ValidationIssue]) -> ValidationSummary:
        return cls(valid=False, issues=tuple(issues))

    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)


# -- packaging & shipping (Phase 5 wave 2, US-515) ---------------------------
#
# Engine-neutral views of stock, supply and analytic records. Quantities
# are Decimal (the adapter converts Tryton floats at the boundary);
# unit costs are Decimal (an average cost price can be fractional per
# unit — only totals are whole IRR).


@dataclass(frozen=True, slots=True)
class StockLocationView:
    """A stock location as read back from the engine."""

    ref: StockLocationRef
    name: str
    location_type: str  # 'warehouse' | 'storage' | 'view' | 'customer' |
    # 'supplier' | 'lost_found' | 'production' | 'transit' | 'drop'
    code: str | None = None
    parent: StockLocationRef | None = None


@dataclass(frozen=True, slots=True)
class PackagingProductView:
    """A packaging material's product as read back from the engine.

    ``quantity`` / ``cost_price`` are context-dependent (location /
    company) and only populated when the read asked for them.
    """

    ref: ProductRef
    template_id: int
    name: str
    code: str | None
    product_type: str  # 'goods' (materials are always goods)
    consumable: bool  # always False for materials (see docs/packaging.md)
    purchasable: bool
    cost_method: str  # 'average' | 'fifo' | 'fixed'
    default_uom_name: str | None = None
    quantity: Decimal | None = None
    cost_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderPointView:
    """A replenishment order point (``stock.order_point``) as read back."""

    ref: OrderPointRef
    product: ProductRef
    warehouse: StockLocationRef
    point_type: str  # 'purchase' (materials) | 'internal'
    min_quantity: Decimal | None
    target_quantity: Decimal
    max_quantity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StockMoveView:
    """A stock move as read back from the engine (any state)."""

    ref: StockMoveRef
    product: ProductRef
    quantity: Decimal
    from_location: StockLocationRef
    to_location: StockLocationRef
    state: str  # 'staging' | 'draft' | 'assigned' | 'done' | 'cancelled'
    company: CompanyRef
    planned_date: FiscalDate | None = None
    effective_date: FiscalDate | None = None
    cost_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PurchaseRequestView:
    """A purchase request (``purchase.request``) as read back."""

    ref: PurchaseRequestRef
    product: ProductRef
    quantity: Decimal
    state: str  # 'draft' | 'quotation' | 'done' | 'cancelled' | 'rejected'
    company: CompanyRef
    warehouse: StockLocationRef | None = None
    supply_date: FiscalDate | None = None
    purchase_date: FiscalDate | None = None
    created_at: datetime.datetime | None = None


@dataclass(frozen=True, slots=True)
class AnalyticAccountView:
    """An analytic account as read back from the engine."""

    ref: AnalyticAccountRef
    name: str
    code: str | None
    account_type: str  # 'root' | 'view' | 'normal' | 'distribution'
    company: CompanyRef
    balance: MoneyIRR | None = None
    debit: MoneyIRR | None = None
    credit: MoneyIRR | None = None


@dataclass(frozen=True, slots=True)
class AnalyticLineView:
    """One analytic line (``analytic_account.line``) as read back."""

    account: AnalyticAccountRef
    date: FiscalDate
    debit: MoneyIRR
    credit: MoneyIRR
    move_line: MoveLineRef | None = None


@dataclass(frozen=True, slots=True)
class InventoryLineView:
    """One line of a stock inventory count as read back."""

    ref: InventoryLineRef
    product: ProductRef
    product_name: str
    expected_quantity: Decimal
    quantity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class InventoryView:
    """A stock inventory count as read back from the engine."""

    ref: InventoryRef
    number: str | None
    location: StockLocationRef
    location_name: str
    date: FiscalDate
    state: str  # 'draft' | 'done' | 'cancelled'
    company: CompanyRef
    lines: tuple[InventoryLineView, ...] = ()


@dataclass(frozen=True, slots=True)
class SaleShipmentCostView:
    """One invoiced shipment-cost line for a sale (``stock.shipment.cost_sale``)."""

    sale: SaleRef
    shipment: OutShipmentRef
    amount: MoneyIRR
    currency_code: str


@dataclass(frozen=True, slots=True)
class OutShipmentCostView:
    """Actual shipment cost on an outgoing shipment (``stock.shipment.out``).

    ``cost`` is the amount paid (or to be paid) to the carrier, from
    ``stock_shipment_cost``; ``cost_sale`` is the amount charged to
    the customer, from ``sale_shipment_cost``. Either may be None
    (not yet entered / not charged).
    """

    ref: OutShipmentRef
    state: str
    carrier_name: str | None = None
    cost: MoneyIRR | None = None
    cost_currency_code: str | None = None
    cost_sale: MoneyIRR | None = None
    cost_sale_currency_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChequeEngineView:
    """An ``account.check`` record as read back from the engine (US-531).

    ``state`` is the engine's accounting state — the single source of
    truth after a transition; the HesabYar registry mirrors it.
    ``move_ids`` maps transition name -> engine move id for every
    journal the cheque's lifecycle has produced (the audit trail,
    report 17). ``sayad_status`` is the engine's Sayyad axis, kept
    separate from ``state``.
    """

    ref: ChequeEngineRef
    company: CompanyRef
    kind: str  # 'received' | 'issued'
    purpose: str  # 'payment' | 'collateral'
    sayad_id: str
    state: str
    sayad_status: str
    amount: MoneyIRR
    issue_date: FiscalDate
    due_date: FiscalDate
    storage_location: str | None = None
    leaf_number: str | None = None
    checkbook_engine_id: int | None = None
    secured_obligation: str | None = None
    move_ids: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckbookEngineView:
    """An ``account.checkbook`` record as read back from the engine (US-532).

    The leaf counts are computed by the engine from the issued
    cheques linked to the checkbook: a leaf is used once an issued
    cheque exists for it in a non-voided state (``issued`` /
    ``cleared`` / ``bounced`` / ``called_in``), voided when its
    cheque is ``voided``. ``bank_account_engine_id`` is the
    checkbook's ``bank.account`` record (its ``account`` is the
    accounting account سند ۱۱ credits when no account was recorded
    at issue).
    """

    ref: CheckbookEngineRef
    company: CompanyRef
    name: str
    bank_engine_id: int | None = None
    bank_account_engine_id: int | None = None
    first_number: int | None = None
    last_number: int | None = None
    total_leaves: int = 0
    used_leaves: int = 0
    voided_leaves: int = 0
    remaining_leaves: int = 0


# -- financial reports (Phase 5-6, US-550) -------------------------------------
#
# The five §11.2 coming-soon reports plus cheque report 7, all read live
# from the engine through the port — never recomputed from a HesabYar
# cache (§11.1). The figures come from the same engine reads Tryton's own
# reference reports render: posted ``account.move.line`` rows (general
# ledger, trial balance, cash flow, notes aging) and the
# ``account.account`` balance Function field in the report context
# (balance sheet, income statement — the figures the
# ``account.account.type.statement`` reference report aggregates; see
# docs/financial-reports.md for why the adapter reads account balances
# instead of the type ``amount`` Function field).
#
# Every view carries its ``source_endpoint`` and a ``freshness`` block
# (stamped by the report query service, None on views built by tests
# that do not go through the service).


@dataclass(frozen=True, slots=True)
class GeneralLedgerLineView:
    """One posted move line in a general-ledger account section.

    ``balance`` is the account's running balance (debit minus credit)
    AFTER this line, starting from the account's opening balance.
    ``move_number`` is the engine move number (``""`` when the move
    has none); ``description`` is the line's own description, falling
    back to the move's description.
    """

    move_id: int
    move_number: str
    date: FiscalDate
    description: str
    debit: MoneyIRR
    credit: MoneyIRR
    balance: MoneyIRR


@dataclass(frozen=True, slots=True)
class GeneralLedgerAccountView:
    """One account's general-ledger section: opening balance + lines.

    ``lines`` is empty in summary mode (no single account requested):
    the summary lists per-account totals only, and the lines load when
    one account is selected (drill-down from the trial balance).
    """

    account: AccountRef
    code: str
    name: str
    opening_balance: MoneyIRR
    lines: tuple[GeneralLedgerLineView, ...] = ()
    total_debit: MoneyIRR = MoneyIRR(0)
    total_credit: MoneyIRR = MoneyIRR(0)

    @property
    def closing_balance(self) -> MoneyIRR:
        """Opening balance plus the period's net activity (debit minus credit)."""
        return MoneyIRR(
            self.opening_balance.amount
            + self.total_debit.amount
            - self.total_credit.amount
        )


@dataclass(frozen=True, slots=True)
class GeneralLedgerView:
    """A general ledger over POSTED moves only, for one company and date range."""

    company: CompanyRef
    date_from: FiscalDate
    date_to: FiscalDate
    accounts: tuple[GeneralLedgerAccountView, ...] = ()
    total_debit: MoneyIRR = MoneyIRR(0)
    total_credit: MoneyIRR = MoneyIRR(0)
    move_count: int = 0
    line_count: int = 0
    source_endpoint: str = "account.move.line (Tryton REST)"
    freshness: ReportFreshness | None = None


@dataclass(frozen=True, slots=True)
class StatementLineView:
    """One row of a financial statement section.

    ``kind`` is ``"type"`` (a subtotal row for an account type) or
    ``"account"`` (one account's figure); ``depth`` is the tree depth
    for indentation (0 = the section's top-level type rows).
    ``amount`` is the statement-presentation amount: assets,
    liabilities, equity, revenue and expenses are POSITIVE magnitudes
    — the sign convention is the section's, not the engine's raw
    credit-minus-debit (see ``domain/reports/statements.py``). The
    one exception is the current-period-result line inside equity,
    which is negative for a net loss. ``amount_cmp`` is the same
    figure for the comparison period (None when no comparison was
    requested).
    """

    kind: str  # 'type' | 'account'
    ref_id: int
    label: str
    code: str | None
    depth: int
    amount: MoneyIRR
    amount_cmp: MoneyIRR | None = None


@dataclass(frozen=True, slots=True)
class StatementSectionView:
    """One section of a financial statement (assets, revenue, ...).

    ``key`` is the stable section key (``"assets"``,
    ``"liabilities"``, ``"equity"``, ``"revenue"``, ``"expenses"``);
    ``title`` is the English type name the section is rooted at (the
    UI maps the key to its Persian label). ``total`` is the section
    total (a positive magnitude); ``lines`` are the section's rows in
    tree order.
    """

    key: str
    title: str
    lines: tuple[StatementLineView, ...] = ()
    total: MoneyIRR = MoneyIRR(0)
    total_cmp: MoneyIRR | None = None


@dataclass(frozen=True, slots=True)
class BalanceSheetView:
    """A balance sheet at a point in time, with an optional comparison date.

    Totals are positive magnitudes; ``balanced`` is the accounting
    identity total assets == total liabilities + total equity
    (current date), ``balanced_cmp`` the same for the comparison
    date. The current-period result is inside equity (the
    profit/loss type sits under equity in the universal chart), so a
    balanced ledger always balances here.
    """

    company: CompanyRef
    as_of: FiscalDate
    comparison_as_of: FiscalDate | None
    sections: tuple[StatementSectionView, ...] = ()
    total_assets: MoneyIRR = MoneyIRR(0)
    total_liabilities: MoneyIRR = MoneyIRR(0)
    total_equity: MoneyIRR = MoneyIRR(0)
    total_assets_cmp: MoneyIRR | None = None
    total_liabilities_cmp: MoneyIRR | None = None
    total_equity_cmp: MoneyIRR | None = None
    balanced: bool = True
    balanced_cmp: bool | None = None
    source_endpoint: str = "account.account.balance (Tryton REST)"
    freshness: ReportFreshness | None = None


@dataclass(frozen=True, slots=True)
class MonthlyRevenueExpenseView:
    """One Jalali month's net revenue and expense (P&L chart data).

    ``revenue`` is the net credit on revenue-classified accounts in
    the month, ``expense`` the net debit on expense-classified
    accounts — usually positive, negative when the month's reversals
    exceed its activity. The classification is fixed by each account
    type's sign over the WHOLE period, so the monthly figures always
    sum to the statement totals (see
    ``domain/reports/statements.py``).
    """

    jalali_month: str  # 'YYYY-MM' in the Jalali calendar, e.g. '1405-08'
    month_start: FiscalDate
    month_end: FiscalDate
    revenue: MoneyIRR
    expense: MoneyIRR


@dataclass(frozen=True, slots=True)
class IncomeStatementView:
    """An income statement (P&L) over a period, with prior-period comparison.

    ``total_revenue`` / ``total_expense`` are positive magnitudes;
    ``net_income`` is revenue minus expense (negative for a net
    loss). ``monthly`` drives the monthly revenue/expense bar chart;
    ``monthly_ties_out`` asserts the monthly figures sum to the
    statement totals.
    """

    company: CompanyRef
    date_from: FiscalDate
    date_to: FiscalDate
    comparison_from: FiscalDate | None
    comparison_to: FiscalDate | None
    sections: tuple[StatementSectionView, ...] = ()
    total_revenue: MoneyIRR = MoneyIRR(0)
    total_expense: MoneyIRR = MoneyIRR(0)
    net_income: MoneyIRR = MoneyIRR(0)
    total_revenue_cmp: MoneyIRR | None = None
    total_expense_cmp: MoneyIRR | None = None
    net_income_cmp: MoneyIRR | None = None
    monthly: tuple[MonthlyRevenueExpenseView, ...] = ()
    monthly_ties_out: bool = True
    source_endpoint: str = "account.account.balance (Tryton REST)"
    freshness: ReportFreshness | None = None


@dataclass(frozen=True, slots=True)
class CashFlowSectionView:
    """One cash-flow section (operating / investing / financing).

    ``inflow`` / ``outflow`` are positive magnitudes; ``net`` is
    inflow minus outflow (negative when the section consumed cash).
    ``line_count`` is the number of cash move lines classified into
    the section.
    """

    key: str  # 'operating' | 'investing' | 'financing'
    title: str
    inflow: MoneyIRR
    outflow: MoneyIRR
    net: MoneyIRR
    line_count: int = 0


@dataclass(frozen=True, slots=True)
class CashFlowView:
    """A direct-method cash-flow statement over a period.

    Tryton has NO cash-flow reference report: this statement is a
    documented HesabYar derivation from the engine's posted cash
    moves (calculation version ``direct-method/1.0`` — see
    ``domain/reports/statements.py`` for the classification rule).
    ``ties_out`` asserts opening + operating + investing + financing
    == closing; ``unclassified_*`` counts cash lines whose
    counterpart could not be classified (they break the tie-out and
    are reported, never silently dropped); ``transfer_count`` counts
    cash-to-cash transfers, excluded from every section (they move
    no cash in or out).
    """

    company: CompanyRef
    date_from: FiscalDate
    date_to: FiscalDate
    opening_cash: MoneyIRR
    closing_cash: MoneyIRR
    sections: tuple[CashFlowSectionView, ...] = ()
    net_change: MoneyIRR = MoneyIRR(0)
    ties_out: bool = True
    cash_accounts: tuple[str, ...] = ()
    transfer_count: int = 0
    unclassified_count: int = 0
    unclassified_total: MoneyIRR = MoneyIRR(0)
    source_endpoint: str = "account.move.line (Tryton REST)"
    freshness: ReportFreshness | None = None


@dataclass(frozen=True, slots=True)
class NotesBalanceRowView:
    """One party's notes balance on one notes account (cheque report 7).

    Amounts are signed nets: for a receivable account the
    debit-minus-credit balance (what the party owes us), for a
    payable account the credit-minus-debit balance (what we owe the
    party) — positive for normal balances, negative when the party
    overpaid. Buckets follow the Tryton aged-balance rule
    (``account.aged_balance``): a line ages from its maturity date
    (falling back to the move date); ``bucket_not_yet_due`` holds
    lines maturing AFTER the as-of date, which Tryton's model puts
    in the balance but in no term — here they get their own bucket so
    the buckets always sum to the row total.
    """

    party_id: int | None
    party_name: str
    instrument: str  # 'receivable' | 'payable'
    account_code: str
    account_name: str
    bucket_not_yet_due: MoneyIRR = MoneyIRR(0)
    bucket_0_30: MoneyIRR = MoneyIRR(0)
    bucket_30_60: MoneyIRR = MoneyIRR(0)
    bucket_60_90: MoneyIRR = MoneyIRR(0)
    bucket_90_120: MoneyIRR = MoneyIRR(0)
    bucket_120_plus: MoneyIRR = MoneyIRR(0)
    total: MoneyIRR = MoneyIRR(0)
    line_count: int = 0


@dataclass(frozen=True, slots=True)
class NotesBalanceView:
    """Notes receivable/payable balances with aging (cheque report 7).

    ``instrument`` is the filter applied (``"both"``,
    ``"receivable"`` or ``"payable"``); ``accounts_read`` lists the
    notes accounts that exist on the company (a company whose chart
    lacks the HesabYar extension has none of 1.2.4/1.2.5/2.1.5 and
    reads an honest empty report). ``totals`` is the bucket totals
    over all rows (party fields empty on the totals row).
    """

    company: CompanyRef
    as_of: FiscalDate
    instrument: str
    rows: tuple[NotesBalanceRowView, ...] = ()
    totals: NotesBalanceRowView | None = None
    accounts_read: tuple[str, ...] = ()
    source_endpoint: str = "account.move.line (Tryton REST)"
    freshness: ReportFreshness | None = None
