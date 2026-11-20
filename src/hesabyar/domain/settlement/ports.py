"""Persistence + audit boundaries for the settlement engine (US-521).

Every read/write is scoped by ``installation_id`` (the caller's
installation, from the authenticated API key): a row of another
installation looks exactly like a row that does not exist.
Repositories flush on save (so ids exist) but never commit — the
caller owns the transaction. The tick methods are the exception:
the daily job runs across ALL installations (there is no caller),
so they take no installation id.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hesabyar.domain.settlement.commands import (
    CreateBankDeposit,
    CreateGatewayPayment,
    CreateSettlementEvent,
    CreateSettlementException,
    CreateSettlementFileImport,
    CreateSettlementFileRow,
    CreateSettlementMatch,
    PostBatchBankReceipt,
)
from hesabyar.domain.settlement.models import (
    BankDeposit,
    DerivedTranche,
    GatewayPayment,
    SettlementEvent,
    SettlementException,
    SettlementFeeRecord,
    SettlementFileImport,
    SettlementFileRow,
    SettlementMatch,
    SettlementPosting,
    SettlementTranche,
    TrancheState,
)

__all__ = ["BankReceiptPosting", "SettlementAuditPort", "SettlementPostingPort", "SettlementRepositoryPort"]


@dataclass
class BankReceiptPosting:
    """The result of posting one batch's bank receipt (Dr bank / Cr clearing).

    ``tryton_journal_id`` is the posted move id in the engine; the
    tie-out service records it in the audit log (the settlement
    tables carry no engine-move field by design — the audit trail is
    the durable record of every auto-post decision, §6.7). A batch
    fans out to one move per tranche, so ``tryton_journal_id`` is
    the FIRST posted move and ``tryton_journal_ids`` holds every
    move, in tranche order: moves posted in this call, moves of
    tranches skipped on retry because they were already posted, and
    earlier partial moves of tranches topped up in this call.
    ``amount_irr`` is the batch's total bank-receipt amount (newly
    posted + already posted on a retry, partials included).
    """

    tryton_journal_id: int
    amount_irr: int
    posted: bool = True
    tryton_journal_ids: tuple[int, ...] = ()


class SettlementPostingPort(Protocol):
    """Engine posting for settlement bank receipts (US-523 seam, US-522 fills it).

    The tie-out service calls this ONLY after the mandatory tie-out
    holds and the policy gate allows auto-post. The implementation
    fans the batch out to one engine posting per tranche (Dr bank /
    Cr clearing, dated at the deposit date), resolving the company,
    the bank account and the clearing account from the channel's
    mappings per tranche. A failure on any tranche raises
    :class:`SettlementPostingError` — the tie-out service catches
    it, keeps the batch in exception and files a review item — but
    tranches posted before the failure STAY posted (partial
    posting); a retry skips them via the posting ledger, it never
    re-posts them. A batch is never silently left unposted and never
    silently posted.
    """

    def post_bank_receipt(self, command: PostBatchBankReceipt) -> BankReceiptPosting:
        """Post the bank receipt move for one settled batch and return its journal id."""
        ...


class SettlementAuditPort(Protocol):
    """Audit-log boundary for settlement decisions that must be traced.

    Bulk re-derivation and write-offs are human decisions with money
    consequences: each one writes one immutable audit entry naming
    the actor, the resource and the reason (phased design §3.1/§3.3).
    """

    def record_settlement_audit(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        api_client_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        actor: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit event (flushes; the caller commits)."""
        ...


class SettlementRepositoryPort(Protocol):
    """Persistence boundary for the settlement engine's tables."""

    # -- gateway payments -------------------------------------------------

    def get_payment(self, installation_id: str, payment_id: str) -> GatewayPayment | None:
        """Return one payment, or None for an unknown id in this installation."""
        ...

    def get_payment_by_transaction(
        self, installation_id: str, channel_id: str, gateway_transaction_id: str
    ) -> GatewayPayment | None:
        """Return the payment for a (channel, gateway transaction) pair, or None."""
        ...

    def list_payments(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        status: str | None = None,
        sales_period_jalali: str | None = None,
    ) -> Sequence[GatewayPayment]:
        """Return the installation's payments, newest confirmation first."""
        ...

    def save_payment(
        self,
        command: CreateGatewayPayment,
        *,
        channel_id: str,
        fee_irr: int,
        fee_vat_irr: int,
        net_irr: int,
        contract_version_id: str,
        fee_rate_snapshot: Any,
        fee_vat_rate_snapshot: Any,
        sales_period_jalali: str,
    ) -> GatewayPayment:
        """Persist a new payment row (amounts already derived) and return it."""
        ...

    def update_payment_status(
        self, payment_id: str, status: str, *, refunds_deducted_irr: int | None = None
    ) -> GatewayPayment | None:
        """Apply a payment status change; None when the payment does not exist."""
        ...

    def update_payment_sales_period(
        self, payment_id: str, sales_period_jalali: str
    ) -> GatewayPayment | None:
        """Correct a payment's sales period; None when the payment does not exist."""
        ...

    def update_payment_amounts(
        self,
        payment_id: str,
        *,
        fee_irr: int,
        fee_vat_irr: int,
        net_irr: int,
        contract_version_id: str,
        fee_rate_snapshot: Any,
        fee_vat_rate_snapshot: Any,
    ) -> GatewayPayment | None:
        """Re-snapshot a payment's derived amounts (bulk re-derivation only)."""
        ...

    # -- tranches ---------------------------------------------------------

    def get_tranche(self, installation_id: str, tranche_id: str) -> SettlementTranche | None:
        """Return one tranche, or None for an unknown id in this installation."""
        ...

    def list_tranches_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementTranche]:
        """Return a payment's tranches, ordered by tranche index."""
        ...

    def list_tranches(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        state: TrancheState | None = None,
        states: Sequence[TrancheState] | None = None,
        sales_period_jalali: str | None = None,
        due_from: datetime.date | None = None,
        due_to: datetime.date | None = None,
    ) -> Sequence[SettlementTranche]:
        """Return the installation's tranches, ordered by due date then index.

        ``state`` filters to one state; ``states`` (when given) filters
        to any of them — the batch matcher needs due + overdue together.
        """
        ...

    def save_tranches(
        self,
        installation_id: str,
        payment_id: str,
        tranches: Sequence[DerivedTranche],
    ) -> Sequence[SettlementTranche]:
        """Persist derived tranches for a payment and return them."""
        ...

    def update_tranche(
        self,
        tranche_id: str,
        *,
        state: TrancheState | None = None,
        confirmed_net_irr: int | None = None,
        received_net_irr: int | None = None,
        variance_irr: int | None = None,
        variance_reason: str | None = None,
        match_method: str | None = None,
        match_confidence: Any | None = None,
        tryton_receivable_line_id: int | None = None,
        expected_window_start: datetime.date | None = None,
        expected_date: datetime.date | None = None,
        sales_period_jalali: str | None = None,
        gross_irr: int | None = None,
        fee_irr: int | None = None,
        fee_vat_irr: int | None = None,
        net_irr: int | None = None,
    ) -> SettlementTranche | None:
        """Apply a partial tranche update; None when the tranche does not exist.

        The amount parameters (US-522) serve refund allocation: a
        partial refund reduces an open tranche's gross/net (fee/VAT
        unchanged — the fee was already charged on the original sale).
        """
        ...

    def delete_tranches(self, tranche_ids: Sequence[str]) -> int:
        """Delete tranches by id (bulk re-derivation only); returns the count."""
        ...

    def find_tranches_due_for_tick(
        self, today: datetime.date
    ) -> Sequence[tuple[SettlementTranche, int]]:
        """Return ``(tranche, grace_days)`` for tranches turning due/overdue.

        Covers every installation (the daily job has no caller):
        ``expected`` tranches with ``expected_date <= today`` and
        ``due`` tranches past ``expected_date + grace_days`` (grace
        from the payment's contract version).
        """
        ...

    # -- settlement events / deposits / matches ---------------------------

    def save_settlement_event(self, command: CreateSettlementEvent, *, channel_id: str) -> SettlementEvent:
        """Persist a settlement event and return it."""
        ...

    def get_settlement_event(
        self, installation_id: str, event_id: str
    ) -> SettlementEvent | None:
        """Return one settlement event, or None for an unknown id in this installation."""
        ...

    def list_settlement_events(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        import_batch_id: str | None = None,
    ) -> Sequence[SettlementEvent]:
        """Return the installation's settlement events, newest payout first."""
        ...

    def save_bank_deposit(self, command: CreateBankDeposit) -> BankDeposit:
        """Persist a bank deposit and return it."""
        ...

    def get_bank_deposit(
        self, installation_id: str, deposit_id: str
    ) -> BankDeposit | None:
        """Return one bank deposit, or None for an unknown id in this installation."""
        ...

    def list_bank_deposits(
        self, installation_id: str, *, unmatched_only: bool = False
    ) -> Sequence[BankDeposit]:
        """Return the installation's bank deposits, newest first.

        ``unmatched_only`` returns only deposits with no settlement
        match links at all (the reverse-matching / sweep candidates).
        """
        ...

    def save_settlement_match(self, command: CreateSettlementMatch) -> SettlementMatch:
        """Persist a tranche↔deposit/event match link and return it."""
        ...

    def update_match(
        self, match_id: str, *, settlement_event_id: str | None = None
    ) -> SettlementMatch | None:
        """Attach a match link to a settlement event (C7 confirmation).

        Used when a deposit that arrived before the file (matched to
        a tranche, no event yet) is confirmed by the file's approval:
        the EXISTING link gains the event — no new link is created,
        so the tie-out never double-counts the deposit. None when the
        match does not exist.
        """
        ...

    def list_matches_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementMatch]:
        """Return a tranche's match links, oldest first."""
        ...

    def list_matches_for_deposit(
        self, installation_id: str, deposit_id: str
    ) -> Sequence[SettlementMatch]:
        """Return a deposit's match links, oldest first."""
        ...

    def list_matches_for_event(
        self, installation_id: str, settlement_event_id: str
    ) -> Sequence[SettlementMatch]:
        """Return a settlement event's match links, oldest first."""
        ...

    # -- exceptions ---------------------------------------------------------

    def save_exception(self, command: CreateSettlementException) -> SettlementException:
        """Persist a settlement exception and return it."""
        ...

    def get_exception(
        self, installation_id: str, exception_id: str
    ) -> SettlementException | None:
        """Return one exception, or None for an unknown id in this installation."""
        ...

    def list_exceptions(
        self,
        installation_id: str,
        *,
        exception_type: str | None = None,
        status: str | None = None,
        tranche_id: str | None = None,
    ) -> Sequence[SettlementException]:
        """Return the installation's exceptions, newest first."""
        ...

    def get_open_exception_for_tranche(
        self, installation_id: str, tranche_id: str, exception_type: str
    ) -> SettlementException | None:
        """Return the open exception of one type for a tranche, if any."""
        ...

    def get_open_exception_for_deposit(
        self, installation_id: str, deposit_id: str, exception_type: str
    ) -> SettlementException | None:
        """Return the open exception of one type for a deposit, if any."""
        ...

    def get_open_exception_for_event(
        self, installation_id: str, settlement_event_id: str, exception_type: str
    ) -> SettlementException | None:
        """Return the open exception of one type for a settlement event, if any."""
        ...

    def update_exception(
        self,
        exception_id: str,
        *,
        status: str | None = None,
        escalation_level: int | None = None,
        resolution: str | None = None,
        resolved_by: str | None = None,
        resolved_at: datetime.datetime | None = None,
        note: str | None = None,
    ) -> SettlementException | None:
        """Apply a partial exception update; None when it does not exist."""
        ...

    # -- fee records ------------------------------------------------------

    def save_fee_record(
        self,
        *,
        installation_id: str,
        gateway_payment_id: str,
        contract_version_id: str,
        fee_percent_applied: Any,
        fee_vat_rate_applied: Any,
        expected_fee_irr: int,
        expected_vat_irr: int,
        source: str = "calculated",
        tranche_id: str | None = None,
        actual_fee_irr: int | None = None,
        fee_variance_irr: int | None = None,
        actual_vat_irr: int | None = None,
        vat_variance_irr: int | None = None,
    ) -> SettlementFeeRecord:
        """Persist a fee record and return it.

        ``actual_*`` / ``*_variance_*`` carry the gateway file's
        figures (US-523): a file fee that differs from the expected
        fee is recorded here AND raised as a ``fee_mismatch``
        exception — never silently absorbed.
        """
        ...

    def list_fee_records(
        self,
        installation_id: str,
        *,
        gateway_payment_id: str | None = None,
        channel_id: str | None = None,
    ) -> Sequence[SettlementFeeRecord]:
        """Return the installation's fee records, newest first."""
        ...

    def update_fee_record(
        self,
        fee_record_id: str,
        *,
        actual_fee_irr: int | None = None,
        actual_vat_irr: int | None = None,
        fee_variance_irr: int | None = None,
        vat_variance_irr: int | None = None,
        tryton_fee_journal_id: int | None = None,
        source: str | None = None,
    ) -> SettlementFeeRecord | None:
        """Apply a partial fee-record update; None when it does not exist (US-522)."""
        ...

    # -- postings (US-522) ------------------------------------------------

    def save_posting(
        self,
        *,
        installation_id: str,
        gateway_payment_id: str,
        kind: str,
        amount_irr: int,
        status: str = "posted",
        tranche_id: str | None = None,
        tryton_move_id: int | None = None,
        tryton_payment_id: int | None = None,
        tryton_reconciliation_id: int | None = None,
        reason: str | None = None,
        event_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SettlementPosting:
        """Persist a settlement posting (the §3.5 audit ledger) and return it."""
        ...

    def get_posting(
        self, installation_id: str, posting_id: str
    ) -> SettlementPosting | None:
        """Return one posting, or None for an unknown id in this installation."""
        ...

    def list_postings_for_payment(
        self, installation_id: str, payment_id: str
    ) -> Sequence[SettlementPosting]:
        """Return a payment's postings, oldest first."""
        ...

    def list_postings_for_tranche(
        self, installation_id: str, tranche_id: str
    ) -> Sequence[SettlementPosting]:
        """Return a tranche's postings, oldest first."""
        ...

    def get_posting_by_kind(
        self,
        installation_id: str,
        kind: str,
        *,
        gateway_payment_id: str | None = None,
        tranche_id: str | None = None,
    ) -> SettlementPosting | None:
        """Return the posting of one kind for a payment/tranche, if any.

        The idempotency lookup for the once-only kinds (fee_at_sale
        per payment, fee_at_settlement per tranche — both DB-unique).
        Clearing / bank_receipt postings repeat (partial confirmations
        / receipts), so the service derives their idempotency from
        cumulative amounts instead of this lookup; for those kinds
        this returns the earliest posting.
        """
        ...

    # -- file imports -----------------------------------------------------

    def get_file_import_by_hash(
        self, installation_id: str, channel_id: str, file_hash_sha256: str
    ) -> SettlementFileImport | None:
        """Return the import of a (channel, file hash) pair, or None."""
        ...

    def get_file_import(
        self, installation_id: str, import_id: str
    ) -> SettlementFileImport | None:
        """Return one file import, or None for an unknown id in this installation."""
        ...

    def get_file_import_by_batch(
        self, installation_id: str, import_batch_id: str
    ) -> SettlementFileImport | None:
        """Return the import of one batch id, or None."""
        ...

    def list_file_imports(
        self,
        installation_id: str,
        *,
        channel_id: str | None = None,
        status: str | None = None,
    ) -> Sequence[SettlementFileImport]:
        """Return the installation's file imports, newest first."""
        ...

    def save_file_import(
        self, command: CreateSettlementFileImport, *, channel_id: str
    ) -> SettlementFileImport:
        """Persist a file import and return it."""
        ...

    def update_file_import(
        self,
        import_id: str,
        *,
        status: str | None = None,
        row_count: int | None = None,
        matched_count: int | None = None,
        new_count: int | None = None,
        error_count: int | None = None,
    ) -> SettlementFileImport | None:
        """Apply a partial file-import update; None when it does not exist."""
        ...

    def save_file_rows(
        self, rows: Sequence[CreateSettlementFileRow]
    ) -> Sequence[SettlementFileRow]:
        """Persist parsed file rows and return them."""
        ...

    def list_file_rows(
        self, import_id: str, *, match_status: str | None = None
    ) -> Sequence[SettlementFileRow]:
        """Return an import's rows, ordered by row number."""
        ...

    def update_file_row(
        self,
        row_id: str,
        *,
        match_status: str | None = None,
        matched_tranche_id: str | None = None,
        note: str | None = None,
    ) -> SettlementFileRow | None:
        """Apply a partial file-row update; None when it does not exist."""
        ...
