"""Mapping between Tryton REST records and HesabYar domain objects.

Money rule (design-eng spec §8.3): canonical money is integer IRR. A Tryton
amount that arrives as a Decimal is converted to MoneyIRR only after proving
it is finite and integral; a fractional Rial value is REJECTED with
``InvalidMoneyError`` -- never silently rounded, never converted to Toman.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

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
from hesabyar.domain.accounting.value_objects import (
    AccountView,
    AnalyticAccountView,
    AnalyticLineView,
    BankStatementLineView,
    BankStatementView,
    CheckbookEngineView,
    ChequeEngineView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    InventoryLineView,
    InventoryView,
    InvoiceLineView,
    InvoiceView,
    JournalDraftView,
    JournalLineView,
    MoveLineView,
    OrderPointView,
    OutShipmentCostView,
    PackagingProductView,
    PaymentJournalView,
    PaymentView,
    PostedJournalView,
    PurchaseRequestView,
    SaleLineView,
    SaleShipmentCostView,
    SaleView,
    StatementJournalView,
    StockLocationView,
    StockMoveView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import InvalidMoneyError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.contracts import Record


def money_from_tryton(value: object) -> MoneyIRR:
    """Convert a decoded Tryton amount to MoneyIRR.

    Accepts int and integral Decimal/str. Rejects float (forbidden in
    financial paths) and any fractional Rial value.
    """
    if isinstance(value, bool):
        raise InvalidMoneyError("Boolean is not a valid monetary amount.")
    if isinstance(value, MoneyIRR):
        return value
    if isinstance(value, int):
        return MoneyIRR(value)
    if isinstance(value, float):
        raise InvalidMoneyError(
            "Float amount from the engine is forbidden; amounts must be integer IRR."
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidMoneyError(f"Non-finite Decimal amount from the engine: {value!r}.")
        if value != value.to_integral_value():
            raise InvalidMoneyError(
                f"Fractional Rial amount from the engine is rejected (never rounded): {value!r}."
            )
        return MoneyIRR(int(value))
    if isinstance(value, str):
        try:
            return money_from_tryton(Decimal(value.strip()))
        except InvalidMoneyError:
            raise
        except Exception as exc:
            raise InvalidMoneyError(f"Cannot parse engine amount {value!r}.") from exc
    raise InvalidMoneyError(f"Unsupported engine amount type: {type(value).__name__}.")


def money_to_tryton(money: MoneyIRR) -> Decimal:
    """Convert MoneyIRR to the Decimal the REST wire encoder turns into a Decimal dict."""
    return Decimal(money.amount)


def date_from_tryton(value: object) -> FiscalDate:
    """Convert a decoded Tryton date to FiscalDate."""
    if isinstance(value, FiscalDate):
        return value
    if isinstance(value, datetime.datetime):
        raise ValueError("Expected a date from the engine, got a datetime.")
    if isinstance(value, datetime.date):
        return FiscalDate.from_gregorian(value)
    if isinstance(value, str):
        return FiscalDate.from_iso_str(value)
    raise ValueError(f"Unsupported engine date value: {value!r}.")


def date_to_tryton(day: FiscalDate) -> datetime.date:
    """Convert FiscalDate to the date the REST wire encoder turns into a date dict."""
    return day.gregorian_date


def quantity_from_tryton(value: object) -> Decimal:
    """Convert a decoded Tryton quantity to an exact Decimal.

    Quantities arrive as JSON numbers (Python int/float) because Tryton
    stores them in Float fields. A float is converted through ``str`` so
    the decimal value the engine sent survives exactly (``1.0`` ->
    ``Decimal('1.0')``); it is never used in float arithmetic afterwards.
    Non-finite values are rejected.
    """
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid quantity.")
    if isinstance(value, Decimal):
        quantity = value
    elif isinstance(value, int):
        quantity = Decimal(value)
    elif isinstance(value, float):
        quantity = Decimal(str(value))
    elif isinstance(value, str):
        try:
            quantity = Decimal(value.strip())
        except Exception as exc:
            raise ValueError(f"Cannot parse engine quantity {value!r}.") from exc
    else:
        raise ValueError(f"Unsupported engine quantity value: {value!r}.")
    if not quantity.is_finite():
        raise ValueError(f"Non-finite quantity from the engine: {value!r}.")
    return quantity


def quantity_to_tryton(quantity: Decimal) -> int | float:
    """Convert a Decimal quantity to the JSON number Tryton's Float field takes.

    Integral quantities go as ints (exact); fractional ones as floats,
    which is the precision the engine's Float field itself offers.
    """
    if quantity == quantity.to_integral_value():
        return int(quantity)
    return float(quantity)


def company_record_to_view(
    record: Record, currency_code: str, *, national_id: str | None = None
) -> CompanyView:
    """Map a company.company record to a CompanyView.

    Note: reading the ``name`` field of company.company via REST fails on
    Tryton 8.2.3 (500, KeyError 'name' -- a Function-field quirk), so callers
    must request ``rec_name`` instead. ``national_id`` comes from the
    company party's ``code`` (US-533 payee check) when the caller
    fetched it; None when it was not fetched or not recorded.
    """
    return CompanyView(
        ref=CompanyRef(engine_id=int(record["id"])),
        name=str(record.get("rec_name") or ""),
        currency_code=currency_code,
        national_id=national_id,
    )


def account_record_to_view(
    record: Record, company: CompanyRef, type_names: dict[int, str]
) -> AccountView:
    """Map an account.account record to an AccountView."""
    type_id = record.get("type")
    parent_id = record.get("parent")
    return AccountView(
        ref=AccountRef(engine_id=int(record["id"])),
        company=company,
        code=str(record.get("code") or ""),
        name=str(record.get("name") or ""),
        type_name=type_names.get(int(type_id)) if type_id is not None else None,
        parent=AccountRef(engine_id=int(parent_id)) if parent_id is not None else None,
        closed=bool(record.get("closed", False)),
        party_required=bool(record.get("party_required", False)),
        active=bool(record.get("active", True)),
    )


def fiscal_year_record_to_view(record: Record) -> FiscalYearView:
    """Map an account.fiscalyear record to a FiscalYearView."""
    return FiscalYearView(
        ref=FiscalYearRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        name=str(record.get("name") or ""),
        start_date=date_from_tryton(record["start_date"]),
        end_date=date_from_tryton(record["end_date"]),
        state=str(record.get("state") or ""),
    )


def period_record_to_view(record: Record) -> FiscalPeriodView:
    """Map an account.period record to a FiscalPeriodView.

    ``id`` / ``fiscalyear`` / ``type`` are optional in the record: readers
    that only need the period's dates and state (e.g. journal validation)
    do not request them.
    """
    record_id = record.get("id")
    fiscal_year_id = record.get("fiscalyear")
    return FiscalPeriodView(
        name=str(record.get("name") or ""),
        start_date=date_from_tryton(record["start_date"]),
        end_date=date_from_tryton(record["end_date"]),
        state=str(record.get("state") or ""),
        ref=FiscalPeriodRef(engine_id=int(record_id)) if record_id is not None else None,
        fiscal_year=FiscalYearRef(engine_id=int(fiscal_year_id))
        if fiscal_year_id is not None
        else None,
        period_type=str(record.get("type") or "standard"),
    )


def parse_move_origin(value: object) -> PostedJournalRef | None:
    """Parse a Tryton move ``origin`` Reference into a posted-journal ref.

    References arrive as ``"account.move,<id>"`` strings; any other value
    (None, a fiscal-year origin, a malformed string) means the move does
    not reverse another journal.
    """
    if not isinstance(value, str):
        return None
    model, sep, raw_id = value.partition(",")
    if not sep or model != "account.move":
        return None
    try:
        return PostedJournalRef(engine_id=int(raw_id))
    except ValueError:
        return None


def parse_invoice_reference(value: object) -> InvoiceRef | None:
    """Parse a Tryton ``related_to`` Reference into an invoice ref.

    References arrive as ``"account.invoice,<id>"`` strings; any other
    value (None, another model, a malformed string) means the record is
    not linked to an invoice.
    """
    if not isinstance(value, str):
        return None
    model, sep, raw_id = value.partition(",")
    if not sep or model != "account.invoice":
        return None
    try:
        return InvoiceRef(engine_id=int(raw_id))
    except ValueError:
        return None


def line_record_to_view(record: Record) -> JournalLineView:
    """Map an account.move.line record to a JournalLineView."""
    return JournalLineView(
        account=AccountRef(engine_id=int(record["account"])),
        debit=money_from_tryton(record.get("debit", 0)),
        credit=money_from_tryton(record.get("credit", 0)),
        state=str(record.get("state") or ""),
        description=str(record.get("description") or ""),
    )


def move_record_to_draft_view(
    record: Record,
    period: FiscalPeriodView,
    lines: list[JournalLineView],
) -> JournalDraftView:
    """Map an account.move record (+ its period and lines) to a JournalDraftView."""
    return JournalDraftView(
        ref=JournalDraftRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        journal=JournalRef(engine_id=int(record["journal"])),
        period=period,
        date=date_from_tryton(record["date"]),
        state=str(record.get("state") or ""),
        description=str(record.get("description") or ""),
        lines=tuple(lines),
    )


def move_record_to_posted_view(
    record: Record,
    period: FiscalPeriodView,
    lines: list[JournalLineView],
) -> PostedJournalView:
    """Map a posted account.move record (+ its period and lines) to a PostedJournalView."""
    post_date = record.get("post_date")
    return PostedJournalView(
        ref=PostedJournalRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        journal=JournalRef(engine_id=int(record["journal"])),
        period=period,
        date=date_from_tryton(record["date"]),
        post_date=date_from_tryton(post_date) if post_date is not None else None,
        number=str(record.get("number") or ""),
        description=str(record.get("description") or ""),
        origin=parse_move_origin(record.get("origin")),
        lines=tuple(lines),
    )


def invoice_line_record_to_view(record: Record) -> InvoiceLineView:
    """Map an account.invoice.line record to an InvoiceLineView."""
    return InvoiceLineView(
        account=AccountRef(engine_id=int(record["account"])),
        quantity=quantity_from_tryton(record.get("quantity", 0)),
        unit_price=money_from_tryton(record.get("unit_price", 0)),
        amount=money_from_tryton(record.get("amount", 0)),
        line_type=str(record.get("type") or "line"),
        description=str(record.get("description") or ""),
    )


def invoice_record_to_view(
    record: Record,
    lines: list[InvoiceLineView],
    party_name: str,
    currency_code: str,
) -> InvoiceView:
    """Map an account.invoice record (+ its lines) to an InvoiceView.

    Amounts are the engine's computed Function totals; ``journal`` is
    None for drafts created without one (the engine requires a journal
    only from validation onwards).
    """
    invoice_date = record.get("invoice_date")
    accounting_date = record.get("accounting_date")
    journal_id = record.get("journal")
    amount_to_pay = record.get("amount_to_pay")
    move_id = record.get("move")
    return InvoiceView(
        ref=InvoiceRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        invoice_type=str(record.get("type") or ""),
        party=PartyRef(engine_id=int(record["party"])),
        party_name=party_name,
        number=str(record["number"]) if record.get("number") is not None else None,
        invoice_date=date_from_tryton(invoice_date) if invoice_date is not None else None,
        accounting_date=date_from_tryton(accounting_date)
        if accounting_date is not None
        else None,
        state=str(record.get("state") or ""),
        description=str(record.get("description") or ""),
        reference=str(record.get("reference") or ""),
        journal=JournalRef(engine_id=int(journal_id)) if journal_id is not None else None,
        account=AccountRef(engine_id=int(record["account"])),
        currency_code=currency_code,
        untaxed_amount=money_from_tryton(record.get("untaxed_amount", 0)),
        tax_amount=money_from_tryton(record.get("tax_amount", 0)),
        total_amount=money_from_tryton(record.get("total_amount", 0)),
        lines=tuple(lines),
        amount_to_pay=money_from_tryton(amount_to_pay) if amount_to_pay is not None else None,
        move=PostedJournalRef(engine_id=int(move_id)) if move_id is not None else None,
    )


def payment_record_to_view(
    record: Record,
    party_name: str,
    journal_name: str,
    currency_code: str,
) -> PaymentView:
    """Map an account.payment record to a PaymentView."""
    move_line_id = record.get("line")
    clearing_move_id = record.get("clearing_move")
    return PaymentView(
        ref=PaymentRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        number=str(record.get("number") or ""),
        kind=str(record.get("kind") or ""),
        party=PartyRef(engine_id=int(record["party"])),
        party_name=party_name,
        date=date_from_tryton(record["date"]),
        amount=money_from_tryton(record.get("amount", 0)),
        state=str(record.get("state") or ""),
        reference=str(record.get("reference") or ""),
        journal_name=journal_name,
        currency_code=currency_code,
        move_line=MoveLineRef(engine_id=int(move_line_id)) if move_line_id is not None else None,
        clearing_move=(
            JournalDraftRef(engine_id=int(clearing_move_id))
            if clearing_move_id is not None
            else None
        ),
    )


def move_line_record_to_view(record: Record) -> MoveLineView:
    """Map an account.move.line record to a MoveLineView (US-522)."""
    maturity_date = record.get("maturity_date")
    party_id = record.get("party")
    reconciliation_id = record.get("reconciliation")
    return MoveLineView(
        ref=MoveLineRef(engine_id=int(record["id"])),
        move=JournalDraftRef(engine_id=int(record["move"])),
        account=AccountRef(engine_id=int(record["account"])),
        debit=money_from_tryton(record.get("debit", 0)),
        credit=money_from_tryton(record.get("credit", 0)),
        state=str(record.get("state") or ""),
        party=PartyRef(engine_id=int(party_id)) if party_id is not None else None,
        maturity_date=date_from_tryton(maturity_date) if maturity_date is not None else None,
        reconciliation=(
            ReconciliationRef(engine_id=int(reconciliation_id))
            if reconciliation_id is not None
            else None
        ),
        description=str(record.get("description") or ""),
    )


def payment_journal_record_to_view(record: Record) -> PaymentJournalView:
    """Map an account.payment.journal record to a PaymentJournalView (US-522)."""
    clearing_account_id = record.get("clearing_account")
    clearing_journal_id = record.get("clearing_journal")
    return PaymentJournalView(
        ref=PaymentJournalRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        name=str(record.get("name") or ""),
        process_method=str(record.get("process_method") or ""),
        clearing_account=(
            AccountRef(engine_id=int(clearing_account_id))
            if clearing_account_id is not None
            else None
        ),
        clearing_journal=(
            JournalRef(engine_id=int(clearing_journal_id))
            if clearing_journal_id is not None
            else None
        ),
    )


def sale_line_record_to_view(record: Record, product_names: dict[int, str]) -> SaleLineView:
    """Map a sale.line record to a SaleLineView.

    Callers map only priced lines (``type == 'line'``), which always
    carry a product; a missing product is an engine-data error, not a
    default.
    """
    product_id = int(record["product"])
    return SaleLineView(
        product=ProductRef(engine_id=product_id),
        product_name=product_names.get(product_id, ""),
        quantity=quantity_from_tryton(record.get("quantity", 0)),
        unit_price=money_from_tryton(record.get("unit_price", 0)),
        amount=money_from_tryton(record.get("amount", 0)),
        description=str(record.get("description") or ""),
    )


def sale_record_to_view(
    record: Record,
    lines: list[SaleLineView],
    party_name: str,
    currency_code: str,
) -> SaleView:
    """Map a sale.sale record (+ its lines) to a SaleView.

    ``invoices`` is the engine's Function Many2Many of the invoices the
    sale produced; only priced lines (``type == 'line'``) are mapped --
    subtotal/title/comment lines carry no product or amount.
    """
    sale_date = record.get("sale_date")
    invoice_ids = record.get("invoices") or []
    return SaleView(
        ref=SaleRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        party=PartyRef(engine_id=int(record["party"])),
        party_name=party_name,
        number=str(record["number"]) if record.get("number") is not None else None,
        sale_date=date_from_tryton(sale_date) if sale_date is not None else None,
        state=str(record.get("state") or ""),
        invoice_method=str(record.get("invoice_method") or ""),
        shipment_method=str(record.get("shipment_method") or ""),
        invoice_state=str(record.get("invoice_state") or ""),
        shipment_state=str(record.get("shipment_state") or ""),
        currency_code=currency_code,
        untaxed_amount=money_from_tryton(record.get("untaxed_amount", 0)),
        total_amount=money_from_tryton(record.get("total_amount", 0)),
        description=str(record.get("description") or ""),
        reference=str(record.get("reference") or ""),
        lines=tuple(lines),
        invoices=tuple(InvoiceRef(engine_id=int(i)) for i in invoice_ids),
    )


def statement_line_record_to_view(
    record: Record, party_names: dict[int, str]
) -> BankStatementLineView:
    """Map an account.statement.line record to a BankStatementLineView."""
    party_id = record.get("party")
    return BankStatementLineView(
        id=int(record["id"]),
        date=date_from_tryton(record["date"]),
        amount=money_from_tryton(record.get("amount", 0)),
        account=AccountRef(engine_id=int(record["account"])),
        description=str(record.get("description") or ""),
        number=str(record["number"]) if record.get("number") is not None else None,
        party=PartyRef(engine_id=int(party_id)) if party_id is not None else None,
        party_name=party_names.get(int(party_id)) if party_id is not None else None,
        related_invoice=parse_invoice_reference(record.get("related_to")),
    )


def statement_record_to_view(
    record: Record,
    lines: list[BankStatementLineView],
    journal_name: str,
    currency_code: str,
) -> BankStatementView:
    """Map an account.statement record (+ its lines) to a BankStatementView.

    Balance fields are None when the statement journal's validation type
    does not use them (``'amount'`` / ``'number_of_lines'`` journals have
    no start/end balance; the engine's ``balance`` Function is 0 there,
    which maps to None to keep "not applicable" distinct from zero).
    """
    validation = str(record.get("validation") or "")
    start_balance = record.get("start_balance")
    end_balance = record.get("end_balance")
    balance = record.get("balance")
    total_amount = record.get("total_amount")
    number_of_lines = record.get("number_of_lines")
    return BankStatementView(
        ref=BankStatementRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        name=str(record.get("name") or ""),
        journal_name=journal_name,
        journal_id=int(record["journal"]),
        date=date_from_tryton(record["date"]),
        state=str(record.get("state") or ""),
        validation=validation,
        currency_code=currency_code,
        start_balance=money_from_tryton(start_balance) if start_balance is not None else None,
        end_balance=money_from_tryton(end_balance) if end_balance is not None else None,
        balance=money_from_tryton(balance)
        if validation == "balance" and balance is not None
        else None,
        total_amount=money_from_tryton(total_amount) if total_amount is not None else None,
        number_of_lines=int(number_of_lines) if number_of_lines is not None else None,
        lines=tuple(lines),
    )


def statement_journal_record_to_view(
    record: Record,
    currency_code: str,
    bank_account_number: str | None,
    *,
    account_code: str = "",
    account_name: str = "",
) -> StatementJournalView:
    """Map an ``account.statement.journal`` record to a StatementJournalView.

    ``account_code``/``account_name`` come from the adapter's batch
    read (US-516 cash-box registration needs them); ``journal`` is
    the posting journal when the search requested it.
    """
    journal = record.get("journal")
    return StatementJournalView(
        ref=StatementJournalRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        name=str(record.get("name") or ""),
        journal=JournalRef(engine_id=int(journal)) if journal else None,
        account=AccountRef(engine_id=int(record["account"])),
        account_code=account_code,
        account_name=account_name,
        currency_code=currency_code,
        validation=str(record.get("validation") or ""),
        bank_account_number=bank_account_number,
        active=bool(record.get("active", True)),
    )


# -- packaging & shipping (Phase 5 wave 2, US-515) ---------------------------


def decimal_from_tryton(value: object) -> Decimal | None:
    """Convert a decoded Tryton numeric to Decimal (None stays None).

    For unit costs and other per-unit figures, which may legitimately
    be fractional (an average cost price). Unlike ``money_from_tryton``,
    this does NOT reject fractional values — only totals are whole IRR.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid decimal.")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value.strip())
    raise ValueError(f"Unsupported engine decimal value: {value!r}.")


def location_record_to_view(record: Record) -> StockLocationView:
    """Map a stock.location record to a StockLocationView."""
    parent = record.get("parent")
    return StockLocationView(
        ref=StockLocationRef(engine_id=int(record["id"])),
        name=str(record.get("name") or ""),
        location_type=str(record.get("type") or ""),
        code=str(record["code"]) if record.get("code") else None,
        parent=StockLocationRef(engine_id=int(parent)) if parent else None,
    )


def packaging_product_record_to_view(
    product_record: Record,
    template_record: Record,
    *,
    quantity: Decimal | None = None,
    cost_price: Decimal | None = None,
) -> PackagingProductView:
    """Map product.product + product.template records to a PackagingProductView."""
    uom = template_record.get("default_uom")
    return PackagingProductView(
        ref=ProductRef(engine_id=int(product_record["id"])),
        template_id=int(template_record["id"]),
        name=str(template_record.get("name") or ""),
        code=str(template_record["code"]) if template_record.get("code") else None,
        product_type=str(template_record.get("type") or ""),
        consumable=bool(template_record.get("consumable", False)),
        purchasable=bool(template_record.get("purchasable", False)),
        cost_method=str(template_record.get("cost_price_method") or ""),
        default_uom_name=str(uom.get("name")) if isinstance(uom, dict) else None,
        quantity=quantity,
        cost_price=cost_price,
    )


def order_point_record_to_view(record: Record) -> OrderPointView:
    """Map a stock.order_point record to an OrderPointView."""
    return OrderPointView(
        ref=OrderPointRef(engine_id=int(record["id"])),
        product=ProductRef(engine_id=int(record["product"])),
        warehouse=StockLocationRef(engine_id=int(record["location"])),
        point_type=str(record.get("type") or ""),
        min_quantity=(
            quantity_from_tryton(record["min_quantity"])
            if record.get("min_quantity") is not None
            else None
        ),
        target_quantity=quantity_from_tryton(record["target_quantity"]),
        max_quantity=(
            quantity_from_tryton(record["max_quantity"])
            if record.get("max_quantity") is not None
            else None
        ),
    )


def stock_move_record_to_view(record: Record) -> StockMoveView:
    """Map a stock.move record to a StockMoveView."""
    planned = record.get("planned_date")
    effective = record.get("effective_date")
    return StockMoveView(
        ref=StockMoveRef(engine_id=int(record["id"])),
        product=ProductRef(engine_id=int(record["product"])),
        quantity=quantity_from_tryton(record["quantity"]),
        from_location=StockLocationRef(engine_id=int(record["from_location"])),
        to_location=StockLocationRef(engine_id=int(record["to_location"])),
        state=str(record.get("state") or ""),
        company=CompanyRef(engine_id=int(record["company"])),
        planned_date=date_from_tryton(planned) if planned is not None else None,
        effective_date=date_from_tryton(effective) if effective is not None else None,
        cost_price=decimal_from_tryton(record.get("cost_price")),
    )


def purchase_request_record_to_view(record: Record) -> PurchaseRequestView:
    """Map a purchase.request record to a PurchaseRequestView."""
    supply = record.get("supply_date")
    purchase = record.get("purchase_date")
    created = record.get("create_date")
    warehouse = record.get("warehouse")
    return PurchaseRequestView(
        ref=PurchaseRequestRef(engine_id=int(record["id"])),
        product=ProductRef(engine_id=int(record["product"])),
        quantity=quantity_from_tryton(record["quantity"]),
        state=str(record.get("state") or ""),
        company=CompanyRef(engine_id=int(record["company"])),
        warehouse=StockLocationRef(engine_id=int(warehouse)) if warehouse else None,
        supply_date=date_from_tryton(supply) if supply is not None else None,
        purchase_date=date_from_tryton(purchase) if purchase is not None else None,
        created_at=created if isinstance(created, datetime.datetime) else None,
    )


def analytic_account_record_to_view(record: Record) -> AnalyticAccountView:
    """Map an analytic_account.account record to an AnalyticAccountView."""
    balance = record.get("balance")
    debit = record.get("debit")
    credit = record.get("credit")
    return AnalyticAccountView(
        ref=AnalyticAccountRef(engine_id=int(record["id"])),
        name=str(record.get("name") or ""),
        code=str(record["code"]) if record.get("code") else None,
        account_type=str(record.get("type") or ""),
        company=CompanyRef(engine_id=int(record["company"])),
        balance=money_from_tryton(balance) if balance is not None else None,
        debit=money_from_tryton(debit) if debit is not None else None,
        credit=money_from_tryton(credit) if credit is not None else None,
    )


def analytic_line_record_to_view(record: Record) -> AnalyticLineView:
    """Map an analytic_account.line record to an AnalyticLineView."""
    move_line = record.get("move_line")
    return AnalyticLineView(
        account=AnalyticAccountRef(engine_id=int(record["account"])),
        date=date_from_tryton(record["date"]),
        debit=money_from_tryton(record.get("debit", 0)),
        credit=money_from_tryton(record.get("credit", 0)),
        move_line=MoveLineRef(engine_id=int(move_line)) if move_line else None,
    )


def inventory_line_record_to_view(record: Record, product_name: str) -> InventoryLineView:
    """Map a stock.inventory.line record to an InventoryLineView."""
    return InventoryLineView(
        ref=InventoryLineRef(engine_id=int(record["id"])),
        product=ProductRef(engine_id=int(record["product"])),
        product_name=product_name,
        expected_quantity=quantity_from_tryton(record.get("expected_quantity") or 0),
        quantity=(
            quantity_from_tryton(record["quantity"])
            if record.get("quantity") is not None
            else None
        ),
    )


def inventory_record_to_view(
    record: Record, location_name: str, lines: list[InventoryLineView]
) -> InventoryView:
    """Map a stock.inventory record + its lines to an InventoryView."""
    return InventoryView(
        ref=InventoryRef(engine_id=int(record["id"])),
        number=str(record["number"]) if record.get("number") else None,
        location=StockLocationRef(engine_id=int(record["location"])),
        location_name=location_name,
        date=date_from_tryton(record["date"]),
        state=str(record.get("state") or ""),
        company=CompanyRef(engine_id=int(record["company"])),
        lines=tuple(lines),
    )


def sale_shipment_cost_record_to_view(
    record: Record, currency_code: str
) -> SaleShipmentCostView:
    """Map a stock.shipment.cost_sale record to a SaleShipmentCostView."""
    shipment = record.get("shipment")
    if shipment is None:
        raise ValueError("Shipment cost row has no shipment reference.")
    shipment_id = int(shipment.split(",")[1]) if isinstance(shipment, str) else int(shipment)
    return SaleShipmentCostView(
        sale=SaleRef(engine_id=int(record["sale"])),
        shipment=OutShipmentRef(engine_id=shipment_id),
        amount=money_from_tryton(record.get("amount", 0)),
        currency_code=currency_code,
    )


def out_shipment_cost_record_to_view(
    record: Record, carrier_name: str | None, currency_codes: dict[int, str]
) -> OutShipmentCostView:
    """Map a stock.shipment.out record to an OutShipmentCostView."""
    cost_currency = record.get("cost_currency")
    cost_sale_currency = record.get("cost_sale_currency")
    return OutShipmentCostView(
        ref=OutShipmentRef(engine_id=int(record["id"])),
        state=str(record.get("state") or ""),
        carrier_name=carrier_name,
        cost=money_from_tryton(record["cost"]) if record.get("cost") is not None else None,
        cost_currency_code=(
            currency_codes.get(int(cost_currency)) if cost_currency is not None else None
        ),
        cost_sale=(
            money_from_tryton(record["cost_sale"])
            if record.get("cost_sale") is not None
            else None
        ),
        cost_sale_currency_code=(
            currency_codes.get(int(cost_sale_currency))
            if cost_sale_currency is not None
            else None
        ),
    )


#: Fields read back for an ``account.check`` record (US-531 + US-532).
CHEQUE_FIELDS: list[str] = [
    "id",
    "company",
    "kind",
    "purpose",
    "sayad_id",
    "sayad_status",
    "state",
    "storage_location",
    "amount",
    "issue_date",
    "due_date",
    "number",
    "bank",
    "branch",
    "issuer_account_number",
    "endorsee",
    "endorsee_type",
    "checkbook",
    "secured_obligation",
    "moves",
]

#: Fields read back for an ``account.checkbook`` record (US-532).
#: The leaf counts are engine-computed Function fields.
CHECKBOOK_FIELDS: list[str] = [
    "id",
    "company",
    "name",
    "bank",
    "bank_account",
    "first_number",
    "last_number",
    "total_leaves",
    "used_leaves",
    "voided_leaves",
    "remaining_leaves",
]


def cheque_record_to_view(record: Record) -> ChequeEngineView:
    """Map an ``account.check`` record to a ChequeEngineView (US-531).

    ``moves`` is the module's one2many of journal moves — a plain
    read returns move ids (ints) with NO transition attribution, so
    a plain read maps to an empty ``move_ids``. The transition
    attribution comes from the ``transition_rest`` action's return
    (``move_id``), which the adapter merges into the view; the
    HesabYar registry accumulates the map across transitions. The
    dict / list-of-``{"transition", "move"}`` shapes are honored
    too, for action-shaped records. A record without moves maps to
    an empty ``move_ids`` — a registered cheque has no journal yet,
    so an absent link is normal, not an error.
    """
    moves: dict[str, int] = {}
    raw_moves = record.get("moves")
    if isinstance(raw_moves, dict):
        for transition, move_id in raw_moves.items():
            if move_id is not None:
                moves[str(transition)] = int(move_id)
    elif isinstance(raw_moves, list):
        for entry in raw_moves:
            if not isinstance(entry, dict):
                continue
            transition = entry.get("transition")
            move_id = entry.get("move")
            if transition is not None and move_id is not None:
                moves[str(transition)] = int(move_id)
    return ChequeEngineView(
        ref=ChequeEngineRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        kind=str(record.get("kind") or ""),
        purpose=str(record.get("purpose") or ""),
        sayad_id=str(record.get("sayad_id") or ""),
        state=str(record.get("state") or ""),
        sayad_status=str(record.get("sayad_status") or ""),
        amount=money_from_tryton(record.get("amount")),
        issue_date=date_from_tryton(record.get("issue_date")),
        due_date=date_from_tryton(record.get("due_date")),
        storage_location=(
            str(record["storage_location"]) if record.get("storage_location") else None
        ),
        leaf_number=str(record["number"]) if record.get("number") else None,
        checkbook_engine_id=(
            int(record["checkbook"]) if record.get("checkbook") else None
        ),
        secured_obligation=(
            str(record["secured_obligation"])
            if record.get("secured_obligation")
            else None
        ),
        move_ids=moves,
    )


def checkbook_record_to_view(record: Record) -> CheckbookEngineView:
    """Map an ``account.checkbook`` record to a CheckbookEngineView (US-532).

    The leaf counts come from the engine's computed Function fields
    (used / voided / remaining from the issued cheques linked to the
    checkbook); a missing count reads as 0.
    """
    return CheckbookEngineView(
        ref=CheckbookEngineRef(engine_id=int(record["id"])),
        company=CompanyRef(engine_id=int(record["company"])),
        name=str(record.get("name") or ""),
        bank_engine_id=int(record["bank"]) if record.get("bank") else None,
        bank_account_engine_id=(
            int(record["bank_account"]) if record.get("bank_account") else None
        ),
        first_number=(
            int(record["first_number"]) if record.get("first_number") is not None else None
        ),
        last_number=(
            int(record["last_number"]) if record.get("last_number") is not None else None
        ),
        total_leaves=int(record.get("total_leaves") or 0),
        used_leaves=int(record.get("used_leaves") or 0),
        voided_leaves=int(record.get("voided_leaves") or 0),
        remaining_leaves=int(record.get("remaining_leaves") or 0),
    )
