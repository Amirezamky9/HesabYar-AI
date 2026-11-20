"""Tryton implementation of the AccountingEnginePort.

Talks to Tryton ONLY through the supported REST API (via
:class:`TrytonRestClient`): no SQL against Tryton's database, no JSON-RPC,
no direct database access anywhere in this module.

Journal lifecycle mapping (Tryton 8.2, verified live against 8.2.3):

- a HesabYar journal draft is a Tryton ``account.move`` in state ``draft``;
- the engine marks a draft's lines ``valid`` when the move balances and
  ``draft`` otherwise (``Move.validate_move``, triggered automatically on
  line create/write), so the engine's own balance verdict is readable;
- posting is the ``post`` model action (``Move.post`` is in
  ``account.move.__rpc__``): one REST call, one engine transaction, atomic.
  It enforces non-empty + balanced, assigns the move number from the
  period/fiscal-year sequence, and stamps the post date;
- a posted move is immutable in the engine (``Move.check_modification``
  raises on delete and on writes outside ``lines``, and
  ``Line.on_modification`` raises on any line change of a posted move),
  and the adapter exposes no edit/delete path for posted journals at
  all: updating a posted journal raises ``JournalAlreadyPostedError``
  before anything reaches the engine;
- a draft move is editable through one REST ``PUT``: date, description
  and period are written as plain fields, and the lines are rewritten
  wholesale with the One2Many command syntax
  (``[["delete", [old ids]], ["create", [new lines]]]``) — deletes run
  before creates (``One2Many.set``), the move's own columns are written
  before the line commands (``ModelSQL.write``), and the whole PUT is
  one engine transaction, so the update is atomic. The engine
  re-validates balance automatically (``Move.validate_move`` on line
  create/delete) and the adapter re-runs ``validate_journal_draft``
  before returning the read-back view. A date change must carry its
  period in the same write: the engine enforces the date-in-period
  domain on write, so a date-only move across periods is rejected
  (verified live). ``on_change_date`` is client-side only and never
  runs for a REST write, so the adapter resolves the open period for
  the new date itself. Reconciled lines refuse a rewrite
  (``Line.on_modification`` raises on deleting them), so the adapter
  pre-checks for them and fails with a stable error instead;
- listing drafts is a company-scoped ``account.move`` search
  (``state = 'draft'``), newest first, with lines and periods batched
  into the views;
- reversal replicates the ``account.move.cancel`` wizard, which REST
  cannot drive (no wizard endpoint; ``Move.cancel`` is not in
  ``__rpc__``): the adapter creates a new move with debit/credit swapped,
  ``origin`` set to the original move, each line's ``origin`` set to the
  original line, then posts it. If the original's period has since closed,
  the reversal is dated today in the currently open period, as the wizard
  does. Reversal is therefore two REST calls, not one transaction: if the
  post of the reversal fails, the reversal draft stays in the engine and
  its id is attached to the raised error's details for recovery.
  Reconciliation of reconcile-account lines (which the wizard also does)
  is NOT replicated: ``account.move.line.reconcile`` is not RPC-exposed.

Pre-validation (design-eng spec §8.2): before delegating a draft to the
engine, the adapter verifies that every account exists for the company, is
open and active, needs no party (tafsili support is a later story), that the
company currency is IRR, and that the date falls in an open fiscal period.
Posting re-validates the draft the same way before calling the engine.

Invoices, payments, statements (Phase 2, US-204):

- an invoice draft is a Tryton ``account.invoice`` in state ``draft``;
  the adapter creates it with nested lines (command syntax) and resolves
  the party's invoice address, the receivable/payable account and the
  revenue/expense journal first. ``validate_invoice_draft`` is the
  read-only HesabYar check (state, lines, engine totals cross-checked
  against the lines, party/address/account/journal, open fiscal period
  for the accounting date); it never calls the engine's
  ``validate_invoice`` transition, which finalizes the draft and belongs
  to a later story's port method;
- payments (``account.payment``) are exposed read-only plus the
  workflow transitions; bank statements (``account.statement``) are
  created by import (US-514) and matched by reconcile (line-field
  writes only — no validate/post on the port). Tryton owns the
  documents; HesabYar keeps no duplicate invoice/payment ledger.

Sales, invoice finalization, credit notes, payment workflow (Phase 5,
US-502; see docs/tryton-spikes-502.md for the spike evidence):

- a sale order is a Tryton ``sale.sale``; the adapter creates it in
  state ``draft`` (resolving addresses, products and each line's unit
  of measure) and drives ``quote`` / ``confirm`` / ``process`` /
  ``cancel`` -- all ``@ModelView.button`` methods, hence in
  ``__rpc__`` and callable as REST actions. ``confirm`` queues the
  engine's own processing step (Tryton queue); the adapter calls
  ``process`` directly afterwards so invoice/shipment creation is
  deterministic and does not depend on a running worker;
- invoice finalization is the ``validate_invoice`` and ``post`` button
  transitions over REST. A credit note is an invoice of the same type
  with negative-quantity lines (``Invoice.credit`` is wizard-only, not
  RPC-exposed, so the adapter mirrors it with a plain create);
- payments are created as drafts and driven through the transitions
  ``submit`` / ``approve`` / ``process`` / ``succeed`` / ``fail``.
  The Process/Succeed *wizards* are not REST-reachable, but no wizard
  is needed: the hesabyar_setup module's ``process_rest`` REST action
  delegates to the engine's ``process`` method for manual processing
  (``process_manual`` is a no-op) and ``succeed`` creates the
  clearing move, with the clearing date passed in the REST
  ``X-Tryton-Context`` header (wire-format date, decoded by the
  server's JSON decoder into the transaction context). NOT the
  ``proceed`` button: it is only visible for succeeded/failed manual
  payments (re-processing) and refuses submitted payments.

Packaging & shipping (Phase 5 wave 2, US-515; see docs/packaging.md
for the spike evidence):

- a material is a Tryton ``product.template`` of type ``goods`` with
  ``consumable`` False, ``purchasable`` True, ``salable`` False under
  the installation's packaging category; the cost method is set
  explicitly (the product configuration default is ``fixed`` on the
  dev database). FIFO needs ``product_cost_fifo`` activated -- the
  adapter checks ``ir.module`` and fails with a stable error
  otherwise (base Tryton only offers ``fixed``/``average``);
- quantities are read through the ``product.product`` Function field
  with the ``locations`` context (children included); cost prices
  through the company-context ``cost_price`` MultiValue;
- consumption is a draft ``stock.move`` (packaging location ->
  production location) followed by the ``do`` button: ``do`` goes
  draft -> done directly and does NOT check stock availability, so
  consumption past zero goes negative by design. The engine computes
  the move's cost price (average) at ``do`` time and, with
  anglo-saxon accounting, posts debit stock-out (packaging expense)
  / credit stock (inventory);
- order points (``stock.order_point``, type ``purchase``) are
  upserted per (product, warehouse, company); purchase requests are
  read-only here (the supply wizard creates them);
- inventories (``stock.inventory``) are created, completed
  (``complete_lines`` button fills expected quantities), line-edited
  and confirmed (``confirm`` button creates the lost-and-found
  adjustment moves) -- all buttons, hence REST actions.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

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
    JournalDraftLine,
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
    ReconciliationRef,
    SaleRef,
    StockLocationRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountBalanceView,
    AccountView,
    AnalyticAccountView,
    AnalyticLineView,
    BalanceSheetView,
    BankStatementLineView,
    BankStatementView,
    CashFlowSectionView,
    CashFlowView,
    CheckbookEngineView,
    ChequeEngineView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    GeneralLedgerAccountView,
    GeneralLedgerLineView,
    GeneralLedgerView,
    IncomeStatementView,
    InventoryView,
    InvoiceLineView,
    InvoiceTotalView,
    InvoiceView,
    JournalDraftView,
    JournalLineView,
    MonthlyRevenueExpenseView,
    MoveLineView,
    NotesBalanceRowView,
    NotesBalanceView,
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
    TrialBalanceLineView,
    TrialBalanceView,
    ValidationIssue,
    ValidationSummary,
)
from hesabyar.domain.cheques.errors import ChequeEngineError
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    AccountNotFoundError,
    AnalyticAccountNotFoundError,
    InvalidBankStatementError,
    InvalidInventoryError,
    InvalidInvoiceError,
    InvalidJournalError,
    InvalidPaymentError,
    InvalidProductError,
    InvalidSaleError,
    InvalidStockMoveError,
    InventoryNotFoundError,
    InvoiceNotFoundError,
    JournalAlreadyPostedError,
    JournalDraftNotFoundError,
    LedgerError,
    OrderPointNotFoundError,
    OutShipmentNotFoundError,
    PartyNotFoundError,
    PaymentNotFoundError,
    PeriodClosedError,
    PostedJournalNotFoundError,
    ProductNotFoundError,
    SaleNotFoundError,
    StockLocationNotFoundError,
    UnbalancedJournalError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.packaging.errors import FifoUnavailableError
from hesabyar.domain.reports.models import TRIAL_BALANCE_SOURCE_ENDPOINT
from hesabyar.domain.reports.statements import (
    CASH_TYPE_NAMES,
    NOTES_ACCOUNT_CODES,
    NOTES_PAYABLE_CODES,
    NOTES_RECEIVABLE_CODES,
    AccountBalanceRow,
    AccountTypeNode,
    aging_bucket,
    build_balance_sheet_sections,
    build_income_statement_sections,
    classify_cash_flow_section,
    classify_income_types,
    jalali_months_in_range,
)
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.contracts import Record
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_ERROR,
    ENGINE_NOT_FOUND,
    ENGINE_VALIDATION_FAILED,
    TrytonAdapterError,
)
from hesabyar.infrastructure.accounting.tryton.mapping import (
    CHECKBOOK_FIELDS,
    CHEQUE_FIELDS,
    account_record_to_view,
    analytic_account_record_to_view,
    analytic_line_record_to_view,
    checkbook_record_to_view,
    cheque_record_to_view,
    company_record_to_view,
    date_from_tryton,
    date_to_tryton,
    decimal_from_tryton,
    fiscal_year_record_to_view,
    inventory_line_record_to_view,
    inventory_record_to_view,
    invoice_line_record_to_view,
    invoice_record_to_view,
    line_record_to_view,
    location_record_to_view,
    money_from_tryton,
    money_to_tryton,
    move_line_record_to_view,
    move_record_to_draft_view,
    move_record_to_posted_view,
    order_point_record_to_view,
    out_shipment_cost_record_to_view,
    packaging_product_record_to_view,
    payment_journal_record_to_view,
    payment_record_to_view,
    period_record_to_view,
    purchase_request_record_to_view,
    quantity_from_tryton,
    quantity_to_tryton,
    sale_line_record_to_view,
    sale_record_to_view,
    sale_shipment_cost_record_to_view,
    statement_journal_record_to_view,
    statement_line_record_to_view,
    statement_record_to_view,
    stock_move_record_to_view,
)

_CHART_PAGE_SIZE = 500
_STOCK_PAGE_SIZE = 500
_TRIAL_BALANCE_PAGE_SIZE = 500

#: Source endpoint for statement reads through the account balance
#: Function field (balance sheet, income statement — US-550).
_ACCOUNT_BALANCE_SOURCE_ENDPOINT = "account.account.balance (Tryton REST)"

# Template fields read back for packaging products (single + batch).
_PACKAGING_TEMPLATE_FIELDS = [
    "name",
    "code",
    "type",
    "consumable",
    "purchasable",
    "cost_price_method",
    "default_uom",
]

_INVOICE_FIELDS = [
    "company",
    "type",
    "party",
    "invoice_address",
    "currency",
    "account",
    "journal",
    "invoice_date",
    "accounting_date",
    "state",
    "number",
    "description",
    "reference",
    "untaxed_amount",
    "tax_amount",
    "total_amount",
    "amount_to_pay",
    "move",
]
_INVOICE_LINE_FIELDS = [
    "invoice",
    "account",
    "quantity",
    "unit_price",
    "amount",
    "type",
    "description",
]
_PAYMENT_FIELDS = [
    "company",
    "number",
    "kind",
    "party",
    "date",
    "amount",
    "state",
    "reference",
    "journal",
    "currency",
    "line",
    "clearing_move",
]
_MOVE_LINE_FIELDS = [
    "move",
    "account",
    "party",
    "debit",
    "credit",
    "state",
    "maturity_date",
    "reconciliation",
    "description",
]
_PAYMENT_JOURNAL_FIELDS = [
    "company",
    "name",
    "process_method",
    "clearing_account",
    "clearing_journal",
]
_SALE_FIELDS = [
    "company",
    "party",
    "number",
    "sale_date",
    "state",
    "invoice_method",
    "shipment_method",
    "invoice_state",
    "shipment_state",
    "currency",
    "untaxed_amount",
    "total_amount",
    "description",
    "reference",
    "invoices",
]
_SALE_LINE_FIELDS = [
    "sale",
    "type",
    "product",
    "quantity",
    "unit_price",
    "amount",
    "description",
]
_STATEMENT_FIELDS = [
    "company",
    "name",
    "journal",
    "currency",
    "date",
    "state",
    "validation",
    "start_balance",
    "end_balance",
    "balance",
    "total_amount",
    "number_of_lines",
]
_STATEMENT_LINE_FIELDS = [
    "id",
    "statement",
    "date",
    "amount",
    "account",
    "party",
    "description",
    "number",
    "related_to",
]
_STATEMENT_JOURNAL_FIELDS = [
    "company",
    "name",
    "journal",
    "currency",
    "validation",
    "account",
    "bank_account",
    "active",
]

_INVOICE_STATES = ("draft", "validated", "posted", "paid", "cancelled")
_PAYMENT_STATES = ("draft", "submitted", "approved", "processing", "succeeded", "failed")
_STATEMENT_STATES = ("draft", "validated", "posted", "cancelled")
_SALE_STATES = ("draft", "quotation", "confirmed", "processing", "done", "cancelled")


class TrytonAccountingAdapter:
    """AccountingEnginePort backed by the Tryton 8.2 REST API."""

    def __init__(self, client: TrytonRestClient) -> None:
        self._client = client
        # Per-instance memo of the company IRR check: the adapter is
        # constructed per request (see get_engine_adapter), so a
        # company validated once in a request stays valid for the
        # rest of it. Without this, a multi-call request (the
        # cash-box report makes four engine calls) pays the company
        # read + currency lookup on every call — ~2-4s each against
        # a remote engine.
        self._irr_company_records: dict[int, Record] = {}
        self._irr_company_currencies: dict[int, int] = {}
        # Currency codes never change mid-request; memoize the lookup
        # the same way (the company check and the invoice reads both
        # call it).
        self._currency_code_memo: dict[int, str] = {}

    # -- companies / chart -------------------------------------------------

    def list_companies(self) -> Sequence[CompanyView]:
        records = self._client.search(
            "company.company",
            fields=["rec_name", "currency", "party"],
            order=[["id", "ASC"]],
        )
        currency_ids = sorted({int(r["currency"]) for r in records if r.get("currency")})
        codes = self._currency_codes(currency_ids)
        national_ids = self._company_party_national_ids(records)
        views = [
            company_record_to_view(
                record,
                codes.get(int(record["currency"]), "") if record.get("currency") else "",
                national_id=national_ids.get(int(record["id"])),
            )
            for record in records
        ]
        return sorted(views, key=lambda view: view.name)

    def _company_party_national_ids(
        self, company_records: list[Record]
    ) -> dict[int, str | None]:
        """The national id of each company, from its party's ``code``.

        The codebase's convention (US-531 issuer parties): a
        party's ``code`` is its national id. The cheque payee check
        (US-533) compares the payload's ``payee_national_id``
        against this. A company whose party has no code yields None
        — the payee check then fails closed to the review queue
        instead of guessing. A missing/unreadable party never fails
        the whole listing: those companies simply report None.
        """
        party_ids = sorted(
            {int(r["party"]) for r in company_records if r.get("party")}
        )
        by_company: dict[int, str | None] = {
            int(r["id"]): None for r in company_records
        }
        if not party_ids:
            return by_company
        try:
            party_records = self._client.search(
                "party.party",
                domain=[("id", "in", party_ids)],
                fields=["code"],
            )
        except Exception:  # noqa: BLE001 - national ids are advisory for the listing
            return by_company
        code_by_party = {
            int(r["id"]): (str(r["code"]).strip() or None)
            for r in party_records
            if r.get("code") is not None
        }
        for record in company_records:
            if record.get("party"):
                by_company[int(record["id"])] = code_by_party.get(
                    int(record["party"])
                )
        return by_company

    def _currency_codes(self, currency_ids: list[int]) -> dict[int, str]:
        if not currency_ids:
            return {}
        missing = [cid for cid in currency_ids if cid not in self._currency_code_memo]
        if missing:
            records = self._client.search(
                "currency.currency",
                domain=[("id", "in", missing)],
                fields=["code"],
            )
            for record in records:
                self._currency_code_memo[int(record["id"])] = str(record.get("code") or "")
        return {
            cid: self._currency_code_memo[cid]
            for cid in currency_ids
            if cid in self._currency_code_memo
        }

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "account.account",
                domain=[("company", "=", company.engine_id)],
                fields=["code", "name", "type", "parent", "closed", "party_required", "active"],
                limit=_CHART_PAGE_SIZE,
                offset=offset,
                order=[["code", "ASC"]],
                context={"company": company.engine_id},
            )
            records.extend(page)
            if len(page) < _CHART_PAGE_SIZE:
                break
            offset += len(page)
        type_ids = sorted({int(r["type"]) for r in records if r.get("type")})
        type_names = self._account_type_names(type_ids)
        views = [account_record_to_view(record, company, type_names) for record in records]
        # Deterministic code order: the chart root has no code (NULL in
        # Tryton) and sorts last, matching the server's NULLS LAST order.
        views.sort(key=lambda view: (view.code == "", view.code))
        return views

    def get_account_by_code(self, company: CompanyRef, code: str) -> AccountView | None:
        """Return one account by code, or None when the company has none.

        One search returning one record — the variance-account lookup
        in the cash-box completion path, which used to fetch the
        whole chart (~28s against a remote engine).
        """
        self._require_irr_company_for_reads(company.engine_id)
        records = self._client.search(
            "account.account",
            domain=[("company", "=", company.engine_id), ("code", "=", code)],
            fields=["code", "name", "type", "parent", "closed", "party_required", "active"],
            context={"company": company.engine_id},
        )
        if not records:
            return None
        record = records[0]
        type_ids = [int(record["type"])] if record.get("type") else []
        type_names = self._account_type_names(type_ids)
        return account_record_to_view(record, company, type_names)

    def _account_type_names(self, type_ids: list[int]) -> dict[int, str]:
        if not type_ids:
            return {}
        records = self._client.search(
            "account.account.type",
            domain=[("id", "in", type_ids)],
            fields=["name"],
        )
        return {int(r["id"]): str(r.get("name") or "") for r in records}

    # -- journal drafts ----------------------------------------------------

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        company_id = command.company.engine_id
        context = {"company": company_id}
        self._require_irr_company(company_id)
        self._resolve_accounts(command.lines, company_id, context)
        period_id = self._resolve_open_period(company_id, command.date, context)
        journal_id = self._resolve_journal(command, context)

        lines = [
            {
                "account": line.account.engine_id,
                "debit": money_to_tryton(line.debit),
                "credit": money_to_tryton(line.credit),
                "description": line.description,
                **({"party": line.party.engine_id} if line.party is not None else {}),
                **(
                    {"maturity_date": date_to_tryton(line.maturity_date)}
                    if line.maturity_date is not None
                    else {}
                ),
            }
            for line in command.lines
        ]
        record = self._client.create(
            "account.move",
            {
                "company": company_id,
                "journal": journal_id,
                "period": period_id,
                "date": date_to_tryton(command.date),
                "description": command.description,
                "lines": [["create", lines]],
            },
            fields=["id", "state"],
            context=context,
        )
        return JournalDraftRef(engine_id=int(record["id"]))

    # -- draft reads ---------------------------------------------------------

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        """Read a draft journal back as a domain view (None when missing)."""
        move = self._read_move(draft.engine_id)
        if move is None:
            return None
        company_id = int(move["company"])
        line_records = self._read_move_lines(draft.engine_id, company_id)
        return move_record_to_draft_view(
            move,
            self._read_move_period(move),
            [line_record_to_view(record) for record in line_records],
        )

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        """Return the company's draft journals (unposted moves), newest first."""
        records = self._client.search(
            "account.move",
            domain=[("company", "=", company.engine_id), ("state", "=", "draft")],
            fields=["company", "journal", "period", "date", "state", "description"],
            order=[["date", "DESC"], ["id", "DESC"]],
            context={"company": company.engine_id},
        )
        return self._draft_views(records)

    def _draft_views(self, records: list[Record]) -> list[JournalDraftView]:
        """Map move records to draft views, batching lines and periods."""
        if not records:
            return []
        move_ids = [int(record["id"]) for record in records]
        context = {"company": int(records[0]["company"])}
        lines_by_move = self._move_lines_by_move(move_ids, context)
        periods = self._period_views(sorted({int(r["period"]) for r in records}))
        views = []
        for record in records:
            period = periods.get(int(record["period"]))
            if period is None:
                raise PeriodClosedError(
                    f"Fiscal period {record['period']} of move {record['id']} "
                    "does not exist.",
                    details={"move_id": int(record["id"])},
                )
            views.append(
                move_record_to_draft_view(
                    record, period, lines_by_move.get(int(record["id"]), [])
                )
            )
        return views

    def _move_lines_by_move(
        self, move_ids: list[int], context: dict[str, Any]
    ) -> dict[int, list[JournalLineView]]:
        """Batch-read lines for several moves, grouped by move id."""
        if not move_ids:
            return {}
        records = self._client.search(
            "account.move.line",
            domain=[("move", "in", move_ids)],
            fields=["move", "account", "debit", "credit", "state", "description"],
            order=[["id", "ASC"]],
            context=context,
        )
        grouped: dict[int, list[JournalLineView]] = {}
        for record in records:
            grouped.setdefault(int(record["move"]), []).append(
                line_record_to_view(record)
            )
        return grouped

    def _period_views(self, period_ids: list[int]) -> dict[int, FiscalPeriodView]:
        """Batch-read fiscal periods by id."""
        if not period_ids:
            return {}
        records = self._client.search(
            "account.period",
            domain=[("id", "in", period_ids)],
            fields=["name", "start_date", "end_date", "state"],
        )
        return {int(r["id"]): period_record_to_view(r) for r in records}

    # -- draft update --------------------------------------------------------

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        """Update a draft journal, atomically, in one REST PUT.

        Only the fields present on the command change; ``lines`` replaces
        the draft's lines wholesale (delete + create in one One2Many
        command list). Pre-validation before the write (same checks as
        creation, raised as stable ledger errors): the draft must exist
        and still be a draft, every new account must exist for the
        company, a new date must fall in an open period (whose id goes
        into the same write — the engine enforces the date-in-period
        domain on write and never recomputes the period for a REST
        write), a lines-only update needs the move's current period
        open, and reconciled lines refuse a rewrite. After the write the
        draft is re-validated (the engine is the final enforcer: it
        re-marks line states on the rewrite) and read back.
        """
        draft_id = command.draft.engine_id
        move = self._read_move(draft_id)
        if move is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {draft_id} does not exist in the engine.",
                details={"draft_id": draft_id},
            )
        state = str(move.get("state") or "")
        if state == "posted":
            raise JournalAlreadyPostedError(
                f"Journal {draft_id} is already posted; a posted journal "
                "is immutable and can only be corrected by a reversal.",
                details={"draft_id": draft_id},
            )
        if state != "draft":
            raise InvalidJournalError(
                f"Journal {draft_id} is in unexpected state {state!r}; "
                "only a draft can be updated.",
                details={"draft_id": draft_id, "state": state},
            )
        company_id = int(move["company"])
        context = {"company": company_id}
        values: dict[str, Any] = {}
        if command.description is not None:
            values["description"] = command.description
        if command.date is not None:
            values["date"] = date_to_tryton(command.date)
            values["period"] = self._resolve_open_period(
                company_id, command.date, context
            )
        if command.lines is not None:
            self._resolve_accounts(command.lines, company_id, context)
            if command.date is None:
                # A date change moves the draft to a new open period; a
                # lines-only update stays in the current one, which the
                # engine refuses to modify once closed.
                self._require_open_move_period(move, draft_id)
            old_lines = self._read_move_lines(
                draft_id, company_id, fields=["id", "reconciliation"]
            )
            for record in old_lines:
                if record.get("reconciliation") is not None:
                    raise InvalidJournalError(
                        f"Journal draft {draft_id} has reconciled lines; "
                        "refusing to rewrite them.",
                        details={"draft_id": draft_id, "line_id": int(record["id"])},
                    )
            values["lines"] = [
                ["delete", [int(record["id"]) for record in old_lines]],
                [
                    "create",
                    [
                        {
                            "account": line.account.engine_id,
                            "debit": money_to_tryton(line.debit),
                            "credit": money_to_tryton(line.credit),
                            "description": line.description,
                            **({"party": line.party.engine_id} if line.party is not None else {}),
                            **(
                                {"maturity_date": date_to_tryton(line.maturity_date)}
                                if line.maturity_date is not None
                                else {}
                            ),
                        }
                        for line in command.lines
                    ],
                ],
            ]
        self._client.write(
            "account.move",
            draft_id,
            values,
            fields=["id", "state"],
            context=context,
        )
        summary = self.validate_journal_draft(command.draft)
        if not summary.valid:
            self._raise_for_validation(summary, draft_id)
        view = self.get_journal_draft(command.draft)
        if view is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {draft_id} vanished after its update.",
                details={"draft_id": draft_id},
            )
        return view

    def delete_journal_draft(self, draft: JournalDraftRef) -> None:
        """Delete a draft journal (an unposted move) from the engine.

        The move is read first: an unknown draft is a stable
        not-found error, and a posted move is refused
        (``LEDGER_POSTED_IMMUTABLE``) — only drafts can be deleted.
        The delete itself is the engine's REST DELETE on
        ``account.move``; a 404 between the read and the delete
        (concurrent delete) surfaces as the same not-found error.
        """
        draft_id = draft.engine_id
        record = self._client.read("account.move", draft_id, fields=["state"])
        if record is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {draft_id} does not exist.",
                details={"draft_id": draft_id},
            )
        if record.get("state") != "draft":
            raise JournalAlreadyPostedError(
                f"Journal {draft_id} is {record.get('state')}, not a draft; "
                "only draft journals can be deleted.",
                details={"draft_id": draft_id, "state": str(record.get("state"))},
            )
        if not self._client.delete("account.move", draft_id):
            raise JournalDraftNotFoundError(
                f"Journal draft {draft_id} does not exist.",
                details={"draft_id": draft_id},
            )

    def _require_open_move_period(self, move: Record, draft_id: int) -> None:
        """Fail fast when the draft's current fiscal period is not open."""
        period = self._read_move_period(move)
        if not period.is_open:
            raise PeriodClosedError(
                f"Fiscal period {period.name} of journal draft {draft_id} "
                f"is {period.state}; its lines cannot be modified.",
                details={
                    "draft_id": draft_id,
                    "period": period.name,
                    "state": period.state,
                },
            )

    def _require_irr_company(
        self,
        company_id: int,
        *,
        error_cls: type[LedgerError] = InvalidJournalError,
        subject: str = "Journal",
    ) -> int:
        """Fail fast if the company books in a currency other than IRR.

        Returns the company's currency id (callers that must pass the
        currency to the engine reuse it instead of reading it again).
        Memoized per adapter instance (one request): a failed check
        is never cached, so a retry re-reads.
        """
        cached = self._irr_company_currencies.get(company_id)
        if cached is not None:
            return cached
        record = self._client.read("company.company", company_id, fields=["currency"])
        if record is None:
            raise error_cls(
                f"Company {company_id} does not exist in the engine.",
                details={"company_id": company_id},
            )
        currency_id = record.get("currency")
        if currency_id is None:
            raise error_cls(
                f"Company {company_id} has no currency configured.",
                details={"company_id": company_id},
            )
        codes = self._currency_codes([int(currency_id)])
        if codes.get(int(currency_id)) != "IRR":
            raise error_cls(
                f"{subject} amounts are integer IRR; refusing to draft into a "
                f"company whose currency is {codes.get(int(currency_id))!r}.",
                details={"company_id": company_id},
            )
        self._irr_company_currencies[company_id] = int(currency_id)
        return int(currency_id)

    def _resolve_accounts(
        self,
        lines: Sequence[JournalDraftLine],
        company_id: int,
        context: dict[str, Any],
    ) -> None:
        """Verify every line account (exists, open, active, party rules).

        Shared by create and update: both fail with stable errors before
        anything reaches the engine. The party rule mirrors the engine's
        ``Line.check_account`` (``bool(party) == account.party_required``):
        a line on a party-required account MUST carry a party, and a
        line on any other account MUST NOT — the adapter rejects a
        mismatch here instead of letting the engine refuse the write.
        """
        wanted = sorted({line.account.engine_id for line in lines})
        records = self._client.search(
            "account.account",
            domain=[("id", "in", wanted), ("company", "=", company_id)],
            fields=["closed", "party_required", "active"],
            context=context,
        )
        found = {int(r["id"]): r for r in records}
        missing = [account_id for account_id in wanted if account_id not in found]
        if missing:
            raise AccountNotFoundError(
                f"Accounts not found for company {company_id}: {missing}.",
                details={"company_id": company_id, "missing_accounts": missing},
            )
        for account_id, record in found.items():
            if record.get("closed"):
                raise InvalidJournalError(
                    f"Account {account_id} is closed and cannot receive lines.",
                    details={"account_id": account_id},
                )
            if not record.get("active", True):
                raise InvalidJournalError(
                    f"Account {account_id} is inactive and cannot receive lines.",
                    details={"account_id": account_id},
                )
        for line in lines:
            record = found[line.account.engine_id]
            if record.get("party_required") and line.party is None:
                raise InvalidJournalError(
                    f"Account {line.account.engine_id} requires a party (tafsili); "
                    "the journal line carries none.",
                    details={"account_id": line.account.engine_id},
                )
            if not record.get("party_required") and line.party is not None:
                raise InvalidJournalError(
                    f"Account {line.account.engine_id} does not take a party; "
                    "the journal line carries one.",
                    details={
                        "account_id": line.account.engine_id,
                        "party_id": line.party.engine_id,
                    },
                )

    def _find_open_period(
        self,
        company_id: int,
        day: FiscalDate,
        context: dict[str, Any],
    ) -> int | None:
        """Return the id of the open period covering ``day`` (None when absent)."""
        records = self._client.search(
            "account.period",
            domain=[
                ("fiscalyear.company", "=", company_id),
                ("state", "=", "open"),
                ("start_date", "<=", date_to_tryton(day)),
                ("end_date", ">=", date_to_tryton(day)),
            ],
            fields=["name"],
            context=context,
        )
        if not records:
            return None
        return int(records[0]["id"])

    def _resolve_open_period(
        self,
        company_id: int,
        day: FiscalDate,
        context: dict[str, Any],
    ) -> int:
        period_id = self._find_open_period(company_id, day, context)
        if period_id is None:
            raise PeriodClosedError(
                f"No open fiscal period covers {day.to_gregorian_str()} "
                f"for company {company_id}.",
                details={"company_id": company_id, "date": day.to_gregorian_str()},
            )
        return period_id

    def _resolve_journal(
        self, command: CreateJournalDraft, context: dict[str, Any]
    ) -> int:
        if command.journal is not None:
            record = self._client.read(
                "account.journal", command.journal.engine_id, fields=["type"], context=context
            )
            if record is None:
                raise InvalidJournalError(
                    f"Journal {command.journal.engine_id} does not exist in the engine.",
                    details={"journal_id": command.journal.engine_id},
                )
            return command.journal.engine_id
        journals = self._client.search(
            "account.journal",
            domain=[("type", "=", "general")],
            fields=["name"],
            order=[["id", "ASC"]],
            context=context,
        )
        if len(journals) != 1:
            raise InvalidJournalError(
                "No journal was specified and the engine has "
                f"{len(journals)} general journals; specify one explicitly.",
                details={"general_journal_count": len(journals)},
            )
        return int(journals[0]["id"])

    # -- validation --------------------------------------------------------

    def validate_journal_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        issues: list[ValidationIssue] = []
        move = self._client.read(
            "account.move",
            draft.engine_id,
            fields=["company", "journal", "period", "date", "state", "description"],
        )
        if move is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {draft.engine_id} does not exist in the engine.",
                details={"draft_id": draft.engine_id},
            )
        company_id = int(move["company"])
        context = {"company": company_id}
        state = str(move.get("state") or "")
        if state == "posted":
            issues.append(
                ValidationIssue(
                    code="LEDGER_POSTED_IMMUTABLE",
                    message="Journal is already posted and immutable.",
                    field="state",
                )
            )
        elif state != "draft":
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_JOURNAL",
                    message=f"Journal is in unexpected state {state!r}.",
                    field="state",
                )
            )

        line_records = self._client.search(
            "account.move.line",
            domain=[("move", "=", draft.engine_id)],
            fields=["account", "debit", "credit", "state", "description"],
            context=context,
        )
        if len(line_records) < 2:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_JOURNAL",
                    message=f"Journal has {len(line_records)} line(s); at least two are required.",
                    field="lines",
                )
            )

        total_debit = MoneyIRR.zero()
        total_credit = MoneyIRR.zero()
        engine_says_balanced = True
        for record in line_records:
            debit = money_from_tryton(record.get("debit", 0))
            credit = money_from_tryton(record.get("credit", 0))
            total_debit = total_debit.add(debit)
            total_credit = total_credit.add(credit)
            if debit.is_negative or credit.is_negative:
                issues.append(
                    ValidationIssue(
                        code="LEDGER_INVALID_JOURNAL",
                        message=f"Line {record['id']} has a negative amount.",
                        field="lines",
                    )
                )
            if debit.is_positive and credit.is_positive:
                issues.append(
                    ValidationIssue(
                        code="LEDGER_INVALID_JOURNAL",
                        message=f"Line {record['id']} is two-sided (debit and credit).",
                        field="lines",
                    )
                )
            if debit.is_zero and credit.is_zero:
                issues.append(
                    ValidationIssue(
                        code="LEDGER_INVALID_JOURNAL",
                        message=f"Line {record['id']} carries a zero amount.",
                        field="lines",
                    )
                )
            if str(record.get("state") or "") != "valid":
                engine_says_balanced = False

        if total_debit.is_zero and total_credit.is_zero:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_JOURNAL",
                    message="Journal total is zero.",
                    field="lines",
                )
            )
        elif total_debit != total_credit:
            issues.append(
                ValidationIssue(
                    code="LEDGER_UNBALANCED",
                    message=(
                        f"Journal is unbalanced: debit {total_debit.amount} IRR "
                        f"!= credit {total_credit.amount} IRR "
                        f"(engine verdict: {'balanced' if engine_says_balanced else 'unbalanced'})."
                    ),
                    field="lines",
                )
            )
        elif not engine_says_balanced and line_records:
            issues.append(
                ValidationIssue(
                    code="LEDGER_UNBALANCED",
                    message=(
                        "HesabYar totals balance but the engine still marks "
                        "the draft's lines as unbalanced."
                    ),
                    field="lines",
                )
            )

        account_ids = sorted({int(r["account"]) for r in line_records})
        if account_ids:
            accounts = self._client.search(
                "account.account",
                domain=[("id", "in", account_ids), ("company", "=", company_id)],
                fields=["closed", "active"],
                context=context,
            )
            found = {int(r["id"]): r for r in accounts}
            for account_id in account_ids:
                record = found.get(account_id)
                if record is None:
                    issues.append(
                        ValidationIssue(
                            code="LEDGER_ACCOUNT_NOT_FOUND",
                            message=(
                                f"Line account {account_id} does not belong "
                                f"to company {company_id}."
                            ),
                            field="lines",
                        )
                    )
                elif record.get("closed") or not record.get("active", True):
                    issues.append(
                        ValidationIssue(
                            code="LEDGER_ACCOUNT_NOT_FOUND",
                            message=f"Line account {account_id} is closed or inactive.",
                            field="lines",
                        )
                    )

        period_id = move.get("period")
        if period_id is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_PERIOD_CLOSED",
                    message="Journal has no fiscal period.",
                    field="period",
                )
            )
        else:
            period_record = self._client.read(
                "account.period", int(period_id), fields=["name", "start_date", "end_date", "state"]
            )
            if period_record is None:
                issues.append(
                    ValidationIssue(
                        code="LEDGER_PERIOD_CLOSED",
                        message=f"Fiscal period {period_id} does not exist.",
                        field="period",
                    )
                )
            else:
                period = period_record_to_view(period_record)
                if not period.is_open:
                    issues.append(
                        ValidationIssue(
                            code="LEDGER_PERIOD_CLOSED",
                            message=f"Fiscal period {period.name} is {period.state}.",
                            field="period",
                        )
                    )
                move_date = date_from_tryton(move["date"])
                if not period.contains(move_date):
                    issues.append(
                        ValidationIssue(
                            code="LEDGER_PERIOD_CLOSED",
                            message=(
                                f"Journal date {move_date.to_gregorian_str()} is outside "
                                f"fiscal period {period.name}."
                            ),
                            field="date",
                        )
                    )

        if issues:
            return ValidationSummary.failed(issues)
        return ValidationSummary.ok()

    # -- posting / reversal ------------------------------------------------

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        """Post a draft journal atomically (design-eng spec §8.2).

        The draft is re-validated first, so unbalanced / unknown-account /
        closed-period attempts fail with stable ledger errors before the
        engine is asked. The engine remains the final enforcer: the ``post``
        action runs in a single engine transaction and either posts the
        whole move or nothing.
        """
        draft_id = command.draft.engine_id
        move = self._read_move(draft_id)
        if move is None:
            raise JournalDraftNotFoundError(
                f"Journal draft {draft_id} does not exist in the engine.",
                details={"draft_id": draft_id},
            )
        state = str(move.get("state") or "")
        if state == "posted":
            raise JournalAlreadyPostedError(
                f"Journal {draft_id} is already posted; a posted journal "
                "is immutable and can only be corrected by a reversal.",
                details={"draft_id": draft_id},
            )
        if state != "draft":
            raise InvalidJournalError(
                f"Journal {draft_id} is in unexpected state {state!r}; "
                "only a draft can be posted.",
                details={"draft_id": draft_id, "state": state},
            )
        summary = self.validate_journal_draft(command.draft)
        if not summary.valid:
            self._raise_for_validation(summary, draft_id)
        posted = self._post_move(draft_id, int(move["company"]))
        return PostedJournalRef(engine_id=int(posted["id"]))

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        """Reverse a posted journal with a new posted reversal journal.

        Replicates the Tryton ``account.move.cancel`` wizard via REST
        primitives (create + post): swapped lines, move/line ``origin``
        linkage, closed-period fallback to today's open period. See the
        module docstring for the atomicity and reconciliation limits.
        """
        original_id = command.posted.engine_id
        original = self._read_move(
            original_id,
            fields=[
                "company",
                "journal",
                "period",
                "date",
                "state",
                "description",
                "number",
                "origin",
            ],
        )
        if original is None:
            raise PostedJournalNotFoundError(
                f"Posted journal {original_id} does not exist in the engine.",
                details={"posted_id": original_id},
            )
        if str(original.get("state") or "") != "posted":
            raise InvalidJournalError(
                f"Journal {original_id} is not posted; only a posted "
                "journal can be reversed.",
                details={"posted_id": original_id, "state": str(original.get("state") or "")},
            )
        company_id = int(original["company"])
        context = {"company": company_id}
        lines = self._read_move_lines(
            original_id,
            company_id,
            fields=[
                "account",
                "debit",
                "credit",
                "description",
                "party",
                "maturity_date",
                "amount_second_currency",
            ],
        )
        if not lines:
            raise InvalidJournalError(
                f"Posted journal {original_id} has no lines; refusing to reverse it.",
                details={"posted_id": original_id},
            )
        date, period_id = self._reversal_target(company_id, original, context)
        description = command.description or str(original.get("description") or "")
        reversal_lines: list[dict[str, Any]] = []
        for record in lines:
            line: dict[str, Any] = {
                "account": int(record["account"]),
                # Reversal semantics (wizard `reversal=True`): swap the
                # sides, do not negate them.
                "debit": money_to_tryton(money_from_tryton(record.get("credit", 0))),
                "credit": money_to_tryton(money_from_tryton(record.get("debit", 0))),
                "description": str(record.get("description") or ""),
                "origin": f"account.move.line,{int(record['id'])}",
            }
            if record.get("party") is not None:
                line["party"] = int(record["party"])
            if record.get("maturity_date") is not None:
                line["maturity_date"] = record["maturity_date"]
            second_currency = record.get("amount_second_currency")
            if second_currency is not None:
                line["amount_second_currency"] = -second_currency
            reversal_lines.append(line)
        created = self._client.create(
            "account.move",
            {
                "company": company_id,
                "journal": int(original["journal"]),
                "period": period_id,
                "date": date_to_tryton(date),
                "description": description,
                "origin": f"account.move,{original_id}",
                "lines": [["create", reversal_lines]],
            },
            fields=["id", "state"],
            context=context,
        )
        reversal_id = int(created["id"])
        try:
            self._post_move(reversal_id, company_id)
        except TrytonAdapterError as exc:
            # The reversal draft survives a failed post (two REST calls,
            # not one transaction); hand the caller its id for recovery.
            exc.details["reversal_draft_id"] = reversal_id
            exc.details["original_posted_id"] = original_id
            raise
        return PostedJournalRef(engine_id=reversal_id)

    def _reversal_target(
        self,
        company_id: int,
        original: Record,
        context: dict[str, Any],
    ) -> tuple[FiscalDate, int]:
        """Pick the reversal's date and period, mirroring the cancel wizard.

        Same date and period as the original while its period is open;
        otherwise today's date in the open period covering today.
        """
        period_id = int(original["period"])
        period_record = self._client.read(
            "account.period", period_id, fields=["name", "start_date", "end_date", "state"]
        )
        if period_record is None:
            raise PeriodClosedError(
                f"Fiscal period {period_id} of journal {original['id']} does not exist.",
                details={"posted_id": int(original["id"]), "period_id": period_id},
            )
        if period_record_to_view(period_record).is_open:
            return date_from_tryton(original["date"]), period_id
        # Closed period: the wizard re-dates the reversal to today.
        today = FiscalDate.from_gregorian(datetime.date.today())
        return today, self._resolve_open_period(company_id, today, context)

    def _post_move(self, move_id: int, company_id: int) -> Record:
        """Call the ``post`` model action and verify the engine confirms it."""
        result = self._client.action(
            "account.move",
            "post",
            move_id,
            fields=["id", "number", "state", "post_date"],
            context={"company": company_id},
        )
        if result is None:
            result = self._read_move(
                move_id, fields=["id", "number", "state", "post_date"]
            )
        if not isinstance(result, dict) or str(result.get("state") or "") != "posted":
            raise TrytonAdapterError(
                f"Engine did not confirm move {move_id} as posted.",
                code=ENGINE_ERROR,
                details={"move_id": move_id},
            )
        return result

    def _raise_for_validation(self, summary: ValidationSummary, draft_id: int) -> None:
        """Translate a failed validation summary into its stable ledger error."""
        first = summary.issues[0]
        details: dict[str, Any] = {
            "draft_id": draft_id,
            "codes": list(summary.codes()),
        }
        if first.code == "LEDGER_UNBALANCED":
            raise UnbalancedJournalError(first.message, details=details)
        if first.code == "LEDGER_ACCOUNT_NOT_FOUND":
            raise AccountNotFoundError(first.message, details=details)
        if first.code == "LEDGER_PERIOD_CLOSED":
            raise PeriodClosedError(first.message, details=details)
        if first.code == "LEDGER_POSTED_IMMUTABLE":
            raise JournalAlreadyPostedError(first.message, details=details)
        raise InvalidJournalError(first.message, details=details)

    # -- posted-journal read ------------------------------------------------

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        """Read a posted journal back as a domain view (None when missing)."""
        move = self._read_move(
            posted.engine_id,
            fields=[
                "company",
                "journal",
                "period",
                "date",
                "state",
                "description",
                "number",
                "post_date",
                "origin",
            ],
        )
        if move is None:
            return None
        if str(move.get("state") or "") != "posted":
            raise InvalidJournalError(
                f"Journal {posted.engine_id} is not posted "
                f"(state {str(move.get('state') or '')!r}); "
                "get_journal reads posted journals only.",
                details={"posted_id": posted.engine_id},
            )
        company_id = int(move["company"])
        period = self._read_move_period(move)
        lines = self._read_move_lines(company_id=company_id, move_id=posted.engine_id)
        return move_record_to_posted_view(
            move, period, [line_record_to_view(record) for record in lines]
        )

    # -- fiscal years / periods ---------------------------------------------

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        records = self._client.search(
            "account.fiscalyear",
            domain=[("company", "=", company.engine_id)],
            fields=["name", "start_date", "end_date", "state", "company"],
            order=[["start_date", "DESC"]],
            context={"company": company.engine_id},
        )
        return [fiscal_year_record_to_view(record) for record in records]

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        if fiscal_year is not None:
            domain: list[Any] = [("fiscalyear", "=", fiscal_year.engine_id)]
        else:
            domain = [("fiscalyear.company", "=", company.engine_id)]
        records = self._client.search(
            "account.period",
            domain=domain,
            fields=["name", "start_date", "end_date", "state", "type", "fiscalyear"],
            order=[["start_date", "ASC"]],
            context={"company": company.engine_id},
        )
        return [period_record_to_view(record) for record in records]

    # -- trial balance (US-403) ----------------------------------------------

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        """Aggregate posted move lines into a trial balance.

        One company-scoped ``account.move.line`` search, filtered on the
        move (``move.state = 'posted'``, ``move.company``, ``move.date``
        within the range) — draft moves never contribute. The search is
        paged through in full (``_TRIAL_BALANCE_PAGE_SIZE`` at a time
        until a short page); totals are computed only over the complete
        line set, never from a partial page. Account codes/names are
        batch-read for the accounts that actually posted. The company
        must exist and book in IRR (HesabYar is Rial-only): a missing
        company is ``ENGINE_NOT_FOUND``, a non-IRR company is
        ``ENGINE_VALIDATION_FAILED``.
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._require_irr_company_for_reads(company.engine_id)
        context = {"company": company.engine_id}
        lines: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "account.move.line",
                domain=[
                    ("move.company", "=", company.engine_id),
                    ("move.state", "=", "posted"),
                    ("move.date", ">=", date_to_tryton(date_from)),
                    ("move.date", "<=", date_to_tryton(date_to)),
                ],
                fields=["move", "account", "debit", "credit"],
                limit=_TRIAL_BALANCE_PAGE_SIZE,
                offset=offset,
                order=[["account", "ASC"], ["id", "ASC"]],
                context=context,
            )
            lines.extend(page)
            if len(page) < _TRIAL_BALANCE_PAGE_SIZE:
                break
            offset += len(page)
        totals: dict[int, tuple[MoneyIRR, MoneyIRR]] = {}
        move_ids: set[int] = set()
        total_debit = MoneyIRR.zero()
        total_credit = MoneyIRR.zero()
        for record in lines:
            account_id = int(record["account"])
            debit = money_from_tryton(record.get("debit", 0))
            credit = money_from_tryton(record.get("credit", 0))
            move_ids.add(int(record["move"]))
            previous_debit, previous_credit = totals.get(
                account_id, (MoneyIRR.zero(), MoneyIRR.zero())
            )
            totals[account_id] = (
                previous_debit.add(debit),
                previous_credit.add(credit),
            )
            total_debit = total_debit.add(debit)
            total_credit = total_credit.add(credit)
        names = self._account_code_names(sorted(totals), context)
        views = [
            TrialBalanceLineView(
                account=AccountRef(engine_id=account_id),
                code=names[account_id][0],
                name=names[account_id][1],
                total_debit=debit,
                total_credit=credit,
            )
            for account_id, (debit, credit) in sorted(
                totals.items(), key=lambda item: names[item[0]][0]
            )
        ]
        return TrialBalanceView(
            company=company,
            date_from=date_from,
            date_to=date_to,
            lines=tuple(views),
            total_debit=total_debit,
            total_credit=total_credit,
            move_count=len(move_ids),
            line_count=len(lines),
            source_endpoint=TRIAL_BALANCE_SOURCE_ENDPOINT,
        )

    # -- financial reports (Phase 5-6, US-550) -----------------------------
    #
    # All five reports read LIVE from the engine, never from a HesabYar
    # cache (§11.1). Two read shapes:
    #
    # - posted ``account.move.line`` rows, paged through in full (general
    #   ledger detail, cash flow, notes aging) — the trial-balance
    #   pattern;
    # - the ``account.account`` balance/debit/credit Function fields
    #   with the report context (balance sheet, income statement,
    #   general-ledger summary + opening balances) — the same figures
    #   Tryton's own reference reports render (the general ledger and
    #   type-statement templates read these fields in the same
    #   company/fiscalyear/date/posted context). Reading Function
    #   fields via REST is an established adapter pattern (invoice
    #   ``total_amount`` / ``amount_to_pay``, product quantities).
    #
    # Context dates travel as real dates (the wire encoder turns them
    # into Tryton date dicts), so ``query_get`` sees the same context
    # the reference reports use: ``date`` for a point in time,
    # ``from_date``/``to_date`` for a period, ``posted`` True always
    # (HesabYar reports are posted-only, like the trial balance).

    def _report_context(
        self, company_id: int, **extra: Any
    ) -> dict[str, Any]:
        """Report context for Function-field reads: company + posted-only."""
        context: dict[str, Any] = {"company": company_id, "posted": True}
        context.update(extra)
        return context

    def _account_type_tree(self, company_id: int) -> list[AccountTypeNode]:
        """Read the company's account-type tree (statement grouping)."""
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "account.account.type",
                domain=[("company", "=", company_id)],
                fields=["name", "parent", "statement", "sequence"],
                limit=_CHART_PAGE_SIZE,
                offset=offset,
                order=[["sequence", "ASC"], ["name", "ASC"]],
                context={"company": company_id},
            )
            records.extend(page)
            if len(page) < _CHART_PAGE_SIZE:
                break
            offset += len(page)
        return [
            AccountTypeNode(
                type_id=int(record["id"]),
                name=str(record.get("name") or ""),
                parent_id=(
                    int(record["parent"]) if record.get("parent") is not None else None
                ),
                statement=(
                    str(record["statement"])
                    if record.get("statement") is not None
                    else None
                ),
                sequence=int(record.get("sequence") or 0),
            )
            for record in records
        ]

    def _account_balance_records(
        self, company_id: int, context: dict[str, Any]
    ) -> list[Record]:
        """Read every account's code/name/type/balance in a report context.

        The ``balance`` Function field is computed by the engine with
        the context: a ``date`` context gives the point-in-time
        balance (cumulative over closed fiscal years), a
        ``from_date``/``to_date`` context the period's net activity.
        """
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "account.account",
                domain=[("company", "=", company_id)],
                fields=["code", "name", "type", "balance"],
                limit=_CHART_PAGE_SIZE,
                offset=offset,
                order=[["code", "ASC"]],
                context=context,
            )
            records.extend(page)
            if len(page) < _CHART_PAGE_SIZE:
                break
            offset += len(page)
        return records

    def _account_balance_rows(
        self,
        company_id: int,
        context: dict[str, Any],
        comparison_context: dict[str, Any] | None,
    ) -> list[AccountBalanceRow]:
        """Merge a balance read with its comparison read, per account."""
        current = {
            int(record["id"]): record
            for record in self._account_balance_records(company_id, context)
        }
        comparison: dict[int, Record] = {}
        if comparison_context is not None:
            comparison = {
                int(record["id"]): record
                for record in self._account_balance_records(company_id, comparison_context)
            }
        rows: list[AccountBalanceRow] = []
        for account_id, record in current.items():
            comparison_record = comparison.get(account_id)
            rows.append(
                AccountBalanceRow(
                    account_id=account_id,
                    code=str(record.get("code") or ""),
                    name=str(record.get("name") or ""),
                    type_id=(
                        int(record["type"])
                        if record.get("type") is not None
                        else None
                    ),
                    balance=money_from_tryton(record.get("balance", 0)),
                    balance_cmp=(
                        money_from_tryton(comparison_record.get("balance", 0))
                        if comparison_record is not None
                        else None
                    ),
                )
            )
        rows.sort(key=lambda row: (row.code == "", row.code))
        return rows

    def _posted_move_lines(
        self,
        company_id: int,
        domain: list[Any],
        fields: list[str],
    ) -> list[Record]:
        """Page through posted move lines matching a domain, in full."""
        lines: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "account.move.line",
                domain=[
                    ("move.company", "=", company_id),
                    ("move.state", "=", "posted"),
                    *domain,
                ],
                fields=fields,
                limit=_TRIAL_BALANCE_PAGE_SIZE,
                offset=offset,
                order=[["account", "ASC"], ["id", "ASC"]],
                context={"company": company_id},
            )
            lines.extend(page)
            if len(page) < _TRIAL_BALANCE_PAGE_SIZE:
                break
            offset += len(page)
        return lines

    def _move_numbers(
        self, move_ids: list[int], company_id: int
    ) -> dict[int, tuple[str, str]]:
        """Batch-read (number, description) for move ids."""
        if not move_ids:
            return {}
        records = self._client.search(
            "account.move",
            domain=[("id", "in", move_ids)],
            fields=["number", "description"],
            context={"company": company_id},
        )
        return {
            int(record["id"]): (
                str(record.get("number") or ""),
                str(record.get("description") or ""),
            )
            for record in records
        }

    def get_general_ledger(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        account: AccountRef | None = None,
    ) -> GeneralLedgerView:
        """General ledger over posted moves: summary or one account's lines.

        Summary mode (no account) reads the period's per-account
        debit/credit through the account Function fields plus the
        opening balances at the day before ``date_from`` — two
        searches, no line paging. Detail mode pages the account's
        posted lines in full and builds running balances from the
        engine-read opening balance.
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._require_irr_company_for_reads(company.engine_id)
        day_before = FiscalDate.from_gregorian(
            date_from.gregorian_date - datetime.timedelta(days=1)
        )
        opening_context = self._report_context(
            company.engine_id, date=date_to_tryton(day_before)
        )
        if account is None:
            period_context = self._report_context(
                company.engine_id,
                from_date=date_to_tryton(date_from),
                to_date=date_to_tryton(date_to),
            )
            # One search for the period's per-account debit/credit
            # (the account Function fields in the period context),
            # one for the opening balances at the day before.
            period_records: dict[int, Record] = {}
            offset = 0
            while True:
                page = self._client.search(
                    "account.account",
                    domain=[("company", "=", company.engine_id)],
                    fields=["code", "name", "debit", "credit"],
                    limit=_CHART_PAGE_SIZE,
                    offset=offset,
                    order=[["code", "ASC"]],
                    context=period_context,
                )
                for record in page:
                    period_records[int(record["id"])] = record
                if len(page) < _CHART_PAGE_SIZE:
                    break
                offset += len(page)
            opening = {
                int(record["id"]): money_from_tryton(record.get("balance", 0))
                for record in self._account_balance_records(
                    company.engine_id, opening_context
                )
            }
            views: list[GeneralLedgerAccountView] = []
            total_debit = MoneyIRR.zero()
            total_credit = MoneyIRR.zero()
            for account_id, record in sorted(
                period_records.items(),
                key=lambda item: (
                    str(item[1].get("code") or "") == "",
                    str(item[1].get("code") or ""),
                ),
            ):
                debit = money_from_tryton(record.get("debit", 0))
                credit = money_from_tryton(record.get("credit", 0))
                if debit.is_zero and credit.is_zero:
                    continue
                total_debit = total_debit.add(debit)
                total_credit = total_credit.add(credit)
                views.append(
                    GeneralLedgerAccountView(
                        account=AccountRef(engine_id=account_id),
                        code=str(record.get("code") or ""),
                        name=str(record.get("name") or ""),
                        opening_balance=opening.get(account_id, MoneyIRR(0)),
                        total_debit=debit,
                        total_credit=credit,
                    )
                )
            return GeneralLedgerView(
                company=company,
                date_from=date_from,
                date_to=date_to,
                accounts=tuple(views),
                total_debit=total_debit,
                total_credit=total_credit,
                source_endpoint=TRIAL_BALANCE_SOURCE_ENDPOINT,
            )

        record = self._client.read(
            "account.account",
            account.engine_id,
            fields=["company", "code", "name"],
            context={"company": company.engine_id},
        )
        if record is None or int(record.get("company") or -1) != company.engine_id:
            raise AccountNotFoundError(
                f"Account {account.engine_id} does not exist for company {company.engine_id}.",
                details={"account_id": account.engine_id, "company_id": company.engine_id},
            )
        opening_record = self._client.read(
            "account.account",
            account.engine_id,
            fields=["balance"],
            context=opening_context,
        )
        opening_balance = money_from_tryton(
            (opening_record or {}).get("balance", 0)
        )
        lines = self._posted_move_lines(
            company.engine_id,
            [
                ("account", "=", account.engine_id),
                ("move.date", ">=", date_to_tryton(date_from)),
                ("move.date", "<=", date_to_tryton(date_to)),
            ],
            ["move", "date", "debit", "credit", "description"],
        )
        move_ids = sorted({int(line["move"]) for line in lines})
        moves = self._move_numbers(move_ids, company.engine_id)
        total_debit = MoneyIRR.zero()
        total_credit = MoneyIRR.zero()
        running = opening_balance.amount
        views_lines: list[GeneralLedgerLineView] = []
        for line in sorted(
            lines,
            key=lambda item: (
                str(item.get("date") or ""),
                int(item["move"]),
                int(item["id"]),
            ),
        ):
            debit = money_from_tryton(line.get("debit", 0))
            credit = money_from_tryton(line.get("credit", 0))
            total_debit = total_debit.add(debit)
            total_credit = total_credit.add(credit)
            running += debit.amount - credit.amount
            move_id = int(line["move"])
            number, move_description = moves.get(move_id, ("", ""))
            line_date = line.get("date")
            if line_date is None:
                raise TrytonAdapterError(
                    "A posted move line has no effective date.",
                    code=ENGINE_ERROR,
                    details={"move_id": move_id, "line_id": int(line["id"])},
                )
            views_lines.append(
                GeneralLedgerLineView(
                    move_id=move_id,
                    move_number=number,
                    date=date_from_tryton(line_date),
                    description=str(line.get("description") or "") or move_description,
                    debit=debit,
                    credit=credit,
                    balance=MoneyIRR(running),
                )
            )
        return GeneralLedgerView(
            company=company,
            date_from=date_from,
            date_to=date_to,
            accounts=(
                GeneralLedgerAccountView(
                    account=account,
                    code=str(record.get("code") or ""),
                    name=str(record.get("name") or ""),
                    opening_balance=opening_balance,
                    lines=tuple(views_lines),
                    total_debit=total_debit,
                    total_credit=total_credit,
                ),
            ),
            total_debit=total_debit,
            total_credit=total_credit,
            move_count=len(move_ids),
            line_count=len(lines),
            source_endpoint=TRIAL_BALANCE_SOURCE_ENDPOINT,
        )

    def get_balance_sheet(
        self,
        company: CompanyRef,
        as_of: FiscalDate,
        comparison_as_of: FiscalDate | None = None,
    ) -> BalanceSheetView:
        """Balance sheet from the engine's point-in-time account balances."""
        self._require_irr_company_for_reads(company.engine_id)
        types = self._account_type_tree(company.engine_id)
        context = self._report_context(
            company.engine_id, date=date_to_tryton(as_of)
        )
        comparison_context = (
            self._report_context(
                company.engine_id, date=date_to_tryton(comparison_as_of)
            )
            if comparison_as_of is not None
            else None
        )
        rows = self._account_balance_rows(company.engine_id, context, comparison_context)
        built = build_balance_sheet_sections(types, rows)
        return BalanceSheetView(
            company=company,
            as_of=as_of,
            comparison_as_of=comparison_as_of,
            sections=built.sections,
            total_assets=built.total_assets,
            total_liabilities=built.total_liabilities,
            total_equity=built.total_equity,
            total_assets_cmp=built.total_assets_cmp,
            total_liabilities_cmp=built.total_liabilities_cmp,
            total_equity_cmp=built.total_equity_cmp,
            balanced=built.balanced,
            balanced_cmp=built.balanced_cmp,
            source_endpoint=_ACCOUNT_BALANCE_SOURCE_ENDPOINT,
        )

    def get_income_statement(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        comparison_from: FiscalDate | None = None,
        comparison_to: FiscalDate | None = None,
    ) -> IncomeStatementView:
        """Income statement from the engine's period account balances.

        The monthly breakdown pages the period's posted lines on
        income-statement accounts once and buckets them by Jalali
        month, classified by each account type's sign over the whole
        period — so the monthly figures always sum to the statement
        totals (asserted as ``monthly_ties_out``).
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        if (comparison_from is None) != (comparison_to is None):
            raise ValueError("comparison_from and comparison_to go together.")
        if (
            comparison_from is not None
            and comparison_to is not None
            and comparison_to < comparison_from
        ):
            raise ValueError("comparison_to must be on or after comparison_from.")
        self._require_irr_company_for_reads(company.engine_id)
        types = self._account_type_tree(company.engine_id)
        context = self._report_context(
            company.engine_id,
            from_date=date_to_tryton(date_from),
            to_date=date_to_tryton(date_to),
        )
        comparison_context = (
            self._report_context(
                company.engine_id,
                from_date=date_to_tryton(comparison_from),
                to_date=date_to_tryton(comparison_to),
            )
            if comparison_from is not None and comparison_to is not None
            else None
        )
        rows = self._account_balance_rows(company.engine_id, context, comparison_context)
        built = build_income_statement_sections(types, rows)
        classification = classify_income_types(types, rows)
        type_by_id = {node.type_id: node for node in types}
        income_account_ids = [
            row.account_id
            for row in rows
            if row.type_id is not None
            and type_by_id.get(row.type_id) is not None
            and type_by_id[row.type_id].statement == "income"
        ]
        account_kind = {
            row.account_id: classification.get(row.type_id)
            for row in rows
            if row.type_id is not None
        }
        months = jalali_months_in_range(date_from, date_to)
        monthly_revenue: dict[str, int] = {label: 0 for label, _, _ in months}
        monthly_expense: dict[str, int] = {label: 0 for label, _, _ in months}
        if income_account_ids:
            lines = self._posted_move_lines(
                company.engine_id,
                [
                    ("account", "in", income_account_ids),
                    ("move.date", ">=", date_to_tryton(date_from)),
                    ("move.date", "<=", date_to_tryton(date_to)),
                ],
                ["account", "date", "debit", "credit"],
            )
            for line in lines:
                kind = account_kind.get(int(line["account"]))
                if kind is None:
                    continue
                line_date = line.get("date")
                if line_date is None:
                    raise TrytonAdapterError(
                        "A posted move line has no effective date.",
                        code=ENGINE_ERROR,
                        details={"line_id": int(line["id"])},
                    )
                jalali = date_from_tryton(line_date).jalali_date
                label = f"{jalali.year:04d}-{jalali.month:02d}"
                if label not in monthly_revenue:
                    continue
                debit = money_from_tryton(line.get("debit", 0)).amount
                credit = money_from_tryton(line.get("credit", 0)).amount
                if kind == "revenue":
                    monthly_revenue[label] += credit - debit
                else:
                    monthly_expense[label] += debit - credit
        monthly = tuple(
            MonthlyRevenueExpenseView(
                jalali_month=label,
                month_start=start,
                month_end=end,
                revenue=MoneyIRR(monthly_revenue[label]),
                expense=MoneyIRR(monthly_expense[label]),
            )
            for label, start, end in months
        )
        monthly_ties_out = (
            sum(view.revenue.amount for view in monthly) == built.total_revenue.amount
            and sum(view.expense.amount for view in monthly) == built.total_expense.amount
        )
        return IncomeStatementView(
            company=company,
            date_from=date_from,
            date_to=date_to,
            comparison_from=comparison_from,
            comparison_to=comparison_to,
            sections=built.sections,
            total_revenue=built.total_revenue,
            total_expense=built.total_expense,
            net_income=built.net_income,
            total_revenue_cmp=built.total_revenue_cmp,
            total_expense_cmp=built.total_expense_cmp,
            net_income_cmp=built.net_income_cmp,
            monthly=monthly,
            monthly_ties_out=monthly_ties_out,
            source_endpoint=_ACCOUNT_BALANCE_SOURCE_ENDPOINT,
        )

    def get_cash_flow(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> CashFlowView:
        """Direct-method cash flow from the engine's posted cash moves.

        Cash accounts are the accounts whose type is cash or petty
        cash (universal-chart type names). Each cash line is
        classified by its largest non-cash counterpart's account code
        (``domain/reports/statements.py``); cash-to-cash transfers
        are excluded from every section.
        """
        if date_to < date_from:
            raise ValueError("date_to must be on or after date_from.")
        self._require_irr_company_for_reads(company.engine_id)
        types = self._account_type_tree(company.engine_id)
        cash_type_ids = {
            node.type_id for node in types if node.name in CASH_TYPE_NAMES
        }
        chart = self._client.search(
            "account.account",
            domain=[("company", "=", company.engine_id)],
            fields=["code", "name", "type"],
            context={"company": company.engine_id},
        )
        account_code = {
            int(record["id"]): str(record.get("code") or "") for record in chart
        }
        cash_accounts = [
            record
            for record in chart
            if record.get("type") is not None
            and int(record["type"]) in cash_type_ids
        ]
        cash_ids = [int(record["id"]) for record in cash_accounts]
        cash_codes = tuple(
            sorted(str(record.get("code") or "") for record in cash_accounts)
        )
        day_before = FiscalDate.from_gregorian(
            date_from.gregorian_date - datetime.timedelta(days=1)
        )
        opening_cash = self._cash_total(
            company.engine_id, cash_ids, day_before
        )
        closing_cash = self._cash_total(company.engine_id, cash_ids, date_to)
        sections_data: dict[str, dict[str, int]] = {
            "operating": {"inflow": 0, "outflow": 0, "count": 0},
            "investing": {"inflow": 0, "outflow": 0, "count": 0},
            "financing": {"inflow": 0, "outflow": 0, "count": 0},
        }
        transfer_count = 0
        unclassified_count = 0
        unclassified_total = 0
        if cash_ids:
            cash_lines = self._posted_move_lines(
                company.engine_id,
                [
                    ("account", "in", cash_ids),
                    ("move.date", ">=", date_to_tryton(date_from)),
                    ("move.date", "<=", date_to_tryton(date_to)),
                ],
                ["move", "account", "debit", "credit"],
            )
            move_ids = sorted({int(line["move"]) for line in cash_lines})
            counterpart_lines: dict[int, list[Record]] = {}
            if move_ids:
                offset = 0
                cash_id_set = set(cash_ids)
                while True:
                    page = self._client.search(
                        "account.move.line",
                        domain=[("move", "in", move_ids)],
                        fields=["move", "account", "debit", "credit"],
                        limit=_TRIAL_BALANCE_PAGE_SIZE,
                        offset=offset,
                        order=[["move", "ASC"], ["id", "ASC"]],
                        context={"company": company.engine_id},
                    )
                    for record in page:
                        if int(record["account"]) in cash_id_set:
                            continue
                        counterpart_lines.setdefault(int(record["move"]), []).append(record)
                    if len(page) < _TRIAL_BALANCE_PAGE_SIZE:
                        break
                    offset += len(page)
            for line in cash_lines:
                counterparts = counterpart_lines.get(int(line["move"]), [])
                if not counterparts:
                    transfer_count += 1
                    continue
                largest = max(
                    counterparts,
                    key=lambda item: abs(
                        money_from_tryton(item.get("debit", 0)).amount
                        - money_from_tryton(item.get("credit", 0)).amount
                    ),
                )
                section = classify_cash_flow_section(
                    account_code.get(int(largest["account"]), "")
                )
                debit = money_from_tryton(line.get("debit", 0)).amount
                credit = money_from_tryton(line.get("credit", 0)).amount
                if section == "transfer":
                    transfer_count += 1
                elif section == "unclassified":
                    unclassified_count += 1
                    unclassified_total += debit - credit
                else:
                    bucket = sections_data[section]
                    bucket["inflow"] += debit
                    bucket["outflow"] += credit
                    bucket["count"] += 1
        sections = tuple(
            CashFlowSectionView(
                key=key,
                title=title,
                inflow=MoneyIRR(sections_data[key]["inflow"]),
                outflow=MoneyIRR(sections_data[key]["outflow"]),
                net=MoneyIRR(
                    sections_data[key]["inflow"] - sections_data[key]["outflow"]
                ),
                line_count=sections_data[key]["count"],
            )
            for key, title in (
                ("operating", "Operating activities"),
                ("investing", "Investing activities"),
                ("financing", "Financing activities"),
            )
        )
        net_change = MoneyIRR(closing_cash.amount - opening_cash.amount)
        section_net = sum(section.net.amount for section in sections)
        return CashFlowView(
            company=company,
            date_from=date_from,
            date_to=date_to,
            opening_cash=opening_cash,
            closing_cash=closing_cash,
            sections=sections,
            net_change=net_change,
            ties_out=(
                opening_cash.amount + section_net == closing_cash.amount
                and unclassified_count == 0
            ),
            cash_accounts=cash_codes,
            transfer_count=transfer_count,
            unclassified_count=unclassified_count,
            unclassified_total=MoneyIRR(unclassified_total),
            source_endpoint=TRIAL_BALANCE_SOURCE_ENDPOINT,
        )

    def _cash_total(
        self, company_id: int, cash_ids: list[int], as_of: FiscalDate
    ) -> MoneyIRR:
        """Sum of the cash accounts' engine balances at a date (0 when none)."""
        if not cash_ids:
            return MoneyIRR(0)
        records = self._client.search(
            "account.account",
            domain=[("id", "in", cash_ids)],
            fields=["balance"],
            context=self._report_context(
                company_id, date=date_to_tryton(as_of)
            ),
        )
        return MoneyIRR(
            sum(money_from_tryton(record.get("balance", 0)).amount for record in records)
        )

    def get_notes_balance(
        self,
        company: CompanyRef,
        as_of: FiscalDate,
        instrument: str = "both",
        party: PartyRef | None = None,
    ) -> NotesBalanceView:
        """Notes receivable/payable balances with aging (cheque report 7).

        Open posted lines on the notes accounts, aged by the Tryton
        aged-balance rule: a line ages from its maturity date
        (falling back to the move date) and is open while
        unreconciled or reconciled after the as-of date.
        """
        if instrument not in {"both", "receivable", "payable"}:
            raise ValueError(
                f"instrument must be 'both', 'receivable' or 'payable', got {instrument!r}."
            )
        self._require_irr_company_for_reads(company.engine_id)
        wanted_codes = (
            NOTES_ACCOUNT_CODES
            if instrument == "both"
            else (
                NOTES_RECEIVABLE_CODES
                if instrument == "receivable"
                else NOTES_PAYABLE_CODES
            )
        )
        notes_records = self._client.search(
            "account.account",
            domain=[("company", "=", company.engine_id), ("code", "in", list(wanted_codes))],
            fields=["code", "name"],
            context={"company": company.engine_id},
        )
        notes_accounts = {
            int(record["id"]): record for record in notes_records
        }
        accounts_read = tuple(
            sorted(str(record.get("code") or "") for record in notes_records)
        )
        if not notes_accounts:
            return NotesBalanceView(
                company=company,
                as_of=as_of,
                instrument=instrument,
                totals=NotesBalanceRowView(
                    party_id=None,
                    party_name="",
                    instrument=instrument,
                    account_code="",
                    account_name="",
                ),
                accounts_read=accounts_read,
                source_endpoint=TRIAL_BALANCE_SOURCE_ENDPOINT,
            )
        domain: list[Any] = [
            ("account", "in", sorted(notes_accounts)),
            ("move.date", "<=", date_to_tryton(as_of)),
        ]
        if party is not None:
            domain.append(("party", "=", party.engine_id))
        lines = self._posted_move_lines(
            company.engine_id,
            domain,
            ["account", "party", "maturity_date", "date", "debit", "credit", "reconciliation"],
        )
        reconciliation_ids = sorted(
            {
                int(line["reconciliation"])
                for line in lines
                if line.get("reconciliation") is not None
            }
        )
        reconciliation_dates: dict[int, FiscalDate] = {}
        if reconciliation_ids:
            for record in self._client.search(
                "account.move.reconciliation",
                domain=[("id", "in", reconciliation_ids)],
                fields=["date"],
                context={"company": company.engine_id},
            ):
                if record.get("date") is not None:
                    reconciliation_dates[int(record["id"])] = date_from_tryton(
                        record["date"]
                    )
        party_ids = sorted(
            {int(line["party"]) for line in lines if line.get("party") is not None}
        )
        party_names = self._party_names(party_ids)
        buckets: dict[tuple[int | None, int], dict[str, int]] = {}
        counts: dict[tuple[int | None, int], int] = {}
        for line in lines:
            reconciliation_id = line.get("reconciliation")
            if reconciliation_id is not None:
                recon_date = reconciliation_dates.get(int(reconciliation_id))
                if recon_date is not None and recon_date.gregorian_date <= as_of.gregorian_date:
                    continue
            account_id = int(line["account"])
            account_code = str(notes_accounts[account_id].get("code") or "")
            debit = money_from_tryton(line.get("debit", 0)).amount
            credit = money_from_tryton(line.get("credit", 0)).amount
            if account_code in NOTES_PAYABLE_CODES:
                amount = credit - debit
            else:
                amount = debit - credit
            maturity = line.get("maturity_date")
            line_date = line.get("date")
            if maturity is not None:
                base = date_from_tryton(maturity)
            elif line_date is not None:
                base = date_from_tryton(line_date)
            else:
                raise TrytonAdapterError(
                    "A posted move line has no maturity or effective date.",
                    code=ENGINE_ERROR,
                    details={"line_id": int(line["id"])},
                )
            age_days = (as_of.gregorian_date - base.gregorian_date).days
            bucket = aging_bucket(age_days)
            party_id = int(line["party"]) if line.get("party") is not None else None
            key = (party_id, account_id)
            row_buckets = buckets.setdefault(
                key,
                {
                    "not_yet_due": 0,
                    "bucket_0_30": 0,
                    "bucket_30_60": 0,
                    "bucket_60_90": 0,
                    "bucket_90_120": 0,
                    "bucket_120_plus": 0,
                },
            )
            row_buckets[bucket] += amount
            counts[key] = counts.get(key, 0) + 1
        rows: list[NotesBalanceRowView] = []
        totals = {
            "not_yet_due": 0,
            "bucket_0_30": 0,
            "bucket_30_60": 0,
            "bucket_60_90": 0,
            "bucket_90_120": 0,
            "bucket_120_plus": 0,
        }
        for (party_id, account_id), row_buckets in sorted(
            buckets.items(),
            key=lambda item: (
                party_names.get(item[0][0] or -1, ""),
                str(notes_accounts[item[0][1]].get("code") or ""),
            ),
        ):
            account_code = str(notes_accounts[account_id].get("code") or "")
            total = sum(row_buckets.values())
            for name, value in row_buckets.items():
                totals[name] += value
            rows.append(
                NotesBalanceRowView(
                    party_id=party_id,
                    party_name=party_names.get(party_id or -1, ""),
                    instrument=(
                        "payable"
                        if account_code in NOTES_PAYABLE_CODES
                        else "receivable"
                    ),
                    account_code=account_code,
                    account_name=str(notes_accounts[account_id].get("name") or ""),
                    bucket_not_yet_due=MoneyIRR(row_buckets["not_yet_due"]),
                    bucket_0_30=MoneyIRR(row_buckets["bucket_0_30"]),
                    bucket_30_60=MoneyIRR(row_buckets["bucket_30_60"]),
                    bucket_60_90=MoneyIRR(row_buckets["bucket_60_90"]),
                    bucket_90_120=MoneyIRR(row_buckets["bucket_90_120"]),
                    bucket_120_plus=MoneyIRR(row_buckets["bucket_120_plus"]),
                    total=MoneyIRR(total),
                    line_count=counts[(party_id, account_id)],
                )
            )
        return NotesBalanceView(
            company=company,
            as_of=as_of,
            instrument=instrument,
            rows=tuple(rows),
            totals=NotesBalanceRowView(
                party_id=None,
                party_name="",
                instrument=instrument,
                account_code="",
                account_name="",
                bucket_not_yet_due=MoneyIRR(totals["not_yet_due"]),
                bucket_0_30=MoneyIRR(totals["bucket_0_30"]),
                bucket_30_60=MoneyIRR(totals["bucket_30_60"]),
                bucket_60_90=MoneyIRR(totals["bucket_60_90"]),
                bucket_90_120=MoneyIRR(totals["bucket_90_120"]),
                bucket_120_plus=MoneyIRR(totals["bucket_120_plus"]),
                total=MoneyIRR(sum(totals.values())),
                line_count=sum(counts.values()),
            ),
            accounts_read=accounts_read,
            source_endpoint=TRIAL_BALANCE_SOURCE_ENDPOINT,
        )

    # -- account balance (US-516) ------------------------------------------

    def get_account_balance(
        self,
        company: CompanyRef,
        account: AccountRef,
        as_of: FiscalDate,
    ) -> AccountBalanceView:
        """Aggregate one account's posted move lines up to ``as_of``.

        One company-scoped ``account.move.line`` search, filtered on
        the account (``move.state = 'posted'``, ``move.company``,
        ``move.date`` on or before ``as_of``) — draft moves never
        contribute. The search is paged through in full (the same
        page size as the trial balance) until a short page; the
        balance is computed only over the complete line set, never
        from a partial page. The account must exist and belong to the
        company; the company must exist and book in IRR.
        """
        self._require_irr_company_for_reads(company.engine_id)
        record = self._client.read(
            "account.account",
            account.engine_id,
            fields=["company", "code", "name"],
            context={"company": company.engine_id},
        )
        if record is None or int(record.get("company") or -1) != company.engine_id:
            raise AccountNotFoundError(
                f"Account {account.engine_id} does not exist for company {company.engine_id}.",
                details={"account_id": account.engine_id, "company_id": company.engine_id},
            )
        context = {"company": company.engine_id}
        total_debit = MoneyIRR.zero()
        total_credit = MoneyIRR.zero()
        line_count = 0
        offset = 0
        while True:
            page = self._client.search(
                "account.move.line",
                domain=[
                    ("account", "=", account.engine_id),
                    ("move.company", "=", company.engine_id),
                    ("move.state", "=", "posted"),
                    ("move.date", "<=", date_to_tryton(as_of)),
                ],
                fields=["debit", "credit"],
                limit=_TRIAL_BALANCE_PAGE_SIZE,
                offset=offset,
                order=[["id", "ASC"]],
                context=context,
            )
            for line in page:
                total_debit = total_debit.add(money_from_tryton(line.get("debit", 0)))
                total_credit = total_credit.add(money_from_tryton(line.get("credit", 0)))
            line_count += len(page)
            if len(page) < _TRIAL_BALANCE_PAGE_SIZE:
                break
            offset += len(page)
        return AccountBalanceView(
            account=account,
            code=str(record.get("code") or ""),
            name=str(record.get("name") or ""),
            as_of=as_of,
            total_debit=total_debit,
            total_credit=total_credit,
            line_count=line_count,
            source_endpoint=TRIAL_BALANCE_SOURCE_ENDPOINT,
        )

    def _require_irr_company_for_reads(self, company_id: int) -> Record:
        """Fail fast when the company is missing or books in a non-IRR currency.

        The read-path twin of :meth:`_require_irr_company`: reads
        report failures with the ENGINE_* adapter taxonomy (translated
        to application errors), while the journal/invoice paths use
        LEDGER_* domain errors. Returns the company record (with
        ``rec_name``) so callers that also need the company name —
        :meth:`get_general_journal` — do not read the company twice.
        Memoized per adapter instance (one request): a failed check
        is never cached, so a retry re-reads.
        """
        cached = self._irr_company_records.get(company_id)
        if cached is not None:
            return cached
        record = self._client.read("company.company", company_id, fields=["currency", "rec_name"])
        if record is None:
            raise TrytonAdapterError(
                f"Company {company_id} does not exist in the engine.",
                code=ENGINE_NOT_FOUND,
                details={"company_id": company_id},
            )
        currency_id = record.get("currency")
        if currency_id is None:
            raise TrytonAdapterError(
                f"Company {company_id} has no currency configured.",
                code=ENGINE_VALIDATION_FAILED,
                details={"company_id": company_id},
            )
        codes = self._currency_codes([int(currency_id)])
        if codes.get(int(currency_id)) != "IRR":
            raise TrytonAdapterError(
                "HesabYar reads integer IRR amounts; refusing to read "
                f"a company whose currency is {codes.get(int(currency_id))!r}.",
                code=ENGINE_VALIDATION_FAILED,
                details={"company_id": company_id},
            )
        self._irr_company_records[company_id] = record
        return record

    def _account_code_names(
        self, account_ids: list[int], context: dict[str, Any]
    ) -> dict[int, tuple[str, str]]:
        """Batch-read (code, name) for account ids."""
        if not account_ids:
            return {}
        records = self._client.search(
            "account.account",
            domain=[("id", "in", account_ids)],
            fields=["code", "name"],
            context=context,
        )
        names = {
            int(record["id"]): (str(record.get("code") or ""), str(record.get("name") or ""))
            for record in records
        }
        missing = [account_id for account_id in account_ids if account_id not in names]
        if missing:
            raise TrytonAdapterError(
                "The engine no longer returns some referenced accounts.",
                code=ENGINE_ERROR,
                details={"account_ids": missing},
            )
        return names

    # -- invoice drafts ----------------------------------------------------

    def create_invoice_draft(self, command: CreateInvoiceDraft) -> InvoiceRef:
        """Create a draft invoice (design-eng spec §8.1).

        Pre-validation before delegating to the engine (mirroring
        ``create_journal_draft``): the company books in IRR, the party
        exists and has an invoice address, the receivable/payable account
        and the revenue/expense journal are resolved, and every line
        account exists for the company. The engine computes the invoice
        totals from the lines; no period check happens here because a
        draft invoice carries no period (the period is derived at posting
        from the accounting date -- ``validate_invoice_draft`` checks it).
        """
        company_id = command.company.engine_id
        context = {"company": company_id}
        currency_id = self._require_irr_company(
            company_id, error_cls=InvalidInvoiceError, subject="Invoice"
        )
        self._require_party(command.party.engine_id)
        if command.payment_term is not None:
            self._require_payment_term(command.payment_term.engine_id)
        for payee in command.alternative_payees:
            self._require_party(payee.engine_id)
        address_id = self._resolve_invoice_address(command, context)
        account_id = self._resolve_invoice_account(command, company_id, context)
        journal_id = self._resolve_invoice_journal(command, context)
        self._resolve_invoice_line_accounts(command, company_id, account_id, context)

        lines = [
            {
                "account": line.account.engine_id,
                "quantity": quantity_to_tryton(line.quantity),
                "unit_price": money_to_tryton(line.unit_price),
                "description": line.description,
            }
            for line in command.lines
        ]
        values: dict[str, Any] = {
            "company": company_id,
            "type": command.invoice_type,
            "party": command.party.engine_id,
            "invoice_address": address_id,
            "currency": currency_id,
            "account": account_id,
            "journal": journal_id,
            "invoice_date": date_to_tryton(command.date),
            "description": command.description,
            "reference": command.reference,
            "lines": [["create", lines]],
        }
        if command.payment_term is not None:
            # Plain REST field write: the term splits the posted invoice
            # into lines_to_pay (one receivable line per leg, each with
            # its maturity date) — docs/tryton-spikes-520.md §4.
            values["payment_term"] = command.payment_term.engine_id
        if command.alternative_payees:
            # Plain REST field write: a single alternative payee moves
            # the posted receivable lines' party from the customer to
            # the payee (the gateway, for settlement sales).
            values["alternative_payees"] = [
                ["add", [payee.engine_id for payee in command.alternative_payees]]
            ]
        record = self._client.create(
            "account.invoice",
            values,
            fields=["id", "state"],
            context=context,
        )
        return InvoiceRef(engine_id=int(record["id"]))

    def _require_payment_term(self, term_id: int) -> None:
        """Fail fast when the payment term does not exist in the engine."""
        record = self._client.read(
            "account.invoice.payment_term", term_id, fields=["name"]
        )
        if record is None:
            raise InvalidInvoiceError(
                f"Payment term {term_id} does not exist in the engine.",
                details={"payment_term_id": term_id},
            )

    def _require_party(self, party_id: int) -> None:
        """Fail fast when the party does not exist in the engine."""
        record = self._client.read("party.party", party_id, fields=["name"])
        if record is None:
            raise PartyNotFoundError(
                f"Party {party_id} does not exist in the engine.",
                details={"party_id": party_id},
            )

    def _resolve_invoice_address(
        self, command: CreateInvoiceDraft, context: dict[str, Any]
    ) -> int:
        """Resolve the invoice address: explicit, else the party's first address."""
        party_id = command.party.engine_id
        if command.invoice_address is not None:
            address_id = command.invoice_address.engine_id
            record = self._client.read(
                "party.address", address_id, fields=["party"], context=context
            )
            if record is None:
                raise InvalidInvoiceError(
                    f"Invoice address {address_id} does not exist in the engine.",
                    details={"address_id": address_id},
                )
            if int(record["party"]) != party_id:
                raise InvalidInvoiceError(
                    f"Invoice address {address_id} belongs to another party, not {party_id}.",
                    details={"address_id": address_id, "party_id": party_id},
                )
            return address_id
        records = self._client.search(
            "party.address",
            domain=[("party", "=", party_id)],
            fields=["party"],
            limit=1,
            order=[["id", "ASC"]],
            context=context,
        )
        if not records:
            raise InvalidInvoiceError(
                f"Party {party_id} has no address; an invoice needs an invoice address.",
                details={"party_id": party_id},
            )
        return int(records[0]["id"])

    def _resolve_invoice_account(
        self,
        command: CreateInvoiceDraft,
        company_id: int,
        context: dict[str, Any],
    ) -> int:
        """Resolve the receivable ('out') / payable ('in') account.

        Mirrors Tryton's own sale-flow fallback (party → company
        configuration default): an explicit account is verified and
        used; otherwise the party's default receivable/payable
        account, then the company's configured default
        (``account.configuration``), then the company's single open
        account of the right type. A configured default is an
        explicit company choice, not a guess — only a missing
        default on an ambiguous chart fails instead of guessing.
        Every candidate is verified the same way (exists for the
        company, open, active, right type).
        """
        wanted_flag = "receivable" if command.invoice_type == "out" else "payable"
        if command.account is not None:
            return self._verify_invoice_account(
                command.account.engine_id,
                company_id,
                wanted_flag,
                context,
                source="the specified account",
            )
        party_field = f"account_{wanted_flag}"
        party = self._client.read(
            "party.party", command.party.engine_id, fields=[party_field]
        )
        party_account = party.get(party_field) if party is not None else None
        if party_account is not None:
            return self._verify_invoice_account(
                int(party_account),
                company_id,
                wanted_flag,
                context,
                source=f"the party's {wanted_flag} account",
            )
        config_field = f"default_account_{wanted_flag}"
        config = self._client.read(
            "account.configuration", 1, fields=[config_field], context=context
        )
        config_account = config.get(config_field) if config is not None else None
        if config_account is not None:
            return self._verify_invoice_account(
                int(config_account),
                company_id,
                wanted_flag,
                context,
                source=f"the configured default {wanted_flag} account",
            )
        records = self._client.search(
            "account.account",
            domain=[("company", "=", company_id), (f"type.{wanted_flag}", "=", True)],
            fields=["closed", "active"],
            context=context,
        )
        usable = [r for r in records if not r.get("closed") and r.get("active", True)]
        if not usable:
            raise InvalidInvoiceError(
                f"Company {company_id} has no open {wanted_flag} account; "
                "specify the invoice account explicitly.",
                details={"company_id": company_id},
            )
        if len(usable) != 1:
            raise InvalidInvoiceError(
                f"Company {company_id} has {len(usable)} open {wanted_flag} accounts; "
                "specify the invoice account explicitly.",
                details={"company_id": company_id, "account_count": len(usable)},
            )
        return int(usable[0]["id"])

    def _verify_invoice_account(
        self,
        account_id: int,
        company_id: int,
        wanted_flag: str,
        context: dict[str, Any],
        *,
        source: str,
    ) -> int:
        """Verify one invoice-account candidate (exists, open, active, right type)."""
        direction = "customer" if wanted_flag == "receivable" else "supplier"
        records = self._client.search(
            "account.account",
            domain=[("id", "=", account_id), ("company", "=", company_id)],
            fields=["closed", "active", "type"],
            context=context,
        )
        if not records:
            raise AccountNotFoundError(
                f"Invoice account {account_id} ({source}) not found for company {company_id}.",
                details={"company_id": company_id, "account_id": account_id},
            )
        record = records[0]
        if record.get("closed") or not record.get("active", True):
            raise InvalidInvoiceError(
                f"Invoice account {account_id} ({source}) is closed or inactive.",
                details={"account_id": account_id},
            )
        type_id = record.get("type")
        if not self._account_type_flag(
            int(type_id) if type_id is not None else None, wanted_flag
        ):
            raise InvalidInvoiceError(
                f"Invoice account {account_id} ({source}) is not a {wanted_flag} account; "
                f"a {direction} invoice needs one.",
                details={"account_id": account_id},
            )
        return account_id

    def _account_type_record(self, type_id: int | None) -> Record | None:
        """Read an account type's direction flags (None when there is no type)."""
        if type_id is None:
            return None
        return self._client.read(
            "account.account.type",
            type_id,
            fields=["receivable", "payable", "revenue", "expense", "debt"],
        )

    def _account_type_flag(self, type_id: int | None, flag: str) -> bool:
        """True when the account type carries the ``receivable``/``payable`` flag."""
        record = self._account_type_record(type_id)
        return bool(record and record.get(flag))

    def _resolve_invoice_journal(
        self, command: CreateInvoiceDraft, context: dict[str, Any]
    ) -> int:
        """Resolve the revenue ('out') / expense ('in') journal.

        Mirrors ``_resolve_journal``: an explicit journal is verified
        (exists, right type); otherwise the engine's single journal of
        the matching type is used.
        """
        wanted_type = "revenue" if command.invoice_type == "out" else "expense"
        direction = "customer" if command.invoice_type == "out" else "supplier"
        if command.journal is not None:
            journal_id = command.journal.engine_id
            record = self._client.read(
                "account.journal", journal_id, fields=["type"], context=context
            )
            if record is None:
                raise InvalidInvoiceError(
                    f"Journal {journal_id} does not exist in the engine.",
                    details={"journal_id": journal_id},
                )
            if str(record.get("type") or "") != wanted_type:
                raise InvalidInvoiceError(
                    f"Journal {journal_id} is a {str(record.get('type') or '')!r} journal; "
                    f"a {direction} invoice needs a {wanted_type} journal.",
                    details={"journal_id": journal_id},
                )
            return journal_id
        journals = self._client.search(
            "account.journal",
            domain=[("type", "=", wanted_type)],
            fields=["name"],
            order=[["id", "ASC"]],
            context=context,
        )
        if len(journals) != 1:
            raise InvalidInvoiceError(
                "No journal was specified and the engine has "
                f"{len(journals)} {wanted_type} journals; specify one explicitly.",
                details={"journal_type": wanted_type, "journal_count": len(journals)},
            )
        return int(journals[0]["id"])

    def _resolve_invoice_line_accounts(
        self,
        command: CreateInvoiceDraft,
        company_id: int,
        invoice_account_id: int,
        context: dict[str, Any],
    ) -> None:
        """Verify every invoice line account (exists, open, active, not the invoice account).

        Unlike journal lines, a ``party_required`` line account is NOT
        rejected: the engine fills the move-line party from the invoice
        itself when the invoice posts (``InvoiceLine.get_move_lines``).
        The engine additionally enforces a direction domain on line
        accounts (``InvoiceLine._account_domain``): customer invoices
        need revenue accounts, supplier invoices need expense or debt
        accounts. The adapter checks that here so a wrong-direction
        account fails with a stable error instead of the engine's
        domain error.
        """
        wanted = sorted({line.account.engine_id for line in command.lines})
        records = self._client.search(
            "account.account",
            domain=[("id", "in", wanted), ("company", "=", company_id)],
            fields=["closed", "active", "type"],
            context=context,
        )
        found = {int(r["id"]): r for r in records}
        missing = [account_id for account_id in wanted if account_id not in found]
        if missing:
            raise AccountNotFoundError(
                f"Invoice line accounts not found for company {company_id}: {missing}.",
                details={"company_id": company_id, "missing_accounts": missing},
            )
        direction = "customer" if command.invoice_type == "out" else "supplier"
        for account_id, record in found.items():
            if account_id == invoice_account_id:
                raise InvalidInvoiceError(
                    f"Invoice line account {account_id} is the invoice's own "
                    "receivable/payable account; the engine forbids lines on it.",
                    details={"account_id": account_id},
                )
            if record.get("closed"):
                raise InvalidInvoiceError(
                    f"Invoice line account {account_id} is closed and cannot receive lines.",
                    details={"account_id": account_id},
                )
            if not record.get("active", True):
                raise InvalidInvoiceError(
                    f"Invoice line account {account_id} is inactive and cannot receive lines.",
                    details={"account_id": account_id},
                )
            type_id = record.get("type")
            type_record = self._account_type_record(
                int(type_id) if type_id is not None else None
            )
            if command.invoice_type == "out":
                ok = bool(type_record and type_record.get("revenue"))
                wanted_kinds = "a revenue account"
            else:
                ok = bool(
                    type_record
                    and (type_record.get("expense") or type_record.get("debt"))
                )
                wanted_kinds = "an expense or debt account"
            if not ok:
                raise InvalidInvoiceError(
                    f"Invoice line account {account_id} is not {wanted_kinds}; "
                    f"a {direction} invoice needs {wanted_kinds} on its lines.",
                    details={"account_id": account_id},
                )

    # -- invoice validation ------------------------------------------------

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        """Validate a draft invoice against the postability invariants.

        Read-only: it never mutates the invoice and never calls the
        engine's ``validate_invoice`` transition. Checks the draft's own
        state, line structure and amounts, cross-checks the engine's
        computed totals against the lines, and verifies the party,
        address, account type, journal and open fiscal period the
        invoice will need when it is posted later (posting derives the
        period from the accounting date via ``Period.find``).

        Polarity-aware (US-511): a credit note is an invoice whose
        priced lines are all negated and whose total is negative
        (``create_credit_note`` mirrors ``Invoice.credit``). The sign
        checks therefore follow the lines — all-positive for a normal
        invoice, all-negative for a credit note — and mixed line signs
        are a defect, since HesabYar never creates mixed-sign invoices.
        """
        issues: list[ValidationIssue] = []
        record = self._client.read("account.invoice", invoice.engine_id, fields=_INVOICE_FIELDS)
        if record is None:
            raise InvoiceNotFoundError(
                f"Invoice {invoice.engine_id} does not exist in the engine.",
                details={"invoice_id": invoice.engine_id},
            )
        company_id = int(record["company"])
        context = {"company": company_id}
        invoice_type = str(record.get("type") or "")
        state = str(record.get("state") or "")
        if state != "draft":
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=f"Invoice is in state {state!r}; only a draft can be validated.",
                    field="state",
                )
            )

        line_records = self._client.search(
            "account.invoice.line",
            domain=[("invoice", "=", invoice.engine_id)],
            fields=_INVOICE_LINE_FIELDS,
            context=context,
        )
        priced = [r for r in line_records if str(r.get("type") or "line") == "line"]
        if not priced:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message="Invoice has no priced lines.",
                    field="lines",
                )
            )

        line_total = MoneyIRR.zero()
        line_signs: set[str] = set()
        for line_record in priced:
            quantity = quantity_from_tryton(line_record.get("quantity", 0))
            unit_price = money_from_tryton(line_record.get("unit_price", 0))
            amount = money_from_tryton(line_record.get("amount", 0))
            if quantity == 0:
                issues.append(
                    ValidationIssue(
                        code="LEDGER_INVALID_INVOICE",
                        message=f"Line {line_record['id']} has a zero quantity.",
                        field="lines",
                    )
                )
            elif quantity < 0:
                # A negated line is a credit-note line; the polarity
                # check below requires ALL lines to share the sign.
                line_signs.add("negative")
            else:
                line_signs.add("positive")
            if unit_price.is_negative:
                issues.append(
                    ValidationIssue(
                        code="LEDGER_INVALID_INVOICE",
                        message=f"Line {line_record['id']} has a negative unit price.",
                        field="lines",
                    )
                )
            expected = quantity * unit_price.amount
            if expected != expected.to_integral_value() or MoneyIRR(int(expected)) != amount:
                issues.append(
                    ValidationIssue(
                        code="LEDGER_INVALID_INVOICE",
                        message=(
                            f"Line {line_record['id']} amount {amount.amount} IRR does not match "
                            f"quantity {quantity} x unit price {unit_price.amount} IRR."
                        ),
                        field="lines",
                    )
                )
            line_total = line_total.add(amount)

        account_ids = sorted({int(r["account"]) for r in priced})
        if account_ids:
            accounts = self._client.search(
                "account.account",
                domain=[("id", "in", account_ids), ("company", "=", company_id)],
                fields=["closed", "active", "type"],
                context=context,
            )
            found = {int(r["id"]): r for r in accounts}
            type_records: dict[int, Record | None] = {}
            for account_id in account_ids:
                account_record = found.get(account_id)
                if account_record is None:
                    issues.append(
                        ValidationIssue(
                            code="LEDGER_ACCOUNT_NOT_FOUND",
                            message=(
                                f"Line account {account_id} does not belong "
                                f"to company {company_id}."
                            ),
                            field="lines",
                        )
                    )
                    continue
                if account_record.get("closed") or not account_record.get("active", True):
                    issues.append(
                        ValidationIssue(
                            code="LEDGER_ACCOUNT_NOT_FOUND",
                            message=f"Line account {account_id} is closed or inactive.",
                            field="lines",
                        )
                    )
                # Direction domain (InvoiceLine._account_domain): customer
                # invoices need revenue accounts, supplier invoices need
                # expense or debt accounts on their lines.
                type_id = account_record.get("type")
                type_key = int(type_id) if type_id is not None else -1
                if type_key not in type_records:
                    type_records[type_key] = self._account_type_record(
                        int(type_id) if type_id is not None else None
                    )
                type_record = type_records[type_key]
                if invoice_type == "out":
                    direction_ok = bool(type_record and type_record.get("revenue"))
                    wanted_kinds = "a revenue account"
                else:
                    direction_ok = bool(
                        type_record
                        and (type_record.get("expense") or type_record.get("debt"))
                    )
                    wanted_kinds = "an expense or debt account"
                if not direction_ok:
                    issues.append(
                        ValidationIssue(
                            code="LEDGER_INVALID_INVOICE",
                            message=(
                                f"Line account {account_id} is not {wanted_kinds}; "
                                "the engine rejects it for this invoice direction."
                            ),
                            field="lines",
                        )
                    )

        untaxed = money_from_tryton(record.get("untaxed_amount", 0))
        tax = money_from_tryton(record.get("tax_amount", 0))
        total = money_from_tryton(record.get("total_amount", 0))
        if line_signs == {"positive", "negative"}:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=(
                        "Invoice lines have mixed quantity signs; a draft "
                        "is either a normal invoice (all positive) or a "
                        "credit note (all negative)."
                    ),
                    field="lines",
                )
            )
        if untaxed != line_total:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=(
                        f"Engine untaxed total {untaxed.amount} IRR != sum of line amounts "
                        f"{line_total.amount} IRR."
                    ),
                    field="total",
                )
            )
        if total != untaxed.add(tax):
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=(
                        f"Engine total {total.amount} IRR != untaxed {untaxed.amount} IRR "
                        f"+ tax {tax.amount} IRR."
                    ),
                    field="total",
                )
            )
        if total.is_zero:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message="Invoice total is 0 IRR; it must be non-zero.",
                    field="total",
                )
            )
        elif line_signs == {"negative"} and not total.is_negative:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=(
                        f"Credit-note total is {total.amount} IRR; "
                        "it must be negative."
                    ),
                    field="total",
                )
            )
        elif line_signs == {"positive"} and total.is_negative:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=f"Invoice total is {total.amount} IRR; it must be greater than zero.",
                    field="total",
                )
            )

        self._validate_invoice_currency(record, company_id, issues)
        self._validate_invoice_party(record, context, issues)
        self._validate_invoice_account(record, company_id, invoice_type, context, issues)
        self._validate_invoice_journal(record, invoice_type, context, issues)
        self._validate_invoice_period(record, company_id, invoice_type, context, issues)

        if issues:
            return ValidationSummary.failed(issues)
        return ValidationSummary.ok()

    def _validate_invoice_currency(
        self, record: Record, company_id: int, issues: list[ValidationIssue]
    ) -> None:
        """The invoice must book in the company currency, which must be IRR."""
        currency_id = record.get("currency")
        company_record = self._client.read("company.company", company_id, fields=["currency"])
        company_currency_id = company_record.get("currency") if company_record else None
        if (
            currency_id is None
            or company_currency_id is None
            or int(currency_id) != int(company_currency_id)
        ):
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message="Invoice currency does not match the company currency.",
                    field="currency",
                )
            )
        elif self._currency_codes([int(currency_id)]).get(int(currency_id)) != "IRR":
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message="Invoice amounts are integer IRR; the company currency is not IRR.",
                    field="currency",
                )
            )

    def _validate_invoice_party(
        self, record: Record, context: dict[str, Any], issues: list[ValidationIssue]
    ) -> None:
        """The party must exist and the invoice address must be one of its addresses."""
        party_id = record.get("party")
        if party_id is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_PARTY_NOT_FOUND",
                    message="Invoice has no party.",
                    field="party",
                )
            )
            return
        party_record = self._client.read("party.party", int(party_id), fields=["name"])
        if party_record is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_PARTY_NOT_FOUND",
                    message=f"Party {party_id} does not exist in the engine.",
                    field="party",
                )
            )
            return
        address_id = record.get("invoice_address")
        if address_id is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message="Invoice has no invoice address.",
                    field="invoice_address",
                )
            )
            return
        address_record = self._client.read(
            "party.address", int(address_id), fields=["party"], context=context
        )
        if address_record is None or int(address_record["party"]) != int(party_id):
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=f"Invoice address {address_id} does not belong to party {party_id}.",
                    field="invoice_address",
                )
            )

    def _validate_invoice_account(
        self,
        record: Record,
        company_id: int,
        invoice_type: str,
        context: dict[str, Any],
        issues: list[ValidationIssue],
    ) -> None:
        """The invoice account must be open, active, and of the right type."""
        account_id = record.get("account")
        if account_id is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_ACCOUNT_NOT_FOUND",
                    message="Invoice has no receivable/payable account.",
                    field="account",
                )
            )
            return
        account_records = self._client.search(
            "account.account",
            domain=[("id", "=", int(account_id)), ("company", "=", company_id)],
            fields=["closed", "active", "type"],
            context=context,
        )
        if not account_records:
            issues.append(
                ValidationIssue(
                    code="LEDGER_ACCOUNT_NOT_FOUND",
                    message=f"Invoice account {account_id} does not belong to company {company_id}.",
                    field="account",
                )
            )
            return
        account_record = account_records[0]
        if account_record.get("closed") or not account_record.get("active", True):
            issues.append(
                ValidationIssue(
                    code="LEDGER_ACCOUNT_NOT_FOUND",
                    message=f"Invoice account {account_id} is closed or inactive.",
                    field="account",
                )
            )
        wanted_flag = "receivable" if invoice_type == "out" else "payable"
        type_id = account_record.get("type")
        if not self._account_type_flag(
            int(type_id) if type_id is not None else None, wanted_flag
        ):
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=f"Invoice account {account_id} is not a {wanted_flag} account.",
                    field="account",
                )
            )

    def _validate_invoice_journal(
        self,
        record: Record,
        invoice_type: str,
        context: dict[str, Any],
        issues: list[ValidationIssue],
    ) -> None:
        """A journal of the matching type is required to post the invoice."""
        journal_id = record.get("journal")
        wanted_type = "revenue" if invoice_type == "out" else "expense"
        if journal_id is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message="Invoice has no journal; a journal is required to post it.",
                    field="journal",
                )
            )
            return
        journal_record = self._client.read(
            "account.journal", int(journal_id), fields=["type"], context=context
        )
        if journal_record is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=f"Journal {journal_id} does not exist in the engine.",
                    field="journal",
                )
            )
        elif str(journal_record.get("type") or "") != wanted_type:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message=(
                        f"Journal {journal_id} is a "
                        f"{str(journal_record.get('type') or '')!r} journal; "
                        f"a {wanted_type} journal is required."
                    ),
                    field="journal",
                )
            )

    def _validate_invoice_period(
        self,
        record: Record,
        company_id: int,
        invoice_type: str,
        context: dict[str, Any],
        issues: list[ValidationIssue],
    ) -> None:
        """The accounting date must fall in an open fiscal period.

        Posting derives the period from the accounting date
        (``accounting_date``, else ``invoice_date``, else today) via
        ``Period.find``; a date outside every open period cannot post.
        Supplier invoices additionally need an invoice date before the
        engine validates them (the ``validate_invoice`` button's
        pre-validation).
        """
        invoice_date = record.get("invoice_date")
        accounting_date = record.get("accounting_date")
        if invoice_type == "in" and invoice_date is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_INVALID_INVOICE",
                    message="Supplier invoices need an invoice date before validation.",
                    field="invoice_date",
                )
            )
        if accounting_date is not None:
            effective_date = date_from_tryton(accounting_date)
        elif invoice_date is not None:
            effective_date = date_from_tryton(invoice_date)
        else:
            effective_date = FiscalDate.from_gregorian(datetime.date.today())
        if self._find_open_period(company_id, effective_date, context) is None:
            issues.append(
                ValidationIssue(
                    code="LEDGER_PERIOD_CLOSED",
                    message=(
                        f"No open fiscal period covers {effective_date.to_gregorian_str()} "
                        f"for company {company_id}."
                    ),
                    field="date",
                )
            )

    # -- invoice reads -----------------------------------------------------

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        """Read an invoice back as a domain view (None when missing)."""
        record = self._client.read("account.invoice", invoice.engine_id, fields=_INVOICE_FIELDS)
        if record is None:
            return None
        return self._invoice_views([record])[0]

    def _validate_invoice_filters(
        self, invoice_type: str | None, state: str | None
    ) -> None:
        """Shared by the invoice list reads (full views and totals)."""
        if invoice_type is not None and invoice_type not in ("out", "in"):
            raise InvalidInvoiceError(
                f"Invoice type filter must be 'out' or 'in', got {invoice_type!r}."
            )
        if state is not None and state not in _INVOICE_STATES:
            raise InvalidInvoiceError(
                f"Invoice state filter must be one of {_INVOICE_STATES}, got {state!r}."
            )

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        """Return the company's invoices, newest first."""
        self._validate_invoice_filters(invoice_type, state)
        domain: list[Any] = [("company", "=", company.engine_id)]
        if invoice_type is not None:
            domain.append(("type", "=", invoice_type))
        if state is not None:
            domain.append(("state", "=", state))
        records = self._client.search(
            "account.invoice",
            domain=domain,
            fields=_INVOICE_FIELDS,
            order=[["invoice_date", "DESC"], ["id", "DESC"]],
            context={"company": company.engine_id},
        )
        return self._invoice_views(records)

    def list_invoice_totals(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceTotalView]:
        """Return one row per invoice: its date and total, nothing else.

        One search — no invoice lines, party names, or currency
        lookups. The cash-box report sums recorded sales from these
        totals; the full ``list_invoices`` read costs three extra
        round-trips the report never uses.
        """
        self._validate_invoice_filters(invoice_type, state)
        domain: list[Any] = [("company", "=", company.engine_id)]
        if invoice_type is not None:
            domain.append(("type", "=", invoice_type))
        if state is not None:
            domain.append(("state", "=", state))
        records = self._client.search(
            "account.invoice",
            domain=domain,
            fields=["invoice_date", "total_amount"],
            order=[["invoice_date", "DESC"], ["id", "DESC"]],
            context={"company": company.engine_id},
        )
        totals: list[InvoiceTotalView] = []
        for record in records:
            invoice_date = record.get("invoice_date")
            totals.append(
                InvoiceTotalView(
                    ref=InvoiceRef(engine_id=int(record["id"])),
                    invoice_date=date_from_tryton(invoice_date)
                    if invoice_date is not None
                    else None,
                    total_amount=money_from_tryton(record.get("total_amount", 0)),
                )
            )
        return totals

    def _invoice_views(self, records: list[Record]) -> list[InvoiceView]:
        """Map invoice records to views, batching lines, party names and currencies."""
        if not records:
            return []
        invoice_ids = [int(record["id"]) for record in records]
        context = {"company": int(records[0]["company"])}
        lines_by_invoice = self._invoice_lines(invoice_ids, context)
        party_names = self._party_names(sorted({int(r["party"]) for r in records}))
        currency_ids = sorted({int(r["currency"]) for r in records if r.get("currency")})
        codes = self._currency_codes(currency_ids)
        return [
            invoice_record_to_view(
                record,
                lines_by_invoice.get(int(record["id"]), []),
                party_names.get(int(record["party"]), ""),
                codes.get(int(record["currency"]), "") if record.get("currency") else "",
            )
            for record in records
        ]

    def _invoice_lines(
        self, invoice_ids: list[int], context: dict[str, Any]
    ) -> dict[int, list[InvoiceLineView]]:
        if not invoice_ids:
            return {}
        records = self._client.search(
            "account.invoice.line",
            domain=[("invoice", "in", invoice_ids)],
            fields=_INVOICE_LINE_FIELDS,
            order=[["id", "ASC"]],
            context=context,
        )
        grouped: dict[int, list[InvoiceLineView]] = {}
        for record in records:
            grouped.setdefault(int(record["invoice"]), []).append(
                invoice_line_record_to_view(record)
            )
        return grouped

    def _party_names(self, party_ids: list[int]) -> dict[int, str]:
        if not party_ids:
            return {}
        records = self._client.search(
            "party.party",
            domain=[("id", "in", party_ids)],
            fields=["name"],
        )
        return {int(r["id"]): str(r.get("name") or "") for r in records}

    # -- invoice finalization + credit notes (Phase 5, US-502) -----------

    def _invoice_action(self, invoice_id: int, action: str) -> InvoiceView:
        """Run one invoice button transition and read the invoice back.

        The REST action route returns the updated record when the
        action returns None (``trytond/protocols/rest.py``), so the
        read-back is one call, not two. A 404 from the action means
        the invoice does not exist.
        """
        try:
            result = self._client.action(
                "account.invoice", action, record_id=invoice_id, fields=_INVOICE_FIELDS
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                raise InvoiceNotFoundError(
                    f"Invoice {invoice_id} does not exist in the engine.",
                    details={"invoice_id": invoice_id},
                ) from exc
            raise
        if not isinstance(result, dict) or "id" not in result:
            raise TrytonAdapterError(
                f"Unexpected {action} response for invoice {invoice_id}: expected a record.",
                code="ENGINE_ERROR",
            )
        return self._invoice_views([result])[0]

    def validate_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        """Finalize a draft invoice (engine ``validate_invoice`` transition)."""
        return self._invoice_action(invoice.engine_id, "validate_invoice")

    def post_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        """Post an invoice (engine ``post`` transition)."""
        return self._invoice_action(invoice.engine_id, "post")

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        """Cancel a draft or validated invoice (engine ``cancel`` transition)."""
        return self._invoice_action(invoice.engine_id, "cancel")

    def create_credit_note(self, command: CreateCreditNote) -> InvoiceRef:
        """Create a credit-note draft from a posted invoice.

        Mirrors ``Invoice.credit`` over REST: the credit note keeps the
        source's type/party/address/account/journal/currency, every
        priced line is recreated with a negated quantity and its origin
        pointing at the source line, and the totals come out negative.

        With ``partial_amount`` (US-511) the credit is amount-based
        instead: a single line for the amount against the source's
        revenue account, which requires all of the source's priced
        lines to share one account (a multi-account invoice cannot be
        partially credited by amount without guessing the split).
        """
        source_id = command.invoice.engine_id
        source = self._client.read(
            "account.invoice",
            source_id,
            fields=[
                "company",
                "type",
                "party",
                "invoice_address",
                "currency",
                "account",
                "journal",
                "state",
                "number",
                "description",
                "total_amount",
            ],
        )
        if source is None:
            raise InvoiceNotFoundError(
                f"Invoice {source_id} does not exist in the engine.",
                details={"invoice_id": source_id},
            )
        if str(source.get("state") or "") != "posted":
            raise InvalidInvoiceError(
                f"Invoice {source_id} is not posted; only a posted invoice "
                "can be credited.",
                details={"invoice_id": source_id, "state": str(source.get("state") or "")},
            )
        company_id = int(source["company"])
        context = {"company": company_id}
        source_lines = self._client.search(
            "account.invoice.line",
            domain=[("invoice", "=", source_id), ("type", "=", "line")],
            fields=["account", "quantity", "unit_price", "description"],
            order=[["id", "ASC"]],
            context=context,
        )
        if not source_lines:
            raise InvalidInvoiceError(
                f"Invoice {source_id} has no priced lines to credit.",
                details={"invoice_id": source_id},
            )
        if command.partial_amount is not None:
            source_total = money_from_tryton(source.get("total_amount", 0))
            if command.partial_amount.amount >= source_total.amount:
                raise InvalidInvoiceError(
                    f"Partial credit amount {command.partial_amount.amount} IRR must be "
                    f"less than the source invoice total {source_total.amount} IRR.",
                    details={"invoice_id": source_id},
                )
            accounts = {int(line["account"]) for line in source_lines}
            if len(accounts) != 1:
                raise InvalidInvoiceError(
                    f"Partial credit needs a single revenue account; invoice "
                    f"{source_id} lines use {len(accounts)} accounts.",
                    details={"invoice_id": source_id},
                )
            lines = [
                {
                    "account": accounts.pop(),
                    "quantity": quantity_to_tryton(Decimal(-1)),
                    "unit_price": money_to_tryton(command.partial_amount),
                    "description": (
                        f"Partial credit ({command.partial_amount.amount} IRR "
                        f"of {source_total.amount} IRR)"
                    ),
                }
            ]
        else:
            lines = [
                {
                    "account": int(line["account"]),
                    "quantity": quantity_to_tryton(-quantity_from_tryton(line.get("quantity", 0))),
                    "unit_price": money_to_tryton(money_from_tryton(line.get("unit_price", 0))),
                    "description": str(line.get("description") or ""),
                    "origin": f"account.invoice.line,{int(line['id'])}",
                }
                for line in source_lines
            ]
        number = source.get("number")
        description = command.description or (
            f"Credit note for invoice {number}" if number else "Credit note"
        )
        record = self._client.create(
            "account.invoice",
            {
                "company": company_id,
                "type": str(source["type"]),
                "party": int(source["party"]),
                "invoice_address": int(source["invoice_address"]),
                "currency": int(source["currency"]),
                "account": int(source["account"]),
                "journal": int(source["journal"]),
                "invoice_date": date_to_tryton(command.date),
                "description": description,
                "lines": [["create", lines]],
            },
            fields=["id", "state"],
            context=context,
        )
        return InvoiceRef(engine_id=int(record["id"]))

    # -- sales (Phase 5, US-502) -----------------------------------------

    def create_sale(self, command: CreateSale) -> SaleRef:
        """Create a draft sale order (design-eng spec §10: store order intake).

        Pre-validation before delegating to the engine (mirroring
        ``create_invoice_draft``): the company books in IRR, the party
        exists and has addresses, and every line product exists and is
        sellable. Each line's unit of measure is resolved from the
        product's sale UoM (the engine's ``on_change_product`` is
        client-side only and never runs for a REST create).
        """
        company_id = command.company.engine_id
        context = {"company": company_id}
        currency_id = self._require_irr_company(
            company_id, error_cls=InvalidSaleError, subject="Sale"
        )
        self._require_party(command.party.engine_id)
        invoice_address_id = self._resolve_party_address(
            command.party.engine_id,
            command.invoice_address.engine_id if command.invoice_address else None,
            "invoice",
            InvalidSaleError,
            context,
        )
        shipment_address_id = self._resolve_party_address(
            command.party.engine_id,
            command.shipment_address.engine_id if command.shipment_address else None,
            "shipment",
            InvalidSaleError,
            context,
        )
        units = self._resolve_sale_line_units(command, context)
        lines = [
            {
                "type": "line",
                "product": line.product.engine_id,
                "quantity": quantity_to_tryton(line.quantity),
                "unit": units[line.product.engine_id],
                "unit_price": money_to_tryton(line.unit_price),
                "description": line.description,
            }
            for line in command.lines
        ]
        record = self._client.create(
            "sale.sale",
            {
                "company": company_id,
                "party": command.party.engine_id,
                "currency": currency_id,
                "sale_date": date_to_tryton(command.date),
                "invoice_method": command.invoice_method,
                "shipment_method": command.shipment_method,
                # sale_shipment_cost defaults shipment_cost_method to
                # 'order', but its domain requires None when the sale
                # has no carrier and leaves draft -- a sale created
                # with the default fails at quote. HesabYar sets no
                # carrier yet (phase 5چ adds shipment costing), so
                # always send None.
                "shipment_cost_method": None,
                "invoice_address": invoice_address_id,
                "shipment_address": shipment_address_id,
                "description": command.description,
                "reference": command.reference,
                "lines": [["create", lines]],
            },
            fields=["id", "state"],
            context=context,
        )
        return SaleRef(engine_id=int(record["id"]))

    def _resolve_party_address(
        self,
        party_id: int,
        address_id: int | None,
        purpose: str,
        error_cls: type[LedgerError],
        context: dict[str, Any],
    ) -> int:
        """Resolve a party address: explicit (verified), else the party's first."""
        if address_id is not None:
            record = self._client.read(
                "party.address", address_id, fields=["party"], context=context
            )
            if record is None:
                raise error_cls(
                    f"{purpose.capitalize()} address {address_id} does not exist "
                    "in the engine.",
                    details={"address_id": address_id},
                )
            if int(record["party"]) != party_id:
                raise error_cls(
                    f"{purpose.capitalize()} address {address_id} belongs to "
                    f"another party, not {party_id}.",
                    details={"address_id": address_id, "party_id": party_id},
                )
            return address_id
        records = self._client.search(
            "party.address",
            domain=[("party", "=", party_id)],
            fields=["party"],
            limit=1,
            order=[["id", "ASC"]],
            context=context,
        )
        if not records:
            raise error_cls(
                f"Party {party_id} has no address; a sale needs a {purpose} address.",
                details={"party_id": party_id},
            )
        return int(records[0]["id"])

    def _resolve_sale_line_units(
        self, command: CreateSale, context: dict[str, Any]
    ) -> dict[int, int]:
        """Verify every line product (exists, sellable) and resolve its sale UoM.

        Returns ``{product_id: unit_id}``. Products are global in the
        engine (not company-scoped); ``salable`` is what gates them for
        a sale line.
        """
        wanted = sorted({line.product.engine_id for line in command.lines})
        records = self._client.search(
            "product.product",
            domain=[("id", "in", wanted)],
            fields=["template"],
            context=context,
        )
        found = {int(r["id"]): r for r in records}
        missing = [pid for pid in wanted if pid not in found]
        if missing:
            raise ProductNotFoundError(
                f"Products not found in the engine: {missing}.",
                details={"missing_products": missing},
            )
        template_ids = sorted({int(r["template"]) for r in records if r.get("template")})
        templates = self._client.search(
            "product.template",
            domain=[("id", "in", template_ids)],
            fields=["salable", "sale_uom", "default_uom"],
            context=context,
        )
        by_template = {int(t["id"]): t for t in templates}
        units: dict[int, int] = {}
        for product_id, record in found.items():
            template = by_template.get(int(record["template"]))
            if template is None or not template.get("salable"):
                raise ProductNotFoundError(
                    f"Product {product_id} is not sellable.",
                    details={"product_id": product_id},
                )
            unit_id = template.get("sale_uom") or template.get("default_uom")
            if unit_id is None:
                raise InvalidSaleError(
                    f"Product {product_id} has no sale unit of measure.",
                    details={"product_id": product_id},
                )
            units[product_id] = int(unit_id)
        return units

    def get_sale(self, sale: SaleRef) -> SaleView | None:
        """Read a sale order back as a domain view (None when missing)."""
        record = self._client.read("sale.sale", sale.engine_id, fields=_SALE_FIELDS)
        if record is None:
            return None
        return self._sale_views([record])[0]

    def list_sales(
        self,
        company: CompanyRef,
        state: str | None = None,
    ) -> Sequence[SaleView]:
        """Return the company's sale orders, newest first."""
        if state is not None and state not in _SALE_STATES:
            raise InvalidSaleError(
                f"Sale state filter must be one of {_SALE_STATES}, got {state!r}."
            )
        domain: list[Any] = [("company", "=", company.engine_id)]
        if state is not None:
            domain.append(("state", "=", state))
        records = self._client.search(
            "sale.sale",
            domain=domain,
            fields=_SALE_FIELDS,
            order=[["sale_date", "DESC"], ["id", "DESC"]],
            context={"company": company.engine_id},
        )
        return self._sale_views(records)

    def _sale_views(self, records: list[Record]) -> list[SaleView]:
        """Map sale records to views, batching lines, party/product names and currencies."""
        if not records:
            return []
        sale_ids = [int(record["id"]) for record in records]
        context = {"company": int(records[0]["company"])}
        lines_by_sale = self._sale_lines(sale_ids, context)
        party_names = self._party_names(sorted({int(r["party"]) for r in records}))
        product_ids = sorted(
            {
                int(line["product"])
                for lines in lines_by_sale.values()
                for line in lines
                if line.get("product") is not None
            }
        )
        product_names = self._product_names(product_ids)
        currency_ids = sorted({int(r["currency"]) for r in records if r.get("currency")})
        codes = self._currency_codes(currency_ids)
        return [
            sale_record_to_view(
                record,
                [
                    sale_line_record_to_view(line, product_names)
                    for line in lines_by_sale.get(int(record["id"]), [])
                ],
                party_names.get(int(record["party"]), ""),
                codes.get(int(record["currency"]), "") if record.get("currency") else "",
            )
            for record in records
        ]

    def _sale_lines(
        self, sale_ids: list[int], context: dict[str, Any]
    ) -> dict[int, list[Record]]:
        """Priced sale lines grouped by sale (subtotal/title/comment lines excluded)."""
        if not sale_ids:
            return {}
        records = self._client.search(
            "sale.line",
            domain=[("sale", "in", sale_ids), ("type", "=", "line")],
            fields=_SALE_LINE_FIELDS,
            order=[["id", "ASC"]],
            context=context,
        )
        grouped: dict[int, list[Record]] = {}
        for record in records:
            grouped.setdefault(int(record["sale"]), []).append(record)
        return grouped

    def _product_names(self, product_ids: list[int]) -> dict[int, str]:
        if not product_ids:
            return {}
        records = self._client.search(
            "product.product",
            domain=[("id", "in", product_ids)],
            fields=["rec_name"],
        )
        return {int(r["id"]): str(r.get("rec_name") or "") for r in records}

    def _sale_action(
        self,
        sale_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> SaleView:
        """Run one sale button transition and read the sale back."""
        try:
            result = self._client.action(
                "sale.sale",
                action,
                record_id=sale_id,
                fields=_SALE_FIELDS,
                context=context,
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                raise SaleNotFoundError(
                    f"Sale {sale_id} does not exist in the engine.",
                    details={"sale_id": sale_id},
                ) from exc
            raise
        if not isinstance(result, dict) or "id" not in result:
            raise TrytonAdapterError(
                f"Unexpected {action} response for sale {sale_id}: expected a record.",
                code="ENGINE_ERROR",
            )
        return self._sale_views([result])[0]

    def quote_sale(self, sale: SaleRef) -> SaleView:
        """Quote a draft sale (engine ``quote`` transition)."""
        return self._sale_action(sale.engine_id, "quote")

    def confirm_sale(self, sale: SaleRef) -> SaleView:
        """Confirm a quoted sale (engine ``confirm`` transition)."""
        return self._sale_action(sale.engine_id, "confirm")

    def process_sale(self, sale: SaleRef) -> SaleView:
        """Process a confirmed sale (engine ``process`` transition).

        Processing creates the invoice from the sale, copying the
        sale line's analytic entries onto the invoice lines. The
        invoice line's ``analytic_accounts_size`` is computed from
        the transaction-context company, so the action must run
        with the sale's company in context — without it the size
        evaluates to 0 and the copied entry fails validation
        ("too long (1 > 0)") as soon as any analytic root exists.
        """
        record = self._client.read("sale.sale", sale.engine_id, fields=["company"])
        if record is None:
            raise SaleNotFoundError(
                f"Sale {sale.engine_id} does not exist in the engine.",
                details={"sale_id": sale.engine_id},
            )
        return self._sale_action(
            sale.engine_id,
            "process",
            context={"company": int(record["company"])},
        )

    def cancel_sale(self, sale: SaleRef) -> SaleView:
        """Cancel a sale order (engine ``cancel`` transition)."""
        return self._sale_action(sale.engine_id, "cancel")

    # -- payment reads -----------------------------------------------------

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        """Read a payment back as a domain view (None when missing)."""
        record = self._client.read("account.payment", payment.engine_id, fields=_PAYMENT_FIELDS)
        if record is None:
            return None
        return self._payment_views([record])[0]

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        """Return the company's payments, newest first."""
        if kind is not None and kind not in ("receivable", "payable"):
            raise InvalidPaymentError(
                f"Payment kind filter must be 'receivable' or 'payable', got {kind!r}."
            )
        if state is not None and state not in _PAYMENT_STATES:
            raise InvalidPaymentError(
                f"Payment state filter must be one of {_PAYMENT_STATES}, got {state!r}."
            )
        domain: list[Any] = [("company", "=", company.engine_id)]
        if kind is not None:
            domain.append(("kind", "=", kind))
        if state is not None:
            domain.append(("state", "=", state))
        records = self._client.search(
            "account.payment",
            domain=domain,
            fields=_PAYMENT_FIELDS,
            order=[["date", "DESC"], ["id", "DESC"]],
            context={"company": company.engine_id},
        )
        return self._payment_views(records)

    def _payment_views(self, records: list[Record]) -> list[PaymentView]:
        """Map payment records to views, batching party/journal names and currencies."""
        if not records:
            return []
        party_names = self._party_names(sorted({int(r["party"]) for r in records}))
        journal_names = self._payment_journal_names(
            sorted({int(r["journal"]) for r in records})
        )
        currency_ids = sorted({int(r["currency"]) for r in records if r.get("currency")})
        codes = self._currency_codes(currency_ids)
        return [
            payment_record_to_view(
                record,
                party_names.get(int(record["party"]), ""),
                journal_names.get(int(record["journal"]), ""),
                codes.get(int(record["currency"]), "") if record.get("currency") else "",
            )
            for record in records
        ]

    def _payment_journal_names(self, journal_ids: list[int]) -> dict[int, str]:
        if not journal_ids:
            return {}
        records = self._client.search(
            "account.payment.journal",
            domain=[("id", "in", journal_ids)],
            fields=["name"],
        )
        return {int(r["id"]): str(r.get("name") or "") for r in records}

    # -- payment create + workflow (Phase 5, US-502) -----------------------

    def create_payment(self, command: CreatePayment) -> PaymentRef:
        """Create a draft payment in the engine.

        Pre-validation before delegating: the company books in IRR, the
        party exists, the payment journal exists for the company (or is
        resolved as the company's single payment journal), and a linked
        move line belongs to the company and is unreconciled. Line and
        account are mutually exclusive in the engine (the clearing
        module's domain forbids an account when a line is set).
        """
        company_id = command.company.engine_id
        context = {"company": company_id}
        self._require_irr_company(
            company_id, error_cls=InvalidPaymentError, subject="Payment"
        )
        self._require_party(command.party.engine_id)
        journal_id = self._resolve_payment_journal(command, company_id, context)
        values: dict[str, Any] = {
            "company": company_id,
            "party": command.party.engine_id,
            "journal": journal_id,
            "kind": command.kind,
            "date": date_to_tryton(command.date),
            "amount": money_to_tryton(command.amount),
            "reference": command.reference,
        }
        if command.line is not None:
            self._require_unreconciled_move_line(command.line.engine_id, company_id, context)
            values["line"] = command.line.engine_id
        elif command.account is not None:
            self._require_payment_account(command.account.engine_id, command.kind, company_id, context)
            values["account"] = command.account.engine_id
        record = self._client.create(
            "account.payment",
            values,
            fields=["id", "state"],
            context=context,
        )
        return PaymentRef(engine_id=int(record["id"]))

    def _resolve_payment_journal(
        self, command: CreatePayment, company_id: int, context: dict[str, Any]
    ) -> int:
        """Resolve the payment journal: explicit (verified), else the company's single one."""
        if command.journal is not None:
            journal_id = command.journal.engine_id
            records = self._client.search(
                "account.payment.journal",
                domain=[("id", "=", journal_id), ("company", "=", company_id)],
                fields=["company"],
                context=context,
            )
            if not records:
                raise InvalidPaymentError(
                    f"Payment journal {journal_id} not found for company {company_id}.",
                    details={"company_id": company_id, "journal_id": journal_id},
                )
            return journal_id
        records = self._client.search(
            "account.payment.journal",
            domain=[("company", "=", company_id)],
            fields=["company"],
            context=context,
        )
        if len(records) != 1:
            raise InvalidPaymentError(
                f"Company {company_id} has {len(records)} payment journals; "
                "pass an explicit payment journal.",
                details={"company_id": company_id, "journal_count": len(records)},
            )
        return int(records[0]["id"])

    def _require_unreconciled_move_line(
        self, line_id: int, company_id: int, context: dict[str, Any]
    ) -> None:
        """Fail fast when the linked move line is missing, foreign or reconciled."""
        records = self._client.search(
            "account.move.line",
            domain=[("id", "=", line_id)],
            fields=["move", "reconciliation"],
            context=context,
        )
        if not records:
            raise InvalidPaymentError(
                f"Move line {line_id} does not exist in the engine.",
                details={"line_id": line_id},
            )
        record = records[0]
        if record.get("reconciliation") is not None:
            raise InvalidPaymentError(
                f"Move line {line_id} is already reconciled; it cannot take a payment.",
                details={"line_id": line_id},
            )
        move = self._client.read(
            "account.move", int(record["move"]), fields=["company"], context=context
        )
        if move is None or int(move["company"]) != company_id:
            raise InvalidPaymentError(
                f"Move line {line_id} does not belong to company {company_id}.",
                details={"line_id": line_id, "company_id": company_id},
            )

    def _require_payment_account(
        self, account_id: int, kind: str, company_id: int, context: dict[str, Any]
    ) -> None:
        """Fail fast when the payment account is missing, closed or of the wrong type."""
        wanted_flag = "receivable" if kind == "receivable" else "payable"
        records = self._client.search(
            "account.account",
            domain=[("id", "=", account_id), ("company", "=", company_id)],
            fields=["closed", "active", "type"],
            context=context,
        )
        if not records:
            raise AccountNotFoundError(
                f"Payment account {account_id} not found for company {company_id}.",
                details={"company_id": company_id, "account_id": account_id},
            )
        record = records[0]
        if record.get("closed") or not record.get("active", True):
            raise InvalidPaymentError(
                f"Payment account {account_id} is closed or inactive.",
                details={"account_id": account_id},
            )
        if not self._account_type_flag(record.get("type"), wanted_flag):
            raise InvalidPaymentError(
                f"Payment account {account_id} is not a {wanted_flag} account.",
                details={"account_id": account_id, "kind": kind},
            )

    def _payment_action(
        self,
        payment_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> PaymentView:
        """Run one payment button transition and read the payment back."""
        try:
            result = self._client.action(
                "account.payment",
                action,
                record_id=payment_id,
                fields=_PAYMENT_FIELDS,
                context=context,
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                raise PaymentNotFoundError(
                    f"Payment {payment_id} does not exist in the engine.",
                    details={"payment_id": payment_id},
                ) from exc
            raise
        if not isinstance(result, dict) or "id" not in result:
            raise TrytonAdapterError(
                f"Unexpected {action} response for payment {payment_id}: expected a record.",
                code="ENGINE_ERROR",
            )
        return self._payment_views([result])[0]

    def submit_payment(self, payment: PaymentRef) -> PaymentView:
        """Submit a draft payment (engine ``submit`` transition)."""
        return self._payment_action(payment.engine_id, "submit")

    def approve_payment(self, payment: PaymentRef) -> PaymentView:
        """Approve a submitted payable payment (engine ``approve`` transition)."""
        return self._payment_action(payment.engine_id, "approve")

    def process_payment(self, payment: PaymentRef) -> PaymentView:
        """Move a submitted/approved payment to processing (engine ``process``).

        Via the hesabyar_setup module's ``process_rest`` REST action,
        which delegates to the engine's ``process`` -- NOT the
        ``proceed`` button, which is only visible for succeeded/failed
        manual payments (re-processing) and refuses submitted payments.
        """
        return self._payment_action(payment.engine_id, "process_rest")

    def succeed_payment(self, command: SucceedPayment) -> PaymentView:
        """Mark a processing payment succeeded (engine ``succeed`` transition).

        The clearing date travels in the REST transaction context
        (wire-format date, decoded server-side into
        ``Transaction().context['clearing_date']``); the clearing move
        is dated with it. Without a date the engine uses today.
        """
        context: dict[str, Any] | None = None
        if command.clearing_date is not None:
            context = {"clearing_date": date_to_tryton(command.clearing_date)}
        return self._payment_action(command.payment.engine_id, "succeed", context=context)

    def fail_payment(self, payment: PaymentRef) -> PaymentView:
        """Mark a processing/succeeded payment failed (engine ``fail`` transition)."""
        return self._payment_action(payment.engine_id, "fail")

    def process_payments(
        self, payments: Sequence[PaymentRef]
    ) -> Sequence[PaymentView]:
        """Move several submitted/approved payments to processing at once.

        One call to the hesabyar_setup module's ``process_rest_batch``
        action (no-id REST route, ids in the body), so a settlement
        batch's payments land in ONE ``account.payment.group``. Each
        payment is read back afterwards; a payment that vanished
        between the call and the read is a stable not-found error.
        """
        if not payments:
            return []
        self._client.action(
            "account.payment",
            "process_rest_batch",
            data={"payment_ids": [payment.engine_id for payment in payments]},
        )
        views: list[PaymentView] = []
        for payment in payments:
            view = self.get_payment(payment)
            if view is None:
                raise PaymentNotFoundError(
                    f"Payment {payment.engine_id} does not exist in the engine.",
                    details={"payment_id": payment.engine_id},
                )
            views.append(view)
        return views

    def get_payment_journal(
        self, journal: PaymentJournalRef
    ) -> PaymentJournalView | None:
        """Read a payment journal back as a domain view (None when missing)."""
        record = self._client.read(
            "account.payment.journal",
            journal.engine_id,
            fields=_PAYMENT_JOURNAL_FIELDS,
        )
        if record is None:
            return None
        return payment_journal_record_to_view(record)

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

        At least one of ``move`` / ``account`` is required (an
        unfiltered read would page the whole ledger). The search is
        paged through in full; lines are company-scoped through their
        move.
        """
        if move is None and account is None:
            raise InvalidJournalError(
                "list_move_lines needs at least one of move / account.",
                details={"company_id": company.engine_id},
            )
        domain: list[Any] = [("move.company", "=", company.engine_id)]
        if move is not None:
            domain.append(("move", "=", move.engine_id))
        if account is not None:
            domain.append(("account", "=", account.engine_id))
        if party is not None:
            domain.append(("party", "=", party.engine_id))
        if maturity_date is not None:
            domain.append(("maturity_date", "=", date_to_tryton(maturity_date)))
        if unreconciled_only:
            domain.append(("reconciliation", "=", None))
        context = {"company": company.engine_id}
        views: list[MoveLineView] = []
        offset = 0
        while True:
            page = self._client.search(
                "account.move.line",
                domain=domain,
                fields=_MOVE_LINE_FIELDS,
                limit=_TRIAL_BALANCE_PAGE_SIZE,
                offset=offset,
                order=[["id", "ASC"]],
                context=context,
            )
            views.extend(move_line_record_to_view(record) for record in page)
            if len(page) < _TRIAL_BALANCE_PAGE_SIZE:
                break
            offset += len(page)
        return views

    def reconcile_move_lines(self, command: ReconcileMoveLines) -> ReconciliationView:
        """Reconcile a set of move lines via the ``reconcile_rest`` wrapper.

        Pre-validation before calling the engine (the same conditions
        ``Reconciliation.check_lines`` enforces, raised as stable
        ledger errors): every line exists for the company, all lines
        share one account and one party, debits == credits, every line
        is 'valid' and unreconciled. The engine's own
        ``ReconciliationError`` is the backstop for a race between the
        read and the call.
        """
        company_id = command.company.engine_id
        line_ids = [line.engine_id for line in command.lines]
        records = self._client.search(
            "account.move.line",
            domain=[
                ("id", "in", line_ids),
                ("move.company", "=", company_id),
            ],
            fields=_MOVE_LINE_FIELDS,
            context={"company": company_id},
        )
        found = {int(record["id"]): record for record in records}
        missing = [line_id for line_id in line_ids if line_id not in found]
        if missing:
            raise InvalidJournalError(
                f"Move lines not found for company {company_id}: {missing}.",
                details={"company_id": company_id, "missing_lines": missing},
            )
        views = [move_line_record_to_view(found[line_id]) for line_id in line_ids]
        accounts = {view.account.engine_id for view in views}
        if len(accounts) != 1:
            raise InvalidJournalError(
                "Reconciliation lines must all be on the same account.",
                details={"account_ids": sorted(accounts)},
            )
        parties = {
            view.party.engine_id if view.party is not None else None
            for view in views
        }
        if len(parties) != 1:
            raise InvalidJournalError(
                "Reconciliation lines must all carry the same party.",
                details={"party_ids": sorted(p for p in parties if p is not None)},
            )
        for view in views:
            if view.state != "valid":
                raise InvalidJournalError(
                    f"Move line {view.ref.engine_id} is in state {view.state!r}; "
                    "only 'valid' lines can be reconciled.",
                    details={"line_id": view.ref.engine_id, "state": view.state},
                )
            if view.reconciliation is not None:
                raise InvalidJournalError(
                    f"Move line {view.ref.engine_id} is already reconciled.",
                    details={"line_id": view.ref.engine_id},
                )
        total = MoneyIRR.zero()
        for view in views:
            total = total.add(view.balance)
        if not total.is_zero:
            raise InvalidJournalError(
                "Reconciliation lines do not sum to zero "
                f"(net {total.amount} IRR); refusing to reconcile.",
                details={"net_irr": total.amount},
            )
        result = self._client.action(
            "account.move.line",
            "reconcile_rest",
            data={"line_ids": line_ids},
        )
        if not isinstance(result, dict) or result.get("reconciliation_id") is None:
            raise TrytonAdapterError(
                f"Unexpected reconcile_rest response for lines {line_ids}: "
                "expected a reconciliation record.",
                code=ENGINE_ERROR,
            )
        return ReconciliationView(
            ref=ReconciliationRef(engine_id=int(result["reconciliation_id"])),
            number=str(result.get("number") or ""),
            lines=tuple(command.lines),
        )

    # -- bank-statement reads ----------------------------------------------

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        """Read a bank/cash statement back as a domain view (None when missing)."""
        record = self._client.read(
            "account.statement", statement.engine_id, fields=_STATEMENT_FIELDS
        )
        if record is None:
            return None
        return self._statement_views([record])[0]

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        """Return the company's bank/cash statements, newest first."""
        if state is not None and state not in _STATEMENT_STATES:
            raise InvalidBankStatementError(
                f"Statement state filter must be one of {_STATEMENT_STATES}, got {state!r}."
            )
        domain: list[Any] = [("company", "=", company.engine_id)]
        if state is not None:
            domain.append(("state", "=", state))
        records = self._client.search(
            "account.statement",
            domain=domain,
            fields=_STATEMENT_FIELDS,
            order=[["date", "DESC"], ["id", "DESC"]],
            context={"company": company.engine_id},
        )
        return self._statement_views(records)

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        """Return the company's statement journals, ordered by name.

        One search on ``account.statement.journal`` (company-scoped),
        with account codes/names, currency codes and Shaba (IBAN)
        numbers batch-read for the journals found — one round-trip
        per batch, never one per journal. The Shaba is resolved per
        journal from its linked bank account's IBAN-type numbers; a
        journal without a bank account (or without an IBAN number)
        reports None. US-514 and US-516 each implemented this read;
        the merge unifies them (the IBAN batch is US-514's, the
        account code/name batch is US-516's).
        """
        company_id = company.engine_id
        context = {"company": company_id}
        records = self._client.search(
            "account.statement.journal",
            domain=[("company", "=", company_id)],
            fields=_STATEMENT_JOURNAL_FIELDS,
            order=[["name", "ASC"], ["id", "ASC"]],
            context=context,
        )
        if not records:
            return []
        account_ids = sorted({int(record["account"]) for record in records})
        names = self._account_code_names(account_ids, context)
        currency_ids = sorted({int(r["currency"]) for r in records if r.get("currency")})
        codes = self._currency_codes(currency_ids)
        bank_account_ids = sorted({int(r["bank_account"]) for r in records if r.get("bank_account")})
        ibans = self._iban_numbers(bank_account_ids)
        return [
            statement_journal_record_to_view(
                record,
                codes.get(int(record["currency"]), "") if record.get("currency") else "",
                ibans.get(int(record["bank_account"]))
                if record.get("bank_account")
                else None,
                account_code=names[int(record["account"])][0],
                account_name=names[int(record["account"])][1],
            )
            for record in records
        ]

    def get_general_journal(self, company: CompanyRef) -> JournalRef:
        """Return the company's general journal for system-created drafts.

        One search on ``account.journal`` (type ``general``). A single
        general journal is returned as-is; with several, the journal
        provisioned for the company (``"<Company Name> General
        Journal"`` — the provisioning convention) wins; with several
        and no company-named journal the choice is genuinely
        ambiguous and the call fails loudly instead of booking a
        system draft to the wrong journal.
        """
        company_record = self._require_irr_company_for_reads(company.engine_id)
        journals = self._client.search(
            "account.journal",
            domain=[("type", "=", "general")],
            fields=["name"],
            order=[["id", "ASC"]],
        )
        if len(journals) == 1:
            return JournalRef(engine_id=int(journals[0]["id"]))
        if not journals:
            raise InvalidJournalError(
                "The engine has no general journal; provision one before "
                "creating system drafts.",
                details={"company_id": company.engine_id},
            )
        # The company name is rec_name (it lives on the party) —
        # company.company has no readable "name" field.
        expected = f"{company_record.get('rec_name') or ''} General Journal"
        for journal in journals:
            if journal.get("name") == expected:
                return JournalRef(engine_id=int(journal["id"]))
        raise InvalidJournalError(
            "The engine has "
            f"{len(journals)} general journals and none is named after "
            "the company; specify the journal explicitly.",
            details={
                "company_id": company.engine_id,
                "general_journal_count": len(journals),
            },
        )

    def _statement_views(self, records: list[Record]) -> list[BankStatementView]:
        """Map statement records to views, batching lines, journal names and currencies."""
        if not records:
            return []
        statement_ids = [int(record["id"]) for record in records]
        context = {"company": int(records[0]["company"])}
        lines_by_statement = self._statement_lines(statement_ids, context)
        journal_names = self._statement_journal_names(
            sorted({int(r["journal"]) for r in records})
        )
        currency_ids = sorted({int(r["currency"]) for r in records if r.get("currency")})
        codes = self._currency_codes(currency_ids)
        return [
            statement_record_to_view(
                record,
                lines_by_statement.get(int(record["id"]), []),
                journal_names.get(int(record["journal"]), ""),
                codes.get(int(record["currency"]), "") if record.get("currency") else "",
            )
            for record in records
        ]

    def _statement_lines(
        self, statement_ids: list[int], context: dict[str, Any]
    ) -> dict[int, list[BankStatementLineView]]:
        if not statement_ids:
            return {}
        records = self._client.search(
            "account.statement.line",
            domain=[("statement", "in", statement_ids)],
            fields=_STATEMENT_LINE_FIELDS,
            order=[["id", "ASC"]],
            context=context,
        )
        party_names = self._party_names(
            sorted({int(r["party"]) for r in records if r.get("party")})
        )
        grouped: dict[int, list[BankStatementLineView]] = {}
        for record in records:
            grouped.setdefault(int(record["statement"]), []).append(
                statement_line_record_to_view(record, party_names)
            )
        return grouped

    def _statement_journal_names(self, journal_ids: list[int]) -> dict[int, str]:
        if not journal_ids:
            return {}
        records = self._client.search(
            "account.statement.journal",
            domain=[("id", "in", journal_ids)],
            fields=["name"],
        )
        return {int(r["id"]): str(r.get("name") or "") for r in records}

    # -- bank-statement writes (US-514 import + reconcile) -------------------

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        """Create a draft bank/cash statement with its lines.

        Pre-validation before delegating: the company books in IRR,
        the statement journal belongs to the company, every line
        account exists and is open for the company, every party
        exists, and every linked invoice is posted, belongs to the
        company, and uses the line's account (Tryton asserts the
        account match at validate time).
        """
        company_id = command.company.engine_id
        context = {"company": company_id}
        self._require_irr_company(
            company_id, error_cls=InvalidBankStatementError, subject="Bank statement"
        )
        self._require_statement_journal(command.journal.engine_id, company_id, context)
        self._require_statement_line_accounts(command, company_id, context)
        self._require_statement_line_parties(command)
        self._require_statement_line_invoices(command, company_id, context)
        lines = []
        for line in command.lines:
            values: dict[str, Any] = {
                "date": date_to_tryton(line.date),
                "amount": money_to_tryton(line.amount),
                "account": line.account.engine_id,
                "description": line.description,
            }
            if line.number is not None:
                values["number"] = line.number
            if line.party is not None:
                values["party"] = line.party.engine_id
            if line.related_invoice is not None:
                values["related_to"] = f"account.invoice,{line.related_invoice.engine_id}"
            lines.append(values)
        values = {
            "company": company_id,
            "journal": command.journal.engine_id,
            "name": command.name,
            "date": date_to_tryton(command.date),
            "lines": [["create", lines]],
        }
        if command.start_balance is not None:
            values["start_balance"] = money_to_tryton(command.start_balance)
        if command.end_balance is not None:
            values["end_balance"] = money_to_tryton(command.end_balance)
        record = self._client.create(
            "account.statement",
            values,
            fields=["id", "state"],
            context=context,
        )
        return BankStatementRef(engine_id=int(record["id"]))

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        """Write match results onto one line of a draft statement.

        Verifies the line belongs to the statement, the statement is
        a draft (Tryton locks line fields once validated), and — when
        an invoice link is set — that the invoice is posted, belongs
        to the statement's company, and uses the line's account.
        """
        line_record = self._client.read(
            "account.statement.line",
            command.line.engine_id,
            fields=["statement", "account", "party", "related_to"],
        )
        if line_record is None:
            raise InvalidBankStatementError(
                f"Statement line {command.line.engine_id} does not exist.",
                details={"line_id": command.line.engine_id},
            )
        if int(line_record["statement"]) != command.statement.engine_id:
            raise InvalidBankStatementError(
                f"Statement line {command.line.engine_id} does not belong to "
                f"statement {command.statement.engine_id}.",
                details={
                    "statement_id": command.statement.engine_id,
                    "line_id": command.line.engine_id,
                },
            )
        statement_record = self._client.read(
            "account.statement",
            command.statement.engine_id,
            fields=["company", "state"],
        )
        if statement_record is None:
            raise InvalidBankStatementError(
                f"Bank statement {command.statement.engine_id} does not exist.",
                details={"statement_id": command.statement.engine_id},
            )
        if str(statement_record.get("state")) != "draft":
            raise InvalidBankStatementError(
                f"Bank statement {command.statement.engine_id} is not a draft "
                f"(state {statement_record.get('state')!r}); its lines are read-only.",
                details={
                    "statement_id": command.statement.engine_id,
                    "state": str(statement_record.get("state")),
                },
            )
        company_id = int(statement_record["company"])
        context = {"company": company_id}
        values: dict[str, Any] = {}
        if command.account is not None:
            self._require_open_account(command.account.engine_id, company_id, context)
            values["account"] = command.account.engine_id
        if command.party is not None:
            self._require_party(command.party.engine_id)
            values["party"] = command.party.engine_id
        if command.clear_related_invoice:
            values["related_to"] = None
        if command.related_invoice is not None:
            account_id = (
                command.account.engine_id
                if command.account is not None
                else int(line_record["account"])
            )
            self._require_invoice_link(command.related_invoice.engine_id, account_id, company_id)
            values["related_to"] = f"account.invoice,{command.related_invoice.engine_id}"
        updated = self._client.write(
            "account.statement.line",
            command.line.engine_id,
            values,
            fields=["id"],
            context=context,
        )
        if updated is None:
            raise InvalidBankStatementError(
                f"Statement line {command.line.engine_id} disappeared during update.",
                details={"line_id": command.line.engine_id},
            )

    def _iban_numbers(self, bank_account_ids: list[int]) -> dict[int, str]:
        """IBAN number per bank account id (first IBAN number wins)."""
        if not bank_account_ids:
            return {}
        records = self._client.search(
            "bank.account.number",
            domain=[("account", "in", bank_account_ids), ("type", "=", "iban")],
            fields=["account", "number"],
            order=[["id", "ASC"]],
        )
        numbers: dict[int, str] = {}
        for record in records:
            account_id = int(record["account"])
            if account_id not in numbers:
                numbers[account_id] = str(record.get("number") or "")
        return numbers

    def _require_statement_journal(
        self, journal_id: int, company_id: int, context: dict[str, Any]
    ) -> None:
        records = self._client.search(
            "account.statement.journal",
            domain=[("id", "=", journal_id), ("company", "=", company_id)],
            fields=["company"],
            context=context,
        )
        if not records:
            raise InvalidBankStatementError(
                f"Statement journal {journal_id} not found for company {company_id}.",
                details={"company_id": company_id, "journal_id": journal_id},
            )

    def _require_open_account(
        self, account_id: int, company_id: int, context: dict[str, Any]
    ) -> None:
        records = self._client.search(
            "account.account",
            domain=[("id", "=", account_id), ("company", "=", company_id)],
            fields=["closed", "active"],
            context=context,
        )
        if not records:
            raise AccountNotFoundError(
                f"Account {account_id} not found for company {company_id}.",
                details={"company_id": company_id, "account_id": account_id},
            )
        if records[0].get("closed"):
            raise InvalidBankStatementError(
                f"Account {account_id} is closed and cannot receive statement lines.",
                details={"account_id": account_id},
            )
        if not records[0].get("active", True):
            raise InvalidBankStatementError(
                f"Account {account_id} is inactive and cannot receive statement lines.",
                details={"account_id": account_id},
            )

    def _require_statement_line_accounts(
        self, command: CreateBankStatement, company_id: int, context: dict[str, Any]
    ) -> None:
        wanted = sorted({line.account.engine_id for line in command.lines})
        records = self._client.search(
            "account.account",
            domain=[("id", "in", wanted), ("company", "=", company_id)],
            fields=["closed", "active"],
            context=context,
        )
        found = {int(r["id"]): r for r in records}
        missing = [account_id for account_id in wanted if account_id not in found]
        if missing:
            raise AccountNotFoundError(
                f"Accounts not found for company {company_id}: {missing}.",
                details={"company_id": company_id, "missing_accounts": missing},
            )
        for account_id, record in found.items():
            if record.get("closed"):
                raise InvalidBankStatementError(
                    f"Account {account_id} is closed and cannot receive statement lines.",
                    details={"account_id": account_id},
                )
            if not record.get("active", True):
                raise InvalidBankStatementError(
                    f"Account {account_id} is inactive and cannot receive statement lines.",
                    details={"account_id": account_id},
                )

    def _require_statement_line_parties(self, command: CreateBankStatement) -> None:
        wanted = sorted(
            {line.party.engine_id for line in command.lines if line.party is not None}
        )
        for party_id in wanted:
            self._require_party(party_id)

    def _require_statement_line_invoices(
        self, command: CreateBankStatement, company_id: int, context: dict[str, Any]
    ) -> None:
        for line in command.lines:
            if line.related_invoice is None:
                continue
            self._require_invoice_link(
                line.related_invoice.engine_id, line.account.engine_id, company_id
            )

    def _require_invoice_link(self, invoice_id: int, account_id: int, company_id: int) -> None:
        """Verify an invoice can be linked from a draft statement line.

        The invoice must exist, belong to the company, be posted (the
        ``related_to`` domain on a draft statement requires it), and
        use the line's account (``Line.reconcile`` asserts this at
        validate time — failing here is cheaper than failing there).
        """
        record = self._client.read(
            "account.invoice",
            invoice_id,
            fields=["company", "account", "state"],
        )
        if record is None:
            raise InvoiceNotFoundError(
                f"Invoice {invoice_id} does not exist in the engine.",
                details={"invoice_id": invoice_id},
            )
        if int(record["company"]) != company_id:
            raise InvalidBankStatementError(
                f"Invoice {invoice_id} belongs to another company.",
                details={"invoice_id": invoice_id, "company_id": company_id},
            )
        if str(record.get("state")) != "posted":
            raise InvalidBankStatementError(
                f"Invoice {invoice_id} is not posted (state {record.get('state')!r}); "
                "only posted invoices link to draft statement lines.",
                details={"invoice_id": invoice_id, "state": str(record.get("state"))},
            )
        if int(record["account"]) != account_id:
            raise InvalidBankStatementError(
                f"Invoice {invoice_id} uses account {record['account']}, not the "
                f"line's account {account_id}; the two must match for reconciliation.",
                details={"invoice_id": invoice_id, "account_id": account_id},
            )

    # -- shared move readers -------------------------------------------------

    def _read_move(
        self,
        move_id: int,
        fields: list[str] | None = None,
    ) -> Record | None:
        if fields is None:
            fields = ["company", "journal", "period", "date", "state", "description"]
        return self._client.read("account.move", move_id, fields=fields)

    def _read_move_lines(
        self,
        move_id: int,
        company_id: int,
        fields: list[str] | None = None,
    ) -> list[Record]:
        if fields is None:
            fields = ["account", "debit", "credit", "state", "description"]
        return self._client.search(
            "account.move.line",
            domain=[("move", "=", move_id)],
            fields=fields,
            context={"company": company_id},
        )

    def _read_move_period(self, move: Record) -> FiscalPeriodView:
        period_record = self._client.read(
            "account.period",
            int(move["period"]),
            fields=["name", "start_date", "end_date", "state"],
        )
        if period_record is None:
            raise PeriodClosedError(
                f"Fiscal period {move['period']} of move {move['id']} does not exist.",
                details={"move_id": int(move["id"])},
            )
        return period_record_to_view(period_record)

    # -- packaging & shipping (Phase 5 wave 2, US-515) -----------------------

    def list_stock_locations(self, company: CompanyRef) -> Sequence[StockLocationView]:
        """All stock locations visible to the company (warehouse tree included)."""
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "stock.location",
                fields=["name", "type", "code", "parent"],
                limit=_STOCK_PAGE_SIZE,
                offset=offset,
                order=[["name", "ASC"]],
                context={"company": company.engine_id},
            )
            records.extend(page)
            if len(page) < _STOCK_PAGE_SIZE:
                break
            offset += len(page)
        return [location_record_to_view(record) for record in records]

    def get_stock_location(self, location: StockLocationRef) -> StockLocationView:
        record = self._client.read(
            "stock.location", location.engine_id, fields=["name", "type", "code", "parent"]
        )
        if record is None:
            raise StockLocationNotFoundError(
                f"Stock location {location.engine_id} does not exist in the engine.",
                details={"location_id": location.engine_id},
            )
        return location_record_to_view(record)

    def create_packaging_product(self, command: CreatePackagingProduct) -> ProductRef:
        """Create a packaging material as a Tryton product.template (goods).

        The cost method is set explicitly (the product configuration
        default is ``fixed`` on the dev database); FIFO is refused
        unless ``product_cost_fifo`` is activated, because base Tryton
        only offers ``fixed``/``average``. The single variant is
        created explicitly afterwards: ``Template.default_products``
        returns ``[]`` for the REST user (``transaction.user == 0``),
        so a template created via REST has NO variant unless one is
        created for it — the Sao default of one empty variant does not
        apply here. ``purchase_uom`` is set to the default UoM: a
        purchasable template requires it (the ``purchase`` module
        validates it).
        """
        company_id = command.company.engine_id
        self._require_irr_company(
            company_id,
            error_cls=InvalidProductError,
            subject="Packaging material",
        )
        context = {"company": company_id}
        category = self._client.read(
            "product.category", command.category_engine_id, fields=["name"]
        )
        if category is None:
            raise InvalidProductError(
                f"Product category {command.category_engine_id} does not exist.",
                details={"category_id": command.category_engine_id},
            )
        if command.cost_method == "fifo" and not self._fifo_module_active():
            raise FifoUnavailableError(
                "FIFO costing needs the product_cost_fifo module, which is not "
                "activated on this engine. Activate it (or choose average) first.",
                details={"module": "product_cost_fifo"},
            )
        uom_id = command.uom_engine_id or self._resolve_unit_uom()
        record = self._client.create(
            "product.template",
            {
                "name": command.name,
                "code": command.code,
                "type": "goods",
                "consumable": False,
                "purchasable": True,
                "salable": False,
                "default_uom": uom_id,
                "purchase_uom": uom_id,
                "account_category": command.category_engine_id,
                "cost_price_method": command.cost_method,
            },
            fields=["id", "cost_price_method"],
            context=context,
        )
        template_id = int(record["id"])
        method_back = str(record.get("cost_price_method") or "")
        if method_back != command.cost_method:
            raise InvalidProductError(
                f"Cost method {command.cost_method!r} did not stick on template "
                f"{template_id} (engine reports {method_back!r}); refusing to "
                "return a material whose costing is silently wrong.",
                details={"template_id": template_id, "cost_method": method_back},
            )
        # The REST user gets no default variant (see docstring) — create
        # the single variant explicitly. If this fails, the template is
        # left orphaned (a template with no variant is harmless: it
        # cannot be stocked or sold, and the error surfaces the id).
        try:
            variant = self._client.create(
                "product.product",
                {"template": template_id},
                fields=["id"],
                context=context,
            )
        except TrytonAdapterError as exc:
            raise InvalidProductError(
                f"Template {template_id} was created but its variant could "
                f"not be created: {exc.message}",
                details={"template_id": template_id},
            ) from exc
        return ProductRef(engine_id=int(variant["id"]))

    def _fifo_module_active(self) -> bool:
        records = self._client.search(
            "ir.module",
            domain=[("name", "=", "product_cost_fifo"), ("state", "=", "activated")],
            fields=["id"],
        )
        return bool(records)

    def _resolve_unit_uom(self) -> int:
        records = self._client.search(
            "product.uom",
            domain=[("name", "=", "Unit")],
            fields=["id"],
            order=[["id", "ASC"]],
        )
        if not records:
            raise InvalidProductError(
                "No unit of measure named 'Unit' exists in the engine; pass "
                "uom_engine_id explicitly.",
            )
        return int(records[0]["id"])

    def get_packaging_product(
        self,
        product: ProductRef,
        company: CompanyRef,
        location: StockLocationRef | None = None,
    ) -> PackagingProductView:
        """Read a material's template data plus quantity and cost price."""
        context: dict[str, Any] = {"company": company.engine_id}
        if location is not None:
            context["locations"] = [location.engine_id]
        record = self._client.read(
            "product.product",
            product.engine_id,
            fields=["template", "quantity", "cost_price"],
            context=context,
        )
        if record is None:
            raise ProductNotFoundError(
                f"Product {product.engine_id} does not exist in the engine.",
                details={"product_id": product.engine_id},
            )
        template = self._client.read(
            "product.template",
            int(record["template"]),
            fields=_PACKAGING_TEMPLATE_FIELDS,
            context={"company": company.engine_id},
        )
        if template is None:
            raise ProductNotFoundError(
                f"Template of product {product.engine_id} does not exist.",
                details={"product_id": product.engine_id},
            )
        quantity = record.get("quantity")
        return packaging_product_record_to_view(
            record,
            template,
            quantity=quantity_from_tryton(quantity) if quantity is not None else None,
            cost_price=decimal_from_tryton(record.get("cost_price")),
        )

    def list_packaging_products(
        self,
        company: CompanyRef,
        products: Sequence[ProductRef],
        location: StockLocationRef | None = None,
    ) -> Sequence[PackagingProductView]:
        """Read several packaging products back in two searches.

        One ``product.product`` search (quantities + cost prices) plus
        one ``product.template`` search (names + methods), joined in
        memory. Products missing from the engine are skipped.
        """
        if not products:
            return []
        context: dict[str, Any] = {"company": company.engine_id}
        if location is not None:
            context["locations"] = [location.engine_id]
        ids = [product.engine_id for product in products]
        records = self._client.search(
            "product.product",
            domain=[("id", "in", ids)],
            fields=["template", "quantity", "cost_price"],
            context=context,
            limit=_STOCK_PAGE_SIZE,
        )
        template_ids = sorted({int(record["template"]) for record in records})
        templates = {
            int(template["id"]): template
            for template in self._client.search(
                "product.template",
                domain=[("id", "in", template_ids)],
                fields=_PACKAGING_TEMPLATE_FIELDS,
                context={"company": company.engine_id},
                limit=_STOCK_PAGE_SIZE,
            )
        }
        views: list[PackagingProductView] = []
        for record in records:
            template = templates.get(int(record["template"]))
            if template is None:
                continue
            quantity = record.get("quantity")
            views.append(
                packaging_product_record_to_view(
                    record,
                    template,
                    quantity=quantity_from_tryton(quantity)
                    if quantity is not None
                    else None,
                    cost_price=decimal_from_tryton(record.get("cost_price")),
                )
            )
        return views

    def get_product_quantity(
        self, product: ProductRef, location: StockLocationRef
    ) -> Decimal:
        record = self._client.read(
            "product.product",
            product.engine_id,
            fields=["quantity"],
            context={"locations": [location.engine_id]},
        )
        if record is None:
            raise ProductNotFoundError(
                f"Product {product.engine_id} does not exist in the engine.",
                details={"product_id": product.engine_id},
            )
        quantity = record.get("quantity")
        if quantity is None:
            raise TrytonAdapterError(
                f"Engine returned no quantity for product {product.engine_id}.",
                code=ENGINE_ERROR,
                details={"product_id": product.engine_id},
            )
        return quantity_from_tryton(quantity)

    def get_product_cost_price(
        self, product: ProductRef, company: CompanyRef
    ) -> Decimal | None:
        record = self._client.read(
            "product.product",
            product.engine_id,
            fields=["cost_price"],
            context={"company": company.engine_id},
        )
        if record is None:
            raise ProductNotFoundError(
                f"Product {product.engine_id} does not exist in the engine.",
                details={"product_id": product.engine_id},
            )
        return decimal_from_tryton(record.get("cost_price"))

    def upsert_order_point(self, command: UpsertOrderPoint) -> OrderPointView:
        """Create or update the purchase order point for (product, warehouse)."""
        context = {"company": command.company.engine_id}
        existing = self._client.search(
            "stock.order_point",
            domain=[
                ("product", "=", command.product.engine_id),
                ("location", "=", command.warehouse.engine_id),
                ("company", "=", command.company.engine_id),
                ("type", "=", "purchase"),
            ],
            fields=["id"],
            context=context,
        )
        values: dict[str, Any] = {
            "min_quantity": quantity_to_tryton(command.min_quantity),
            "target_quantity": quantity_to_tryton(command.target_quantity),
        }
        if existing:
            order_point_id = int(existing[0]["id"])
            self._client.write("stock.order_point", order_point_id, values, context=context)
        else:
            record = self._client.create(
                "stock.order_point",
                {
                    "product": command.product.engine_id,
                    "location": command.warehouse.engine_id,
                    "company": command.company.engine_id,
                    "type": "purchase",
                    **values,
                },
                fields=["id"],
                context=context,
            )
            order_point_id = int(record["id"])
        return self._read_order_point(order_point_id, context)

    def get_order_point(
        self,
        product: ProductRef,
        warehouse: StockLocationRef,
        company: CompanyRef,
    ) -> OrderPointView | None:
        records = self._client.search(
            "stock.order_point",
            domain=[
                ("product", "=", product.engine_id),
                ("location", "=", warehouse.engine_id),
                ("company", "=", company.engine_id),
                ("type", "=", "purchase"),
            ],
            fields=["product", "location", "type", "min_quantity", "target_quantity", "max_quantity"],
            context={"company": company.engine_id},
        )
        if not records:
            return None
        return order_point_record_to_view(records[0])

    def list_order_points(self, company: CompanyRef) -> Sequence[OrderPointView]:
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "stock.order_point",
                domain=[("company", "=", company.engine_id), ("type", "=", "purchase")],
                fields=[
                    "product",
                    "location",
                    "type",
                    "min_quantity",
                    "target_quantity",
                    "max_quantity",
                ],
                limit=_STOCK_PAGE_SIZE,
                offset=offset,
                order=[["product", "ASC"]],
                context={"company": company.engine_id},
            )
            records.extend(page)
            if len(page) < _STOCK_PAGE_SIZE:
                break
            offset += len(page)
        return [order_point_record_to_view(record) for record in records]

    def _read_order_point(self, order_point_id: int, context: dict[str, Any]) -> OrderPointView:
        record = self._client.read(
            "stock.order_point",
            order_point_id,
            fields=["product", "location", "type", "min_quantity", "target_quantity", "max_quantity"],
            context=context,
        )
        if record is None:
            raise OrderPointNotFoundError(
                f"Order point {order_point_id} does not exist in the engine.",
                details={"order_point_id": order_point_id},
            )
        return order_point_record_to_view(record)

    def create_consumption_move(self, command: CreateConsumptionMove) -> StockMoveView:
        """Create a draft consumption move and drive it to done with ``do``.

        ``do`` goes draft -> done directly and does NOT check stock
        availability, so consumption past zero goes negative by design
        (a warning signal, not an error). If ``do`` fails, the draft
        move id is attached to the error details so the caller can
        recover or cancel it.
        """
        company_id = command.company.engine_id
        # Validation only (the move itself carries no currency — see below).
        self._require_irr_company(
            company_id,
            error_cls=InvalidStockMoveError,
            subject="Consumption move",
        )
        context = {"company": company_id}
        product = self._client.read(
            "product.product",
            command.product.engine_id,
            fields=["template"],
            context=context,
        )
        if product is None:
            raise ProductNotFoundError(
                f"Product {command.product.engine_id} does not exist in the engine.",
                details={"product_id": command.product.engine_id},
            )
        template = self._client.read(
            "product.template",
            int(product["template"]),
            fields=["default_uom", "type"],
            context=context,
        )
        if template is None or template.get("type") != "goods":
            raise InvalidStockMoveError(
                f"Product {command.product.engine_id} is not a goods product; "
                "consumption moves need a stock-tracked material.",
                details={"product_id": command.product.engine_id},
            )
        # NOTE: no "currency" and no "unit_price" keys. stock.move's
        # domains require both to be None when unit_price_required is
        # False (standalone internal moves like this one); sending
        # either is rejected (contract test caught it against live
        # Tryton). The engine computes the accounting cost price itself
        # at ``do`` time; the read-back reports it.
        values: dict[str, Any] = {
            "product": command.product.engine_id,
            "quantity": quantity_to_tryton(command.quantity),
            "unit": int(template["default_uom"]),
            "from_location": command.from_location.engine_id,
            "to_location": command.to_location.engine_id,
            "planned_date": date_to_tryton(command.date),
            "company": company_id,
        }
        record = self._client.create(
            "stock.move", values, fields=["id"], context=context
        )
        move_id = int(record["id"])
        try:
            # NOT the stock "do" button: it is readonly on draft moves
            # out of a storage location (assignation_required), and
            # "assign" is not exposed over REST at all. The
            # hesabyar_setup module's assign_and_do_rest wrapper calls
            # stock Tryton's own assign + do in one transaction (see
            # tryton-modules/hesabyar_setup/.../move.py).
            self._client.action(
                "stock.move", "assign_and_do_rest", move_id, context=context
            )
        except Exception as exc:
            raise InvalidStockMoveError(
                f"Consumption move {move_id} could not be completed: {exc}",
                details={"move_id": move_id},
            ) from exc
        return self._read_stock_move(move_id, context)

    def list_consumption_moves(
        self,
        company: CompanyRef,
        from_location: StockLocationRef,
        to_location: StockLocationRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        product: ProductRef | None = None,
    ) -> Sequence[StockMoveView]:
        domain: list[tuple[str, str, object]] = [
            ("company", "=", company.engine_id),
            ("from_location", "=", from_location.engine_id),
            ("to_location", "=", to_location.engine_id),
            ("state", "=", "done"),
            ("effective_date", ">=", date_to_tryton(date_from)),
            ("effective_date", "<=", date_to_tryton(date_to)),
        ]
        if product is not None:
            domain.append(("product", "=", product.engine_id))
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "stock.move",
                domain=domain,
                fields=[
                    "product",
                    "quantity",
                    "from_location",
                    "to_location",
                    "state",
                    "company",
                    "planned_date",
                    "effective_date",
                    "cost_price",
                ],
                limit=_STOCK_PAGE_SIZE,
                offset=offset,
                order=[["effective_date", "ASC"], ["id", "ASC"]],
                context={"company": company.engine_id},
            )
            records.extend(page)
            if len(page) < _STOCK_PAGE_SIZE:
                break
            offset += len(page)
        return [stock_move_record_to_view(record) for record in records]

    def _read_stock_move(self, move_id: int, context: dict[str, Any]) -> StockMoveView:
        record = self._client.read(
            "stock.move",
            move_id,
            fields=[
                "product",
                "quantity",
                "from_location",
                "to_location",
                "state",
                "company",
                "planned_date",
                "effective_date",
                "cost_price",
            ],
            context=context,
        )
        if record is None:
            raise InvalidStockMoveError(
                f"Stock move {move_id} does not exist in the engine.",
                details={"move_id": move_id},
            )
        return stock_move_record_to_view(record)

    def list_purchase_requests(
        self,
        company: CompanyRef,
        state: str | None = None,
        after_id: int | None = None,
    ) -> Sequence[PurchaseRequestView]:
        domain: list[tuple[str, str, object]] = [("company", "=", company.engine_id)]
        if state is not None:
            domain.append(("state", "=", state))
        if after_id is not None:
            domain.append(("id", ">", after_id))
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "purchase.request",
                domain=domain,
                fields=[
                    "product",
                    "quantity",
                    "state",
                    "company",
                    "warehouse",
                    "supply_date",
                    "purchase_date",
                    "create_date",
                ],
                limit=_STOCK_PAGE_SIZE,
                offset=offset,
                order=[["id", "ASC"]],
                context={"company": company.engine_id},
            )
            records.extend(page)
            if len(page) < _STOCK_PAGE_SIZE:
                break
            offset += len(page)
        return [purchase_request_record_to_view(record) for record in records]

    def create_analytic_account(self, command: CreateAnalyticAccount) -> AnalyticAccountRef:
        values: dict[str, Any] = {
            "name": command.name,
            "code": command.code,
            "company": command.company.engine_id,
            "type": command.account_type,
        }
        if command.root is not None:
            values["root"] = command.root.engine_id
        if command.parent is not None:
            values["parent"] = command.parent.engine_id
        record = self._client.create(
            "analytic_account.account",
            values,
            fields=["id"],
            context={"company": command.company.engine_id},
        )
        return AnalyticAccountRef(engine_id=int(record["id"]))

    def get_analytic_account(self, account: AnalyticAccountRef) -> AnalyticAccountView:
        record = self._client.read(
            "analytic_account.account",
            account.engine_id,
            fields=["name", "code", "type", "company", "balance", "debit", "credit"],
        )
        if record is None:
            raise AnalyticAccountNotFoundError(
                f"Analytic account {account.engine_id} does not exist in the engine.",
                details={"analytic_account_id": account.engine_id},
            )
        return analytic_account_record_to_view(record)

    def list_analytic_lines(
        self, account: AnalyticAccountRef
    ) -> Sequence[AnalyticLineView]:
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "analytic_account.line",
                domain=[("account", "=", account.engine_id)],
                fields=["account", "date", "debit", "credit", "move_line"],
                limit=_STOCK_PAGE_SIZE,
                offset=offset,
                order=[["date", "ASC"], ["id", "ASC"]],
            )
            records.extend(page)
            if len(page) < _STOCK_PAGE_SIZE:
                break
            offset += len(page)
        return [analytic_line_record_to_view(record) for record in records]

    def create_inventory(self, command: CreateInventory) -> InventoryRef:
        self._require_irr_company(
            command.company.engine_id,
            error_cls=InvalidInventoryError,
            subject="Inventory count",
        )
        record = self._client.create(
            "stock.inventory",
            {
                "location": command.location.engine_id,
                "date": date_to_tryton(command.date),
                "company": command.company.engine_id,
                # Uncounted lines keep their expected quantity (no
                # adjustment). 'zero' would wipe the stock of any
                # material left uncounted — dangerous. The service
                # requires all lines counted before confirm anyway;
                # this only makes the engine robust if one slips
                # through (and lets complete_lines fill lines for
                # materials the user does not count this round).
                "empty_quantity": "keep",
            },
            fields=["id"],
            context={"company": command.company.engine_id},
        )
        return InventoryRef(engine_id=int(record["id"]))

    def get_inventory(self, inventory: InventoryRef) -> InventoryView:
        record = self._client.read(
            "stock.inventory",
            inventory.engine_id,
            fields=["number", "location", "date", "state", "company"],
        )
        if record is None:
            raise InventoryNotFoundError(
                f"Inventory {inventory.engine_id} does not exist in the engine.",
                details={"inventory_id": inventory.engine_id},
            )
        location = self.get_stock_location(
            StockLocationRef(engine_id=int(record["location"]))
        )
        line_records = self._client.search(
            "stock.inventory.line",
            domain=[("inventory", "=", inventory.engine_id)],
            fields=["product", "expected_quantity", "quantity"],
            order=[["id", "ASC"]],
        )
        names = self._inventory_product_names(
            sorted({int(r["product"]) for r in line_records})
        )
        lines = [
            inventory_line_record_to_view(record_line, names.get(int(record_line["product"]), ""))
            for record_line in line_records
        ]
        return inventory_record_to_view(record, location.name, lines)

    def _inventory_product_names(self, product_ids: list[int]) -> dict[int, str]:
        if not product_ids:
            return {}
        records = self._client.search(
            "product.product",
            domain=[("id", "in", product_ids)],
            fields=["template"],
        )
        template_ids = sorted({int(r["template"]) for r in records})
        templates = self._client.search(
            "product.template",
            domain=[("id", "in", template_ids)],
            fields=["name"],
        )
        template_names = {int(r["id"]): str(r.get("name") or "") for r in templates}
        return {
            int(r["id"]): template_names.get(int(r["template"]), "")
            for r in records
        }

    def _inventory_company_id(self, inventory: InventoryRef) -> int:
        """Read an inventory's company (needed as action context).

        ``confirm`` runs the tolerance check, which reads each
        product's ``cost_price`` — a company-dependent MultiValue
        that resolves to None without a company in context, crashing
        the confirm with ``None * Decimal`` (contract test caught it).
        """
        record = self._client.read(
            "stock.inventory", inventory.engine_id, fields=["company"]
        )
        if record is None:
            raise InventoryNotFoundError(
                f"Inventory {inventory.engine_id} does not exist in the engine.",
                details={"inventory_id": inventory.engine_id},
            )
        return int(record["company"])

    def complete_inventory_lines(self, inventory: InventoryRef) -> InventoryView:
        context = {"company": self._inventory_company_id(inventory)}
        self._client.action(
            "stock.inventory", "complete_lines", inventory.engine_id, context=context
        )
        return self.get_inventory(inventory)

    def update_inventory_line(self, command: UpdateInventoryLine) -> InventoryView:
        line = self._client.read(
            "stock.inventory.line",
            command.line.engine_id,
            fields=["inventory"],
        )
        if line is None:
            raise InvalidInventoryError(
                f"Inventory line {command.line.engine_id} does not exist.",
                details={"inventory_line_id": command.line.engine_id},
            )
        self._client.write(
            "stock.inventory.line",
            command.line.engine_id,
            {"quantity": quantity_to_tryton(command.quantity)},
        )
        return self.get_inventory(InventoryRef(engine_id=int(line["inventory"])))

    def confirm_inventory(self, inventory: InventoryRef) -> InventoryView:
        view = self.get_inventory(inventory)
        if view.state != "draft":
            raise InvalidInventoryError(
                f"Inventory {inventory.engine_id} is {view.state!r}, not draft; "
                "only a draft count can be confirmed.",
                details={"inventory_id": inventory.engine_id, "state": view.state},
            )
        self._client.action(
            "stock.inventory",
            "confirm",
            inventory.engine_id,
            context={"company": self._inventory_company_id(inventory)},
        )
        return self.get_inventory(inventory)

    def cancel_inventory(self, inventory: InventoryRef) -> InventoryView:
        view = self.get_inventory(inventory)
        if view.state != "draft":
            raise InvalidInventoryError(
                f"Inventory {inventory.engine_id} is {view.state!r}, not draft; "
                "only a draft count can be cancelled.",
                details={"inventory_id": inventory.engine_id, "state": view.state},
            )
        self._client.action(
            "stock.inventory",
            "cancel",
            inventory.engine_id,
            context={"company": self._inventory_company_id(inventory)},
        )
        return self.get_inventory(inventory)

    def get_sale_shipment_costs(self, sale: SaleRef) -> Sequence[SaleShipmentCostView]:
        records = self._client.search(
            "stock.shipment.cost_sale",
            domain=[("sale", "=", sale.engine_id)],
            fields=["sale", "shipment", "amount", "currency"],
            order=[["id", "ASC"]],
        )
        currency_ids = sorted({int(r["currency"]) for r in records if r.get("currency")})
        codes = self._currency_codes(currency_ids)
        return [
            sale_shipment_cost_record_to_view(
                record,
                codes.get(int(record["currency"]), "") if record.get("currency") else "",
            )
            for record in records
        ]

    def get_out_shipment_cost(self, shipment: OutShipmentRef) -> OutShipmentCostView:
        record = self._client.read(
            "stock.shipment.out",
            shipment.engine_id,
            fields=[
                "state",
                "carrier",
                "cost",
                "cost_currency",
                "cost_sale",
                "cost_sale_currency",
            ],
        )
        if record is None:
            raise OutShipmentNotFoundError(
                f"Outgoing shipment {shipment.engine_id} does not exist.",
                details={"shipment_id": shipment.engine_id},
            )
        carrier_name: str | None = None
        carrier = record.get("carrier")
        if carrier is not None:
            carrier_record = self._client.read(
                "carrier", int(carrier), fields=["rec_name"]
            )
            if carrier_record is not None:
                carrier_name = str(carrier_record.get("rec_name") or "") or None
        currency_ids = sorted(
            {
                int(record[field])
                for field in ("cost_currency", "cost_sale_currency")
                if record.get(field) is not None
            }
        )
        codes = self._currency_codes(currency_ids)
        return out_shipment_cost_record_to_view(record, carrier_name, codes)

    # -- cheques (Phase 5-4, US-531) ---------------------------------------
    #
    # The HesabYar layer reaches ``account_check_ir`` ONLY here. The
    # module's REST contract is ``docs/cheque-engine-contract.md``:
    # intake is a direct REST ``create`` on ``account.check``; every
    # transition is ONE ``transition_rest`` ``__rpc__`` action call.
    # The module performs the state change and its journal in a
    # single Tryton transaction and the adapter reads the record
    # back, so a failure leaves neither a journal without a state
    # nor a state without its journal. The action's return carries
    # the created move id — a plain read cannot attribute moves to
    # transitions (``moves`` is an unattributed one2many of move
    # ids), so the adapter merges the returned move into the view
    # and the registry accumulates the map across transitions.

    def _cheque_action_error(self, exc: TrytonAdapterError, operation: str) -> ChequeEngineError:
        """Translate an engine failure on a cheque call into a stable cheque error."""

        if exc.status_code in (403, 404):
            return ChequeEngineError(
                f"{operation}: the engine does not expose account_check_ir "
                "(the module is not installed, or its REST actions are not "
                f"exposed). Original error: {exc.message}",
                details={"operation": operation, "status_code": exc.status_code},
            )
        return ChequeEngineError(
            f"{operation}: {exc.message}",
            details={"operation": operation, "status_code": exc.status_code},
        )

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        """Register a cheque in the engine (intake, NO journal).

        Direct REST ``create`` on ``account.check`` — the module
        exposes no ``create_rest`` action (US-530 scaffold: the
        model validates + defaults itself). Only the fields the
        model actually has travel: the 24-field payload's
        issuer/payee names, national ids, order/customer refs,
        reason and scan ref have NO engine field — they stay in the
        HesabYar registry, and the issuer identity reaches the
        engine only as ``party``. When the command carries no
        party, it is resolved here (find-or-create by national id →
        ``party.code``) — every cheque chart account is
        ``party_required``, so no journal can post without one.
        ``sayad_registered`` maps to ``sayad_status``
        (``registered`` | ``unregistered``). For an issued cheque
        the resolved party is the BENEFICIARY (payee national id),
        not the issuer; the checkbook (when given) and the secured
        obligation travel to the engine record as well.
        """
        company_id = command.company.engine_id
        context = {"company": company_id}
        self._require_irr_company(company_id)
        party_id = (
            command.party.engine_id
            if command.party is not None
            else self._resolve_cheque_party(command, context)
        )
        payload: dict[str, Any] = {
            "company": company_id,
            "kind": command.kind,
            "purpose": command.purpose,
            "sayad_id": command.sayad_id,
            "amount": money_to_tryton(command.amount),
            "issue_date": date_to_tryton(command.issue_date),
            "due_date": date_to_tryton(command.due_date),
            "sayad_status": (
                "registered" if command.sayad_registered else "unregistered"
            ),
            "source": command.source,
            **({"party": party_id} if party_id is not None else {}),
            **({"number": command.leaf_number} if command.leaf_number else {}),
            **({"branch": command.issuer_branch} if command.issuer_branch else {}),
            **(
                {"issuer_account_number": command.issuer_account_number}
                if command.issuer_account_number
                else {}
            ),
            **({"invoice": command.invoice_engine_id} if command.invoice_engine_id else {}),
            **(
                {"installment_leg": command.installment_leg_id}
                if command.installment_leg_id
                else {}
            ),
            **(
                {"checkbook": command.checkbook_engine_id}
                if command.checkbook_engine_id is not None
                else {}
            ),
            **(
                {"secured_obligation": command.secured_obligation}
                if command.secured_obligation
                else {}
            ),
        }
        try:
            record = self._client.create(
                "account.check", payload, fields=CHEQUE_FIELDS, context=context
            )
        except TrytonAdapterError as exc:
            raise self._cheque_action_error(exc, "create_engine_cheque") from exc
        if isinstance(record, dict) and record.get("id") is not None:
            view = self.get_engine_cheque(ChequeEngineRef(engine_id=int(record["id"])))
            if view is not None:
                return view
            raise ChequeEngineError(
                "The engine created the cheque but it cannot be read back.",
                details={"engine_id": int(record["id"])},
            )
        raise ChequeEngineError(
            "The engine did not return the created cheque record.",
            details={"operation": "create_engine_cheque"},
        )

    def _resolve_cheque_party(
        self, command: CreateEngineCheque, context: dict[str, Any]
    ) -> int | None:
        """Find-or-create the cheque's party by national id.

        The natural key is the national id (no ``party.identifier``
        model is installed): a party whose ``code`` is the national
        id is reused; otherwise one is created. For a received
        cheque the party is the ISSUER (the journals need the
        issuer); for an issued cheque it is the BENEFICIARY (the
        payee — سند ۱۰/۱۱/۱۲/۱۳ are per-supplier). None when the
        command carries no national id — the cheque is then created
        without a party and its first journal fails closed in the
        engine.
        """
        if command.kind == "issued":
            party_name = command.payee_name
            national_id = (command.payee_national_id or "").strip()
        else:
            party_name = command.issuer_name
            national_id = (command.issuer_national_id or "").strip()
        if not national_id:
            return None
        try:
            records = self._client.search(
                "party.party",
                domain=[("code", "=", national_id)],
                fields=["id"],
                context=context,
            )
        except TrytonAdapterError as exc:
            raise self._cheque_action_error(exc, "create_engine_cheque") from exc
        if records:
            return int(records[0]["id"])
        try:
            record = self._client.create(
                "party.party",
                {"name": party_name, "code": national_id},
                context=context,
            )
        except TrytonAdapterError as exc:
            raise self._cheque_action_error(exc, "create_engine_cheque") from exc
        if isinstance(record, dict) and record.get("id") is not None:
            return int(record["id"])
        # The create answered without a record (no readable fields):
        # re-search — the party is there if the create committed.
        try:
            records = self._client.search(
                "party.party",
                domain=[("code", "=", national_id)],
                fields=["id"],
                context=context,
            )
        except TrytonAdapterError as exc:
            raise self._cheque_action_error(exc, "create_engine_cheque") from exc
        if records:
            return int(records[0]["id"])
        raise ChequeEngineError(
            "The engine created the cheque party but it cannot be read back.",
            details={"national_id": national_id},
        )

    def _resolve_issuer_party(
        self, command: CreateEngineCheque, context: dict[str, Any]
    ) -> int | None:
        """Backwards-compatible alias: party resolution for a cheque."""
        return self._resolve_cheque_party(command, context)

    def get_engine_cheque(self, cheque: ChequeEngineRef) -> ChequeEngineView | None:
        """Read an engine cheque back as a domain view (None when missing)."""
        try:
            record = self._client.read(
                "account.check", cheque.engine_id, fields=CHEQUE_FIELDS
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                return None
            raise self._cheque_action_error(exc, "get_engine_cheque") from exc
        if record is None:
            return None
        return cheque_record_to_view(record)

    def list_engine_cheques(
        self, company: CompanyRef, *, state: str | None = None
    ) -> Sequence[ChequeEngineView]:
        """Return the company's engine cheques, newest first."""
        domain: list[Any] = [("company", "=", company.engine_id)]
        if state is not None:
            domain.append(("state", "=", state))
        records: list[Record] = []
        offset = 0
        while True:
            page = self._client.search(
                "account.check",
                domain=domain,
                fields=CHEQUE_FIELDS,
                order=[["id", "DESC"]],
                limit=_CHART_PAGE_SIZE,
                offset=offset,
                context={"company": company.engine_id},
            )
            records.extend(page)
            if len(page) < _CHART_PAGE_SIZE:
                break
            offset += len(page)
        return [cheque_record_to_view(record) for record in records]

    def transition_engine_cheque(self, command: TransitionEngineCheque) -> ChequeEngineView:
        """Run one cheque lifecycle transition in the engine, atomically.

        One ``transition_rest`` action call; the module performs the
        state change and its journal (§2.4 سند ۱–۱۳) in a single
        Tryton transaction. The action returns ``{"id", "state",
        "move_id", "move_ids"}`` — ``move_id`` is the journal this
        transition created (null when the transition has no journal,
        e.g. expiry, or when it is deferred); ``move_ids`` is the
        full map for transitions that post more than one move
        (``call_in``: ۹الف + ۹ب). The adapter reads the record back
        for the full view and merges the returned moves into it; the
        HesabYar registry accumulates the move map across transitions
        (a plain read cannot attribute moves to transitions).
        """
        company_id = command.company.engine_id
        context = {"company": company_id}
        self._require_irr_company(company_id)
        data: dict[str, Any] = {
            "transition": command.transition,
            "auto_post": command.auto_post,
            "defer_journal": command.defer_journal,
            **(
                {"destination_account": command.destination_account.engine_id}
                if command.destination_account is not None
                else {}
            ),
            **({"bounce_destination": command.bounce_destination} if command.bounce_destination else {}),
            **({"bounce_reason": command.bounce_reason} if command.bounce_reason else {}),
            **(
                {"bounce_date": date_to_tryton(command.bounce_date)}
                if command.bounce_date is not None
                else {}
            ),
            **(
                {"certificate_reference": command.certificate_reference}
                if command.certificate_reference
                else {}
            ),
            **({"endorsee_name": command.endorsee_name} if command.endorsee_name else {}),
            **({"endorsee_type": command.endorsee_type} if command.endorsee_type else {}),
            **(
                {"sayad_transfer_reference": command.sayad_transfer_reference}
                if command.sayad_transfer_reference
                else {}
            ),
            **({"storage_location": command.storage_location} if command.storage_location else {}),
            **(
                {"statement_reference": command.statement_reference}
                if command.statement_reference
                else {}
            ),
            **({"mismatch_detail": command.mismatch_detail} if command.mismatch_detail else {}),
            **(
                {"accountant_approval": command.accountant_approval}
                if command.accountant_approval
                else {}
            ),
            **({"void_reason": command.void_reason} if command.void_reason else {}),
        }
        try:
            result = self._client.action(
                "account.check",
                "transition_rest",
                record_id=command.cheque.engine_id,
                data=data,
                context=context,
            )
        except TrytonAdapterError as exc:
            raise self._cheque_action_error(exc, "transition_engine_cheque") from exc
        view = self.get_engine_cheque(command.cheque)
        if view is None:
            raise ChequeEngineError(
                "The engine transitioned the cheque but it cannot be read back.",
                details={"engine_id": command.cheque.engine_id, "transition": command.transition},
            )
        move_ids: dict[str, int] = {}
        if isinstance(result, dict):
            # ``call_in`` posts TWO moves in one transaction (۹الف +
            # ۹ب): the action returns the full map. A single-move
            # transition returns ``move_id`` only (US-531 shape).
            raw_map = result.get("move_ids")
            if isinstance(raw_map, dict):
                for key, value in raw_map.items():
                    if value is not None:
                        move_ids[str(key)] = int(value)
            move_id = result.get("move_id")
            if move_id is not None:
                move_ids.setdefault(command.transition, int(move_id))
        if move_ids:
            view = replace(
                view,
                move_ids={**view.move_ids, **move_ids},
            )
        return view

    def list_engine_checkbooks(
        self, company: CompanyRef
    ) -> Sequence[CheckbookEngineView]:
        """Return the company's checkbooks, newest first (US-532).

        The view carries the engine-computed leaf counts (used /
        voided / remaining) — the checkbook page (report 8) data.
        """
        context = {"company": company.engine_id}
        self._require_irr_company(company.engine_id)
        records: list[Record] = []
        offset = 0
        while True:
            try:
                page = self._client.search(
                    "account.checkbook",
                    domain=[("company", "=", company.engine_id)],
                    fields=CHECKBOOK_FIELDS,
                    order=[["id", "DESC"]],
                    limit=_CHART_PAGE_SIZE,
                    offset=offset,
                    context=context,
                )
            except TrytonAdapterError as exc:
                raise self._cheque_action_error(exc, "list_engine_checkbooks") from exc
            records.extend(page)
            if len(page) < _CHART_PAGE_SIZE:
                break
            offset += len(page)
        return [checkbook_record_to_view(record) for record in records]

    def get_engine_checkbook(
        self, company: CompanyRef, checkbook: CheckbookEngineRef
    ) -> CheckbookEngineView | None:
        """Read one engine checkbook back (None when missing / other company)."""
        try:
            record = self._client.read(
                "account.checkbook",
                checkbook.engine_id,
                fields=CHECKBOOK_FIELDS,
                context={"company": company.engine_id},
            )
        except TrytonAdapterError as exc:
            if exc.status_code == 404:
                return None
            raise self._cheque_action_error(exc, "get_engine_checkbook") from exc
        if record is None:
            return None
        view = checkbook_record_to_view(record)
        if view.company.engine_id != company.engine_id:
            return None
        return view
