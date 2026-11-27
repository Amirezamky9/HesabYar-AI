"""Engine-neutral references to accounting records.

A reference identifies a record held by the accounting engine (Tryton) without
exposing any engine-specific type. The engine record id is an opaque integer
as far as the domain is concerned: domain code never interprets it, never
builds SQL from it, and never assumes which engine issued it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompanyRef:
    """Reference to the engine company holding a tenant's books."""

    engine_id: int

    def __str__(self) -> str:
        return f"company:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Reference to a chart-of-accounts entry in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"account:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class JournalRef:
    """Reference to an accounting journal in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"journal:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class JournalDraftRef:
    """Reference to a draft journal (an unposted move) in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"journal-draft:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class PostedJournalRef:
    """Reference to a posted journal in the engine. Posted journals are immutable."""

    engine_id: int

    def __str__(self) -> str:
        return f"posted-journal:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class FiscalYearRef:
    """Reference to a fiscal year in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"fiscal-year:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class FiscalPeriodRef:
    """Reference to a fiscal period in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"fiscal-period:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class PartyRef:
    """Reference to a party (customer / supplier) in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"party:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class PartyAddressRef:
    """Reference to one address of a party in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"party-address:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class InvoiceRef:
    """Reference to an invoice in the engine (any state: draft .. paid)."""

    engine_id: int

    def __str__(self) -> str:
        return f"invoice:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class PaymentRef:
    """Reference to a payment in the engine (any state: draft .. failed)."""

    engine_id: int

    def __str__(self) -> str:
        return f"payment:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class BankStatementRef:
    """Reference to a bank/cash statement in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"bank-statement:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class BankStatementLineRef:
    """Reference to one line of a bank/cash statement in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"bank-statement-line:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class StatementJournalRef:
    """Reference to a statement journal (``account.statement.journal``) in the engine.

    A statement journal is the daybook for one bank account or cash box:
    it carries the account whose statements it holds and the validation
    type (``balance`` / ``amount`` / ``number_of_lines``). Distinct from
    ``JournalRef`` (``account.journal``), which the statement journal
    itself references for posting.
    """

    engine_id: int

    def __str__(self) -> str:
        return f"statement-journal:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class SaleRef:
    """Reference to a sale order in the engine (any state: draft .. done)."""

    engine_id: int

    def __str__(self) -> str:
        return f"sale:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class ProductRef:
    """Reference to a sellable product variant in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"product:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class PaymentJournalRef:
    """Reference to a payment journal (``account.payment.journal``) in the engine.

    Distinct from ``JournalRef`` (``account.journal``): a payment journal
    carries the process method and, with the clearing module, the clearing
    account and journal used when a payment succeeds.
    """

    engine_id: int

    def __str__(self) -> str:
        return f"payment-journal:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class MoveLineRef:
    """Reference to one line of an engine journal (``account.move.line``).

    Used to link a payment to the receivable/payable line it settles.
    """

    engine_id: int

    def __str__(self) -> str:
        return f"move-line:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class PaymentTermRef:
    """Reference to an invoice payment term in the engine (US-522).

    A payment term splits an invoice into ``lines_to_pay`` — one
    receivable/payable line per leg, each with its own maturity date.
    The settlement engine uses one term per contract schedule so each
    tranche has its own receivable line to pay against.
    """

    engine_id: int

    def __str__(self) -> str:
        return f"payment-term:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class ReconciliationRef:
    """Reference to a move-line reconciliation in the engine (US-522)."""

    engine_id: int

    def __str__(self) -> str:
        return f"reconciliation:{self.engine_id}"


# -- packaging & shipping (Phase 5 wave 2, US-515) ---------------------------
#
# Stock, supply and analytic references for the packaging module. Like
# every ref here, the engine id is opaque: domain code never interprets
# it and never builds SQL from it.


@dataclass(frozen=True, slots=True)
class StockLocationRef:
    """Reference to a stock location (``stock.location``) in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"stock-location:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class OrderPointRef:
    """Reference to a replenishment order point (``stock.order_point``)."""

    engine_id: int

    def __str__(self) -> str:
        return f"order-point:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class StockMoveRef:
    """Reference to a stock move (``stock.move``) in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"stock-move:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class PurchaseRequestRef:
    """Reference to a purchase request (``purchase.request``) in the engine."""

    engine_id: int

    def __str__(self) -> str:
        return f"purchase-request:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class AnalyticAccountRef:
    """Reference to an analytic account (``analytic_account.account``)."""

    engine_id: int

    def __str__(self) -> str:
        return f"analytic-account:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class InventoryRef:
    """Reference to a stock inventory count (``stock.inventory``)."""

    engine_id: int

    def __str__(self) -> str:
        return f"inventory:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class InventoryLineRef:
    """Reference to one line of a stock inventory (``stock.inventory.line``)."""

    engine_id: int

    def __str__(self) -> str:
        return f"inventory-line:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class OutShipmentRef:
    """Reference to an outgoing shipment (``stock.shipment.out``)."""

    engine_id: int

    def __str__(self) -> str:
        return f"out-shipment:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class ChequeEngineRef:
    """Reference to an ``account.check`` record in the engine (US-531)."""

    engine_id: int

    def __str__(self) -> str:
        return f"cheque:{self.engine_id}"


@dataclass(frozen=True, slots=True)
class CheckbookEngineRef:
    """Reference to an ``account.checkbook`` record in the engine (US-532)."""

    engine_id: int

    def __str__(self) -> str:
        return f"checkbook:{self.engine_id}"
