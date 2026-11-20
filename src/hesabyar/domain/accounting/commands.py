"""Accounting commands: engine-neutral requests to change engine state.

Commands validate the HesabYar-side journal invariants (ARCHITECTURE.md §8.3
/ design-eng spec §8.2) at construction, before anything reaches the engine:

- a journal has at least two lines;
- every line is one-sided: debit XOR credit, both non-negative;
- total debit equals total credit and the total is greater than zero.

Invoice drafts (Phase 2, US-204) follow the same fail-fast pattern:

- an invoice has at least one priced line;
- every line has a positive quantity and a non-negative unit price;
- every line amount (quantity x unit price) is an integral number of Rials
  -- fractional Rial amounts are rejected, never rounded (spec §8.3);
- the invoice total is greater than zero (credit notes -- negative totals
  -- are a later story).

The engine remains the final transactional enforcer; these checks fail fast
with stable HesabYar error codes instead of leaking engine errors.

Updating a draft (``UpdateJournalDraft``) reuses the same line invariants
when lines are replaced, and requires at least one changed field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from hesabyar.domain.accounting.refs import (
    AccountRef,
    AnalyticAccountRef,
    BankStatementLineRef,
    BankStatementRef,
    ChequeEngineRef,
    CompanyRef,
    InventoryLineRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    MoveLineRef,
    PartyAddressRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PaymentTermRef,
    PostedJournalRef,
    ProductRef,
    StatementJournalRef,
    StockLocationRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    InvalidAnalyticAccountError,
    InvalidBankStatementError,
    InvalidInventoryError,
    InvalidInvoiceError,
    InvalidJournalError,
    InvalidOrderPointError,
    InvalidPaymentError,
    InvalidProductError,
    InvalidSaleError,
    InvalidStockMoveError,
    UnbalancedJournalError,
)
from hesabyar.domain.common.money import MoneyIRR


@dataclass(frozen=True, slots=True)
class JournalDraftLine:
    """One line of a journal draft: exactly one side carries an amount.

    ``party`` names the line's third party (tafsili) — required on
    party-required accounts, forbidden on other accounts (the engine
    enforces ``bool(party) == account.party_required``; the adapter
    pre-checks it). ``maturity_date`` is the line's payable/
    receivable date (a free Date on any line; the settlement engine
    uses it to carry the tranche due date on receivable legs).
    """

    account: AccountRef
    debit: MoneyIRR = field(default_factory=MoneyIRR.zero)
    credit: MoneyIRR = field(default_factory=MoneyIRR.zero)
    description: str = ""
    party: PartyRef | None = None
    maturity_date: FiscalDate | None = None

    def __post_init__(self) -> None:
        if self.debit.is_negative or self.credit.is_negative:
            raise InvalidJournalError(
                "Journal line amounts must be non-negative.",
                details={"account": str(self.account)},
            )
        if self.debit.is_positive and self.credit.is_positive:
            raise InvalidJournalError(
                "Journal line must be one-sided: debit XOR credit.",
                details={"account": str(self.account)},
            )
        if self.debit.is_zero and self.credit.is_zero:
            raise InvalidJournalError(
                "Journal line must carry a non-zero amount on exactly one side.",
                details={"account": str(self.account)},
            )


def _check_journal_lines(lines: tuple[JournalDraftLine, ...]) -> None:
    """Enforce the §8.2 line-set invariants shared by create and update.

    At least two lines, a total greater than zero, and a balanced total
    (debit equals credit). Each line's own one-sidedness is already
    enforced by ``JournalDraftLine`` construction.
    """
    if len(lines) < 2:
        raise InvalidJournalError(
            "A journal draft needs at least two lines.",
            details={"line_count": len(lines)},
        )
    total_debit = MoneyIRR.zero()
    total_credit = MoneyIRR.zero()
    for line in lines:
        total_debit = total_debit.add(line.debit)
        total_credit = total_credit.add(line.credit)
    if total_debit.is_zero and total_credit.is_zero:
        raise InvalidJournalError(
            "A journal draft total must be greater than zero.",
        )
    if total_debit != total_credit:
        raise UnbalancedJournalError(
            f"Journal draft is unbalanced: debit {total_debit.amount} IRR "
            f"!= credit {total_credit.amount} IRR.",
            details={
                "total_debit_irr": total_debit.amount,
                "total_credit_irr": total_credit.amount,
            },
        )


@dataclass(frozen=True, slots=True)
class CreateJournalDraft:
    """Command to create a draft journal (an unposted move) in the engine.

    ``journal`` may be None, in which case the adapter uses the company's
    single general journal when exactly one exists, and fails otherwise.
    """

    company: CompanyRef
    date: FiscalDate
    lines: tuple[JournalDraftLine, ...]
    description: str = ""
    journal: JournalRef | None = None

    def __post_init__(self) -> None:
        _check_journal_lines(self.lines)

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


@dataclass(frozen=True, slots=True)
class UpdateJournalDraft:
    """Command to update a draft journal (an unposted move) in the engine.

    Only present fields change: ``date`` and ``description`` replace the
    draft's values, ``lines`` replaces the draft's lines wholesale. At
    least one field must be present — an empty update is rejected at
    construction instead of silently reading the draft back. When
    ``lines`` is present it must satisfy the §8.2 invariants (>= 2 lines,
    balanced, total > 0), checked here like ``CreateJournalDraft``.

    The draft itself must still be a draft: the adapter rejects updates
    to a posted journal with ``LEDGER_POSTED_IMMUTABLE``.
    """

    draft: JournalDraftRef
    date: FiscalDate | None = None
    description: str | None = None
    lines: tuple[JournalDraftLine, ...] | None = None

    def __post_init__(self) -> None:
        if self.date is None and self.description is None and self.lines is None:
            raise InvalidJournalError(
                "An update must change at least one of date, description or lines.",
                details={"draft_id": self.draft.engine_id},
            )
        if self.lines is not None:
            _check_journal_lines(self.lines)


@dataclass(frozen=True, slots=True)
class PostJournal:
    """Command to post a draft journal (design-eng spec §2.2).

    Posting is atomic on the engine and irreversible through normal paths:
    a posted journal can only be corrected by a reversal. Posting an
    already-posted journal is an error, not a no-op -- the engine refuses
    it and the adapter surfaces that as LEDGER_POSTED_IMMUTABLE.
    """

    draft: JournalDraftRef


@dataclass(frozen=True, slots=True)
class ReverseJournal:
    """Command to reverse a posted journal (design-eng spec §2.2).

    The reversal is a new posted journal whose lines cancel the original's
    (debit/credit swapped) and whose origin links back to the original.
    ``description`` overrides the reversal's description; when empty, the
    reversal keeps the original journal's description (the Tryton
    ``account.move.cancel`` wizard's behaviour).
    """

    posted: PostedJournalRef
    description: str = ""


@dataclass(frozen=True, slots=True)
class InvoiceDraftLine:
    """One priced line of an invoice draft.

    ``quantity`` is a Decimal (never a float: quantities multiply into
    money, and spec §8.3 forbids float arithmetic in financial paths).
    The line amount (quantity x unit price) must be an integral number of
    Rials; a fractional amount is rejected at construction, never rounded.
    A zero unit price is allowed (a free line); the invoice total must
    still be greater than zero (see ``CreateInvoiceDraft``).
    """

    account: AccountRef
    unit_price: MoneyIRR
    quantity: Decimal = Decimal(1)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.quantity.is_finite():
            raise InvalidInvoiceError(
                "Invoice line quantity must be finite.",
                details={"account": str(self.account)},
            )
        if self.quantity <= 0:
            raise InvalidInvoiceError(
                "Invoice line quantity must be greater than zero.",
                details={"account": str(self.account)},
            )
        if self.unit_price.is_negative:
            raise InvalidInvoiceError(
                "Invoice line unit price must be non-negative.",
                details={"account": str(self.account)},
            )
        amount = self.quantity * self.unit_price.amount
        if amount != amount.to_integral_value():
            raise InvalidInvoiceError(
                "Invoice line amount must be an integral number of Rials "
                f"(quantity {self.quantity} x unit price "
                f"{self.unit_price.amount} IRR = {amount} IRR); "
                "fractional Rial amounts are never rounded.",
                details={"account": str(self.account)},
            )

    @property
    def amount(self) -> MoneyIRR:
        """The line amount in integer IRR (quantity x unit price)."""
        return MoneyIRR(int(self.quantity * self.unit_price.amount))


@dataclass(frozen=True, slots=True)
class CreateInvoiceDraft:
    """Command to create a draft invoice in the engine.

    ``invoice_type`` is ``'out'`` (customer invoice, receivable account,
    revenue journal) or ``'in'`` (supplier invoice, payable account,
    expense journal). ``date`` is the invoice date.

    ``journal``, ``account`` and ``invoice_address`` may be None, in which
    case the adapter resolves them: the engine's single journal of the
    matching type, the invoice account (explicit → the party's default
    receivable/payable account → the company's configured default →
    the company's single receivable/payable account), and the party's
    first address. When the adapter cannot resolve one unambiguously
    it fails with a stable error instead of guessing.

    No taxes are carried in this story: lines are created without taxes,
    so the engine computes a zero tax amount. Tax / VAT handling is a
    later story (Iranian tax policy, Phase 4).

    ``payment_term`` splits the posted invoice into ``lines_to_pay``
    (one receivable/payable line per leg, each with its own maturity
    date). ``alternative_payees`` (at most one before posting — the
    engine enforces this) moves the posted receivable lines' party
    from the customer to the payee: the settlement engine passes the
    gateway party here so the receivable is from the gateway, not the
    customer (docs/tryton-spikes-520.md §4).
    """

    company: CompanyRef
    invoice_type: str
    party: PartyRef
    date: FiscalDate
    lines: tuple[InvoiceDraftLine, ...]
    description: str = ""
    reference: str = ""
    journal: JournalRef | None = None
    account: AccountRef | None = None
    invoice_address: PartyAddressRef | None = None
    payment_term: PaymentTermRef | None = None
    alternative_payees: tuple[PartyRef, ...] = ()

    def __post_init__(self) -> None:
        if self.invoice_type not in ("out", "in"):
            raise InvalidInvoiceError(
                f"Invoice type must be 'out' (customer) or 'in' (supplier), "
                f"got {self.invoice_type!r}.",
            )
        if len(self.alternative_payees) > 1:
            raise InvalidInvoiceError(
                "An invoice may carry at most one alternative payee before "
                f"posting (got {len(self.alternative_payees)}).",
            )
        if not self.lines:
            raise InvalidInvoiceError(
                "An invoice draft needs at least one line.",
                details={"line_count": 0},
            )
        if self.total_amount.is_zero:
            raise InvalidInvoiceError(
                "An invoice draft total must be greater than zero.",
            )

    @property
    def total_amount(self) -> MoneyIRR:
        total = MoneyIRR.zero()
        for line in self.lines:
            total = total.add(line.amount)
        return total


# -- sales (Phase 5, US-502) ------------------------------------------------
#
# A store order becomes a Tryton ``sale.sale`` (draft -> quotation ->
# confirmed -> processing -> done). The sale's lines reference engine
# products (created by the store sync / manually -- product management is
# not part of this port); the adapter resolves each product's sale unit
# of measure. ``invoice_method`` / ``shipment_method`` are the engine's
# own selections: 'order' invoices on confirmation, 'manual' leaves the
# invoice/shipment to an explicit step (the store connector's default:
# invoice on order, shipment manual until the fulfilment phase lands).

SALE_INVOICE_METHODS = ("manual", "order", "shipment")
SALE_SHIPMENT_METHODS = ("manual", "order", "invoice")


@dataclass(frozen=True, slots=True)
class SaleLine:
    """One priced line of a sale order.

    Same integral-Rial rule as ``InvoiceDraftLine``: quantity x unit
    price must be an integral number of Rials, never rounded.
    """

    product: ProductRef
    unit_price: MoneyIRR
    quantity: Decimal = Decimal(1)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.quantity.is_finite():
            raise InvalidSaleError(
                "Sale line quantity must be finite.",
                details={"product": str(self.product)},
            )
        if self.quantity <= 0:
            raise InvalidSaleError(
                "Sale line quantity must be greater than zero.",
                details={"product": str(self.product)},
            )
        if self.unit_price.is_negative:
            raise InvalidSaleError(
                "Sale line unit price must be non-negative.",
                details={"product": str(self.product)},
            )
        amount = self.quantity * self.unit_price.amount
        if amount != amount.to_integral_value():
            raise InvalidSaleError(
                "Sale line amount must be an integral number of Rials "
                f"(quantity {self.quantity} x unit price "
                f"{self.unit_price.amount} IRR = {amount} IRR); "
                "fractional Rial amounts are never rounded.",
                details={"product": str(self.product)},
            )

    @property
    def amount(self) -> MoneyIRR:
        """The line amount in integer IRR (quantity x unit price)."""
        return MoneyIRR(int(self.quantity * self.unit_price.amount))


@dataclass(frozen=True, slots=True)
class CreateSale:
    """Command to create a draft sale order in the engine.

    ``invoice_address`` / ``shipment_address`` / ``warehouse`` may be
    None, in which case the adapter resolves them: the party's first
    address for both addresses, and the company's single warehouse when
    exactly one exists (a warehouse is only required when the shipment
    method needs one -- 'manual' shipments do not).
    """

    company: CompanyRef
    party: PartyRef
    date: FiscalDate
    lines: tuple[SaleLine, ...]
    invoice_method: str = "order"
    shipment_method: str = "manual"
    description: str = ""
    reference: str = ""
    invoice_address: PartyAddressRef | None = None
    shipment_address: PartyAddressRef | None = None

    def __post_init__(self) -> None:
        if self.invoice_method not in SALE_INVOICE_METHODS:
            raise InvalidSaleError(
                f"Invoice method must be one of {SALE_INVOICE_METHODS}, "
                f"got {self.invoice_method!r}.",
            )
        if self.shipment_method not in SALE_SHIPMENT_METHODS:
            raise InvalidSaleError(
                f"Shipment method must be one of {SALE_SHIPMENT_METHODS}, "
                f"got {self.shipment_method!r}.",
            )
        if not self.lines:
            raise InvalidSaleError(
                "A sale order needs at least one line.",
                details={"line_count": 0},
            )
        if self.total_amount.is_zero:
            raise InvalidSaleError(
                "A sale order total must be greater than zero.",
            )

    @property
    def total_amount(self) -> MoneyIRR:
        total = MoneyIRR.zero()
        for line in self.lines:
            total = total.add(line.amount)
        return total


# -- payments (Phase 5, US-502) ---------------------------------------------
#
# A payment is a Tryton ``account.payment`` (draft -> submitted ->
# [approved] -> processing -> succeeded/failed). The adapter drives the
# workflow through the model's button transitions over REST -- the
# Process/Succeed *wizards* are not REST-reachable (docs/tryton-spikes-502.md
# spike 1), but every transition the settlement flow needs is a button
# and therefore an RPC action: ``submit`` / ``approve`` / ``proceed``
# (replaces the Process wizard for manual processing -- its
# ``process_manual`` is a no-op) / ``succeed`` (creates the clearing
# move when the clearing module is installed; the clearing date travels
# in the REST context) / ``fail``.
#
# Spike 3 warning: a Draft payment already reduces the linked invoice
# line's ``amount_to_pay`` (any non-failed payment counts), so callers
# must create payments only when the tranche is actually being
# processed -- never speculatively.


@dataclass(frozen=True, slots=True)
class CreatePayment:
    """Command to create a draft payment in the engine.

    ``journal`` may be None, in which case the adapter uses the
    company's single payment journal when exactly one exists, and fails
    otherwise. ``line`` links the payment to the receivable/payable
    move line it settles (an invoice's line-to-pay); when no line is
    given, ``account`` carries the receivable/payable account instead
    (required by the clearing module for the clearing move).
    """

    company: CompanyRef
    party: PartyRef
    kind: str
    amount: MoneyIRR
    date: FiscalDate
    journal: PaymentJournalRef | None = None
    line: MoveLineRef | None = None
    account: AccountRef | None = None
    reference: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("receivable", "payable"):
            raise InvalidPaymentError(
                f"Payment kind must be 'receivable' or 'payable', got {self.kind!r}.",
            )
        if not self.amount.is_positive:
            raise InvalidPaymentError(
                "A payment amount must be greater than zero.",
                details={"amount_irr": self.amount.amount},
            )


@dataclass(frozen=True, slots=True)
class SucceedPayment:
    """Command to mark a processing payment succeeded.

    ``clearing_date`` is the date the gateway's money actually cleared
    (the settlement deposit date): the clearing move is dated with it.
    When omitted, the engine dates the clearing move today.
    """

    payment: PaymentRef
    clearing_date: FiscalDate | None = None


# -- reconciliation (Phase 5-3, US-522) -------------------------------------
#
# The settlement engine reconciles the tranche's receivable lines
# explicitly (docs/tryton-spikes-520.md §5, option (a)): the clearing
# ``succeed`` reconciles only when the payment's line and the clearing
# leg sum to zero, which never holds for a NET tranche payment against
# a GROSS invoice line. The three lines that DO sum to zero — invoice
# line (gross debit) + fee line (fee+VAT credit) + clearing leg (net
# credit), all on the gateway receivable account with party=gateway —
# are reconciled through the hesabyar_setup ``reconcile_rest`` wrapper.


@dataclass(frozen=True, slots=True)
class ReconcileMoveLines:
    """Command to reconcile a set of move lines in the engine.

    The engine requires at least two lines, all on the same account,
    all with the same party, debits == credits, and every line in
    'valid' state (a balanced draft move's lines are valid). The
    adapter pre-validates these conditions by reading the lines and
    raises a stable error before calling the engine; the engine's own
    ``ReconciliationError`` is the backstop.
    """

    company: CompanyRef
    lines: tuple[MoveLineRef, ...]

    def __post_init__(self) -> None:
        if len(self.lines) < 2:
            raise InvalidJournalError(
                "Reconciliation needs at least two move lines.",
                details={"line_count": len(self.lines)},
            )
        if len({line.engine_id for line in self.lines}) != len(self.lines):
            raise InvalidJournalError(
                "Reconciliation line ids must be distinct.",
                details={"line_ids": [line.engine_id for line in self.lines]},
            )


# -- credit notes (Phase 5, US-502) -----------------------------------------
#
# Tryton 8.2 has no separate credit-note invoice type: a credit note is
# an invoice of the same type ('out' / 'in') whose lines carry NEGATIVE
# quantities (``InvoiceLine._credit``). ``Invoice.credit`` itself is not
# RPC-exposed (it is driven by the Credit wizard), so the adapter
# mirrors it over REST: read the source invoice, create a draft with
# negated line quantities and the same party/account/journal/currency,
# each credit line's origin pointing at its source line.


@dataclass(frozen=True, slots=True)
class CreateCreditNote:
    """Command to create a credit-note draft from a posted invoice.

    The source invoice must be posted (a draft has nothing to credit);
    the adapter verifies that before creating anything. By default the
    credit note is a FULL credit of the source invoice (every line,
    full quantity). ``partial_amount`` requests an amount-based
    PARTIAL credit instead (US-511, phased design §2 §1.4: a partial
    refund is corrected with a credit note, never by rewriting the
    invoice): a single line for the amount against the source's
    revenue account, which requires all of the source's priced lines
    to share one account. The partial credit is an approximation —
    the VAT split follows the tax-line work — and the draft is
    human-reviewed before posting, so the approximation is visible.
    """

    invoice: InvoiceRef
    date: FiscalDate
    description: str = ""
    partial_amount: MoneyIRR | None = None

    def __post_init__(self) -> None:
        if self.partial_amount is not None and not self.partial_amount.is_positive:
            raise InvalidInvoiceError(
                "A partial credit amount must be greater than zero.",
                details={"amount_irr": self.partial_amount.amount},
            )


# -- bank statements (Phase 5 wave 2, US-514) --------------------------------
#
# Importing a statement writes the raw file lines into Tryton
# ``account.statement`` (draft): the engine owns the statement; the
# HesabYar import batch owns the file provenance. Tryton requires an
# account on every statement line (``Line.__setup__`` sets
# ``account.required = True``), so uncoded lines carry the import's
# default (suspense) account — the preview shows the coding before
# anything is written. ``related_to`` accepts invoices only
# (``Line._get_relations``); a payment match sets party + account.
#
# Reconcile writes line fields (related_to / account / party) through
# ``UpdateBankStatementLine``; it never validates or posts the
# statement — the accountant validates in the engine, where Tryton
# natively reconciles lines whose ``related_to`` is set
# (``Line.reconcile`` on validate).


@dataclass(frozen=True, slots=True)
class CreateBankStatementLine:
    """One line of a statement to create (signed amount, Rial)."""

    date: FiscalDate
    amount: MoneyIRR
    account: AccountRef
    description: str = ""
    number: str | None = None
    party: PartyRef | None = None
    related_invoice: InvoiceRef | None = None


@dataclass(frozen=True, slots=True)
class CreateBankStatement:
    """Command to create a draft bank/cash statement in the engine.

    ``journal`` is the engine ``account.statement.journal`` id. The
    statement is always created in state ``draft``. Start/end
    balances come from the file's balance column when present;
    balance-validated journals need them at validation time, not at
    creation. At least one line is required (an empty statement is a
    data error, not a statement).
    """

    company: CompanyRef
    journal: StatementJournalRef
    name: str
    date: FiscalDate
    lines: tuple[CreateBankStatementLine, ...]
    start_balance: MoneyIRR | None = None
    end_balance: MoneyIRR | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidBankStatementError("A bank statement needs a name.")
        if not self.lines:
            raise InvalidBankStatementError(
                "A bank statement needs at least one line.",
                details={"statement_name": self.name},
            )


# -- packaging & shipping (Phase 5 wave 2, US-515) ---------------------------
#
# Commands for the packaging module. Materials are Tryton products of
# type 'goods' with consumable=False and purchasable=True (see
# docs/packaging.md for why the Tryton 'consumable' flag must stay
# off); consumption moves run from the packaging storage location to
# a production location so anglo-saxon accounting expenses them at
# use; inventories adjust through the lost-and-found location.


@dataclass(frozen=True, slots=True)
class CreatePackagingProduct:
    """Command to create a packaging material's product in the engine.

    Creates a ``product.template`` (type ``goods``, ``consumable``
    False, ``purchasable`` True, ``salable`` False, cost method
    ``average`` by default) under the installation's packaging
    category; Tryton creates the single product variant with it.
    ``cost_method`` ``fifo`` needs the ``product_cost_fifo`` module
    activated — the adapter raises a stable error otherwise.
    """

    company: CompanyRef
    name: str
    category_engine_id: int
    cost_method: str = "average"
    code: str | None = None
    uom_engine_id: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidProductError("A packaging product needs a name.")
        if self.cost_method not in ("average", "fifo"):
            raise InvalidProductError(
                f"Cost method must be 'average' or 'fifo', got {self.cost_method!r}."
            )
        if self.category_engine_id <= 0:
            raise InvalidProductError(
                "A packaging product needs a product category.",
                details={"category_id": self.category_engine_id},
            )


@dataclass(frozen=True, slots=True)
class CancelInvoice:
    """Command to cancel a draft or validated invoice in the engine.

    Cancelling is the engine's own transition (draft/validated →
    cancelled): a cancelled invoice keeps its history and can never
    be posted. A posted invoice cannot be cancelled this way — it is
    corrected with a credit note instead (US-511: post-invoice
    cancellation is a corrective document, never a rewrite). The
    adapter verifies the state before cancelling.
    """

    invoice: InvoiceRef
    description: str = ""


@dataclass(frozen=True, slots=True)
class UpdateBankStatementLine:
    """Command to write match results onto one draft statement line.

    Only the given fields change; ``related_invoice`` None leaves the
    link untouched, ``clear_related_invoice`` removes it. The adapter
    verifies the statement is a draft (Tryton locks line fields once
    validated) and that an invoice link's account equals the invoice's
    account (Tryton asserts this at validate time).
    """

    statement: BankStatementRef
    line: BankStatementLineRef
    account: AccountRef | None = None
    party: PartyRef | None = None
    related_invoice: InvoiceRef | None = None
    clear_related_invoice: bool = False

    def __post_init__(self) -> None:
        if (
            self.account is None
            and self.party is None
            and self.related_invoice is None
            and not self.clear_related_invoice
        ):
            raise InvalidBankStatementError(
                "UpdateBankStatementLine needs at least one changed field.",
                details={"statement_id": self.statement.engine_id, "line_id": self.line.engine_id},
            )


@dataclass(frozen=True, slots=True)
class UpsertOrderPoint:
    """Command to create or update a purchase order point for a material.

    ``min_quantity <= target_quantity`` is enforced here (the engine
    enforces it again). ``max_quantity`` is only meaningful for
    internal points and is ignored for purchase points.
    """

    company: CompanyRef
    product: ProductRef
    warehouse: StockLocationRef
    min_quantity: Decimal
    target_quantity: Decimal

    def __post_init__(self) -> None:
        if self.min_quantity < 0 or self.target_quantity <= 0:
            raise InvalidOrderPointError(
                "min_quantity must be >= 0 and target_quantity > 0."
            )
        if self.min_quantity > self.target_quantity:
            raise InvalidOrderPointError(
                f"min_quantity ({self.min_quantity}) must not exceed "
                f"target_quantity ({self.target_quantity})."
            )


@dataclass(frozen=True, slots=True)
class CreateConsumptionMove:
    """Command to record a packaging consumption as a stock move.

    The adapter creates a draft move (packaging location → production
    location) and does it in one call, then reads back the move's
    cost price (the engine's average cost at move time). Doing a
    draft move directly does not check stock availability, so a
    consumption larger than the stock goes negative — by design
    (a warning signal, not an error; see docs/packaging.md).
    """

    company: CompanyRef
    product: ProductRef
    quantity: Decimal
    from_location: StockLocationRef
    to_location: StockLocationRef
    date: FiscalDate

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidStockMoveError("Consumption quantity must be greater than zero.")
        if self.from_location == self.to_location:
            raise InvalidStockMoveError(
                "Consumption needs two different locations (packaging → production)."
            )


@dataclass(frozen=True, slots=True)
class CreateAnalyticAccount:
    """Command to create one analytic account (cost centre) in the engine.

    ``account_type`` is ``normal`` for per-order cost centres (under
    the installation's orders root) and ``root`` for the root itself.
    A normal account needs a root and a parent (the root may be its
    own parent).
    """

    company: CompanyRef
    name: str
    account_type: str = "normal"
    code: str | None = None
    root: AnalyticAccountRef | None = None
    parent: AnalyticAccountRef | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidAnalyticAccountError("An analytic account needs a name.")
        if self.account_type not in ("root", "view", "normal", "distribution"):
            raise InvalidAnalyticAccountError(
                f"Analytic account type must be root/view/normal/distribution, "
                f"got {self.account_type!r}."
            )
        if self.account_type != "root" and (self.root is None or self.parent is None):
            raise InvalidAnalyticAccountError(
                "A non-root analytic account needs a root and a parent.",
            )
        if self.account_type == "root" and (self.root is not None or self.parent is not None):
            raise InvalidAnalyticAccountError(
                "A root analytic account has no root or parent.",
            )


@dataclass(frozen=True, slots=True)
class CreateInventory:
    """Command to open a stock inventory count at one storage location."""

    company: CompanyRef
    location: StockLocationRef
    date: FiscalDate


@dataclass(frozen=True, slots=True)
class UpdateInventoryLine:
    """Command to set the counted quantity on one inventory line."""

    line: InventoryLineRef
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise InvalidInventoryError("A counted quantity must be non-negative.")


# -- cheques (Phase 5-4, US-531) ---------------------------------------------
#
# The engine (``account_check_ir``) owns every cheque transition's
# journal: one Tryton transaction carries the state change and its
# سند together. The HesabYar layer sends the transition name plus
# the business parameters; it never builds move lines for a cheque.
# Transition names are the single definition in
# ``hesabyar.domain.cheques.state_machine``.


@dataclass(frozen=True, slots=True)
class CreateEngineCheque:
    """Command to register a cheque in the engine (intake, NO journal).

    The engine creates the ``account.check`` record in state
    ``registered``. ``party`` is the mapped Tryton party for the
    issuer when the mapping center knows it, else None (the engine
    model has no issuer-name text field — the name / national id /
    order / customer refs stay in the HesabYar registry, and the
    issuer identity reaches the engine only as ``party``).
    ``installment_leg_id`` travels as the site's leg identifier; the
    ۵ج installment module owns the engine-side link.
    """

    company: CompanyRef
    kind: str  # 'received' | 'issued'
    purpose: str  # 'payment' | 'collateral'
    sayad_id: str
    amount: MoneyIRR
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
    party: PartyRef | None = None
    leaf_number: str | None = None
    issuer_branch: str | None = None
    issuer_account_number: str | None = None
    reason: str | None = None
    invoice_engine_id: int | None = None
    installment_leg_id: str | None = None
    scan_ref: str | None = None
    #: Engine checkbook id the issued leaf was drawn from (US-532;
    #: required for kind='issued' — the engine validates the leaf
    #: number against the checkbook range).
    checkbook_engine_id: int | None = None
    #: Which obligation a collateral cheque secures (report 6
    #: dimension; US-532). NOT part of the 24-field site payload.
    secured_obligation: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("received", "issued"):
            raise InvalidJournalError(
                f"Cheque kind must be 'received' or 'issued', got {self.kind!r}.",
                details={"kind": self.kind},
            )
        if self.purpose not in ("payment", "collateral"):
            raise InvalidJournalError(
                f"Cheque purpose must be 'payment' or 'collateral', got {self.purpose!r}.",
                details={"purpose": self.purpose},
            )
        if not self.amount.is_positive:
            raise InvalidJournalError(
                "Cheque amount must be greater than zero.",
                details={"amount_irr": self.amount.amount},
            )
        if self.due_date.date < self.issue_date.date:
            raise InvalidJournalError(
                "Cheque due date must be on or after its issue date.",
                details={
                    "issue_date": self.issue_date.date.isoformat(),
                    "due_date": self.due_date.date.isoformat(),
                },
            )


@dataclass(frozen=True, slots=True)
class TransitionEngineCheque:
    """Command to run one cheque lifecycle transition in the engine.

    The engine performs the state change AND its journal (when the
    transition has one, §2.4 سند ۱–۱۳) in ONE Tryton transaction and
    returns the cheque as read back. ``auto_post`` carries the F3/F4
    policy-gate verdict: False (the default) leaves the created move
    a draft; True posts it — only when the gate allowed it.
    ``defer_journal`` is the accountant-gated path (endorsement to a
    non-supplier, §9.1 #12): the state moves, NO journal is guessed,
    and the caller files a review item for the journal design.
    ``kind`` is the cheque's kind as the caller knows it (the
    service passes it): the bounce rules differ by direction — a
    received bounce needs the bank certificate + destination, an
    issued bounce needs reason + date only. Per-transition required
    parameters are enforced here, before anything reaches the engine.
    """

    cheque: ChequeEngineRef
    company: CompanyRef
    transition: str
    auto_post: bool = False
    destination_account: AccountRef | None = None
    bounce_destination: str | None = None
    bounce_reason: str | None = None
    bounce_date: FiscalDate | None = None
    certificate_reference: str | None = None
    endorsee_name: str | None = None
    endorsee_type: str | None = None
    sayad_transfer_reference: str | None = None
    storage_location: str | None = None
    statement_reference: str | None = None
    mismatch_detail: str | None = None
    defer_journal: bool = False
    kind: str | None = None
    accountant_approval: str | None = None
    void_reason: str | None = None

    def __post_init__(self) -> None:
        from hesabyar.domain.cheques.errors import (  # noqa: PLC0415 - cheque errors live in the cheque domain
            ChequeValidationError,
        )
        from hesabyar.domain.cheques.state_machine import (  # noqa: PLC0415 - transition names live in the cheque domain
            TRANSITION_BOUNCE,
            TRANSITION_CALL_IN,
            TRANSITION_CLEAR,
            TRANSITION_CONFIRM_RECEIPT,
            TRANSITION_DEPOSIT,
            TRANSITION_ENDORSE,
            TRANSITION_ENDORSED_BOUNCE,
            TRANSITION_EXPIRE,
            TRANSITION_ISSUE,
            TRANSITION_REJECT_MISMATCH,
            TRANSITION_RETURN,
            TRANSITION_UNEXPECTED_CALL_IN,
            TRANSITION_VOID,
            TRANSITION_VOID_INTAKE,
        )

        known = {
            TRANSITION_CONFIRM_RECEIPT,
            TRANSITION_DEPOSIT,
            TRANSITION_CLEAR,
            TRANSITION_BOUNCE,
            TRANSITION_ENDORSE,
            TRANSITION_RETURN,
            TRANSITION_EXPIRE,
            TRANSITION_REJECT_MISMATCH,
            TRANSITION_ISSUE,
            TRANSITION_VOID,
            TRANSITION_CALL_IN,
            TRANSITION_UNEXPECTED_CALL_IN,
            TRANSITION_ENDORSED_BOUNCE,
            TRANSITION_VOID_INTAKE,
        }
        if self.transition not in known:
            raise ChequeValidationError(
                f"Unknown cheque transition {self.transition!r}.",
                field_errors={"transition": f"Unknown transition {self.transition!r}."},
            )
        missing: list[str] = []
        if self.transition in (TRANSITION_DEPOSIT, TRANSITION_ISSUE) and self.destination_account is None:
            missing.append("destination_account")
        if self.transition == TRANSITION_BOUNCE:
            if not (self.bounce_reason or "").strip():
                missing.append("bounce_reason")
            if self.bounce_date is None:
                missing.append("bounce_date")
            if self.kind != "issued":
                # A received bounce needs the bank's non-payment
                # certificate + the claim destination; an issued
                # bounce (our own leaf) has neither.
                if not (self.certificate_reference or "").strip():
                    missing.append("certificate_reference")
                if self.bounce_destination not in ("receivables", "bounced_receivables"):
                    missing.append("bounce_destination")
        if self.transition == TRANSITION_ENDORSE:
            if not (self.endorsee_name or "").strip():
                missing.append("endorsee_name")
            if self.endorsee_type not in ("supplier", "other"):
                missing.append("endorsee_type")
            if not (self.sayad_transfer_reference or "").strip():
                missing.append("sayad_transfer_reference")
        if self.transition == TRANSITION_REJECT_MISMATCH and not (self.mismatch_detail or "").strip():
            missing.append("mismatch_detail")
        if self.transition == TRANSITION_CALL_IN and not (self.accountant_approval or "").strip():
            missing.append("accountant_approval")
        if self.transition == TRANSITION_VOID and not (self.void_reason or "").strip():
            missing.append("void_reason")
        if self.transition == TRANSITION_UNEXPECTED_CALL_IN and not (self.statement_reference or "").strip():
            missing.append("statement_reference")
        if self.transition == TRANSITION_ENDORSED_BOUNCE:
            if not (self.bounce_reason or "").strip():
                missing.append("bounce_reason")
            if self.bounce_date is None:
                missing.append("bounce_date")
            if not (self.certificate_reference or "").strip():
                missing.append("certificate_reference")
        if missing:
            raise ChequeValidationError(
                f"Cheque transition {self.transition!r} is missing required parameter(s): "
                f"{', '.join(missing)}.",
                field_errors={name: "This parameter is required for this transition." for name in missing},
            )
        if self.transition == TRANSITION_CLEAR and self.defer_journal:
            raise ChequeValidationError(
                "Clearing never defers its journal.",
                field_errors={"defer_journal": "defer_journal must be false for the clear transition."},
            )
