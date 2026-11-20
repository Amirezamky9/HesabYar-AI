"""Engine-neutral accounting port.

Domain and application code depend only on this protocol. The production
implementation is the Tryton adapter
(``hesabyar.infrastructure.accounting.tryton``); a future native-ledger
adapter may exist only as a separately configured, mutually exclusive mode
(ADR 0002).

Scope (Phase 5 wave 2, US-514 + US-515): company/chart reads, journal draft create +
validate + list + update, journal post + reverse, posted-journal read,
fiscal year/period reads, trial balance, invoice draft create +
validate + finalize (validate/post) + credit notes, invoice reads,
sale create + workflow (quote/confirm/process/cancel) + reads, and
payment create + workflow (submit/approve/proceed/succeed/fail) +
reads; bank statements are created by import and matched by reconcile
(no validate/post on the port — the accountant validates in the
engine); packaging/stock reads and writes (products, quantities,
order points, consumption moves, purchase requests, inventories,
analytic accounts/lines, shipment costs). US-516 (cash box) adds:
one account's balance over posted moves (``get_account_balance`` —
the book balance a counting session snapshots), one account by code
(``get_account_by_code`` — a targeted read; fetching the whole chart
for one account costs ~28s against a remote engine), invoice totals
(``list_invoice_totals`` — date + total per invoice, no lines /
party names / currencies; the cash-box report's sales side),
statement-journal reads (``list_statement_journals`` — the daybook
behind each cash box), draft deletion (``delete_journal_draft`` —
recount cleanup; posted journals are never deletable), and the
company's general journal (``get_general_journal`` — the journal a
system-created draft, like a cash-box variance journal, is booked
to; ``create_journal_draft`` with no journal fails once the engine
holds several general journals, so internal drafts resolve one
explicitly). Reporting (``get_report`` in the design-eng spec
§2.2 sketch) joins the port when its Tryton operations are
contract-verified; the port is the seam, so extending it does not
touch domain callers.

The port is synchronous: there is no async infrastructure in the
repository yet, and the Tryton REST calls are plain HTTP. A later story may
make the port async when the MCP/application layer needs it.

Transition style: pure workflow transitions take the record ref
directly (``quote_sale(sale)``); commands are used only where the call
carries data beyond the ref (``SucceedPayment`` carries the clearing
date, ``CreateCreditNote`` carries the date). This matches the
existing ``validate_*`` (ref) vs ``PostJournal`` (command) split.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from hesabyar.domain.accounting.commands import (
    CreateAnalyticAccount,
    CreateBankStatement,
    CreateConsumptionMove,
    CreateCreditNote,
    CreateEngineCheque,
    CreateInventory,
    CreateInvoiceDraft,
    CreateJournalDraft,
    CreatePackagingProduct,
    CreatePayment,
    CreateSale,
    PostJournal,
    ReconcileMoveLines,
    ReverseJournal,
    SucceedPayment,
    TransitionEngineCheque,
    UpdateBankStatementLine,
    UpdateInventoryLine,
    UpdateJournalDraft,
    UpsertOrderPoint,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    AnalyticAccountRef,
    BankStatementRef,
    CheckbookEngineRef,
    ChequeEngineRef,
    CompanyRef,
    FiscalYearRef,
    InventoryRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    OutShipmentRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PostedJournalRef,
    ProductRef,
    SaleRef,
    StockLocationRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountBalanceView,
    AccountView,
    AnalyticAccountView,
    AnalyticLineView,
    BankStatementView,
    CheckbookEngineView,
    ChequeEngineView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    InventoryView,
    InvoiceTotalView,
    InvoiceView,
    JournalDraftView,
    MoveLineView,
    OrderPointView,
    OutShipmentCostView,
    PackagingProductView,
    PaymentJournalView,
    PaymentView,
    PostedJournalView,
    PurchaseRequestView,
    ReconciliationView,
    SaleShipmentCostView,
    SaleView,
    StatementJournalView,
    StockLocationView,
    StockMoveView,
    TrialBalanceView,
    ValidationSummary,
)
from hesabyar.domain.common.dates import FiscalDate


class AccountingEnginePort(Protocol):
    """Engine-neutral access to the accounting ledger."""

    def list_companies(self) -> Sequence[CompanyView]:
        """Return every company visible to the engine credentials."""
        ...

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        """Return the company's chart of accounts, ordered by account code."""
        ...

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        """Create a draft journal (unposted move) in the engine.

        The command itself is already invariant-checked at construction; the
        adapter additionally verifies that every account exists for the
        company and that the date falls in an open fiscal period before
        delegating to the engine.
        """
        ...

    def validate_journal_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        """Validate a draft journal against the §8.3 invariants.

        Checks the draft's own state, line structure, balance, account
        validity, and fiscal-period state, and cross-checks the engine's own
        balance verdict on the draft's lines. Never mutates the draft.
        """
        ...

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        """Read a draft journal back as a domain view (None when missing).

        The view carries the move's state, so a draft that has since been
        posted reads back with ``state == "posted"`` rather than vanishing.
        """
        ...

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        """Return the company's draft journals (unposted moves), newest first.

        Company-scoped: only drafts of ``company`` are returned, and only
        moves still in state ``draft`` — posted journals never appear here.
        """
        ...

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        """Update a draft journal's date, description and/or lines.

        Only the fields present on the command change; ``lines`` replaces
        the draft's lines wholesale. The update is atomic in the engine
        and re-validated before it returns: unknown accounts, unbalanced
        lines and closed periods fail with the same stable ledger errors
        as creation. Updating a posted journal raises
        ``JournalAlreadyPostedError`` (LEDGER_POSTED_IMMUTABLE) — a
        posted journal is immutable and can only be corrected by a
        reversal. Returns the updated draft as read back from the engine.
        """
        ...

    def delete_journal_draft(self, draft: JournalDraftRef) -> None:
        """Delete a draft journal (an unposted move) from the engine.

        Only drafts can be deleted: deleting a posted journal raises
        ``JournalAlreadyPostedError`` (LEDGER_POSTED_IMMUTABLE) — a
        posted journal is immutable and can only be corrected by a
        reversal. Deleting an unknown draft raises a stable
        not-found error. Used by the cash-box recount flow to remove
        a superseded variance draft; no other flow deletes drafts.
        """
        ...

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        """Post a draft journal, atomically, in the engine.

        The draft is re-validated against the §8.2 invariants first:
        unbalanced drafts, drafts with unknown/closed accounts, and drafts
        in closed periods are rejected with stable ledger errors before
        anything reaches the engine, which remains the final enforcer.
        Posting an already-posted journal raises
        ``JournalAlreadyPostedError`` (LEDGER_POSTED_IMMUTABLE).
        """
        ...

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        """Reverse a posted journal with a new posted reversal journal.

        The reversal's lines cancel the original's (debit/credit swapped)
        and its origin links back to the original journal, mirroring the
        Tryton ``account.move.cancel`` wizard. Only posted journals can be
        reversed. If the original's period has since closed, the reversal
        is dated today in the currently open period, as the wizard does.
        """
        ...

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        """Read a posted journal back as a domain view (None when missing).

        Raises ``InvalidJournalError`` when the referenced move exists but
        is not posted: this read is for posted journals only.
        """
        ...

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        """Return the company's fiscal years, newest first."""
        ...

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        """Return the company's fiscal periods, ordered by start date.

        When ``fiscal_year`` is given, only that year's periods are
        returned; otherwise every period of the company's fiscal years.
        """
        ...

    # -- trial balance (US-403) --------------------------------------------

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        """Return the company's trial balance over posted moves only.

        Aggregates the debit/credit of every ``account.move.line`` whose
        move is in state ``posted``, belongs to ``company`` and is dated
        within ``[date_from, date_to]``. Draft moves never contribute.
        The read pages through the engine's complete line set — no figure
        is ever computed from a paginated partial read. P&L, balance
        sheet and cash flow are NOT part of this read (they stay honestly
        deferred); only the trial balance closes the Phase 2
        ``get_report`` gap.
        """
        ...

    # -- account balance (US-516) ----------------------------------------

    def get_account_balance(
        self,
        company: CompanyRef,
        account: AccountRef,
        as_of: FiscalDate,
    ) -> AccountBalanceView:
        """Return one account's balance over posted moves up to ``as_of``.

        The cumulative debit minus credit of every posted move line on
        the account dated on or before ``as_of`` (draft moves never
        contribute). Unlike the trial balance — which reports activity
        inside a range — this is the account's balance at a point in
        time: the book balance a cash-box counting session snapshots
        read-only. The read pages through the account's complete line
        set — no figure is ever computed from a paginated partial
        read. An unknown account is a stable ledger error.
        """
        ...

    def get_account_by_code(self, company: CompanyRef, code: str) -> AccountView | None:
        """Return one account by code, or None when the company has none.

        A targeted read for flows that need a single account (a
        cash-box variance account): fetching the whole chart for one
        code costs ~28s against a remote engine — nearly the BFF's
        whole 30s budget for one request — while this read returns
        one record.
        """
        ...

    # -- invoices ----------------------------------------------------------

    def create_invoice_draft(self, command: CreateInvoiceDraft) -> InvoiceRef:
        """Create a draft invoice in the engine.

        The command itself is already invariant-checked at construction;
        the adapter additionally verifies the party and its invoice
        address, the receivable/payable account and the journal, and that
        every line account exists for the company, before delegating to
        the engine. The engine computes the invoice totals from the lines.
        """
        ...

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        """Validate a draft invoice against the postability invariants.

        Checks the draft's own state, line structure and amounts,
        cross-checks the engine's computed totals against the lines, and
        verifies the party, address, account type, journal and open
        fiscal period the invoice will need when it is posted later.
        Never mutates the invoice: this is the read-only HesabYar check,
        mirroring ``validate_journal_draft``. Finalizing the invoice in
        the engine (the Tryton ``validate_invoice`` transition) is a
        later story's port method, not this one.
        """
        ...

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        """Read an invoice back as a domain view (None when missing)."""
        ...

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        """Return the company's invoices, newest first.

        ``invoice_type`` filters to ``'out'`` (customer) or ``'in'``
        (supplier); ``state`` filters to one engine state (``'draft'``,
        ``'validated'``, ``'posted'``, ``'paid'``, ``'cancelled'``).
        """
        ...

    def list_invoice_totals(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceTotalView]:
        """Return one row per invoice: its date and total, nothing else.

        A targeted read for reports that sum sales (the cash-box
        report): ``list_invoices`` also fetches every invoice's
        lines, party names, and currency codes — three extra
        round-trips the report never uses.
        """
        ...

    def validate_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        """Finalize a draft invoice (engine ``validate_invoice`` transition).

        Draft -> validated: the engine assigns the invoice number (for
        supplier invoices) and checks the invoice is postable. A
        non-draft invoice raises a stable error. Returns the invoice as
        read back from the engine.
        """
        ...

    def post_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        """Post an invoice (engine ``post`` transition).

        Draft/validated -> posted: the engine creates the accounting
        move (receivable/payable line with ``maturity_date`` from the
        payment term, party from the invoice -- or from the single
        alternative payee, see docs/tryton-spikes-502.md spike 4) and
        assigns the number for customer invoices. Posting requires an
        open fiscal period for the accounting date. Returns the invoice
        as read back from the engine.
        """
        ...

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        """Cancel a draft or validated invoice (engine ``cancel`` transition).

        Draft/validated -> cancelled: the invoice keeps its history
        and can never be posted. A posted invoice cannot be cancelled
        — it is corrected with a credit note instead (US-511). Returns
        the invoice as read back from the engine.
        """
        ...

    def create_credit_note(self, command: CreateCreditNote) -> InvoiceRef:
        """Create a credit-note draft from a posted invoice.

        Tryton 8.2 has no separate credit-note type: the credit note is
        an invoice of the same type whose lines carry negative
        quantities (mirroring ``Invoice.credit``), each line's origin
        pointing at its source line. The source invoice must be posted.
        By default the credit is FULL (every line, full quantity); a
        command with ``partial_amount`` creates an amount-based partial
        credit instead (US-511: a partial refund is corrected with a
        credit note, never by rewriting the invoice). Returns the
        credit-note draft's ref; validate/post it with
        ``validate_invoice`` / ``post_invoice`` like any invoice.
        """
        ...

    # -- sales -------------------------------------------------------------

    def create_sale(self, command: CreateSale) -> SaleRef:
        """Create a draft sale order in the engine.

        The command itself is already invariant-checked at construction;
        the adapter additionally verifies the party and its addresses,
        that every line product exists and is sellable, and resolves
        each line's unit of measure from the product. The engine
        computes the sale totals from the lines.
        """
        ...

    def get_sale(self, sale: SaleRef) -> SaleView | None:
        """Read a sale order back as a domain view (None when missing)."""
        ...

    def list_sales(
        self,
        company: CompanyRef,
        state: str | None = None,
    ) -> Sequence[SaleView]:
        """Return the company's sale orders, newest first.

        ``state`` filters to one engine state (``'draft'``,
        ``'quotation'``, ``'confirmed'``, ``'processing'``, ``'done'``,
        ``'cancelled'``).
        """
        ...

    def quote_sale(self, sale: SaleRef) -> SaleView:
        """Quote a draft sale (engine ``quote`` transition).

        Draft -> quotation: the engine checks the sale is quotable and
        assigns the sale number. Returns the sale as read back.
        """
        ...

    def confirm_sale(self, sale: SaleRef) -> SaleView:
        """Confirm a quoted sale (engine ``confirm`` transition).

        Quotation -> confirmed: the engine stamps the sale date. The
        engine queues its own processing step (Tryton queue); the
        adapter does NOT wait for it -- call ``process_sale`` to run
        invoice/shipment creation deterministically. Returns the sale
        as read back.
        """
        ...

    def process_sale(self, sale: SaleRef) -> SaleView:
        """Process a confirmed sale (engine ``process`` transition).

        Creates the sale's invoices (per ``invoice_method``) and
        shipments (per ``shipment_method``) and advances the sale
        towards ``'done'``. The returned view lists the invoices the
        sale produced. Calling it on a draft/quotation raises a stable
        error -- quote and confirm first.
        """
        ...

    def cancel_sale(self, sale: SaleRef) -> SaleView:
        """Cancel a sale order (engine ``cancel`` transition).

        Returns the sale as read back.
        """
        ...

    # -- payments ----------------------------------------------------------

    def create_payment(self, command: CreatePayment) -> PaymentRef:
        """Create a draft payment in the engine.

        The command itself is already invariant-checked at construction;
        the adapter additionally verifies the party, the payment
        journal, and the linked move line / account when given. Note
        (spike 3): a Draft payment already reduces the linked invoice
        line's ``amount_to_pay`` -- create payments only when the
        tranche is actually being processed.
        """
        ...

    def submit_payment(self, payment: PaymentRef) -> PaymentView:
        """Submit a draft payment (engine ``submit`` transition).

        Draft -> submitted. Returns the payment as read back.
        """
        ...

    def approve_payment(self, payment: PaymentRef) -> PaymentView:
        """Approve a submitted payable payment (engine ``approve`` transition).

        Submitted -> approved. Receivable payments skip this step (they
        go submitted -> processing directly). Returns the payment as
        read back.
        """
        ...

    def process_payment(self, payment: PaymentRef) -> PaymentView:
        """Move a submitted/approved payment to processing (engine ``process``).

        Via the hesabyar_setup module's ``process_rest`` REST action,
        which delegates to the engine's ``process`` method (exposed
        over REST by the module, since wizards are not REST-reachable)
        for manual processing: it runs the submitted/approved ->
        processing transition plus the journal's ``process_<method>``
        hook, a no-op for manual payments. NOT the ``proceed`` button
        -- that is only visible for succeeded/failed manual payments
        (re-processing); see docs/tryton-spikes-502.md spike 1.
        Returns the payment as read back.
        """
        ...

    def process_payments(
        self, payments: Sequence[PaymentRef]
    ) -> Sequence[PaymentView]:
        """Move several submitted/approved payments to processing at once.

        One call to the hesabyar_setup module's ``process_rest_batch``
        action, so a settlement batch's payments land in ONE
        ``account.payment.group`` (one group per (company, journal,
        kind) — per-payment calls would give each payment its own
        group). Returns the payments as read back, in input order.
        An empty input returns an empty sequence without calling the
        engine.
        """
        ...

    def get_payment_journal(
        self, journal: PaymentJournalRef
    ) -> PaymentJournalView | None:
        """Read a payment journal back as a domain view (None when missing).

        The settlement engine uses this for the fail-closed clearing
        check: ``succeed`` on a journal without a clearing account +
        journal creates NO clearing move, so the engine must refuse to
        process on such a journal.
        """
        ...

    def succeed_payment(self, command: SucceedPayment) -> PaymentView:
        """Mark a processing payment succeeded (engine ``succeed`` transition).

        Processing -> succeeded. With the clearing module installed the
        engine creates the clearing move (receivable/payable -> clearing
        account) dated with the command's ``clearing_date`` (today when
        omitted) and reconciles the payment's line. Returns the payment
        as read back.
        """
        ...

    def fail_payment(self, payment: PaymentRef) -> PaymentView:
        """Mark a processing/succeeded payment failed (engine ``fail`` transition).

        Returns the payment as read back.
        """
        ...

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        """Read a payment back as a domain view (None when missing)."""
        ...

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        """Return the company's payments, newest first.

        ``kind`` filters to ``'receivable'`` or ``'payable'``; ``state``
        filters to one engine state (``'draft'``, ``'submitted'``,
        ``'approved'``, ``'processing'``, ``'succeeded'``, ``'failed'``).
        """
        ...

    # -- move lines + reconciliation (Phase 5-3, US-522) -------------------

    def list_move_lines(
        self,
        company: CompanyRef,
        *,
        move: JournalDraftRef | None = None,
        account: AccountRef | None = None,
        party: PartyRef | None = None,
        maturity_date: FiscalDate | None = None,
        unreconciled_only: bool = False,
    ) -> Sequence[MoveLineView]:
        """Return move lines matching the given filters, oldest first.

        Every filter is optional, but at least one of ``move`` /
        ``account`` must be given (an unfiltered read would page the
        whole ledger). The settlement engine gathers a tranche's
        reconcilable lines by (account, party, maturity_date) and
        reads a move's lines to find the clearing leg a payment
        created.
        """
        ...

    def reconcile_move_lines(self, command: ReconcileMoveLines) -> ReconciliationView:
        """Reconcile a set of move lines in the engine.

        Via the hesabyar_setup module's ``reconcile_rest`` REST
        action, which delegates to the engine's ``Line.reconcile``
        (not REST-reachable itself — the Reconcile wizard cannot run
        over REST). The adapter pre-validates the engine's conditions
        (same account, same party, debits == credits, every line
        'valid' and unreconciled) by reading the lines first; the
        engine's own ``ReconciliationError`` is the backstop. Returns
        the created reconciliation.
        """
        ...

    # -- bank statements ---------------------------------------------------

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        """Read a bank/cash statement back as a domain view (None when missing)."""
        ...

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        """Return the company's bank/cash statements, newest first.

        ``state`` filters to one engine state (``'draft'``,
        ``'validated'``, ``'posted'``, ``'cancelled'``).
        """
        ...

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        """Create a draft bank/cash statement with its lines (US-514 import).

        The statement is created in state ``draft``; every line carries
        an account (Tryton requires one). The company must book in IRR;
        the journal must be a statement journal of the company; every
        line account must belong to the company and be open.
        """
        ...

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        """Write match results onto one line of a draft statement (US-514 reconcile).

        Only the fields set on the command change. The statement must
        be a draft (Tryton locks line fields once validated) and the
        line must belong to it; an invoice link requires the line's
        account to equal the invoice's account.
        """
        ...

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        """Return the company's statement journals (``account.statement.journal``).

        The import form offers these as the destination of a new
        statement; the reconcile service resolves a statement's
        destination bank account through them. One statement journal
        is also the daybook behind each cash box (US-516): a cash
        box is registered against its journal, and the journal's
        account is the box's cash account.
        """
        ...

    # -- packaging & shipping (Phase 5 wave 2, US-515) ---------------------

    def list_stock_locations(self, company: CompanyRef) -> Sequence[StockLocationView]:
        """Return the engine's stock locations (warehouses, storage, ...).

        Locations are global in the engine, not company-scoped; the
        company only sets the read context. Ordered by name.
        """
        ...

    def get_stock_location(self, location: StockLocationRef) -> StockLocationView | None:
        """Read one stock location back (None when missing)."""
        ...

    def create_packaging_product(self, command: CreatePackagingProduct) -> ProductRef:
        """Create a packaging material's product in the engine.

        Creates a ``product.template`` (type ``goods``,
        ``consumable`` False, ``purchasable`` True, ``salable``
        False) under the given category with the requested cost
        method, and returns the single product variant Tryton creates
        with it. ``fifo`` raises ``FifoUnavailableError`` unless the
        ``product_cost_fifo`` module is activated (base Tryton only
        offers ``fixed``/``average``). The adapter sets the cost
        method explicitly: the product configuration default is
        ``fixed`` on the dev database.
        """
        ...

    def get_packaging_product(
        self,
        product: ProductRef,
        company: CompanyRef,
        location: StockLocationRef | None = None,
    ) -> PackagingProductView | None:
        """Read a packaging product back (None when missing).

        ``quantity`` is populated when ``location`` is given (the
        product's on-hand quantity at that location, children
        included); ``cost_price`` is the product's current cost price
        for the company.
        """
        ...

    def list_packaging_products(
        self,
        company: CompanyRef,
        products: Sequence[ProductRef],
        location: StockLocationRef | None = None,
    ) -> Sequence[PackagingProductView]:
        """Read several packaging products back in one batch.

        Same fields as :meth:`get_packaging_product`, but a single
        engine round-trip (two searches) instead of one read pair per
        product. The materials list needs this: per-product reads took
        ~30s for 20 materials over the remote DB (US-515, 2026-11-18),
        right at the BFF timeout. Products missing from the engine are
        skipped (not an error — the caller reports them as unknown).
        """
        ...

    def get_product_quantity(
        self, product: ProductRef, location: StockLocationRef
    ) -> Decimal:
        """Return a product's on-hand quantity at one location (may be negative)."""
        ...

    def get_product_cost_price(
        self, product: ProductRef, company: CompanyRef
    ) -> Decimal | None:
        """Return a product's current cost price for a company (None when unset)."""
        ...

    def upsert_order_point(self, command: UpsertOrderPoint) -> OrderPointView:
        """Create or update a purchase order point for a product at a warehouse.

        One purchase point exists per (product, warehouse, company) —
        the adapter updates the existing one when present instead of
        creating a duplicate.
        """
        ...

    def get_order_point(
        self, product: ProductRef, warehouse: StockLocationRef, company: CompanyRef
    ) -> OrderPointView | None:
        """Read a product's purchase order point at a warehouse (None when missing)."""
        ...

    def list_order_points(self, company: CompanyRef) -> Sequence[OrderPointView]:
        """Return the company's purchase order points, ordered by product."""
        ...

    def create_consumption_move(self, command: CreateConsumptionMove) -> StockMoveView:
        """Record a packaging consumption as a done stock move.

        Creates a draft move (packaging location → production
        location) and does it, then reads it back with the cost price
        the engine computed at move time. Two REST calls, not one
        transaction: if the ``do`` fails, the draft move stays in the
        engine and its id is attached to the raised error's details
        for recovery (same pattern as the journal reversal). Doing a
        draft move directly does not check stock availability, so a
        consumption larger than the stock goes negative — by design.
        """
        ...

    def list_consumption_moves(
        self,
        company: CompanyRef,
        from_location: StockLocationRef,
        to_location: StockLocationRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        product: ProductRef | None = None,
    ) -> Sequence[StockMoveView]:
        """Return done consumption moves in a date range, oldest first.

        Moves from ``from_location`` to ``to_location`` in state
        ``done`` whose effective date falls within
        ``[date_from, date_to]``. The days-of-cover computation reads
        these — the read pages through the complete set, never a
        partial page.
        """
        ...

    def list_purchase_requests(
        self,
        company: CompanyRef,
        state: str | None = None,
        after_id: int | None = None,
    ) -> Sequence[PurchaseRequestView]:
        """Return the company's purchase requests, oldest first.

        ``state`` filters to one engine state; ``after_id`` returns
        only requests with a higher engine id (the supply-check
        watermark). The supply wizard creates these; the port only
        reads them — HesabYar never creates purchase requests
        directly (Tryton's own rule).
        """
        ...

    def create_analytic_account(self, command: CreateAnalyticAccount) -> AnalyticAccountRef:
        """Create an analytic account in the engine (root or normal)."""
        ...

    def get_analytic_account(
        self, account: AnalyticAccountRef
    ) -> AnalyticAccountView | None:
        """Read an analytic account back with its balance (None when missing)."""
        ...

    def list_analytic_lines(
        self, account: AnalyticAccountRef
    ) -> Sequence[AnalyticLineView]:
        """Return an analytic account's lines, oldest first (all dates)."""
        ...

    def create_inventory(self, command: CreateInventory) -> InventoryRef:
        """Open a stock inventory count at one storage location (draft)."""
        ...

    def get_inventory(self, inventory: InventoryRef) -> InventoryView | None:
        """Read an inventory count back with its lines (None when missing)."""
        ...

    def complete_inventory_lines(self, inventory: InventoryRef) -> InventoryView:
        """Fill an inventory's lines with expected quantities (engine ``complete_lines``).

        Draft inventories only. Lines for products with stock at the
        location are created with their expected quantity; the
        operator then sets the counted quantities.
        """
        ...

    def update_inventory_line(self, command: UpdateInventoryLine) -> InventoryView:
        """Set the counted quantity on one inventory line; returns the inventory."""
        ...

    def confirm_inventory(self, inventory: InventoryRef) -> InventoryView:
        """Confirm a draft inventory (engine ``confirm`` button).

        The engine creates the adjustment moves (to/from the
        lost-and-found location) and does them, then the inventory is
        ``done``. This is the human approval step — nothing in
        HesabYar calls it automatically.
        """
        ...

    def cancel_inventory(self, inventory: InventoryRef) -> InventoryView:
        """Cancel a draft inventory (engine ``cancel`` button).

        Only draft inventories can be cancelled; a done inventory is
        final (its adjustment moves are already posted).
        """
        ...

    def get_sale_shipment_costs(self, sale: SaleRef) -> Sequence[SaleShipmentCostView]:
        """Return the invoiced shipment-cost lines of a sale (``stock.shipment.cost_sale``).

        Empty when the sale's shipment-cost method is ``none`` (the
        cost is absorbed into the margin instead of invoiced).
        """
        ...

    def get_out_shipment_cost(self, shipment: OutShipmentRef) -> OutShipmentCostView | None:
        """Read an outgoing shipment's actual + charged cost (None when missing)."""
        ...

    def get_general_journal(self, company: CompanyRef) -> JournalRef:
        """Return the company's general journal for system-created drafts.

        ``create_journal_draft`` with no journal only resolves when
        the engine holds exactly one general journal — Tryton's own
        modules already ship several (``Stock``, ``Miscellaneous``),
        so an internal draft (a cash-box variance journal, an event
        pipeline draft) must resolve its journal explicitly. The
        implementation prefers the journal provisioned for the
        company (``"<Company Name> General Journal"`` — the
        provisioning convention), falls back to the only general
        journal when there is exactly one, and raises a stable
        not-found/ambiguous error otherwise.
        """
        ...

    # -- cheques (Phase 5-4, US-531) ---------------------------------------
    #
    # The HesabYar layer reaches ``account_check_ir`` ONLY through
    # these methods. Each transition is ONE engine call that performs
    # the state change AND its journal in a single Tryton transaction
    # (the golden rule, §5 §2); the HesabYar registry mirrors the
    # returned view afterwards and never writes first. The exact REST
    # contract the Tryton adapter speaks is ``docs/cheque-engine-
    # contract.md`` (model ``account.check``, actions ``create_rest``
    # / ``transition_rest``).

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        """Register a cheque in the engine (intake, NO journal).

        Creates the ``account.check`` record in state ``registered``.
        Raises a stable engine error when ``account_check_ir`` is not
        installed on this engine.
        """
        ...

    def get_engine_cheque(self, cheque: ChequeEngineRef) -> ChequeEngineView | None:
        """Read an engine cheque back as a domain view (None when missing)."""
        ...

    def list_engine_cheques(
        self, company: CompanyRef, *, state: str | None = None
    ) -> Sequence[ChequeEngineView]:
        """Return the company's engine cheques, newest first.

        ``state`` filters to one engine state (``'registered'``,
        ``'held'``, ``'in_clearing'``, ...).
        """
        ...

    def transition_engine_cheque(self, command: TransitionEngineCheque) -> ChequeEngineView:
        """Run one cheque lifecycle transition in the engine, atomically.

        The state change and its journal (سند ۱–۱۳, when the transition
        has one) commit in ONE Tryton transaction: a failure leaves
        neither a journal without a state nor a state without its
        journal. ``auto_post=False`` (the default) leaves the created
        move a draft; ``auto_post=True`` posts it — the caller passes
        True only when the F3/F4 policy gate allowed it. Returns the
        cheque as read back from the engine.
        """
        ...

    def list_engine_checkbooks(
        self, company: CompanyRef
    ) -> Sequence[CheckbookEngineView]:
        """Return the company's checkbooks, newest first (US-532).

        The view carries the engine-computed leaf counts (used /
        voided / remaining) — the checkbook page (report 8) data.
        """
        ...

    def get_engine_checkbook(
        self, company: CompanyRef, checkbook: CheckbookEngineRef
    ) -> CheckbookEngineView | None:
        """Read one engine checkbook back (None when missing / other company)."""
        ...
