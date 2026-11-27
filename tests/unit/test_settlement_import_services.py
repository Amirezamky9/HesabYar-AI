"""Service tests for settlement import + matching + tie-out (US-523).

DB-free: the import service runs against the in-memory fake
settlement repository (from ``tests.unit.test_settlement_services``),
the shared fake integrations registry, a recording audit fake, and
small fakes for the review queue, file reader, outbox, posting port
and policy gate. The SQLAlchemy repository has its own DB tests
(``tests/infrastructure/test_settlement_import_repository.py``).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_import import SettlementImportService
from hesabyar.application.queries.review_queue import ReviewItemView
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.bank.models import StatementFileFormat
from hesabyar.domain.bank.ports import DetectedFile
from hesabyar.domain.events.models import OutboxEvent
from hesabyar.domain.integrations.models import TrancheLeg
from hesabyar.domain.integrations.policy_gate import PolicyGateDecision
from hesabyar.domain.review_queue.models import (
    ReviewItem,
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters
from hesabyar.domain.review_queue.suggestions import derive_suggestion
from hesabyar.domain.settlement.commands import (
    ApproveSettlementImport,
    CreateGatewayPayment,
    ImportSettlementFile,
    MatchDepositManually,
    PostBatchBankReceipt,
    RecordBankDeposit,
    RejectSettlementImport,
    RunSettlementTieOut,
)
from hesabyar.domain.settlement.errors import (
    SettlementImportInvalidError,
    SettlementImportStateError,
    SettlementPostingError,
    SettlementPostingNotConfiguredError,
)
from hesabyar.domain.settlement.models import (
    GatewayPayment,
    PaymentSource,
    SettlementExceptionType,
    SettlementTranche,
    TrancheState,
)
from hesabyar.domain.settlement.ports import BankReceiptPosting
from tests.unit.test_integrations_services import FakeIntegrationsRepository
from tests.unit.test_settlement_services import (
    FakeSettlementAudit,
    FakeSettlementRepository,
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000521"
TENANT_ID = "00000000-0000-0000-0000-000000000522"
NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
COMPANY = CompanyRef(engine_id=1)

FOUR_TRANCHES = (
    TrancheLeg(1, 5, Decimal("25")),
    TrancheLeg(2, 5, Decimal("25")),
    TrancheLeg(3, 5, Decimal("25")),
    TrancheLeg(4, 5, Decimal("25")),
)

#: One tranche of a 100,000,000 gross payment (fee 10%, VAT 10%).
TRANCHE_NET = 22_250_000
TRANCHE_FEE = 2_500_000
TRANCHE_VAT = 250_000
TRANCHE_GROSS = 25_000_000

HEADERS: list[str | None] = [
    "شماره تراکنش",
    "تاریخ تراکنش",
    "مبلغ ناخالص",
    "کارمزد",
    "مالیات",
    "مبلغ خالص",
    "نوع تراکنش",
    "شماره پیگیری",
]
COLUMNS = {
    "transaction_id": "شماره تراکنش",
    "date": "تاریخ تراکنش",
    "gross": "مبلغ ناخالص",
    "fee": "کارمزد",
    "vat": "مالیات",
    "net": "مبلغ خالص",
    "tran_type": "نوع تراکنش",
    "tracking_ref": "شماره پیگیری",
}


def _new_id() -> str:
    return str(uuid.uuid4())


def _detail_row(
    txn: str = "TXN-1",
    *,
    gross: int = TRANCHE_GROSS,
    fee: int = TRANCHE_FEE,
    vat: int = TRANCHE_VAT,
    net: int = TRANCHE_NET,
    date: str = "1405/08/15",
    tran_type: str = "فروش",
    tracking_ref: str = "",
) -> list[str | None]:
    return [txn, date, str(gross), str(fee), str(vat), str(net), tran_type, tracking_ref]


@dataclass
class FakeReviewQueue:
    """In-memory review queue: command + query roles over one store."""

    items: dict[str, ReviewItem] = field(default_factory=dict)
    actions: list[tuple[str, str, str]] = field(default_factory=list)

    def file_item(
        self,
        *,
        tenant_id: str,
        item_type: ReviewItemType,
        severity: ReviewSeverity,
        reason: str,
        suggested_action: str,
        company_id: str | None = None,
        subtype: str | None = None,
        channel: str | None = None,
        source_event_ref: str | None = None,
        amount_irr: int | None = None,
        assignee: str | None = None,
        actor: str,
    ) -> ReviewItem:
        item = ReviewItem(
            id=_new_id(),
            tenant_id=tenant_id,
            item_type=item_type,
            severity=severity,
            status=ReviewItemStatus.OPEN,
            reason=reason,
            suggested_action=suggested_action,
            created_at=NOW,
            company_id=company_id,
            subtype=subtype,
            channel=channel,
            source_event_ref=source_event_ref,
            amount_irr=amount_irr,
            assignee=assignee,
        )
        self.items[item.id] = item
        return item

    def perform_action(
        self,
        *,
        item_id: str,
        tenant_id: str,
        action_code: str,
        actor: str,
        note: str | None = None,
    ) -> ReviewItem:
        item = self.items[item_id]
        values = dict(item.__dict__)
        values["status"] = ReviewItemStatus.RESOLVED
        values["resolved_by"] = actor
        values["resolved_at"] = NOW
        updated = ReviewItem(**values)
        self.items[item_id] = updated
        self.actions.append((item_id, action_code, actor))
        return updated

    def list_items(
        self, tenant_id: str, filters: ReviewItemFilters, *, now: Any = None
    ) -> Sequence[ReviewItemView]:
        found = [i for i in self.items.values() if i.tenant_id == tenant_id]
        if filters.item_type is not None:
            found = [i for i in found if i.item_type == filters.item_type]
        if filters.status is not None:
            found = [i for i in found if i.status == filters.status]
        return [
            ReviewItemView(
                item=item,
                age_hours=0.0,
                sla_deadline=NOW + datetime.timedelta(hours=24),
                sla_breached=False,
                sla_warn=False,
                suggested_action_code=derive_suggestion(item).action_code,
                suggested_reason_key=derive_suggestion(item).reason_key,
            )
            for item in found
        ]


@dataclass
class FakeFileReader:
    """File reader returning a configured grid (ignores the bytes)."""

    grid: list[list[str | None]] = field(default_factory=list)
    raise_error: bool = False

    def read_file(self, content: bytes, filename: str) -> DetectedFile:
        if self.raise_error:
            from hesabyar.domain.bank.errors import BankImportFileError

            raise BankImportFileError("unreadable file")
        return DetectedFile(
            file_format=StatementFileFormat.XLSX, grid=[row[:] for row in self.grid]
        )


@dataclass
class FakeOutbox:
    """Recording outbox (save returns the event, stored by id)."""

    events: dict[str, OutboxEvent] = field(default_factory=dict)

    def save(self, event: OutboxEvent) -> OutboxEvent:
        self.events[event.id] = event
        return event


@dataclass
class FakePosting:
    """Configurable posting port: records calls, or fails closed."""

    calls: list[PostBatchBankReceipt] = field(default_factory=list)
    journal_id: int = 777
    fail_not_configured: bool = False
    fail_error: bool = False

    def post_bank_receipt(self, command: PostBatchBankReceipt) -> BankReceiptPosting:
        self.calls.append(command)
        if self.fail_not_configured:
            raise SettlementPostingNotConfiguredError("posting not configured")
        if self.fail_error:
            raise SettlementPostingError("engine rejected the move")
        return BankReceiptPosting(
            tryton_journal_id=self.journal_id, amount_irr=command.amount_irr
        )


@dataclass
class FakePolicyGate:
    """Configurable policy gate: allowed or denied, with a reason."""

    allowed: bool = True
    reason: str = "all layers passed"

    def evaluate(
        self,
        installation_id: str,
        *,
        event_type: str,
        company: CompanyRef,
        event_date: datetime.date,
        amount_irr: int,
        channel_key: str | None = None,
        tieout_ok: bool | None = None,
    ) -> PolicyGateDecision:
        return PolicyGateDecision(
            allowed=self.allowed,
            action="auto_post" if self.allowed else "draft",
            layers=(),
            policy_config_id=None,
            contract_id=None,
            contract_version=None,
            reason=self.reason,
            evaluated_at=NOW,
        )


def _make_registry(installation_id: str | None = None) -> FakeIntegrationsRepository:
    """Seed snapppay + its contract under ``installation_id``.

    Defaults to this module's INSTALLATION_ID; callers with their own
    installation (e.g. the settlement-received consumer tests) must
    pass theirs — the channel registry is installation-scoped, so a
    channel seeded under another installation is invisible.
    """
    installation_id = installation_id if installation_id is not None else INSTALLATION_ID
    repo = FakeIntegrationsRepository()
    channels = ChannelRegistryService(repo)
    contracts = GatewayContractService(repo)
    channels.create_channel(installation_id, "snapppay", "اسنپ‌پی", "bnpl")
    contracts.create_version(
        installation_id,
        "snapppay",
        valid_from=datetime.date(2026, 10, 23),
        fee_percent=Decimal("10"),
        fee_vat_rate=Decimal("10"),
        tranche_legs=FOUR_TRANCHES,
        payout_window_start_day=1,
        payout_window_end_day=5,
        grace_days_after_due=3,
    )
    return repo


@pytest.fixture()
def registry() -> FakeIntegrationsRepository:
    return _make_registry()


@pytest.fixture()
def repo() -> FakeSettlementRepository:
    return FakeSettlementRepository()


@pytest.fixture()
def audit() -> FakeSettlementAudit:
    return FakeSettlementAudit()


@pytest.fixture()
def review() -> FakeReviewQueue:
    return FakeReviewQueue()


@pytest.fixture()
def reader() -> FakeFileReader:
    return FakeFileReader()


@pytest.fixture()
def outbox() -> FakeOutbox:
    return FakeOutbox()


@pytest.fixture()
def posting() -> FakePosting:
    return FakePosting()


@pytest.fixture()
def gate() -> FakePolicyGate:
    return FakePolicyGate()


@pytest.fixture()
def commands(
    repo: FakeSettlementRepository,
    registry: FakeIntegrationsRepository,
    audit: FakeSettlementAudit,
) -> SettlementCommandService:
    return SettlementCommandService(repo, registry, audit)


@pytest.fixture()
def service(
    repo: FakeSettlementRepository,
    registry: FakeIntegrationsRepository,
    audit: FakeSettlementAudit,
    commands: SettlementCommandService,
    review: FakeReviewQueue,
    reader: FakeFileReader,
    outbox: FakeOutbox,
    posting: FakePosting,
    gate: FakePolicyGate,
) -> SettlementImportService:
    return SettlementImportService(
        repo,
        registry,
        audit,
        commands,
        review,  # type: ignore[arg-type]
        review,  # type: ignore[arg-type]
        reader,  # type: ignore[arg-type]
        outbox,  # type: ignore[arg-type]
        posting,  # type: ignore[arg-type]
        gate,  # type: ignore[arg-type]
        now=NOW,
    )


def _record(
    commands: SettlementCommandService, *, txn: str = "TXN-1", gross: int = 100_000_000
) -> GatewayPayment:
    result = commands.record_payment(
        CreateGatewayPayment(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            gateway_transaction_id=txn,
            gross_irr=gross,
            gateway_confirmed_at=NOW,
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id="ORD-1",
        )
    )
    return result.payment


def _tranches_of(repo: FakeSettlementRepository, payment_id: str) -> list[SettlementTranche]:
    return sorted(
        repo.list_tranches_for_payment(INSTALLATION_ID, payment_id),
        key=lambda t: t.tranche_index,
    )


def _import(
    service: SettlementImportService,
    reader: FakeFileReader,
    rows: list[list[str | None]],
    *,
    file_type: str = "detail",
    content: bytes = b"fake-xlsx-bytes",
    currency_unit: str = "rial",
    period_jalali: str = "1405/08",
) -> Any:
    reader.grid = [HEADERS, *rows]
    return service.import_file(
        ImportSettlementFile(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            file_type=file_type,
            period_jalali=period_jalali,
            filename="snapppay-1405-08.xlsx",
            content=content,
            currency_unit=currency_unit,
            columns=COLUMNS,
            date_kind="jalali",
            actor="tester",
        ),
        tenant_id=TENANT_ID,
    )


def _approve(service: SettlementImportService, import_id: str) -> Any:
    return service.approve_import(
        INSTALLATION_ID,
        ApproveSettlementImport(import_id=import_id, actor="tester"),
        tenant_id=TENANT_ID,
    )


def _deposit(
    service: SettlementImportService,
    *,
    amount_irr: int,
    deposit_date: datetime.date,
    description: str | None = None,
    tracking_ref: str | None = None,
    channel_key: str | None = "snapppay",
    company: CompanyRef | None = None,
) -> Any:
    return service.record_deposit(
        RecordBankDeposit(
            installation_id=INSTALLATION_ID,
            tenant_id=TENANT_ID,
            deposit_date=deposit_date,
            amount_irr=amount_irr,
            description=description,
            tracking_ref=tracking_ref,
            source="manual",
            channel_key=channel_key,
            actor="tester",
        ),
        company=company,
    )


class TestImport:
    """C1: import → parse → dry-run preview → review item."""

    def test_import_detail_file_awaiting_review_with_preview(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        review: FakeReviewQueue,
        repo: FakeSettlementRepository,
    ) -> None:
        """A detail row matching a known payment previews as matched."""
        _record(commands)
        result = _import(service, reader, [_detail_row()])
        assert result.duplicate is False
        assert result.file_import.status == "awaiting_review"
        assert result.preview.matched_rows == 1
        assert result.preview.new_payment_rows == 0
        assert result.preview.net_irr == TRANCHE_NET
        assert result.preview.expected_deposit_irr == TRANCHE_NET
        assert result.review_item_id is not None
        item = review.items[result.review_item_id]
        assert item.item_type == ReviewItemType.MANUAL_IMPORT_REVIEW
        rows = repo.list_file_rows(result.file_import.id)
        assert len(rows) == 1
        assert rows[0].match_status == "matched"
        assert rows[0].gateway_transaction_id == "TXN-1"

    def test_reimport_same_file_is_noop(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        review: FakeReviewQueue,
    ) -> None:
        """File-hash dedupe: re-importing the same file returns the existing import."""
        _record(commands)
        first = _import(service, reader, [_detail_row()])
        items_before = len(review.items)
        second = _import(service, reader, [_detail_row()])
        assert second.duplicate is True
        assert second.file_import.id == first.file_import.id
        assert second.review_item_id is None
        assert len(review.items) == items_before

    def test_import_unknown_transaction_is_new_payment(
        self, service: SettlementImportService, reader: FakeFileReader
    ) -> None:
        """A row for an unknown transaction previews as a new payment."""
        result = _import(service, reader, [_detail_row("TXN-9")])
        assert result.preview.new_payment_rows == 1
        assert result.preview.matched_rows == 0

    def test_import_unknown_file_type_rejected(
        self, service: SettlementImportService, reader: FakeFileReader
    ) -> None:
        """An unknown file_type fails closed at the boundary."""
        with pytest.raises(SettlementImportInvalidError, match="Unknown file_type"):
            _import(service, reader, [_detail_row()], file_type="weird")

    def test_import_empty_file_rejected(
        self, service: SettlementImportService, reader: FakeFileReader
    ) -> None:
        """An empty upload fails closed."""
        reader.grid = [HEADERS, _detail_row()]
        with pytest.raises(SettlementImportInvalidError, match="empty"):
            service.import_file(
                ImportSettlementFile(
                    installation_id=INSTALLATION_ID,
                    channel_key="snapppay",
                    file_type="detail",
                    period_jalali="1405/08",
                    filename="empty.xlsx",
                    content=b"",
                    currency_unit="rial",
                    columns=COLUMNS,
                ),
                tenant_id=TENANT_ID,
            )

    def test_import_drops_net_sale_rows(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """Net Sale rows are dropped at parse and logged, never stored."""
        _record(commands)
        net_sale: list[str | None] = ["", "1405/08/15", "25000000", "2500000", "250000", "22250000", "Net Sale", ""]
        result = _import(service, reader, [_detail_row(), net_sale])
        assert len(result.dropped_rows) == 1
        assert result.dropped_rows[0][0] == 3
        rows = repo.list_file_rows(result.file_import.id)
        assert len(rows) == 1
        assert result.preview.net_irr == TRANCHE_NET

    def test_import_toman_file_converts(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
    ) -> None:
        """A Toman file converts ×10 at the import boundary."""
        _record(commands)
        row = _detail_row(
            gross=TRANCHE_GROSS // 10,
            fee=TRANCHE_FEE // 10,
            vat=TRANCHE_VAT // 10,
            net=TRANCHE_NET // 10,
        )
        result = _import(service, reader, [row], currency_unit="toman")
        assert result.preview.net_irr == TRANCHE_NET
        assert result.preview.matched_rows == 1


class TestApprove:
    """C1/C2: approve → authoritative re-match + event + canonical event."""

    def test_approve_confirms_tranche_and_publishes_event(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        outbox: FakeOutbox,
        review: FakeReviewQueue,
    ) -> None:
        """Approve confirms the tranche, creates the event and publishes the canonical event."""
        payment = _record(commands)
        result = _import(service, reader, [_detail_row()])
        approved = _approve(service, result.file_import.id)
        assert approved.file_import.status == "approved"
        assert approved.settlement_event.net_irr == TRANCHE_NET
        assert approved.settlement_event.import_batch_id == result.file_import.import_batch_id
        assert len(approved.confirmed_tranche_ids) == 1
        tranche = repo.get_tranche(INSTALLATION_ID, approved.confirmed_tranche_ids[0])
        assert tranche is not None
        assert tranche.state == TrancheState.CONFIRMED
        assert tranche.confirmed_net_irr == TRANCHE_NET
        assert tranche.gateway_payment_id == payment.id
        # One canonical event in the outbox, keyed by the event id.
        assert len(outbox.events) == 1
        canonical = next(iter(outbox.events.values()))
        assert canonical.event_type == "payment.settlement.received"
        assert canonical.id == approved.canonical_event_id
        assert canonical.payload["import_batch_id"] == result.file_import.import_batch_id
        # The review item is resolved with approve_preview.
        assert review.actions == [(result.review_item_id, "approve_preview", "tester")]
        # The match link ties tranche ↔ event.
        matches = repo.list_matches_for_event(INSTALLATION_ID, approved.settlement_event.id)
        assert len(matches) == 1
        assert matches[0].tranche_id == tranche.id
        assert matches[0].allocated_amount_irr == TRANCHE_NET

    def test_approve_twice_rejected(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
    ) -> None:
        """Approving an already-approved import is a 409."""
        _record(commands)
        result = _import(service, reader, [_detail_row()])
        _approve(service, result.file_import.id)
        with pytest.raises(SettlementImportStateError, match="cannot be approved"):
            _approve(service, result.file_import.id)

    def test_approve_fee_mismatch_opens_exception(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        review: FakeReviewQueue,
    ) -> None:
        """A file fee differing from the expected fee opens a fee_mismatch exception."""
        _record(commands)
        # Fee 3,000,000 instead of 2,500,000; net adjusted so the file arithmetic holds.
        row = _detail_row(fee=3_000_000, net=TRANCHE_NET - 500_000)
        result = _import(service, reader, [row])
        approved = _approve(service, result.file_import.id)
        fee_mismatches = [
            e
            for e in approved.exceptions
            if e.exception_type == SettlementExceptionType.FEE_MISMATCH
        ]
        assert len(fee_mismatches) == 1
        assert fee_mismatches[0].expected_amount_irr == TRANCHE_FEE
        assert fee_mismatches[0].actual_amount_irr == 3_000_000
        # The exception also reaches the unified review queue.
        variance_items = [
            i
            for i in review.items.values()
            if i.item_type == ReviewItemType.SETTLEMENT_VARIANCE
            and i.subtype == "fee_mismatch"
        ]
        assert len(variance_items) == 1
        # The tranche is still confirmed (partially — the row net is short).
        tranche = repo.get_tranche(INSTALLATION_ID, approved.confirmed_tranche_ids[0])
        assert tranche is not None
        assert tranche.state == TrancheState.PARTIALLY_CONFIRMED

    def test_approve_amount_mismatch_opens_exception(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
    ) -> None:
        """A row net differing from the tranche net opens an amount_mismatch exception."""
        _record(commands)
        row = _detail_row(net=TRANCHE_NET - 1_000_000, gross=TRANCHE_GROSS - 1_000_000)
        result = _import(service, reader, [row])
        approved = _approve(service, result.file_import.id)
        mismatches = [
            e
            for e in approved.exceptions
            if e.exception_type == SettlementExceptionType.AMOUNT_MISMATCH
        ]
        assert len(mismatches) == 1
        assert mismatches[0].expected_amount_irr == TRANCHE_NET
        assert mismatches[0].actual_amount_irr == TRANCHE_NET - 1_000_000

    def test_approve_creates_payment_for_unknown_transaction(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        repo: FakeSettlementRepository,
    ) -> None:
        """An unknown-reference row with a gross amount creates the payment at approval."""
        result = _import(service, reader, [_detail_row("TXN-9")])
        approved = _approve(service, result.file_import.id)
        assert len(approved.created_payment_ids) == 1
        payment = repo.get_payment(INSTALLATION_ID, approved.created_payment_ids[0])
        assert payment is not None
        assert payment.gateway_transaction_id == "TXN-9"
        assert payment.gross_irr == TRANCHE_GROSS
        assert len(approved.confirmed_tranche_ids) == 1

    def test_approve_refund_row_cancels_payment(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """A refund row cancels the payment; its net never enters the event."""
        payment = _record(commands)
        refund = _detail_row(
            gross=-TRANCHE_GROSS,
            fee=-TRANCHE_FEE,
            vat=-TRANCHE_VAT,
            net=-TRANCHE_NET,
            tran_type="برگشت",
        )
        result = _import(service, reader, [refund])
        approved = _approve(service, result.file_import.id)
        assert approved.settlement_event.net_irr == 0
        updated = repo.get_payment(INSTALLATION_ID, payment.id)
        assert updated is not None
        assert updated.status.value == "cancelled"

    def test_reject_import(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        outbox: FakeOutbox,
        review: FakeReviewQueue,
    ) -> None:
        """Rejecting an import applies nothing and resolves the review item."""
        _record(commands)
        result = _import(service, reader, [_detail_row()])
        rejected = service.reject_import(
            INSTALLATION_ID,
            RejectSettlementImport(
                import_id=result.file_import.id, actor="tester", reason="wrong month"
            ),
            tenant_id=TENANT_ID,
        )
        assert rejected.status == "rejected"
        assert len(outbox.events) == 0
        assert review.actions == [(result.review_item_id, "cancel_batch", "tester")]

    def test_approve_payouts_file_batch_matches_fifo(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """A payouts file batch-matches the event net across due tranches, FIFO."""
        payment = _record(commands)
        # Batch matching only considers due/overdue tranches: move the
        # first tranche to due (the daily tick does this in production).
        first = _tranches_of(repo, payment.id)[0]
        repo.update_tranche(first.id, state=TrancheState.DUE)
        payout = _detail_row("PAYOUT-1", tran_type="واریز")
        result = _import(service, reader, [payout], file_type="payouts")
        approved = _approve(service, result.file_import.id)
        assert approved.settlement_event.net_irr == TRANCHE_NET
        assert len(approved.confirmed_tranche_ids) == 1
        tranche = repo.get_tranche(INSTALLATION_ID, approved.confirmed_tranche_ids[0])
        assert tranche is not None
        assert tranche.tranche_index == 1
        assert tranche.gateway_payment_id == payment.id
        matches = repo.list_matches_for_event(INSTALLATION_ID, approved.settlement_event.id)
        assert matches[0].method == "batch_fifo"



class TestDeposits:
    """C4/C5: bank deposits in confidence order + reverse matching."""

    def _approved_event(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        *,
        tracking_ref: str = "",
    ) -> Any:
        _record(commands)
        result = _import(
            service, reader, [_detail_row(tracking_ref=tracking_ref)]
        )
        return _approve(service, result.file_import.id)

    def test_deposit_exact_reference_auto_matches(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """An event tracking ref in the bank narrative auto-matches at 1.0."""
        approved = self._approved_event(
            service, reader, commands, tracking_ref="TRK-987654"
        )
        result = _deposit(
            service,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
            description="واریز اسنپ‌پی TRK-987654",
        )
        assert result.decision_method == "exact_reference"
        assert result.decision_confidence == Decimal("1")
        assert result.matched_tranche_ids == approved.confirmed_tranche_ids
        tranche = repo.get_tranche(INSTALLATION_ID, approved.confirmed_tranche_ids[0])
        assert tranche is not None
        assert tranche.state == TrancheState.RECEIVED
        assert tranche.received_net_irr == TRANCHE_NET
        matches = repo.list_matches_for_deposit(INSTALLATION_ID, result.deposit.id)
        assert len(matches) == 1
        assert matches[0].settlement_event_id == approved.settlement_event.id

    def test_deposit_amount_and_date_auto_matches(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """Amount + date within the window auto-matches at 0.95 when unique."""
        approved = self._approved_event(service, reader, commands)
        result = _deposit(
            service,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
        )
        assert result.decision_method == "amount_and_date"
        assert result.decision_confidence == Decimal("0.95")
        tranche = repo.get_tranche(INSTALLATION_ID, approved.confirmed_tranche_ids[0])
        assert tranche is not None
        assert tranche.state == TrancheState.RECEIVED

    def test_deposit_amount_only_suggests(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        review: FakeReviewQueue,
    ) -> None:
        """Amount-only (far date) files a review item with ranked suggestions."""
        approved = self._approved_event(service, reader, commands)
        result = _deposit(
            service,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 25),
        )
        assert result.decision_method == "amount_only"
        assert result.matched_tranche_ids == ()
        assert len(result.suggestions) >= 1
        assert result.suggestions[0][1] == approved.settlement_event.id
        assert result.suggestions[0][2] <= Decimal("0.70")
        variance_items = [
            i
            for i in review.items.values()
            if i.item_type == ReviewItemType.SETTLEMENT_VARIANCE
            and i.subtype == "amount_only_match"
        ]
        assert len(variance_items) == 1

    def test_deposit_no_candidate_opens_exception(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        review: FakeReviewQueue,
    ) -> None:
        """No candidate at any level opens a deposit_without_file exception."""
        self._approved_event(service, reader, commands)
        result = _deposit(
            service,
            amount_irr=123_456_789,
            deposit_date=datetime.date(2026, 11, 7),
        )
        assert result.decision_method == "none"
        assert result.matched_tranche_ids == ()
        exceptions = [
            e
            for e in result.exceptions
            if e.exception_type == SettlementExceptionType.DEPOSIT_WITHOUT_FILE
        ]
        assert len(exceptions) == 1
        assert exceptions[0].bank_deposit_id == result.deposit.id
        variance_items = [
            i
            for i in review.items.values()
            if i.item_type == ReviewItemType.SETTLEMENT_VARIANCE
            and i.subtype == "deposit_without_file"
        ]
        assert len(variance_items) == 1

    def test_deposit_overpayment_parks(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """An overpaid deposit parks the remainder — never auto-allocated."""
        approved = self._approved_event(
            service, reader, commands, tracking_ref="TRK-555555"
        )
        result = _deposit(
            service,
            amount_irr=TRANCHE_NET + 5_000_000,
            deposit_date=datetime.date(2026, 11, 7),
            description="واریز TRK-555555",
        )
        assert result.decision_method == "exact_reference"
        exceptions = [
            e
            for e in result.exceptions
            if e.exception_type == SettlementExceptionType.OVERPAYMENT
        ]
        assert len(exceptions) == 1
        assert exceptions[0].expected_amount_irr == TRANCHE_NET
        assert exceptions[0].actual_amount_irr == TRANCHE_NET + 5_000_000
        # Only the tranche's outstanding was allocated; the other
        # tranches stay untouched (no auto-allocation to the future).
        matches = repo.list_matches_for_deposit(INSTALLATION_ID, result.deposit.id)
        assert len(matches) == 1
        assert matches[0].allocated_amount_irr == TRANCHE_NET
        confirmed = repo.get_tranche(
            INSTALLATION_ID, approved.confirmed_tranche_ids[0]
        )
        assert confirmed is not None
        payment_tranches = repo.list_tranches_for_payment(
            INSTALLATION_ID, confirmed.gateway_payment_id
        )
        untouched = [t for t in payment_tranches if t.id != approved.confirmed_tranche_ids[0]]
        assert all(t.received_net_irr == 0 for t in untouched)

    def test_reverse_match_deposit_before_file(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """C7: a deposit before the file attaches to expected tranches; the file confirms it."""
        payment = _record(commands)
        first = _tranches_of(repo, payment.id)[0]
        # The deposit arrives before any file: amount + expected date
        # (the tranche is due 2026-11-26) match within the window.
        result = _deposit(
            service,
            amount_irr=TRANCHE_NET,
            deposit_date=first.expected_date,
        )
        assert result.decision_method == "amount_and_date"
        assert result.matched_tranche_ids == (first.id,)
        tranche = repo.get_tranche(INSTALLATION_ID, first.id)
        assert tranche is not None
        assert tranche.state == TrancheState.RECEIVED
        assert tranche.confirmed_net_irr == 0  # received without confirmed
        # The file arrives later and confirms the tranche.
        imported = _import(service, reader, [_detail_row()])
        approved = _approve(service, imported.file_import.id)
        assert approved.confirmed_tranche_ids == (first.id,)
        tranche = repo.get_tranche(INSTALLATION_ID, first.id)
        assert tranche is not None
        assert tranche.state == TrancheState.RECEIVED
        assert tranche.confirmed_net_irr == TRANCHE_NET
        # The deposit's match link gained the event (no duplicate link).
        links = repo.list_matches_for_deposit(INSTALLATION_ID, result.deposit.id)
        assert len(links) == 1
        assert links[0].settlement_event_id == approved.settlement_event.id

    def test_manual_match_resolves_exception(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
    ) -> None:
        """A manual match resolves the open deposit_without_file exception explicitly."""
        approved = self._approved_event(service, reader, commands)
        recorded = _deposit(
            service,
            amount_irr=123_456_789,
            deposit_date=datetime.date(2026, 11, 7),
        )
        assert recorded.decision_method == "none"
        open_exc = repo.get_open_exception_for_deposit(
            INSTALLATION_ID, recorded.deposit.id, "deposit_without_file"
        )
        assert open_exc is not None
        tranche_id = approved.confirmed_tranche_ids[0]
        result = service.match_deposit_manually(
            INSTALLATION_ID,
            MatchDepositManually(
                deposit_id=recorded.deposit.id,
                tranche_id=tranche_id,
                allocated_amount_irr=TRANCHE_NET,
                actor="tester",
                tenant_id=TENANT_ID,
            ),
        )
        assert result.decision_method == "manual"
        assert result.matched_tranche_ids == (tranche_id,)
        resolved = repo.get_exception(INSTALLATION_ID, open_exc.id)
        assert resolved is not None
        assert resolved.status.value == "resolved"
        assert resolved.resolved_by == "tester"
        tranche = repo.get_tranche(INSTALLATION_ID, tranche_id)
        assert tranche is not None
        assert tranche.state == TrancheState.RECEIVED


class TestTieOut:
    """C6: the mandatory tie-out gates every auto-post."""

    def _settled_setup(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        *,
        deposit_date: datetime.date = datetime.date(2026, 11, 7),
        deposit_amount: int = TRANCHE_NET,
    ) -> Any:
        """Import + approve + deposit: a batch ready for tie-out."""
        _record(commands)
        result = _import(service, reader, [_detail_row()])
        approved = _approve(service, result.file_import.id)
        _deposit(service, amount_irr=deposit_amount, deposit_date=deposit_date)
        return approved

    def test_tieout_holds_and_posts_when_gate_allows(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        posting: FakePosting,
    ) -> None:
        """Tie-out holds + gate allows → auto-post + tranches settled."""
        approved = self._settled_setup(service, reader, commands)
        result = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=COMPANY,
        )
        assert result.tieout.holds is True
        assert result.tieout.difference_irr == 0
        assert result.status == "settled"
        assert result.posted is True
        assert result.tryton_journal_id == 777
        assert result.gate_allowed is True
        assert result.settled_tranche_ids == approved.confirmed_tranche_ids
        assert len(posting.calls) == 1
        assert posting.calls[0].amount_irr == TRANCHE_NET
        assert posting.calls[0].company_engine_id == 1
        tranche = repo.get_tranche(INSTALLATION_ID, approved.confirmed_tranche_ids[0])
        assert tranche is not None
        assert tranche.state == TrancheState.SETTLED
        file_import = repo.get_file_import(INSTALLATION_ID, approved.file_import.id)
        assert file_import is not None
        assert file_import.status == "settled"

    def test_tieout_fails_without_deposit(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        posting: FakePosting,
    ) -> None:
        """No deposit → tie-out fails, batch in exception, NO posting."""
        _record(commands)
        result = _import(service, reader, [_detail_row()])
        approved = _approve(service, result.file_import.id)
        run = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=COMPANY,
        )
        assert run.tieout.holds is False
        assert run.tieout.difference_irr == TRANCHE_NET
        assert run.status == "exception"
        assert run.posted is False
        assert posting.calls == []
        assert run.gate_allowed is False
        mismatches = [
            e
            for e in run.exceptions
            if e.exception_type == SettlementExceptionType.AMOUNT_MISMATCH
        ]
        assert len(mismatches) == 1
        file_import = repo.get_file_import(INSTALLATION_ID, approved.file_import.id)
        assert file_import is not None
        assert file_import.status == "exception"

    def test_tieout_holds_but_gate_denies_no_post(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        gate: FakePolicyGate,
        posting: FakePosting,
    ) -> None:
        """Tie-out holds but the gate denies → draft-hold, no post, tranches open."""
        gate.allowed = False
        gate.reason = "auto-post disabled for this installation"
        approved = self._settled_setup(service, reader, commands)
        run = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=COMPANY,
        )
        assert run.tieout.holds is True
        assert run.status == "approved"
        assert run.posted is False
        assert run.gate_allowed is False
        assert run.gate_reason == "auto-post disabled for this installation"
        assert posting.calls == []
        assert run.settled_tranche_ids == ()

    def test_tieout_holds_but_posting_unconfigured_no_post(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        posting: FakePosting,
        review: FakeReviewQueue,
    ) -> None:
        """Tie-out holds + gate allows but posting is unconfigured → exception, no post."""
        posting.fail_not_configured = True
        approved = self._settled_setup(service, reader, commands)
        run = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=COMPANY,
        )
        assert run.tieout.holds is True
        assert run.gate_allowed is True
        assert run.status == "exception"
        assert run.posted is False
        assert run.settled_tranche_ids == ()
        posting_items = [
            i
            for i in review.items.values()
            if i.item_type == ReviewItemType.SETTLEMENT_VARIANCE
            and i.subtype == "posting_failed"
        ]
        assert len(posting_items) == 1

    def test_tieout_without_company_never_posts(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        posting: FakePosting,
    ) -> None:
        """Without a company the gate cannot be evaluated: fail-closed, no post."""
        approved = self._settled_setup(service, reader, commands)
        run = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=None,
        )
        assert run.tieout.holds is True
        assert run.status == "approved"
        assert run.posted is False
        assert run.gate_allowed is False
        assert posting.calls == []

    def test_tieout_partial_deposit_fails(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        posting: FakePosting,
    ) -> None:
        """A short deposit fails the tie-out — an undocumented deduction is not a deduction."""
        _record(commands)
        result = _import(service, reader, [_detail_row(tracking_ref="TRK-777777")])
        approved = _approve(service, result.file_import.id)
        # A short deposit can only match via exact reference (rules 2–3
        # need an exact amount): the reference attaches it, partially.
        recorded = _deposit(
            service,
            amount_irr=TRANCHE_NET - 1_000_000,
            deposit_date=datetime.date(2026, 11, 7),
            description="واریز TRK-777777",
        )
        assert recorded.decision_method == "exact_reference"
        run = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=COMPANY,
        )
        assert run.tieout.holds is False
        assert run.tieout.difference_irr == 1_000_000
        assert run.status == "exception"
        assert run.posted is False
        assert posting.calls == []

    def test_tieout_inline_after_deposit(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        posting: FakePosting,
    ) -> None:
        """Recording the completing deposit with a company ties out + posts inline."""
        _record(commands)
        result = _import(service, reader, [_detail_row()])
        approved = _approve(service, result.file_import.id)
        recorded = _deposit(
            service,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
            company=COMPANY,
        )
        assert recorded.tieout is not None
        assert recorded.tieout.holds is True
        assert recorded.posted_journal_id == 777
        assert len(posting.calls) == 1
        file_import = repo.get_file_import(INSTALLATION_ID, approved.file_import.id)
        assert file_import is not None
        assert file_import.status == "settled"

    def test_tieout_on_awaiting_review_rejected(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
    ) -> None:
        """Tie-out needs an approved batch: awaiting-review is a 409."""
        _record(commands)
        result = _import(service, reader, [_detail_row()])
        with pytest.raises(SettlementImportStateError, match="tie-out needs an approved batch"):
            service.run_tieout(
                INSTALLATION_ID,
                RunSettlementTieOut(
                    import_id=result.file_import.id, actor="tester", tenant_id=TENANT_ID
                ),
                company=COMPANY,
            )

    def test_tieout_rerun_on_settled_batch_never_reposts(
        self,
        service: SettlementImportService,
        reader: FakeFileReader,
        commands: SettlementCommandService,
        repo: FakeSettlementRepository,
        posting: FakePosting,
    ) -> None:
        """Idempotent re-run (US-525): a settled batch recomputes the
        tie-out but posts nothing — the explicit endpoint needs the
        same settled guard as the inline path."""
        approved = self._settled_setup(service, reader, commands)
        first = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=COMPANY,
        )
        assert first.status == "settled"
        assert first.posted is True
        assert len(posting.calls) == 1
        second = service.run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=approved.file_import.id, actor="tester", tenant_id=TENANT_ID
            ),
            company=COMPANY,
        )
        assert second.status == "settled"
        assert second.posted is False
        assert second.tieout.holds is True
        assert len(posting.calls) == 1
        assert second.settled_tranche_ids == approved.confirmed_tranche_ids
        file_import = repo.get_file_import(INSTALLATION_ID, approved.file_import.id)
        assert file_import is not None
        assert file_import.status == "settled"
