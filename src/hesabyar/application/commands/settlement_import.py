"""Settlement file import + three-way matching + mandatory tie-out (US-523).

Phased design §3.2ب + §3.4 + §3.7: a gateway settlement file (portal
Excel or the monthly detail Excel emailed by SnappPay — no browser
automation anywhere) is imported, validated and deduplicated, matched
against the expected tranche schedule and the bank deposits, and only
posted when the mandatory tie-out holds.

Flow (C1):

1. ``import_file`` — upload → parse (``Net Sale`` rows and totals are
   dropped and logged) → dry-run match preview (advisory) → the
   import is born ``awaiting_review`` with a ``manual_import_review``
   queue item carrying the accounting-effect preview. Re-importing
   the same file (channel + sha256) is a no-op returning the existing
   import. Nothing is applied yet.
2. ``approve_import`` — the authoritative match runs fresh (preview
   state is never trusted): exact ``transaction_id`` matches confirm
   tranches, unknown transactions create new payments
   (``source=manual_import`` — idempotent on (gateway,
   transaction_id)), payouts files batch-match FIFO, refunds/cancels
   go through the cancel flow, and one settlement event + one
   canonical ``payment.settlement.received`` outbox event are
   published per import.
3. ``record_deposit`` — a bank deposit (C4) is matched in confidence
   order (C5): exact tracking ref → amount+date (unique) → amount
   only (ranked suggestions in the review queue) → no candidate
   (``deposit_without_file``). A deposit arriving before the file
   attaches to expected tranches (C7 reverse match); the later file
   confirms it.
4. ``run_tieout`` — the MANDATORY tie-out (C6): ``Σ confirmed tranche
   nets − Σ documented period deductions = Σ matched bank deposits``
   within ``tieout_tolerance_irr``. Undocumented deductions are NOT
   deductions — they become ``amount_mismatch`` exceptions. If the
   tie-out holds and the policy gate allows, the bank move (clearing
   → bank) is posted through the :class:`SettlementPostingPort` and
   fully-received tranches close (``settled``). If not, the whole
   batch stays in exception and NO automatic posting happens.

Every exception the service opens is ALSO filed in the unified review
queue as ``settlement_variance`` (subtype = the §3.7 exception type)
with severity, linked tranche/event/deposit, expected vs actual
amounts — and no exception is ever resolved silently. Overpayment
parks as an unallocated receipt (the deposit row + an ``overpayment``
exception) and is NEVER auto-allocated to future tranches.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from hesabyar.application.commands.integrations import PolicyGateService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.errors import IntegrationChannelNotFoundError, ReviewInvalidStateError
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.bank.ports import StatementFileReaderPort
from hesabyar.domain.events.envelope import canonical_payload_hash
from hesabyar.domain.events.models import OutboxEvent, OutboxEventStatus
from hesabyar.domain.events.ports import EventOutboxRepositoryPort
from hesabyar.domain.integrations.models import PaymentChannel
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters
from hesabyar.domain.settlement.commands import (
    ApproveSettlementImport,
    CancelGatewayPayment,
    CreateBankDeposit,
    CreateGatewayPayment,
    CreateSettlementEvent,
    CreateSettlementException,
    CreateSettlementFileImport,
    CreateSettlementFileRow,
    CreateSettlementMatch,
    ImportSettlementFile,
    MatchDepositManually,
    PostBatchBankReceipt,
    ReassignSalesPeriod,
    RecordBankDeposit,
    RejectSettlementImport,
    RunSettlementTieOut,
)
from hesabyar.domain.settlement.errors import (
    SettlementContractMissingError,
    SettlementDepositNotFoundError,
    SettlementDerivationError,
    SettlementFeeVatRateUnknownError,
    SettlementFileImportNotFoundError,
    SettlementImportInvalidError,
    SettlementImportStateError,
    SettlementMatchError,
    SettlementPaymentExistsError,
    SettlementPaymentInvalidError,
    SettlementPostingError,
    SettlementPostingNotConfiguredError,
    SettlementTrancheNotFoundError,
)
from hesabyar.domain.settlement.import_parsing import (
    ParsedSettlementRow,
    rows_from_settlement_grid,
    suggest_settlement_mapping,
    validate_settlement_mapping_columns,
)
from hesabyar.domain.settlement.matching import (
    DepositMatchDecision,
    DepositToMatch,
    EventCandidate,
    TieOutResult,
    TrancheCandidate,
    batch_match_allocations,
    compute_tieout,
    match_deposit,
)
from hesabyar.domain.settlement.models import (
    BankDeposit,
    GatewayPayment,
    PaymentSource,
    SettlementEvent,
    SettlementException,
    SettlementExceptionSeverity,
    SettlementExceptionStatus,
    SettlementExceptionType,
    SettlementFileImport,
    SettlementFileRow,
    SettlementTranche,
    TrancheState,
    sales_period_from,
)
from hesabyar.domain.settlement.ports import (
    SettlementAuditPort,
    SettlementPostingPort,
    SettlementRepositoryPort,
)
from hesabyar.domain.settlement.state_machine import require_transition

__all__ = [
    "ApproveImportResult",
    "ImportPreview",
    "ImportSettlementFileResult",
    "RecordDepositResult",
    "SettlementImportService",
    "TieOutRunResult",
]

#: Canonical event published per approved import (phased design §3.4 C1).
SETTLEMENT_RECEIVED_EVENT_TYPE = "payment.settlement.received"

#: Import statuses (free string on settlement_file_imports.status).
IMPORT_AWAITING_REVIEW = "awaiting_review"
IMPORT_APPROVED = "approved"
IMPORT_REJECTED = "rejected"
IMPORT_EXCEPTION = "exception"
IMPORT_SETTLED = "settled"

#: File types (§3.2ب).
FILE_TYPE_DETAIL = "detail"
FILE_TYPE_PAYOUTS = "payouts"
FILE_TYPE_STATEMENT = "statement"

#: Row match statuses (§3.2ب).
ROW_MATCHED = "matched"
ROW_NEW_PAYMENT = "new_payment"
ROW_UNKNOWN_REFERENCE = "unknown_reference"
ROW_DUPLICATE = "duplicate"
ROW_AMOUNT_MISMATCH = "amount_mismatch"

#: Tranche states that can still receive money (open for matching).
_OPEN_TRANCHE_STATES: tuple[TrancheState, ...] = (
    TrancheState.EXPECTED,
    TrancheState.DUE,
    TrancheState.OVERDUE,
    TrancheState.PARTIALLY_CONFIRMED,
    TrancheState.PARTIALLY_RECEIVED,
)

#: Tranche states that count as "confirmed" for the tie-out.
_CONFIRMED_TRANCHE_STATES: tuple[TrancheState, ...] = (
    TrancheState.CONFIRMED,
    TrancheState.RECEIVED,
    TrancheState.SETTLED,
    TrancheState.PARTIALLY_CONFIRMED,
    TrancheState.PARTIALLY_RECEIVED,
)


@dataclass(frozen=True)
class ImportPreview:
    """The accounting-effect preview of an import (U4, §3.2ب).

    Advisory figures for the review item: the approve step re-derives
    everything from the rows. ``receivable_before_irr`` is the
    channel's open receivable (Σ outstanding of open tranches);
    ``receivable_after_irr`` subtracts the tranches this batch would
    confirm.
    """

    gross_irr: int
    fee_irr: int
    vat_irr: int
    net_irr: int
    refunds_irr: int
    deductions_irr: int
    expected_deposit_irr: int
    receivable_before_irr: int
    receivable_after_irr: int
    matched_rows: int
    new_payment_rows: int
    unknown_rows: int
    duplicate_rows: int
    invalid_rows: int
    dropped_rows: int


@dataclass(frozen=True)
class ImportSettlementFileResult:
    """The outcome of an import: the import + its preview."""

    file_import: SettlementFileImport
    preview: ImportPreview
    duplicate: bool = False
    """True when the file was already imported (a no-op re-import)."""
    review_item_id: str | None = None
    dropped_rows: tuple[tuple[int, str], ...] = ()
    """(row number, reason) for the Net Sale / totals rows dropped at parse."""


@dataclass(frozen=True)
class ApproveImportResult:
    """The outcome of approving an import."""

    file_import: SettlementFileImport
    settlement_event: SettlementEvent
    canonical_event_id: str
    confirmed_tranche_ids: tuple[str, ...] = ()
    created_payment_ids: tuple[str, ...] = ()
    exceptions: tuple[SettlementException, ...] = ()
    review_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordDepositResult:
    """The outcome of recording a bank deposit."""

    deposit: BankDeposit
    decision_method: str
    decision_confidence: Decimal
    matched_tranche_ids: tuple[str, ...] = ()
    suggestions: tuple[tuple[str, str, Decimal, str], ...] = ()
    """Rule-3 ranked suggestions: (kind, target id, confidence, reason)."""
    exceptions: tuple[SettlementException, ...] = ()
    tieout: TieOutResult | None = None
    """The tie-out of the first affected batch, when one was attempted."""
    posted_journal_id: int | None = None


@dataclass(frozen=True)
class TieOutRunResult:
    """The outcome of a tie-out run (C6)."""

    file_import: SettlementFileImport
    tieout: TieOutResult
    status: str
    """``settled`` | ``approved`` (held, draft-hold) | ``exception``."""
    gate_allowed: bool | None = None
    gate_reason: str | None = None
    posted: bool = False
    tryton_journal_id: int | None = None
    settled_tranche_ids: tuple[str, ...] = ()
    exceptions: tuple[SettlementException, ...] = ()
    review_item_ids: tuple[str, ...] = ()


@dataclass
class _ExceptionCollector:
    """Collects exceptions + review items opened during one operation."""

    exceptions: list[SettlementException] = dataclasses.field(default_factory=list)
    review_item_ids: list[str] = dataclasses.field(default_factory=list)


@dataclass(frozen=True)
class _TieOutSums:
    """The pure tie-out inputs for one import batch (US-554).

    Computed read-only by :meth:`SettlementImportService._tieout_sums`:
    the tie-out core and the read-only figures endpoint share it, so
    the batch variance report (US-554) sums exactly what the tie-out
    sums — no second calculator.
    """

    events: tuple[SettlementEvent, ...]
    confirmed_total_irr: int
    deductions_irr: int
    matched_deposits_irr: int
    confirmed_tranches: tuple[SettlementTranche, ...]
    latest_payout: datetime.date
    deposit_ids: frozenset[str]
    per_event: tuple[tuple[SettlementEvent, int, int], ...]
    """Per event: (event, confirmed tranche nets, allocated deposits)."""


class SettlementImportService:
    """Settlement import + matching + tie-out over the ports."""

    def __init__(
        self,
        repository: SettlementRepositoryPort,
        registry: IntegrationsRegistryPort,
        audit: SettlementAuditPort,
        commands: SettlementCommandService,
        review: ReviewQueueCommandService,
        review_queries: ReviewQueueQueryService,
        reader: StatementFileReaderPort,
        outbox: EventOutboxRepositoryPort,
        posting: SettlementPostingPort,
        policy_gate: PolicyGateService,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._audit = audit
        self._commands = commands
        self._review = review
        self._review_queries = review_queries
        self._reader = reader
        self._outbox = outbox
        self._posting = posting
        self._policy_gate = policy_gate
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- shared helpers -----------------------------------------------------

    def _channel(self, installation_id: str, channel_key: str) -> PaymentChannel:
        """Resolve a channel key or raise the standard 404."""
        channel = self._registry.get_channel(installation_id, channel_key)
        if channel is None:
            raise IntegrationChannelNotFoundError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        return channel

    def _match_window_days(self, installation_id: str) -> int:
        """The deposit-matching date window (±days, default 2)."""
        settings = self._registry.get_installation_settings(installation_id)
        if settings is None:
            return 2
        return settings.matching_window_days

    def _tolerances(self, channel_id: str) -> tuple[int, int]:
        """(match_tolerance_irr, tieout_tolerance_irr) from the ACTIVE contract.

        No active contract → exact matching (tolerance 0): a batch
        without a contract never silently absorbs a difference.
        """
        contract = self._registry.get_active_contract(channel_id)
        if contract is None:
            return 0, 0
        return contract.match_tolerance_irr, contract.tieout_tolerance_irr

    def _file_exception(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        channel_key: str,
        exception_type: SettlementExceptionType,
        severity: SettlementExceptionSeverity,
        reason: str,
        suggested_action: str,
        collector: _ExceptionCollector,
        tranche_id: str | None = None,
        settlement_event_id: str | None = None,
        bank_deposit_id: str | None = None,
        gateway_payment_id: str | None = None,
        expected_amount_irr: int | None = None,
        actual_amount_irr: int | None = None,
        dedupe: bool = True,
    ) -> SettlementException | None:
        """Open a settlement exception AND file its unified review item.

        Dedupes on the open exception of the same type for the same
        tranche/deposit/event (a re-run never files the same exception
        twice). Returns None when deduped. The review item is
        ``settlement_variance`` with the exception type as subtype,
        carrying the expected vs actual amounts.
        """
        if dedupe:
            existing: SettlementException | None = None
            if tranche_id is not None:
                existing = self._repository.get_open_exception_for_tranche(
                    installation_id, tranche_id, exception_type.value
                )
            elif bank_deposit_id is not None:
                existing = self._repository.get_open_exception_for_deposit(
                    installation_id, bank_deposit_id, exception_type.value
                )
            elif settlement_event_id is not None:
                existing = self._repository.get_open_exception_for_event(
                    installation_id, settlement_event_id, exception_type.value
                )
            if existing is not None:
                return None
        exc = self._repository.save_exception(
            CreateSettlementException(
                installation_id=installation_id,
                exception_type=exception_type.value,
                severity=severity.value,
                tranche_id=tranche_id,
                settlement_event_id=settlement_event_id,
                bank_deposit_id=bank_deposit_id,
                gateway_payment_id=gateway_payment_id,
                expected_amount_irr=expected_amount_irr,
                actual_amount_irr=actual_amount_irr,
                note=reason,
            )
        )
        collector.exceptions.append(exc)
        amount = actual_amount_irr if actual_amount_irr is not None else expected_amount_irr
        item = self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.SETTLEMENT_VARIANCE,
            severity=ReviewSeverity(severity.value),
            reason=reason,
            suggested_action=suggested_action,
            subtype=exception_type.value,
            channel=channel_key,
            source_event_ref=f"settlement-exception:{exc.id}",
            amount_irr=amount,
            actor="settlement-import",
        )
        collector.review_item_ids.append(item.id)
        return exc

    def _resolve_review_item(self, tenant_id: str, item_id: str, action_code: str, actor: str) -> None:
        """Resolve a linked review item; an already-terminal item is tolerated."""
        try:
            self._review.perform_action(
                item_id=item_id, tenant_id=tenant_id, action_code=action_code, actor=actor
            )
        except ReviewInvalidStateError:
            # The item was already resolved elsewhere (e.g. from the
            # queue page); the import decision stands either way.
            return

    def _find_batch_review_item(self, tenant_id: str, import_id: str) -> str | None:
        """Find the open MANUAL_IMPORT_REVIEW item for an import, if any."""
        items = self._review_queries.list_items(
            tenant_id,
            ReviewItemFilters(
                item_type=ReviewItemType.MANUAL_IMPORT_REVIEW,
                status=None,
            ),
        )
        wanted = f"settlement-import:{import_id}"
        for view in items:
            item = view.item
            if item.source_event_ref == wanted and item.status not in (
                ReviewItemStatus.RESOLVED,
                ReviewItemStatus.WRITTEN_OFF,
            ):
                return item.id
        return None

    def _require_import(
        self, installation_id: str, import_id: str
    ) -> SettlementFileImport:
        file_import = self._repository.get_file_import(installation_id, import_id)
        if file_import is None:
            raise SettlementFileImportNotFoundError(
                f"Settlement file import {import_id!r} does not exist.",
                details={"import_id": import_id},
            )
        return file_import

    # -- C1: import -----------------------------------------------------------

    def import_file(
        self, command: ImportSettlementFile, *, tenant_id: str
    ) -> ImportSettlementFileResult:
        """Import a settlement file into an awaiting-review batch (C1).

        Parse → validate → dedupe → dry-run match preview → review
        item. Nothing is applied: tranche states, events and canonical
        events all wait for ``approve_import``. Re-importing the same
        file is a no-op returning the existing import.
        """
        if command.file_type not in (FILE_TYPE_DETAIL, FILE_TYPE_PAYOUTS, FILE_TYPE_STATEMENT):
            raise SettlementImportInvalidError(
                f"Unknown file_type {command.file_type!r} "
                "(expected 'detail', 'payouts' or 'statement').",
                details={"file_type": command.file_type},
            )
        if not command.content:
            raise SettlementImportInvalidError(
                "The uploaded file is empty.",
                details={"filename": command.filename},
            )
        channel = self._channel(command.installation_id, command.channel_key)
        file_hash = hashlib.sha256(command.content).hexdigest()
        existing = self._repository.get_file_import_by_hash(
            command.installation_id, channel.id, file_hash
        )
        if existing is not None:
            # File-hash dedupe: re-importing the same file is a no-op.
            rows = self._repository.list_file_rows(existing.id)
            preview = self._preview_from_rows(
                command.installation_id, channel.id, rows, command.file_type
            )
            return ImportSettlementFileResult(
                file_import=existing, preview=preview, duplicate=True
            )
        detected = self._reader.read_file(command.content, command.filename)
        if not detected.grid:
            raise SettlementImportInvalidError(
                "The file has no rows.",
                details={"filename": command.filename},
            )
        headers = [str(cell or "") for cell in detected.grid[command.header_row]]
        columns = command.columns
        if columns is None:
            columns = suggest_settlement_mapping(headers)
        problems = validate_settlement_mapping_columns(columns, command.file_type)
        if problems:
            raise SettlementImportInvalidError(
                f"The column mapping does not fit this file: {'; '.join(problems)}.",
                details={"problems": problems},
            )
        parsed = rows_from_settlement_grid(
            detected.grid,
            columns,
            currency_unit=command.currency_unit,
            date_kind=command.date_kind,
            header_row=command.header_row,
            file_type=command.file_type,
        )
        if not parsed.valid_rows:
            errors = [err for row in parsed.rows for err in row.errors][:5]
            raise SettlementImportInvalidError(
                "The file has no valid settlement rows"
                + (f": {'; '.join(errors)}" if errors else "."),
                details={"rows": len(parsed.rows), "errors": errors},
            )
        import_batch_id = str(uuid.uuid4())
        file_import = self._repository.save_file_import(
            CreateSettlementFileImport(
                installation_id=command.installation_id,
                channel_key=command.channel_key,
                file_type=command.file_type,
                period_jalali=command.period_jalali,
                file_hash_sha256=file_hash,
                import_batch_id=import_batch_id,
                uploaded_by=command.uploaded_by,
                row_count=len(parsed.rows),
            ),
            channel_id=channel.id,
        )
        # Dry-run match (advisory): the approve step re-runs it fresh.
        match_tolerance, _ = self._tolerances(channel.id)
        row_commands: list[CreateSettlementFileRow] = []
        matched = 0
        new_payments = 0
        for row in parsed.rows:
            status, note, matched_tranche_id = self._dry_run_row(
                command.installation_id,
                channel.id,
                row,
                command.file_type,
                match_tolerance,
            )
            if status == ROW_MATCHED:
                matched += 1
            elif status == ROW_NEW_PAYMENT:
                new_payments += 1
            row_commands.append(
                CreateSettlementFileRow(
                    import_id=file_import.id,
                    row_no=row.row_number,
                    raw_json={key: str(value) for key, value in row.raw.items()},
                    match_status=status,
                    gateway_transaction_id=row.transaction_id,
                    tran_type=row.tran_type,
                    amount_irr=row.gross_irr,
                    fee_irr=row.fee_irr,
                    net_irr=row.net_irr,
                    transaction_date=row.row_date.gregorian_date if row.row_date else None,
                    tracking_ref=row.tracking_ref,
                    matched_tranche_id=matched_tranche_id,
                    note=note,
                )
            )
            if row.deductions_irr:
                # The row table has no deductions column: a row's
                # deductions-column figure is stored as a synthetic
                # deduction row (same row number — the table has no
                # (import_id, row_no) unique constraint) so the
                # approve step sums it as a documented deduction.
                row_commands.append(
                    CreateSettlementFileRow(
                        import_id=file_import.id,
                        row_no=row.row_number,
                        raw_json={
                            "deductions": str(row.deductions_irr),
                            "source_row": str(row.row_number),
                        },
                        match_status=ROW_MATCHED,
                        gateway_transaction_id=row.transaction_id,
                        tran_type="deduction",
                        amount_irr=None,
                        fee_irr=None,
                        net_irr=row.deductions_irr,
                        transaction_date=row.row_date.gregorian_date
                        if row.row_date
                        else None,
                        tracking_ref=row.tracking_ref,
                        note=f"documented deduction from row {row.row_number} (deductions column)",
                    )
                )
        saved_rows = self._repository.save_file_rows(row_commands)
        updated = self._repository.update_file_import(
            file_import.id,
            status=IMPORT_AWAITING_REVIEW,
            row_count=len(saved_rows),
            matched_count=matched,
            new_count=new_payments,
            error_count=len(parsed.invalid_rows),
        )
        assert updated is not None
        preview = self._preview_from_rows(
            command.installation_id, channel.id, saved_rows, command.file_type
        )
        item = self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.MANUAL_IMPORT_REVIEW,
            severity=ReviewSeverity.WARNING,
            reason=(
                f"ایمپورت فایل تسویهٔ {command.channel_key} ({command.filename}): "
                f"{preview.matched_rows} ردیف منطبق، {preview.new_payment_rows} پرداخت جدید، "
                f"{preview.unknown_rows} مرجع ناشناخته، {preview.invalid_rows} نامعتبر؛ "
                f"خالص {preview.net_irr:,} ریال، واریز موردانتظار {preview.expected_deposit_irr:,} ریال."
            ),
            suggested_action="پیش‌نمایش اثر حسابداری را بررسی و تأیید کنید، یا بچ را رد کنید.",
            channel=command.channel_key,
            source_event_ref=f"settlement-import:{file_import.id}",
            amount_irr=preview.net_irr,
            actor=command.actor or "settlement-import",
        )
        return ImportSettlementFileResult(
            file_import=updated,
            preview=preview,
            duplicate=False,
            review_item_id=item.id,
            dropped_rows=tuple(
                (dropped.row_number, dropped.reason) for dropped in parsed.dropped_rows
            ),
        )

    def _dry_run_row(
        self,
        installation_id: str,
        channel_id: str,
        row: ParsedSettlementRow,
        file_type: str,
        match_tolerance: int,
    ) -> tuple[str, str | None, str | None]:
        """Advisory match for one parsed row (preview only, no writes)."""
        if not row.is_valid:
            return ROW_UNKNOWN_REFERENCE, "; ".join(row.errors), None
        if file_type in (FILE_TYPE_PAYOUTS, FILE_TYPE_STATEMENT):
            if row.tran_type == "deduction":
                return ROW_MATCHED, "documented period deduction (no tranche)", None
            return ROW_MATCHED, "batch-matched at approval (FIFO by due date)", None
        # Detail rows: exact transaction_id match against the registry.
        if row.tran_type in ("refund", "cancel"):
            payment = (
                self._repository.get_payment_by_transaction(
                    installation_id, channel_id, row.transaction_id
                )
                if row.transaction_id
                else None
            )
            if payment is None:
                return ROW_UNKNOWN_REFERENCE, "refund/cancel for an unknown transaction", None
            return ROW_MATCHED, "refund/cancel — processed at approval", None
        if row.tran_type == "deduction":
            return ROW_MATCHED, "documented period deduction (no tranche)", None
        if row.tran_type == "adjustment" and (row.net_irr or 0) < 0:
            # A negative adjustment row is how the gateway deducts
            # already-paid tranches from a later settlement (§3.3) —
            # a documented deduction, not a tranche confirmation.
            return ROW_MATCHED, "negative adjustment (documented deduction)", None
        if not row.transaction_id:
            return ROW_UNKNOWN_REFERENCE, "detail row without a transaction id", None
        payment = self._repository.get_payment_by_transaction(
            installation_id, channel_id, row.transaction_id
        )
        if payment is None:
            if row.gross_irr is None or row.gross_irr <= 0:
                return (
                    ROW_UNKNOWN_REFERENCE,
                    "unknown transaction without a gross amount (cannot create a payment)",
                    None,
                )
            return ROW_NEW_PAYMENT, "new payment — created at approval", None
        tranches = self._repository.list_tranches_for_payment(installation_id, payment.id)
        confirmable = self._confirmable_tranches(tranches)
        if not confirmable:
            return ROW_DUPLICATE, "payment has no tranches left to confirm (already settled)", None
        target = self._pick_tranche_for_row(confirmable, row.net_irr or 0, match_tolerance)
        if target is None:
            return ROW_DUPLICATE, "no tranche left to confirm", None
        if row.net_irr is not None and abs(row.net_irr - target.net_irr) > match_tolerance:
            return (
                ROW_AMOUNT_MISMATCH,
                f"row net {row.net_irr:,} differs from tranche net {target.net_irr:,}",
                target.id,
            )
        return ROW_MATCHED, None, target.id

    @staticmethod
    def _pick_tranche_for_row(
        open_tranches: Sequence[SettlementTranche],
        row_net_irr: int,
        match_tolerance: int,
    ) -> SettlementTranche | None:
        """Pick the tranche a detail row confirms: earliest due whose net matches.

        Prefers an exact (within-tolerance) net match; falls back to
        the earliest-due open tranche (the caller flags the amount
        difference as ``amount_mismatch``).
        """
        ordered = sorted(open_tranches, key=lambda t: (t.expected_date, t.tranche_index))
        for tranche in ordered:
            if abs(tranche.net_irr - row_net_irr) <= match_tolerance:
                return tranche
        return ordered[0] if ordered else None

    def _preview_from_rows(
        self,
        installation_id: str,
        channel_id: str,
        rows: Sequence[SettlementFileRow],
        file_type: str,
    ) -> ImportPreview:
        """Build the accounting-effect preview from stored rows."""
        valid = [row for row in rows if row.net_irr is not None]

        def _is_deduction(row: SettlementFileRow) -> bool:
            return row.tran_type == "deduction" or (
                row.tran_type == "adjustment" and (row.net_irr or 0) < 0
            )

        settlement_rows = [
            row
            for row in valid
            if not _is_deduction(row) and row.tran_type not in ("refund", "cancel")
        ]
        gross = sum(row.amount_irr or 0 for row in settlement_rows if (row.amount_irr or 0) > 0)
        fee = sum(row.fee_irr or 0 for row in settlement_rows)
        # VAT is not in the file: it is the fee VAT snapshotted on
        # the tranches these rows match (one tranche's VAT counted
        # once, however many rows point at it). Unmatched rows carry
        # no tranche, so they contribute no VAT.
        matched_tranche_ids = {
            row.matched_tranche_id
            for row in settlement_rows
            if row.matched_tranche_id is not None
        }
        vat = 0
        for tranche_id in matched_tranche_ids:
            tranche = self._repository.get_tranche(installation_id, tranche_id)
            if tranche is not None:
                vat += tranche.fee_vat_irr
        net = sum(row.net_irr or 0 for row in settlement_rows)
        refunds = -sum(
            row.net_irr or 0
            for row in valid
            if row.tran_type in ("refund", "cancel") and (row.net_irr or 0) < 0
        )
        deductions = abs(sum(row.net_irr or 0 for row in valid if _is_deduction(row)))
        expected_deposit = net - deductions
        open_tranches = self._repository.list_tranches(
            installation_id, channel_id=channel_id, states=list(_OPEN_TRANCHE_STATES)
        )
        receivable_before = sum(t.outstanding_net_irr for t in open_tranches)
        matched_net = sum(
            row.net_irr or 0 for row in valid if row.match_status == ROW_MATCHED
        )
        receivable_after = max(0, receivable_before - matched_net)
        return ImportPreview(
            gross_irr=gross,
            fee_irr=fee,
            vat_irr=vat,
            net_irr=net,
            refunds_irr=refunds,
            deductions_irr=deductions,
            expected_deposit_irr=expected_deposit,
            receivable_before_irr=receivable_before,
            receivable_after_irr=receivable_after,
            matched_rows=sum(1 for row in rows if row.match_status == ROW_MATCHED),
            new_payment_rows=sum(1 for row in rows if row.match_status == ROW_NEW_PAYMENT),
            unknown_rows=sum(1 for row in rows if row.match_status == ROW_UNKNOWN_REFERENCE),
            duplicate_rows=sum(1 for row in rows if row.match_status == ROW_DUPLICATE),
            invalid_rows=sum(1 for row in rows if row.net_irr is None),
            dropped_rows=0,
        )

    def preview_for_import(
        self, installation_id: str, import_id: str
    ) -> ImportPreview:
        """Recompute the accounting-effect preview from stored rows (US-554).

        The same preview ``import_file`` computed at upload — the
        manual-import batch re-run uses it to refresh the preview
        from the stored rows without re-uploading the file.
        """
        file_import = self._require_import(installation_id, import_id)
        rows = self._repository.list_file_rows(file_import.id)
        return self._preview_from_rows(
            installation_id,
            file_import.payment_channel_id,
            rows,
            file_import.file_type,
        )

    # -- C1: approve / reject ---------------------------------------------------

    def approve_import(
        self, installation_id: str, command: ApproveSettlementImport, *, tenant_id: str
    ) -> ApproveImportResult:
        """Approve an awaiting-review import (C1): authoritative matching.

        Re-runs the match fresh from the rows (preview state is never
        trusted), confirms tranches, creates the settlement event and
        publishes the canonical ``payment.settlement.received`` event.
        Idempotent on (gateway, transaction_id): a payment the webhook
        recorded between import and approve is matched, not duplicated.
        """
        file_import = self._require_import(installation_id, command.import_id)
        if file_import.status != IMPORT_AWAITING_REVIEW:
            raise SettlementImportStateError(
                f"Import {command.import_id} is {file_import.status} and cannot be approved.",
                details={"import_id": command.import_id, "status": file_import.status},
            )
        channel = self._registry.get_channel_by_id(file_import.payment_channel_id)
        channel_key = channel.channel_key if channel is not None else file_import.payment_channel_id
        match_tolerance, _ = self._tolerances(file_import.payment_channel_id)
        rows = self._repository.list_file_rows(file_import.id)
        collector = _ExceptionCollector()
        confirmed: list[tuple[str, int]] = []
        created_payments: list[str] = []
        event_net = 0
        deductions = 0
        payout_date: datetime.date | None = None
        tracking_ref: str | None = None
        reassigned_payments: set[str] = set()
        for row in rows:
            if row.net_irr is None or row.transaction_date is None:
                # Invalid rows were counted at import; they never match.
                continue
            if payout_date is None or row.transaction_date > payout_date:
                payout_date = row.transaction_date
            if tracking_ref is None and row.tracking_ref:
                tracking_ref = row.tracking_ref
            if row.tran_type == "deduction" or (
                row.tran_type == "adjustment" and (row.net_irr or 0) < 0
            ):
                deductions += abs(row.net_irr)
                self._repository.update_file_row(
                    row.id, match_status=ROW_MATCHED, note="documented period deduction"
                )
                continue
            if file_import.file_type in (FILE_TYPE_PAYOUTS, FILE_TYPE_STATEMENT):
                event_net += row.net_irr
                self._repository.update_file_row(
                    row.id, match_status=ROW_MATCHED, note="payout row — batch-matched"
                )
                continue
            # Detail rows: exact transaction_id match (C2).
            if row.tran_type in ("refund", "cancel"):
                self._apply_refund_row(
                    installation_id,
                    tenant_id,
                    channel_key,
                    file_import,
                    row,
                    collector,
                )
                continue
            outcome = self._apply_detail_row(
                installation_id,
                tenant_id,
                channel_key,
                file_import,
                row,
                match_tolerance,
                collector,
                reassigned_payments,
                created_payments,
            )
            if outcome is not None:
                confirmed.append(outcome)
                event_net += row.net_irr or 0
            # outcome None = duplicate / unknown / error — already handled.
        if payout_date is None:
            payout_date = self._clock().date()
        # Batch match (C2) for payouts/statement files: allocate the
        # event net across due tranches of the channel+period, FIFO.
        if file_import.file_type in (FILE_TYPE_PAYOUTS, FILE_TYPE_STATEMENT):
            batch_confirmed = self._apply_batch_match(
                installation_id,
                tenant_id,
                channel_key,
                file_import,
                event_net,
                match_tolerance,
                collector,
            )
            confirmed.extend(batch_confirmed)
        # One settlement event per import (C1).
        canonical_event_id = str(uuid.uuid4())
        event = self._repository.save_settlement_event(
            CreateSettlementEvent(
                installation_id=installation_id,
                channel_key=channel_key,
                payout_date=payout_date,
                net_irr=event_net,
                source=(
                    "detail_file_derived"
                    if file_import.file_type == FILE_TYPE_DETAIL
                    else "payouts_file"
                ),
                deductions_irr=deductions,
                tracking_ref=tracking_ref,
                import_batch_id=file_import.import_batch_id,
                canonical_event_id=canonical_event_id,
            ),
            channel_id=file_import.payment_channel_id,
        )
        # Link the confirmed tranches to the event (C2 match links).
        for tranche_id, allocated in confirmed:
            self._repository.save_settlement_match(
                CreateSettlementMatch(
                    installation_id=installation_id,
                    tranche_id=tranche_id,
                    allocated_amount_irr=allocated,
                    method="exact_transaction"
                    if file_import.file_type == FILE_TYPE_DETAIL
                    else "batch_fifo",
                    settlement_event_id=event.id,
                    confidence=Decimal("1"),
                    is_automatic=True,
                )
            )
        confirmed_ids = tuple(tranche_id for tranche_id, _ in confirmed)
        self._publish_settlement_received(
            tenant_id=tenant_id,
            installation_id=installation_id,
            channel_key=channel_key,
            event=event,
            file_import=file_import,
            tranche_ids=confirmed_ids,
            canonical_event_id=canonical_event_id,
        )
        # Reverse matching (C7): deposits that arrived before the file
        # now get their event confirmation.
        self._confirm_prior_deposits(
            installation_id, tenant_id, channel_key, event, collector
        )
        updated = self._repository.update_file_import(
            file_import.id,
            status=IMPORT_APPROVED,
            matched_count=len(confirmed),
            new_count=len(created_payments),
        )
        assert updated is not None
        review_item_id = self._find_batch_review_item(tenant_id, file_import.id)
        if review_item_id is not None:
            self._resolve_review_item(
                tenant_id, review_item_id, "approve_preview", command.actor
            )
        self._audit.record_settlement_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=None,
            action="settlement.import.approve",
            resource_type="settlement_file_import",
            resource_id=file_import.id,
            actor=command.actor,
            metadata={
                "channel_key": channel_key,
                "import_batch_id": file_import.import_batch_id,
                "settlement_event_id": event.id,
                "canonical_event_id": canonical_event_id,
                "confirmed_tranche_ids": list(confirmed_ids),
                "created_payment_ids": created_payments,
                "net_irr": event_net,
                "deductions_irr": deductions,
            },
        )
        return ApproveImportResult(
            file_import=updated,
            settlement_event=event,
            canonical_event_id=canonical_event_id,
            confirmed_tranche_ids=confirmed_ids,
            created_payment_ids=tuple(created_payments),
            exceptions=tuple(collector.exceptions),
            review_item_ids=tuple(collector.review_item_ids),
        )

    # -- C2: row application -------------------------------------------------

    def _apply_detail_row(
        self,
        installation_id: str,
        tenant_id: str,
        channel_key: str,
        file_import: SettlementFileImport,
        row: SettlementFileRow,
        match_tolerance: int,
        collector: _ExceptionCollector,
        reassigned_payments: set[str],
        created_payments: list[str],
    ) -> tuple[str, int] | None:
        """Apply one detail row at approval: match + confirm one tranche.

        Returns ``(tranche_id, allocated_amount_irr)`` or None when the
        row is a duplicate / unknown reference (already handled).
        """
        if not row.gateway_transaction_id:
            self._file_exception(
                tenant_id=tenant_id,
                installation_id=installation_id,
                channel_key=channel_key,
                exception_type=SettlementExceptionType.UNKNOWN_REFERENCE,
                severity=SettlementExceptionSeverity.WARNING,
                reason=(
                    f"ردیف {row.row_no} فایل تسویه شناسهٔ تراکنش ندارد و قابل تطبیق نیست."
                ),
                suggested_action="ردیف را بررسی کنید؛ شناسهٔ تراکنش را از فایل درگاه بگیرید یا ردیف را نادیده بگیرید.",
                collector=collector,
                expected_amount_irr=None,
                actual_amount_irr=row.net_irr,
            )
            self._repository.update_file_row(row.id, match_status=ROW_UNKNOWN_REFERENCE)
            return None
        payment = self._repository.get_payment_by_transaction(
            installation_id, file_import.payment_channel_id, row.gateway_transaction_id
        )
        if payment is None:
            payment = self._create_payment_from_row(
                installation_id,
                channel_key,
                file_import,
                row,
                collector,
                tenant_id,
            )
            if payment is None:
                return None
            created_payments.append(payment.id)
        # Period reassignment (§3.2): the file's month wins; open
        # tranches re-derive, confirmed/received ones are untouched.
        if row.transaction_date is not None and payment.id not in reassigned_payments:
            row_period = sales_period_from(
                datetime.datetime.combine(row.transaction_date, datetime.time.min, tzinfo=datetime.UTC)
            )
            if row_period != payment.sales_period_jalali:
                self._commands.reassign_period(
                    installation_id,
                    ReassignSalesPeriod(
                        payment_id=payment.id,
                        new_sales_period_jalali=row_period,
                        reason=(
                            f"Gateway file {file_import.id} shows the transaction in "
                            f"{row_period}, not {payment.sales_period_jalali}."
                        ),
                        actor="settlement-import",
                    ),
                    tenant_id=tenant_id,
                )
                reassigned_payments.add(payment.id)
        tranches = self._repository.list_tranches_for_payment(installation_id, payment.id)
        confirmable = self._confirmable_tranches(tranches)
        if not confirmable:
            self._repository.update_file_row(
                row.id,
                match_status=ROW_DUPLICATE,
                note="payment has no tranches left to confirm (already settled)",
            )
            return None
        target = self._pick_tranche_for_row(
            confirmable, row.net_irr or 0, match_tolerance
        )
        if target is None:
            self._repository.update_file_row(row.id, match_status=ROW_DUPLICATE)
            return None
        # Fee check: the file's fee vs the tranche's expected fee.
        if row.fee_irr is not None and abs(row.fee_irr - target.fee_irr) > match_tolerance:
            self._file_exception(
                tenant_id=tenant_id,
                installation_id=installation_id,
                channel_key=channel_key,
                exception_type=SettlementExceptionType.FEE_MISMATCH,
                severity=SettlementExceptionSeverity.WARNING,
                reason=(
                    f"کارمزد فایل برای قسط {target.tranche_index} پرداخت {payment.id} "
                    f"({row.fee_irr:,} ریال) با کارمزد موردانتظار ({target.fee_irr:,} ریال) "
                    "تفاوت دارد."
                ),
                suggested_action="مغایرت کارمزد را بررسی کنید؛ تعدیل با سند اصلاحی ثبت می‌شود یا قرارداد اصلاح می‌شود.",
                collector=collector,
                tranche_id=target.id,
                gateway_payment_id=payment.id,
                expected_amount_irr=target.fee_irr,
                actual_amount_irr=row.fee_irr,
            )
            self._repository.save_fee_record(
                installation_id=installation_id,
                gateway_payment_id=payment.id,
                contract_version_id=payment.contract_version_id,
                fee_percent_applied=payment.fee_rate_snapshot,
                fee_vat_rate_applied=payment.fee_vat_rate_snapshot,
                expected_fee_irr=target.fee_irr,
                expected_vat_irr=target.fee_vat_irr,
                source="file",
                tranche_id=target.id,
                actual_fee_irr=row.fee_irr,
                fee_variance_irr=row.fee_irr - target.fee_irr,
                # File rows carry no VAT column (the row table has no
                # VAT field): the fee record keeps the expected VAT
                # and only the fee itself is reconciled with actuals.
                actual_vat_irr=None,
                vat_variance_irr=None,
            )
        # Amount check: the row net vs the tranche net.
        row_net = row.net_irr or 0
        if abs(row_net - target.net_irr) > match_tolerance:
            self._file_exception(
                tenant_id=tenant_id,
                installation_id=installation_id,
                channel_key=channel_key,
                exception_type=SettlementExceptionType.AMOUNT_MISMATCH,
                severity=SettlementExceptionSeverity.WARNING,
                reason=(
                    f"خالص فایل برای قسط {target.tranche_index} پرداخت {payment.id} "
                    f"({row_net:,} ریال) با خالص موردانتظار ({target.net_irr:,} ریال) "
                    "تفاوت دارد."
                ),
                suggested_action="کسورات و بدهی دورهٔ قبل را در صورت‌وضعیت بررسی کنید؛ مغایرت بدون مستند، کسورات نیست.",
                collector=collector,
                tranche_id=target.id,
                gateway_payment_id=payment.id,
                expected_amount_irr=target.net_irr,
                actual_amount_irr=row_net,
            )
            self._repository.update_file_row(row.id, match_status=ROW_AMOUNT_MISMATCH)
        else:
            self._repository.update_file_row(
                row.id, match_status=ROW_MATCHED, matched_tranche_id=target.id
            )
        allocated = self._confirm_tranche(
            target, min(row_net, target.net_irr - target.confirmed_net_irr)
        )
        return (target.id, allocated) if allocated > 0 else None

    def _create_payment_from_row(
        self,
        installation_id: str,
        channel_key: str,
        file_import: SettlementFileImport,
        row: SettlementFileRow,
        collector: _ExceptionCollector,
        tenant_id: str,
    ) -> GatewayPayment | None:
        """Create a payment for an unknown transaction (new_payment rows).

        Idempotent on (gateway, transaction_id): a payment the webhook
        recorded between import and approve is matched, not duplicated.
        Returns None (row handled) when the payment cannot be created.
        """
        if row.amount_irr is None or row.amount_irr <= 0 or row.transaction_date is None:
            self._file_exception(
                tenant_id=tenant_id,
                installation_id=installation_id,
                channel_key=channel_key,
                exception_type=SettlementExceptionType.UNKNOWN_REFERENCE,
                severity=SettlementExceptionSeverity.WARNING,
                reason=(
                    f"ردیف {row.row_no} به تراکنش ناشناختهٔ {row.gateway_transaction_id} "
                    "اشاره می‌کند و مبلغ ناخالص ندارد؛ پرداخت جدید ساخته نشد."
                ),
                suggested_action="پرداخت را دستی بسازید یا ردیف را با دلیل نادیده بگیرید.",
                collector=collector,
                expected_amount_irr=None,
                actual_amount_irr=row.net_irr,
            )
            self._repository.update_file_row(row.id, match_status=ROW_UNKNOWN_REFERENCE)
            return None
        confirmed_at = datetime.datetime.combine(
            row.transaction_date, datetime.time.min, tzinfo=datetime.UTC
        )
        try:
            result = self._commands.record_payment(
                CreateGatewayPayment(
                    installation_id=installation_id,
                    channel_key=channel_key,
                    gateway_transaction_id=row.gateway_transaction_id or "",
                    gross_irr=row.amount_irr,
                    gateway_confirmed_at=confirmed_at,
                    source=PaymentSource.MANUAL_IMPORT,
                )
            )
        except SettlementPaymentExistsError:
            # The webhook landed the payment after the preview: match it.
            existing = self._repository.get_payment_by_transaction(
                installation_id, file_import.payment_channel_id, row.gateway_transaction_id or ""
            )
            if existing is None:
                self._repository.update_file_row(row.id, match_status=ROW_UNKNOWN_REFERENCE)
                return None
            return existing
        except (
            SettlementContractMissingError,
            SettlementFeeVatRateUnknownError,
            SettlementDerivationError,
            SettlementPaymentInvalidError,
        ) as exc:
            # A missing contract / VAT rate / bad schedule must not kill
            # the batch: the row is an unknown reference with the reason.
            self._file_exception(
                tenant_id=tenant_id,
                installation_id=installation_id,
                channel_key=channel_key,
                exception_type=SettlementExceptionType.UNKNOWN_REFERENCE,
                severity=SettlementExceptionSeverity.WARNING,
                reason=(
                    f"ساخت پرداخت برای تراکنش {row.gateway_transaction_id} ممکن نشد: {exc}."
                ),
                suggested_action="قرارداد فعال کانال و نرخ مالیات کارمزد را بررسی کنید.",
                collector=collector,
                expected_amount_irr=None,
                actual_amount_irr=row.net_irr,
                dedupe=False,
            )
            self._repository.update_file_row(
                row.id, match_status=ROW_UNKNOWN_REFERENCE, note=str(exc)
            )
            return None
        self._repository.update_file_row(row.id, match_status=ROW_NEW_PAYMENT)
        return result.payment

    def _apply_refund_row(
        self,
        installation_id: str,
        tenant_id: str,
        channel_key: str,
        file_import: SettlementFileImport,
        row: SettlementFileRow,
        collector: _ExceptionCollector,
    ) -> None:
        """Apply a refund/cancel row: cancel the payment (open tranches cancel).

        Already-paid tranches become a negative adjustment via the
        cancel flow (which opens ``cancelled_after_settlement`` when
        money was already paid).
        """
        payment = (
            self._repository.get_payment_by_transaction(
                installation_id, file_import.payment_channel_id, row.gateway_transaction_id
            )
            if row.gateway_transaction_id
            else None
        )
        if payment is None:
            self._file_exception(
                tenant_id=tenant_id,
                installation_id=installation_id,
                channel_key=channel_key,
                exception_type=SettlementExceptionType.UNKNOWN_REFERENCE,
                severity=SettlementExceptionSeverity.WARNING,
                reason=(
                    f"ردیف برگشت/لغو {row.row_no} به تراکنش ناشناختهٔ "
                    f"{row.gateway_transaction_id} اشاره می‌کند."
                ),
                suggested_action="تراکنش را در درگاه بررسی کنید؛ پرداخت را دستی بسازید یا ردیف را نادیده بگیرید.",
                collector=collector,
                expected_amount_irr=None,
                actual_amount_irr=row.net_irr,
            )
            self._repository.update_file_row(row.id, match_status=ROW_UNKNOWN_REFERENCE)
            return
        if payment.status.value in ("cancelled", "partially_cancelled"):
            self._repository.update_file_row(
                row.id, match_status=ROW_DUPLICATE, note="payment already cancelled"
            )
            return
        result = self._commands.cancel_payment(
            installation_id,
            CancelGatewayPayment(
                payment_id=payment.id,
                reason=f"Gateway file {file_import.id} row {row.row_no}: refund/cancel.",
                actor="settlement-import",
            ),
            tenant_id=tenant_id,
        )
        if result.exception is not None:
            collector.exceptions.append(result.exception)
        self._repository.update_file_row(
            row.id, match_status=ROW_MATCHED, note="payment cancelled at approval"
        )

    def _apply_batch_match(
        self,
        installation_id: str,
        tenant_id: str,
        channel_key: str,
        file_import: SettlementFileImport,
        event_net_irr: int,
        match_tolerance: int,
        collector: _ExceptionCollector,
    ) -> list[tuple[str, int]]:
        """Batch-match a payouts/statement event net across due tranches (C2).

        Due tranches of the same gateway + period, FIFO by due date.
        A residual above tolerance is an ``amount_mismatch`` (the
        event net does not cover — or exceeds — the due schedule).
        """
        tranches = self._repository.list_tranches(
            installation_id,
            channel_id=file_import.payment_channel_id,
            states=[
                TrancheState.DUE,
                TrancheState.OVERDUE,
                TrancheState.PARTIALLY_CONFIRMED,
            ],
            sales_period_jalali=file_import.period_jalali,
        )
        candidates = [
            TrancheCandidate(
                id=t.id,
                gateway_payment_id=t.gateway_payment_id,
                net_irr=t.net_irr,
                outstanding_net_irr=t.net_irr - t.confirmed_net_irr,
                expected_date=t.expected_date,
                state=t.state.value,
                sales_period_jalali=t.sales_period_jalali,
                tranche_index=t.tranche_index,
            )
            for t in tranches
            if t.net_irr - t.confirmed_net_irr > 0
        ]
        allocations, residual = batch_match_allocations(
            event_net_irr, candidates, match_tolerance
        )
        by_id = {t.id: t for t in tranches}
        confirmed: list[tuple[str, int]] = []
        for alloc in allocations:
            tranche = by_id[alloc.tranche_id]
            amount = self._confirm_tranche(tranche, alloc.allocated_amount_irr)
            if amount > 0:
                confirmed.append((tranche.id, amount))
        if residual > match_tolerance:
            self._file_exception(
                tenant_id=tenant_id,
                installation_id=installation_id,
                channel_key=channel_key,
                exception_type=SettlementExceptionType.AMOUNT_MISMATCH,
                severity=SettlementExceptionSeverity.WARNING,
                reason=(
                    f"خالص رویداد تسویه ({event_net_irr:,} ریال) با جمع قسط‌های سررسیدشدهٔ "
                    f"دورهٔ {file_import.period_jalali} ({event_net_irr - residual:,} ریال) "
                    "هم‌خوانی ندارد."
                ),
                suggested_action="قسط‌های دوره را با صورت‌وضعیت درگاه مقایسه کنید؛ مغایرت را تعدیل یا پیگیری کنید.",
                collector=collector,
                gateway_payment_id=None,
                expected_amount_irr=event_net_irr - residual,
                actual_amount_irr=event_net_irr,
                dedupe=False,
            )
        return confirmed

    def _publish_settlement_received(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        channel_key: str,
        event: SettlementEvent,
        file_import: SettlementFileImport,
        tranche_ids: tuple[str, ...],
        canonical_event_id: str,
    ) -> None:
        """Publish the canonical ``payment.settlement.received`` event (C1).

        Written to the transactional outbox in the same transaction as
        the settlement event: a worker publishes pending rows, and the
        outbox row is the durable record. The pipeline consumer for
        this type is the US-554 settlement-received handler
        (``SettlementReceivedHandler``): a receipt arriving as an
        event flows through the same tie-out + auto-post rules as a
        file import.
        """
        now = self._clock()
        payload: dict[str, object] = {
            "import_batch_id": file_import.import_batch_id,
            "channel_key": channel_key,
            "settlement_event_id": event.id,
            "payout_date": event.payout_date.isoformat(),
            "net_irr": event.net_irr,
            "deductions_irr": event.deductions_irr,
            "tranche_ids": list(tranche_ids),
            "file_type": file_import.file_type,
            "source": event.source,
        }
        self._outbox.save(
            OutboxEvent(
                id=canonical_event_id,
                tenant_id=tenant_id,
                aggregate_type="settlement_event",
                aggregate_id=event.id,
                event_type=SETTLEMENT_RECEIVED_EVENT_TYPE,
                payload=payload,
                payload_hash=canonical_payload_hash(payload),
                status=OutboxEventStatus.PENDING,
                attempts=0,
                last_attempt_at=None,
                last_error=None,
                published_at=None,
                correlation_id=file_import.import_batch_id,
                created_at=now,
                updated_at=now,
            )
        )

    def _confirm_prior_deposits(
        self,
        installation_id: str,
        tenant_id: str,
        channel_key: str,
        event: SettlementEvent,
        collector: _ExceptionCollector,
    ) -> None:
        """Confirm C7 deposits: earlier deposits now get their event (C7).

        Two cases:

        (a) Reverse-matched deposits — a deposit that arrived before
        the file attached to expected tranches (``received`` without
        ``confirmed``). The file's approval already confirmed the
        tranches (``_apply_detail_row`` → ``_confirm_tranche``'s
        RECEIVED branch); here the deposit's EXISTING match links
        gain the event. No new link is created: a duplicate link
        would double-count the deposit in the tie-out.

        (b) Rule-4 sweep — deposits with no links at all are
        re-matched against the new event; an auto-match applies
        immediately (a fresh deposit→tranche→event link).
        """
        matches = self._repository.list_matches_for_event(installation_id, event.id)
        tranche_ids = {m.tranche_id for m in matches}
        if not tranche_ids:
            return
        # (a) Attach the event to reverse-matched deposits' links.
        for tranche_id in sorted(tranche_ids):
            links = self._repository.list_matches_for_tranche(
                installation_id, tranche_id
            )
            for link in links:
                if link.bank_deposit_id is None or link.settlement_event_id is not None:
                    continue
                self._repository.update_match(link.id, settlement_event_id=event.id)
        # (b) Sweep fully unmatched deposits against the new event.
        unmatched = self._repository.list_bank_deposits(
            installation_id, unmatched_only=True
        )
        if not unmatched:
            return
        tranches = [
            self._repository.get_tranche(installation_id, tranche_id)
            for tranche_id in sorted(tranche_ids)
        ]
        candidates = [
            TrancheCandidate(
                id=t.id,
                gateway_payment_id=t.gateway_payment_id,
                net_irr=t.net_irr,
                outstanding_net_irr=t.net_irr - t.received_net_irr,
                expected_date=t.expected_date,
                state=t.state.value,
                sales_period_jalali=t.sales_period_jalali,
                tranche_index=t.tranche_index,
            )
            for t in tranches
            if t is not None and t.net_irr - t.received_net_irr > 0
        ]
        event_candidate = EventCandidate(
            id=event.id,
            net_irr=event.net_irr,
            payout_date=event.payout_date,
            tracking_ref=event.tracking_ref,
            tranche_ids=tuple(sorted(tranche_ids)),
        )
        window_days = self._match_window_days(installation_id)
        for deposit in unmatched:
            decision = match_deposit(
                DepositToMatch(
                    id=deposit.id,
                    deposit_date=deposit.deposit_date,
                    amount_irr=deposit.amount_irr,
                    description=deposit.description or "",
                    tracking_ref=deposit.tracking_ref,
                ),
                [event_candidate],
                candidates,
                {event.id: candidates},
                window_days=window_days,
            )
            if decision.auto_matched:
                self._apply_deposit_decision(
                    installation_id,
                    tenant_id,
                    channel_key,
                    deposit,
                    decision,
                    collector,
                    actor="settlement-import",
                )

    # -- tranche transitions -------------------------------------------------

    def _advance_to_due_if_expected(self, tranche: SettlementTranche) -> SettlementTranche:
        """Move an ``expected`` tranche to ``due`` (money arrived early).

        The §3.3 machine has no expected→confirmed/received edge: the
        tick moves expected→due at the due date; a file/deposit
        proving the tranche payable now takes the same edge early.
        """
        if tranche.state != TrancheState.EXPECTED:
            return tranche
        require_transition(tranche.state, TrancheState.DUE)
        updated = self._repository.update_tranche(tranche.id, state=TrancheState.DUE)
        assert updated is not None
        return updated

    @staticmethod
    def _confirmable_tranches(
        tranches: Sequence[SettlementTranche],
    ) -> list[SettlementTranche]:
        """Tranches a detail row can still confirm.

        Open tranches (expected/due/overdue), partially confirmed
        ones, and received tranches whose confirmed amount has not
        caught up yet (reverse match C7: the deposit arrived before
        the file, so the file only confirms — see ``_confirm_tranche``'s
        RECEIVED branch). Fully confirmed / settled / cancelled /
        written-off tranches have nothing left to confirm.
        """
        return [
            t
            for t in tranches
            if t.is_open
            or t.state == TrancheState.PARTIALLY_CONFIRMED
            or (
                t.state in (TrancheState.RECEIVED, TrancheState.PARTIALLY_RECEIVED)
                and t.confirmed_net_irr < t.net_irr
            )
        ]

    def _confirm_tranche(self, tranche: SettlementTranche, amount_irr: int) -> int:
        """Confirm ``amount_irr`` of a tranche (C2); returns the amount applied."""
        if amount_irr <= 0:
            return 0
        if tranche.state in (
            TrancheState.RECEIVED,
            TrancheState.PARTIALLY_RECEIVED,
            TrancheState.SETTLED,
        ):
            # Reverse match (C7): the deposit already arrived; the file
            # only confirms — update the confirmed amount, no state change.
            # (PARTIALLY_RECEIVED has no edge to CONFIRMED in the §3.3
            # machine, so it takes the same no-state-change path.)
            new_confirmed = min(tranche.net_irr, tranche.confirmed_net_irr + amount_irr)
            applied = new_confirmed - tranche.confirmed_net_irr
            if applied > 0:
                self._repository.update_tranche(
                    tranche.id, confirmed_net_irr=new_confirmed
                )
            return applied
        tranche = self._advance_to_due_if_expected(tranche)
        new_confirmed = min(tranche.net_irr, tranche.confirmed_net_irr + amount_irr)
        applied = new_confirmed - tranche.confirmed_net_irr
        if applied <= 0:
            return 0
        new_state = (
            TrancheState.CONFIRMED
            if new_confirmed >= tranche.net_irr
            else TrancheState.PARTIALLY_CONFIRMED
        )
        require_transition(tranche.state, new_state)
        self._repository.update_tranche(
            tranche.id,
            state=new_state,
            confirmed_net_irr=new_confirmed,
            match_method="exact_transaction",
        )
        return applied

    def _receive_tranche(
        self,
        tranche: SettlementTranche,
        amount_irr: int,
        method: str,
        confidence: Decimal,
    ) -> int:
        """Receive ``amount_irr`` of a tranche (C5); returns the amount applied."""
        if amount_irr <= 0:
            return 0
        tranche = self._advance_to_due_if_expected(tranche)
        new_received = min(tranche.net_irr, tranche.received_net_irr + amount_irr)
        applied = new_received - tranche.received_net_irr
        if applied <= 0:
            return 0
        new_state = (
            TrancheState.RECEIVED
            if new_received >= tranche.net_irr
            else TrancheState.PARTIALLY_RECEIVED
        )
        require_transition(tranche.state, new_state)
        self._repository.update_tranche(
            tranche.id,
            state=new_state,
            received_net_irr=new_received,
            match_method=method,
            match_confidence=confidence,
            variance_irr=new_received - tranche.net_irr,
            variance_reason=(
                f"Partially received: {new_received:,} of {tranche.net_irr:,} IRR."
                if new_state == TrancheState.PARTIALLY_RECEIVED
                else None
            ),
        )
        return applied

    # -- C1: reject ----------------------------------------------------------

    def reject_import(
        self, installation_id: str, command: RejectSettlementImport, *, tenant_id: str
    ) -> SettlementFileImport:
        """Reject (cancel) an awaiting-review import — nothing is applied."""
        file_import = self._require_import(installation_id, command.import_id)
        if file_import.status != IMPORT_AWAITING_REVIEW:
            raise SettlementImportStateError(
                f"Import {command.import_id} is {file_import.status} and cannot be rejected.",
                details={"import_id": command.import_id, "status": file_import.status},
            )
        updated = self._repository.update_file_import(file_import.id, status=IMPORT_REJECTED)
        assert updated is not None
        review_item_id = self._find_batch_review_item(tenant_id, file_import.id)
        if review_item_id is not None:
            self._resolve_review_item(
                tenant_id, review_item_id, "cancel_batch", command.actor
            )
        self._audit.record_settlement_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=None,
            action="settlement.import.reject",
            resource_type="settlement_file_import",
            resource_id=file_import.id,
            actor=command.actor,
            metadata={
                "import_batch_id": file_import.import_batch_id,
                "reason": command.reason,
            },
        )
        return updated

    # -- C4/C5: bank deposits + matching --------------------------------------

    def record_deposit(
        self,
        command: RecordBankDeposit,
        *,
        company: CompanyRef | None = None,
    ) -> RecordDepositResult:
        """Record a bank deposit and run C5 matching (C4/C5).

        The deposit is matched in confidence order; auto-matches apply
        immediately (tranches → ``received``). When ``company`` is
        given and the deposit completes a batch, the tie-out + auto-post
        runs inline; otherwise posting waits for an explicit tie-out run.
        """
        if command.amount_irr <= 0:
            raise SettlementMatchError(
                f"A bank deposit must be positive, got {command.amount_irr}.",
                details={"amount_irr": command.amount_irr},
            )
        channel_key = command.channel_key or ""
        channel_id: str | None = None
        if command.channel_key:
            channel = self._channel(command.installation_id, command.channel_key)
            channel_id = channel.id
        deposit = self._repository.save_bank_deposit(
            CreateBankDeposit(
                installation_id=command.installation_id,
                deposit_date=command.deposit_date,
                amount_irr=command.amount_irr,
                bank_account_ref=command.bank_account_ref,
                description=command.description,
                tracking_ref=command.tracking_ref,
                bank_import_row_id=command.bank_import_row_id,
                source=command.source,
            )
        )
        collector = _ExceptionCollector()
        decision = self._decide_deposit(
            command.installation_id, deposit, channel_id=channel_id
        )
        matched = self._apply_deposit_decision(
            command.installation_id,
            command.tenant_id,
            channel_key,
            deposit,
            decision,
            collector,
            actor=command.actor or "settlement-import",
        )
        tieout: TieOutResult | None = None
        posted_journal_id: int | None = None
        if decision.settlement_event_id is not None and company is not None:
            event = self._repository.get_settlement_event(
                command.installation_id, decision.settlement_event_id
            )
            if event is not None and event.import_batch_id is not None:
                run = self._attempt_tieout_for_batch(
                    command.installation_id,
                    command.tenant_id,
                    event.import_batch_id,
                    company,
                    command.actor or "settlement-import",
                )
                if run is not None:
                    tieout = run.tieout
                    posted_journal_id = run.tryton_journal_id
                    collector.exceptions.extend(run.exceptions)
        return RecordDepositResult(
            deposit=deposit,
            decision_method=decision.method,
            decision_confidence=decision.confidence,
            matched_tranche_ids=tuple(matched),
            suggestions=tuple(
                (s.target_kind, s.target_id, s.confidence, s.reason)
                for s in decision.suggestions
            ),
            exceptions=tuple(collector.exceptions),
            tieout=tieout,
            posted_journal_id=posted_journal_id,
        )

    def _decide_deposit(
        self,
        installation_id: str,
        deposit: BankDeposit,
        *,
        channel_id: str | None = None,
    ) -> DepositMatchDecision:
        """Build the C5 candidates and run the pure deposit matcher."""
        events = self._repository.list_settlement_events(
            installation_id, channel_id=channel_id
        )
        event_candidates: list[EventCandidate] = []
        event_tranches: dict[str, list[TrancheCandidate]] = {}
        for event in events:
            matches = self._repository.list_matches_for_event(installation_id, event.id)
            tranches: list[TrancheCandidate] = []
            for match in matches:
                tranche = self._repository.get_tranche(installation_id, match.tranche_id)
                if tranche is None:
                    continue
                outstanding = tranche.net_irr - tranche.received_net_irr
                if outstanding > 0:
                    tranches.append(
                        TrancheCandidate(
                            id=tranche.id,
                            gateway_payment_id=tranche.gateway_payment_id,
                            net_irr=tranche.net_irr,
                            outstanding_net_irr=outstanding,
                            expected_date=tranche.expected_date,
                            state=tranche.state.value,
                            sales_period_jalali=tranche.sales_period_jalali,
                            tranche_index=tranche.tranche_index,
                        )
                    )
            if tranches:
                event_candidates.append(
                    EventCandidate(
                        id=event.id,
                        net_irr=event.net_irr,
                        payout_date=event.payout_date,
                        tracking_ref=event.tracking_ref,
                        tranche_ids=tuple(t.id for t in tranches),
                    )
                )
                event_tranches[event.id] = tranches
        # Reverse-matching (C7) candidates: open tranches with no event
        # yet (expected/due/overdue). Confirmed tranches are covered by
        # their event; partially-received ones by their event too.
        open_tranches = self._repository.list_tranches(
            installation_id,
            channel_id=channel_id,
            states=[TrancheState.EXPECTED, TrancheState.DUE, TrancheState.OVERDUE],
        )
        tranche_candidates = [
            TrancheCandidate(
                id=t.id,
                gateway_payment_id=t.gateway_payment_id,
                net_irr=t.net_irr,
                outstanding_net_irr=t.net_irr - t.received_net_irr,
                expected_date=t.expected_date,
                state=t.state.value,
                sales_period_jalali=t.sales_period_jalali,
                tranche_index=t.tranche_index,
            )
            for t in open_tranches
            if t.net_irr - t.received_net_irr > 0
        ]
        return match_deposit(
            DepositToMatch(
                id=deposit.id,
                deposit_date=deposit.deposit_date,
                amount_irr=deposit.amount_irr,
                description=deposit.description or "",
                tracking_ref=deposit.tracking_ref,
            ),
            event_candidates,
            tranche_candidates,
            event_tranches,
            window_days=self._match_window_days(installation_id),
        )

    def _apply_deposit_decision(
        self,
        installation_id: str,
        tenant_id: str,
        channel_key: str,
        deposit: BankDeposit,
        decision: DepositMatchDecision,
        collector: _ExceptionCollector,
        *,
        actor: str,
    ) -> list[str]:
        """Apply a C5 decision: match links, tranche states, exceptions."""
        matched: list[str] = []
        if decision.auto_matched:
            for alloc in decision.allocations:
                tranche = self._repository.get_tranche(installation_id, alloc.tranche_id)
                if tranche is None:
                    continue
                self._repository.save_settlement_match(
                    CreateSettlementMatch(
                        installation_id=installation_id,
                        tranche_id=tranche.id,
                        allocated_amount_irr=alloc.allocated_amount_irr,
                        method=decision.method,
                        bank_deposit_id=deposit.id,
                        settlement_event_id=alloc.settlement_event_id
                        or decision.settlement_event_id,
                        confidence=decision.confidence,
                        matched_by=actor,
                        is_automatic=True,
                    )
                )
                applied = self._receive_tranche(
                    tranche, alloc.allocated_amount_irr, decision.method, decision.confidence
                )
                if applied > 0:
                    matched.append(tranche.id)
                    refreshed = self._repository.get_tranche(installation_id, tranche.id)
                    if (
                        refreshed is not None
                        and refreshed.state == TrancheState.PARTIALLY_RECEIVED
                    ):
                        self._file_exception(
                            tenant_id=tenant_id,
                            installation_id=installation_id,
                            channel_key=channel_key,
                            exception_type=SettlementExceptionType.PARTIAL_TRANCHE,
                            severity=SettlementExceptionSeverity.WARNING,
                            reason=(
                                f"قسط {refreshed.tranche_index} فقط {refreshed.received_net_irr:,} "
                                f"ریال از {refreshed.net_irr:,} ریال دریافت کرده است؛ "
                                f"باقی‌مانده {refreshed.outstanding_net_irr:,} ریال باز می‌ماند."
                            ),
                            suggested_action="دلیل پرداخت جزئی را در فایل یا صورت‌وضعیت درگاه بررسی کنید.",
                            collector=collector,
                            tranche_id=tranche.id,
                            bank_deposit_id=deposit.id,
                            gateway_payment_id=tranche.gateway_payment_id,
                            expected_amount_irr=tranche.net_irr,
                            actual_amount_irr=refreshed.received_net_irr,
                        )
            if decision.overpayment_irr > 0:
                # Overpayment parks as an unallocated receipt — NEVER
                # auto-allocated to future tranches.
                self._file_exception(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    channel_key=channel_key,
                    exception_type=SettlementExceptionType.OVERPAYMENT,
                    severity=SettlementExceptionSeverity.WARNING,
                    reason=(
                        f"واریز {deposit.amount_irr:,} ریالی {deposit.deposit_date.isoformat()} "
                        f"{decision.overpayment_irr:,} ریال بیشتر از قسط‌های منطبق است؛ "
                        "مازاد به‌عنوان «دریافت تخصیص‌نیافته» پارک شد و هرگز خودکار "
                        "به قسط‌های آینده تخصیص نمی‌یابد."
                    ),
                    suggested_action="مازاد را شناسایی و دستی تخصیص دهید.",
                    collector=collector,
                    bank_deposit_id=deposit.id,
                    settlement_event_id=decision.settlement_event_id,
                    expected_amount_irr=deposit.amount_irr - decision.overpayment_irr,
                    actual_amount_irr=deposit.amount_irr,
                )
            return matched
        if decision.method == "amount_only" and decision.suggestions:
            suggestions_text = "؛ ".join(
                f"{s.target_label} (اطمینان {s.confidence})" for s in decision.suggestions
            )
            item = self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.SETTLEMENT_VARIANCE,
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"واریز {deposit.amount_irr:,} ریالی {deposit.deposit_date.isoformat()} "
                    f"با چند قسط/رویداد هم‌مبلغ است؛ تطبیق خودکار انجام نشد. پیشنهادها: "
                    f"{suggestions_text}."
                ),
                suggested_action="از میان پیشنهادهای رتبه‌بندی‌شده یکی را انتخاب کنید یا دستی تطبیق دهید.",
                subtype="amount_only_match",
                channel=channel_key,
                source_event_ref=f"settlement-deposit:{deposit.id}",
                amount_irr=deposit.amount_irr,
                actor=actor,
            )
            collector.review_item_ids.append(item.id)
            return matched
        # Rule 4 — no candidate.
        self._file_exception(
            tenant_id=tenant_id,
            installation_id=installation_id,
            channel_key=channel_key,
            exception_type=SettlementExceptionType.DEPOSIT_WITHOUT_FILE,
            severity=SettlementExceptionSeverity.WARNING,
            reason=(
                f"واریز {deposit.amount_irr:,} ریالی {deposit.deposit_date.isoformat()} "
                "با هیچ رویداد تسویه یا قسطی تطبیق نخورد."
            ),
            suggested_action="واریز را دستی تطبیق دهید یا فایل تسویه را از درگاه درخواست کنید.",
            collector=collector,
            bank_deposit_id=deposit.id,
            expected_amount_irr=None,
            actual_amount_irr=deposit.amount_irr,
        )
        return matched

    def match_deposit_manually(
        self,
        installation_id: str,
        command: MatchDepositManually,
        *,
        company: CompanyRef | None = None,
    ) -> RecordDepositResult:
        """Apply a human-confirmed deposit↔tranche match (C5 rule-3 follow-up).

        Resolves the deposit's open ``deposit_without_file`` exception
        explicitly (resolver + note — never silently) and attempts the
        tie-out for the affected batch when a company is given.
        """
        deposit = self._repository.get_bank_deposit(installation_id, command.deposit_id)
        if deposit is None:
            raise SettlementDepositNotFoundError(
                f"Bank deposit {command.deposit_id!r} does not exist.",
                details={"deposit_id": command.deposit_id},
            )
        tranche = self._repository.get_tranche(installation_id, command.tranche_id)
        if tranche is None:
            raise SettlementTrancheNotFoundError(
                f"Settlement tranche {command.tranche_id!r} does not exist.",
                details={"tranche_id": command.tranche_id},
            )
        if command.allocated_amount_irr <= 0:
            raise SettlementMatchError(
                "The allocated amount must be positive.",
                details={"allocated_amount_irr": command.allocated_amount_irr},
            )
        if command.allocated_amount_irr > tranche.outstanding_net_irr:
            raise SettlementMatchError(
                f"Cannot allocate {command.allocated_amount_irr:,} IRR to a tranche "
                f"with {tranche.outstanding_net_irr:,} IRR outstanding.",
                details={
                    "allocated_amount_irr": command.allocated_amount_irr,
                    "outstanding_net_irr": tranche.outstanding_net_irr,
                    "tranche_id": tranche.id,
                },
            )
        existing = self._repository.list_matches_for_deposit(installation_id, deposit.id)
        allocated_so_far = sum(m.allocated_amount_irr for m in existing)
        if command.allocated_amount_irr > deposit.amount_irr - allocated_so_far:
            raise SettlementMatchError(
                f"Cannot allocate {command.allocated_amount_irr:,} IRR: the deposit "
                f"has only {deposit.amount_irr - allocated_so_far:,} IRR unallocated.",
                details={
                    "allocated_amount_irr": command.allocated_amount_irr,
                    "unallocated_irr": deposit.amount_irr - allocated_so_far,
                    "deposit_id": deposit.id,
                },
            )
        # Link the tranche's event when it has one (the tie-out sums
        # deposit links per event).
        event_id: str | None = None
        for match in self._repository.list_matches_for_tranche(installation_id, tranche.id):
            if match.settlement_event_id is not None:
                event_id = match.settlement_event_id
                break
        self._repository.save_settlement_match(
            CreateSettlementMatch(
                installation_id=installation_id,
                tranche_id=tranche.id,
                allocated_amount_irr=command.allocated_amount_irr,
                method="manual",
                bank_deposit_id=deposit.id,
                settlement_event_id=event_id,
                confidence=Decimal("1"),
                matched_by=command.actor,
                is_automatic=False,
            )
        )
        applied = self._receive_tranche(
            tranche, command.allocated_amount_irr, "manual", Decimal("1")
        )
        collector = _ExceptionCollector()
        open_exc = self._repository.get_open_exception_for_deposit(
            installation_id, deposit.id, SettlementExceptionType.DEPOSIT_WITHOUT_FILE.value
        )
        if open_exc is not None:
            self._repository.update_exception(
                open_exc.id,
                status=SettlementExceptionStatus.RESOLVED.value,
                resolution="adjusted",
                resolved_by=command.actor,
                resolved_at=self._clock(),
                note=f"Manually matched to tranche {tranche.id} ({command.allocated_amount_irr:,} IRR).",
            )
            review_item_id = self._find_review_item_by_ref(
                command.tenant_id, f"settlement-exception:{open_exc.id}"
            )
            if review_item_id is not None:
                self._resolve_review_item(
                    command.tenant_id, review_item_id, "accept_variance", command.actor
                )
        tieout: TieOutResult | None = None
        posted_journal_id: int | None = None
        if event_id is not None and company is not None:
            event = self._repository.get_settlement_event(installation_id, event_id)
            if event is not None and event.import_batch_id is not None:
                run = self._attempt_tieout_for_batch(
                    installation_id,
                    command.tenant_id,
                    event.import_batch_id,
                    company,
                    command.actor,
                )
                if run is not None:
                    tieout = run.tieout
                    posted_journal_id = run.tryton_journal_id
                    collector.exceptions.extend(run.exceptions)
        return RecordDepositResult(
            deposit=deposit,
            decision_method="manual",
            decision_confidence=Decimal("1"),
            matched_tranche_ids=(tranche.id,) if applied > 0 else (),
            exceptions=tuple(collector.exceptions),
            tieout=tieout,
            posted_journal_id=posted_journal_id,
        )

    def _find_review_item_by_ref(self, tenant_id: str, source_event_ref: str) -> str | None:
        """Find an open review item by its source ref, if any."""
        items = self._review_queries.list_items(
            tenant_id, ReviewItemFilters(status=None)
        )
        for view in items:
            item = view.item
            if item.source_event_ref == source_event_ref and item.status not in (
                ReviewItemStatus.RESOLVED,
                ReviewItemStatus.WRITTEN_OFF,
            ):
                return item.id
        return None

    # -- C6: mandatory tie-out -------------------------------------------------

    def run_tieout(
        self,
        installation_id: str,
        command: RunSettlementTieOut,
        *,
        company: CompanyRef | None = None,
    ) -> TieOutRunResult:
        """Run the mandatory tie-out (C6) for one import batch.

        Idempotent: re-running a settled batch recomputes the tie-out
        without re-posting. A held tie-out with a denied gate (or no
        company) leaves the batch ``approved`` (draft-hold); a failed
        tie-out moves it to ``exception`` and posts nothing.
        """
        file_import = self._require_import(installation_id, command.import_id)
        if file_import.status in (IMPORT_AWAITING_REVIEW, IMPORT_REJECTED):
            raise SettlementImportStateError(
                f"Import {command.import_id} is {file_import.status}; tie-out needs an approved batch.",
                details={"import_id": command.import_id, "status": file_import.status},
            )
        return self._run_tieout_core(
            installation_id,
            command.tenant_id,
            file_import,
            company,
            command.actor,
        )

    def _attempt_tieout_for_batch(
        self,
        installation_id: str,
        tenant_id: str,
        import_batch_id: str,
        company: CompanyRef,
        actor: str,
    ) -> TieOutRunResult | None:
        """Attempt the tie-out for a batch after a deposit matched it.

        Returns None when the batch is not in a tie-out-able state
        (still awaiting review, rejected, or already settled — the
        settled case is a no-op, never a re-post).
        """
        file_import = self._repository.get_file_import_by_batch(
            installation_id, import_batch_id
        )
        if file_import is None:
            return None
        if file_import.status in (IMPORT_AWAITING_REVIEW, IMPORT_REJECTED, IMPORT_SETTLED):
            return None
        return self._run_tieout_core(
            installation_id, tenant_id, file_import, company, actor
        )

    def run_tieout_for_batch(
        self,
        installation_id: str,
        tenant_id: str,
        import_batch_id: str,
        company: CompanyRef | None,
        actor: str,
    ) -> TieOutRunResult | None:
        """Run the tie-out for one import batch by its batch id (US-554).

        The public entry the manual-import batch approve step and the
        settlement-received event consumer share: one tie-out path
        for file imports and for events. Returns None when the batch
        is not in a tie-out-able state (still awaiting review,
        rejected, or already settled — the settled case is a no-op,
        never a re-post). A ``company`` of None runs the tie-out in
        draft-hold mode (the gate fails closed without a company).
        """
        file_import = self._repository.get_file_import_by_batch(
            installation_id, import_batch_id
        )
        if file_import is None:
            return None
        if file_import.status in (IMPORT_AWAITING_REVIEW, IMPORT_REJECTED, IMPORT_SETTLED):
            return None
        return self._run_tieout_core(
            installation_id, tenant_id, file_import, company, actor
        )

    def compute_tieout_figures(
        self, installation_id: str, import_batch_id: str
    ) -> TieOutResult | None:
        """Compute a batch's tie-out figures WITHOUT side effects (US-554).

        Read-only: no exceptions filed, no status changed, nothing
        posted — the batch variance report reads the same figures the
        tie-out itself computes. Returns None when the batch has no
        import or no settlement event yet.
        """
        file_import = self._repository.get_file_import_by_batch(
            installation_id, import_batch_id
        )
        if file_import is None:
            return None
        sums = self._tieout_sums(installation_id, file_import)
        if sums is None:
            return None
        _, tieout_tolerance = self._tolerances(file_import.payment_channel_id)
        return compute_tieout(
            confirmed_total_irr=sums.confirmed_total_irr,
            deductions_irr=sums.deductions_irr,
            matched_deposits_irr=sums.matched_deposits_irr,
            tolerance_irr=tieout_tolerance,
        )

    def _tieout_sums(
        self, installation_id: str, file_import: SettlementFileImport
    ) -> _TieOutSums | None:
        """The pure tie-out inputs for one import batch (no side effects).

        Sums: confirmed tranche nets (from the tranches) vs ALLOCATED
        deposit amounts (from the deposit match links). An overpaid
        deposit's parked remainder is NOT in the deposit sum — it is
        accounted for separately as an unallocated receipt. Returns
        None when the batch has no settlement event yet.
        """
        events = self._repository.list_settlement_events(
            installation_id, import_batch_id=file_import.import_batch_id
        )
        if not events:
            return None
        confirmed_total = 0
        deductions = 0
        matched_deposits = 0
        confirmed_tranches: list[SettlementTranche] = []
        latest_payout = events[0].payout_date
        deposit_ids: set[str] = set()
        per_event: list[tuple[SettlementEvent, int, int]] = []
        for event in events:
            deductions += event.deductions_irr
            if event.payout_date > latest_payout:
                latest_payout = event.payout_date
            matches = self._repository.list_matches_for_event(installation_id, event.id)
            event_confirmed = 0
            event_deposits = 0
            seen_tranches: set[str] = set()
            for match in matches:
                if match.bank_deposit_id is not None:
                    event_deposits += match.allocated_amount_irr
                    deposit_ids.add(match.bank_deposit_id)
                if match.tranche_id in seen_tranches:
                    continue
                seen_tranches.add(match.tranche_id)
                tranche = self._repository.get_tranche(installation_id, match.tranche_id)
                if tranche is None:
                    continue
                if tranche.state in _CONFIRMED_TRANCHE_STATES:
                    event_confirmed += tranche.confirmed_net_irr
                    confirmed_tranches.append(tranche)
            confirmed_total += event_confirmed
            matched_deposits += event_deposits
            per_event.append((event, event_confirmed, event_deposits))
        return _TieOutSums(
            events=tuple(events),
            confirmed_total_irr=confirmed_total,
            deductions_irr=deductions,
            matched_deposits_irr=matched_deposits,
            confirmed_tranches=tuple(confirmed_tranches),
            latest_payout=latest_payout,
            deposit_ids=frozenset(deposit_ids),
            per_event=tuple(per_event),
        )

    def _run_tieout_core(
        self,
        installation_id: str,
        tenant_id: str,
        file_import: SettlementFileImport,
        company: CompanyRef | None,
        actor: str,
    ) -> TieOutRunResult:
        """The tie-out itself: compute, gate, post-or-hold, settle-or-except."""
        collector = _ExceptionCollector()
        channel = self._registry.get_channel_by_id(file_import.payment_channel_id)
        channel_key = channel.channel_key if channel is not None else file_import.payment_channel_id
        _, tieout_tolerance = self._tolerances(file_import.payment_channel_id)
        sums = self._tieout_sums(installation_id, file_import)
        if sums is None:
            raise SettlementImportStateError(
                f"Import {file_import.id} has no settlement event to tie out.",
                details={"import_id": file_import.id},
            )
        events = list(sums.events)
        confirmed_total = sums.confirmed_total_irr
        deductions = sums.deductions_irr
        matched_deposits = sums.matched_deposits_irr
        confirmed_tranches = list(sums.confirmed_tranches)
        latest_payout = sums.latest_payout
        deposit_ids = set(sums.deposit_ids)
        per_event = list(sums.per_event)
        for event, _event_confirmed, event_deposits in per_event:
            # Missing-deposit detection (per event + per tranche).
            if event_deposits == 0:
                self._file_exception(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    channel_key=channel_key,
                    exception_type=SettlementExceptionType.FILE_WITHOUT_DEPOSIT,
                    severity=SettlementExceptionSeverity.WARNING,
                    reason=(
                        f"رویداد تسویهٔ {event.payout_date.isoformat()} ({event.net_irr:,} ریال) "
                        "هنوز واریز بانکی ندارد."
                    ),
                    suggested_action="گردش حساب را بررسی کنید و واریز را ثبت یا از درگاه پیگیری کنید.",
                    collector=collector,
                    settlement_event_id=event.id,
                    expected_amount_irr=event.net_irr,
                    actual_amount_irr=0,
                )
        for tranche in confirmed_tranches:
            if tranche.received_net_irr < tranche.confirmed_net_irr:
                self._file_exception(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    channel_key=channel_key,
                    exception_type=SettlementExceptionType.MISSING_DEPOSIT,
                    severity=SettlementExceptionSeverity.WARNING,
                    reason=(
                        f"قسط {tranche.tranche_index} تأیید شده ({tranche.confirmed_net_irr:,} ریال) "
                        f"ولی فقط {tranche.received_net_irr:,} ریال واریز بانکی دارد."
                    ),
                    suggested_action="گردش حساب را بررسی کنید و واریز را ثبت یا از درگاه پیگیری کنید.",
                    collector=collector,
                    tranche_id=tranche.id,
                    gateway_payment_id=tranche.gateway_payment_id,
                    expected_amount_irr=tranche.confirmed_net_irr,
                    actual_amount_irr=tranche.received_net_irr,
                )
        tieout = compute_tieout(
            confirmed_total_irr=confirmed_total,
            deductions_irr=deductions,
            matched_deposits_irr=matched_deposits,
            tolerance_irr=tieout_tolerance,
        )
        if not tieout.holds:
            # Undocumented deductions are NOT deductions: the difference
            # is an amount_mismatch, and the whole batch stays in
            # exception — NO automatic posting happens.
            for event, event_confirmed, event_deposits in per_event:
                event_expected = event_confirmed - event.deductions_irr
                if abs(event_expected - event_deposits) > tieout_tolerance:
                    self._file_exception(
                        tenant_id=tenant_id,
                        installation_id=installation_id,
                        channel_key=channel_key,
                        exception_type=SettlementExceptionType.AMOUNT_MISMATCH,
                        severity=SettlementExceptionSeverity.CRITICAL,
                        reason=(
                            f"tie-out بچ {file_import.import_batch_id[:8]} برقرار نیست: "
                            f"خالص تأییدشدهٔ {event_confirmed:,} منهای کسورات مستند "
                            f"{event.deductions_irr:,} برابر {event_expected:,} ریال است ولی "
                            f"واریزهای تطبیق‌شده {event_deposits:,} ریال‌اند "
                            f"(اختلاف {event_expected - event_deposits:,} ریال)."
                        ),
                        suggested_action="کسورات بدون مستند را بررسی کنید؛ مغایرت را تعدیل یا از درگاه پیگیری کنید.",
                        collector=collector,
                        settlement_event_id=event.id,
                        expected_amount_irr=event_expected,
                        actual_amount_irr=event_deposits,
                    )
            updated = self._repository.update_file_import(file_import.id, status=IMPORT_EXCEPTION)
            assert updated is not None
            self._audit.record_settlement_audit(
                tenant_id=tenant_id,
                installation_id=installation_id,
                api_client_id=None,
                action="settlement.tieout.failed",
                resource_type="settlement_file_import",
                resource_id=file_import.id,
                actor=actor,
                metadata={
                    "import_batch_id": file_import.import_batch_id,
                    "confirmed_total_irr": confirmed_total,
                    "deductions_irr": deductions,
                    "matched_deposits_irr": matched_deposits,
                    "difference_irr": tieout.difference_irr,
                    "tolerance_irr": tieout_tolerance,
                },
            )
            return TieOutRunResult(
                file_import=updated,
                tieout=tieout,
                status=IMPORT_EXCEPTION,
                gate_allowed=False,
                gate_reason="Tie-out failed: no auto-post before tie-out.",
                posted=False,
                exceptions=tuple(collector.exceptions),
                review_item_ids=tuple(collector.review_item_ids),
            )
        if file_import.status == IMPORT_SETTLED:
            # Idempotent re-run (US-525): the tie-out was recomputed above
            # and holds, but the batch already posted — never re-post a
            # settled batch. The inline path guards this too
            # (_attempt_tieout_for_batch); the explicit endpoint needs
            # the same guard or a second run would double-post.
            return TieOutRunResult(
                file_import=file_import,
                tieout=tieout,
                status=IMPORT_SETTLED,
                gate_allowed=True,
                gate_reason="Batch already settled: tie-out recomputed, nothing re-posted.",
                posted=False,
                settled_tranche_ids=tuple(
                    t.id
                    for t in confirmed_tranches
                    if t.state == TrancheState.SETTLED
                ),
                exceptions=tuple(collector.exceptions),
                review_item_ids=tuple(collector.review_item_ids),
            )
        # Tie-out holds: the policy gate decides auto-post vs draft-hold.
        if company is None:
            self._audit.record_settlement_audit(
                tenant_id=tenant_id,
                installation_id=installation_id,
                api_client_id=None,
                action="settlement.tieout.held_draft",
                resource_type="settlement_file_import",
                resource_id=file_import.id,
                actor=actor,
                metadata={
                    "import_batch_id": file_import.import_batch_id,
                    "reason": (
                        "No company supplied: the fiscal-period layer cannot be "
                        "evaluated, so the gate fails closed."
                    ),
                    "matched_deposits_irr": matched_deposits,
                },
            )
            return TieOutRunResult(
                file_import=file_import,
                tieout=tieout,
                status=IMPORT_APPROVED,
                gate_allowed=False,
                gate_reason="No company supplied: the fiscal-period layer cannot be evaluated (fail-closed).",
                posted=False,
                exceptions=tuple(collector.exceptions),
                review_item_ids=tuple(collector.review_item_ids),
            )
        decision = self._policy_gate.evaluate(
            installation_id,
            event_type=SETTLEMENT_RECEIVED_EVENT_TYPE,
            company=company,
            event_date=latest_payout,
            amount_irr=matched_deposits,
            channel_key=channel_key,
            tieout_ok=True,
        )
        if not decision.allowed:
            self._audit.record_settlement_audit(
                tenant_id=tenant_id,
                installation_id=installation_id,
                api_client_id=None,
                action="settlement.tieout.held_draft",
                resource_type="settlement_file_import",
                resource_id=file_import.id,
                actor=actor,
                metadata={
                    "import_batch_id": file_import.import_batch_id,
                    "reason": decision.reason,
                    "layers": [
                        {"layer": layer.layer, "passed": layer.passed, "detail": layer.detail}
                        for layer in decision.layers
                    ],
                    "matched_deposits_irr": matched_deposits,
                },
            )
            return TieOutRunResult(
                file_import=file_import,
                tieout=tieout,
                status=IMPORT_APPROVED,
                gate_allowed=False,
                gate_reason=decision.reason,
                posted=False,
                exceptions=tuple(collector.exceptions),
                review_item_ids=tuple(collector.review_item_ids),
            )
        # Gate allows: post the bank move (clearing → bank) via the
        # posting port (US-522's engine implementation).
        try:
            posting = self._posting.post_bank_receipt(
                PostBatchBankReceipt(
                    installation_id=installation_id,
                    tenant_id=tenant_id,
                    channel_key=channel_key,
                    settlement_event_id=events[0].id,
                    bank_deposit_ids=tuple(sorted(deposit_ids)),
                    tranche_ids=tuple(t.id for t in confirmed_tranches),
                    amount_irr=matched_deposits,
                    payout_date=latest_payout,
                    deposit_date=self._clock().date(),
                    import_batch_id=file_import.import_batch_id,
                    company_engine_id=company.engine_id,
                )
            )
        except (SettlementPostingNotConfiguredError, SettlementPostingError) as exc:
            updated = self._repository.update_file_import(file_import.id, status=IMPORT_EXCEPTION)
            assert updated is not None
            item = self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.SETTLEMENT_VARIANCE,
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"tie-out بچ {file_import.import_batch_id[:8]} برقرار است ولی ثبت سند بانکی "
                    f"ممکن نشد: {exc.message} قسط‌ها بسته نشدند."
                ),
                suggested_action="پیکربندی ثبت تسویه را بررسی کنید و tie-out را دوباره اجرا کنید.",
                subtype="posting_failed",
                channel=channel_key,
                source_event_ref=f"settlement-import:{file_import.id}",
                amount_irr=matched_deposits,
                actor=actor,
            )
            collector.review_item_ids.append(item.id)
            self._audit.record_settlement_audit(
                tenant_id=tenant_id,
                installation_id=installation_id,
                api_client_id=None,
                action="settlement.tieout.posting_failed",
                resource_type="settlement_file_import",
                resource_id=file_import.id,
                actor=actor,
                metadata={
                    "import_batch_id": file_import.import_batch_id,
                    "error": exc.message,
                    "code": exc.code,
                },
            )
            return TieOutRunResult(
                file_import=updated,
                tieout=tieout,
                status=IMPORT_EXCEPTION,
                gate_allowed=True,
                gate_reason=decision.reason,
                posted=False,
                exceptions=tuple(collector.exceptions),
                review_item_ids=tuple(collector.review_item_ids),
            )
        # Close the fully-received tranches (settled); a partially
        # received tranche stays open with its exception.
        settled: list[str] = []
        for tranche in confirmed_tranches:
            refreshed = self._repository.get_tranche(installation_id, tranche.id)
            if refreshed is None:
                continue
            if (
                refreshed.received_net_irr >= refreshed.net_irr
                and refreshed.state == TrancheState.RECEIVED
            ):
                require_transition(refreshed.state, TrancheState.SETTLED)
                self._repository.update_tranche(
                    refreshed.id, state=TrancheState.SETTLED
                )
                settled.append(refreshed.id)
        # Resolve the missing-deposit exceptions the tie-out just closed:
        # they were opened by EARLIER runs (this run's collector holds
        # only this run's new exceptions, which stay open).
        all_settled = True
        for t in confirmed_tranches:
            current = self._repository.get_tranche(installation_id, t.id)
            if current is None or current.state != TrancheState.SETTLED:
                all_settled = False
                break
        new_status = IMPORT_SETTLED if all_settled else IMPORT_APPROVED
        updated = self._repository.update_file_import(file_import.id, status=new_status)
        assert updated is not None
        self._audit.record_settlement_audit(
            tenant_id=tenant_id,
            installation_id=installation_id,
            api_client_id=None,
            action="settlement.tieout.auto_post",
            resource_type="settlement_file_import",
            resource_id=file_import.id,
            actor=actor,
            metadata={
                "import_batch_id": file_import.import_batch_id,
                "tryton_journal_id": posting.tryton_journal_id,
                "amount_irr": matched_deposits,
                "settled_tranche_ids": settled,
                "gate_reason": decision.reason,
            },
        )
        return TieOutRunResult(
            file_import=updated,
            tieout=tieout,
            status=new_status,
            gate_allowed=True,
            gate_reason=decision.reason,
            posted=True,
            tryton_journal_id=posting.tryton_journal_id,
            settled_tranche_ids=tuple(settled),
            exceptions=tuple(collector.exceptions),
            review_item_ids=tuple(collector.review_item_ids),
        )
