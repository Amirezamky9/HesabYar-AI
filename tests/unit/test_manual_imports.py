"""Unit tests for the manual-import batch cycle (US-554).

DB-free: the batch service runs over the REAL bank + settlement
import services (their own fakes come from
``tests.unit.test_bank_services`` and
``tests.unit.test_settlement_import_services``) and an in-memory
fake manual-import repository defined here. The SQLAlchemy
repository + migration have their own DB tests.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Any, cast

import pytest

from hesabyar.application.commands.bank_import import BankImportService
from hesabyar.application.commands.manual_imports import (
    POSTING_DRAFT_HOLD,
    POSTING_HELD_GATE,
    POSTING_POSTED,
    POSTING_TIEOUT_FAILED,
    ManualImportService,
)
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.application.commands.settlement_import import SettlementImportService
from hesabyar.application.queries.review_queue import ReviewQueueQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.bank.models import (
    FileCurrencyUnit,
    ImportBatchStatus,
    StatementFileFormat,
)
from hesabyar.domain.manual_imports.errors import (
    ManualImportFileError,
    ManualImportInvalidStateError,
    ManualImportNotFoundError,
    SettlementColumnMappingInvalidError,
    SettlementColumnMappingNotFoundError,
)
from hesabyar.domain.manual_imports.models import (
    BatchVarianceReport,
    ImportBatchPreview,
    ManualImportBatch,
    ManualImportBatchStatus,
    ManualImportKind,
    ManualImportStage,
    SettlementColumnMapping,
)
from hesabyar.domain.manual_imports.ports import ManualImportRepositoryPort
from hesabyar.domain.manual_imports.state_machine import (
    can_transition,
    require_transition,
)
from hesabyar.domain.review_queue.models import ReviewItemType
from hesabyar.domain.settlement.commands import (
    CreateGatewayPayment,
    RecordBankDeposit,
    RunSettlementTieOut,
)
from hesabyar.domain.settlement.models import PaymentSource
from hesabyar.infrastructure.bank.file_readers import StatementFileReader
from tests.unit.test_bank_services import (
    COLUMNS as BANK_COLUMNS,
)
from tests.unit.test_bank_services import (
    COMPANY_ID,
    CSV_BYTES,
    TENANT,
    FakeBankRepo,
    FakeEngine,
    FakeReviewQueue,
)
from tests.unit.test_settlement_import_services import (
    COLUMNS as SETTLEMENT_COLUMNS,
)
from tests.unit.test_settlement_import_services import (
    HEADERS,
    TRANCHE_NET,
    FakeFileReader,
    FakeOutbox,
    FakePolicyGate,
    FakePosting,
    _detail_row,
    _make_registry,
)
from tests.unit.test_settlement_services import (
    FakeSettlementAudit,
    FakeSettlementRepository,
)

INSTALLATION_ID = "00000000-0000-0000-0000-000000000521"
NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
COMPANY = CompanyRef(engine_id=1)


class FakeManualImportRepository(ManualImportRepositoryPort):
    """In-memory batch + settlement-mapping store."""

    def __init__(self) -> None:
        self.batches: dict[str, ManualImportBatch] = {}
        self.mappings: dict[str, SettlementColumnMapping] = {}

    def save_batch(self, batch: ManualImportBatch) -> ManualImportBatch:
        self.batches[batch.id] = batch
        return batch

    def update_batch(self, batch: ManualImportBatch) -> ManualImportBatch:
        self.batches[batch.id] = batch
        return batch

    def get_batch(self, batch_id: str, tenant_id: str) -> ManualImportBatch | None:
        batch = self.batches.get(batch_id)
        return batch if batch is not None and batch.tenant_id == tenant_id else None

    def list_batches(
        self,
        tenant_id: str,
        *,
        kind: ManualImportKind | None = None,
        status: ManualImportBatchStatus | None = None,
        company_id: str | None = None,
        source_import_id: str | None = None,
    ) -> Sequence[ManualImportBatch]:
        return [
            batch
            for batch in self.batches.values()
            if batch.tenant_id == tenant_id
            and (kind is None or batch.kind == kind)
            and (status is None or batch.status == status)
            and (company_id is None or batch.company_id == company_id)
            and (source_import_id is None or batch.source_import_id == source_import_id)
        ]

    def get_batch_by_file_hash(
        self,
        tenant_id: str,
        kind: ManualImportKind,
        source_key: str,
        file_hash_sha256: str,
    ) -> ManualImportBatch | None:
        for batch in self.batches.values():
            if (
                batch.tenant_id == tenant_id
                and batch.kind == kind
                and batch.source_key == source_key
                and batch.file_hash_sha256 == file_hash_sha256
            ):
                return batch
        return None

    def list_settlement_mappings(
        self,
        tenant_id: str,
        channel_key: str | None = None,
        company_id: str | None = None,
    ) -> Sequence[SettlementColumnMapping]:
        return [
            mapping
            for mapping in self.mappings.values()
            if mapping.tenant_id == tenant_id
            and (channel_key is None or mapping.channel_key == channel_key)
            and (company_id is None or mapping.company_id in (None, company_id))
        ]

    def get_settlement_mapping(
        self, mapping_id: str, tenant_id: str
    ) -> SettlementColumnMapping | None:
        mapping = self.mappings.get(mapping_id)
        return (
            mapping
            if mapping is not None and mapping.tenant_id == tenant_id
            else None
        )

    def save_settlement_mapping(
        self, mapping: SettlementColumnMapping
    ) -> SettlementColumnMapping:
        self.mappings[mapping.id] = mapping
        return mapping

    def update_settlement_mapping(
        self, mapping: SettlementColumnMapping
    ) -> SettlementColumnMapping:
        self.mappings[mapping.id] = mapping
        return mapping


@pytest.fixture()
def world() -> dict[str, Any]:
    """A fresh fake world: engine + repos + the real import services."""
    engine = FakeEngine()
    bank = FakeBankRepo()
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, queue, now=NOW)
    review_queries = ReviewQueueQueryService(queue, queue, now=NOW)
    engine_port = cast(AccountingEnginePort, engine)
    bank_import = BankImportService(
        engine_port, bank, review, StatementFileReader(), now=NOW
    )
    settlement_repo = FakeSettlementRepository()
    registry = _make_registry()
    audit = FakeSettlementAudit()
    commands = SettlementCommandService(settlement_repo, registry, audit)
    reader = FakeFileReader()
    outbox = FakeOutbox()
    posting = FakePosting()
    gate = FakePolicyGate()
    settlement_import = SettlementImportService(
        settlement_repo,
        registry,
        audit,
        commands,
        review,
        review_queries,
        reader,  # type: ignore[arg-type]
        outbox,  # type: ignore[arg-type]
        posting,  # type: ignore[arg-type]
        gate,  # type: ignore[arg-type]
        now=NOW,
    )
    batches = FakeManualImportRepository()
    service = ManualImportService(
        batches,
        bank,
        bank_import,
        settlement_import,
        settlement_repo,
        review,
        reader,  # type: ignore[arg-type]
        now=NOW,
    )
    return {
        "engine": engine,
        "bank": bank,
        "queue": queue,
        "review": review,
        "bank_import": bank_import,
        "settlement_repo": settlement_repo,
        "registry": registry,
        "commands": commands,
        "reader": reader,
        "posting": posting,
        "gate": gate,
        "settlement_import": settlement_import,
        "batches": batches,
        "service": service,
    }


def _record_payment(
    commands: SettlementCommandService, *, txn: str = "TXN-1", gross: int = 100_000_000
) -> Any:
    """Record a 100M-gross SnappPay payment (4 tranches of 25M)."""
    return commands.record_payment(
        CreateGatewayPayment(
            installation_id=INSTALLATION_ID,
            channel_key="snapppay",
            gateway_transaction_id=txn,
            gross_irr=gross,
            gateway_confirmed_at=NOW,
            source=PaymentSource.STORE_WEBHOOK,
            store_order_id="ORD-1",
        )
    ).payment


def _upload_statement(world: dict[str, Any], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "installation_id": INSTALLATION_ID,
        "company": COMPANY,
        "company_id": COMPANY_ID,
        "bank_key": "melli",
        "statement_journal_id": 700,
        "filename": "baam.csv",
        "content": CSV_BYTES,
        "currency_unit": FileCurrencyUnit.RIAL,
        "default_account_id": 900,
        "mapping_columns": BANK_COLUMNS,
        "actor": "operator",
    }
    kwargs.update(overrides)
    return world["service"].upload_statement_batch(**kwargs)


def _upload_settlement(
    world: dict[str, Any], rows: list[list[str | None]] | None = None, **overrides: Any
) -> Any:
    grid_rows = [_detail_row()] if rows is None else rows
    world["reader"].grid = [HEADERS, *grid_rows]
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "installation_id": INSTALLATION_ID,
        "company_id": COMPANY_ID,
        "channel_key": "snapppay",
        "file_type": "detail",
        "period_jalali": "1405/08",
        "filename": "snapppay-1405-08.xlsx",
        "content": b"fake-xlsx-bytes",
        "currency_unit": "rial",
        "mapping_columns": SETTLEMENT_COLUMNS,
        "actor": "operator",
    }
    kwargs.update(overrides)
    return world["service"].upload_settlement_batch(**kwargs)


def _deposit(
    world: dict[str, Any], *, amount_irr: int, deposit_date: datetime.date
) -> Any:
    return world["settlement_import"].record_deposit(
        RecordBankDeposit(
            installation_id=INSTALLATION_ID,
            tenant_id=TENANT,
            deposit_date=deposit_date,
            amount_irr=amount_irr,
            description=None,
            tracking_ref=None,
            source="manual",
            channel_key="snapppay",
            actor="tester",
        ),
        company=None,
    )


# -- state machine -------------------------------------------------------------


class TestStateMachine:
    """Every transition of the batch lifecycle, allowed and forbidden."""

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (ManualImportBatchStatus.UPLOADED, ManualImportBatchStatus.MAPPED),
            (ManualImportBatchStatus.MAPPED, ManualImportBatchStatus.VALIDATED),
            (ManualImportBatchStatus.VALIDATED, ManualImportBatchStatus.PREVIEWED),
            (ManualImportBatchStatus.PREVIEWED, ManualImportBatchStatus.AWAITING_REVIEW),
            (ManualImportBatchStatus.AWAITING_REVIEW, ManualImportBatchStatus.APPROVED),
            (ManualImportBatchStatus.APPROVED, ManualImportBatchStatus.POSTED),
            (ManualImportBatchStatus.UPLOADED, ManualImportBatchStatus.REJECTED),
            (ManualImportBatchStatus.MAPPED, ManualImportBatchStatus.REJECTED),
            (ManualImportBatchStatus.VALIDATED, ManualImportBatchStatus.REJECTED),
            (ManualImportBatchStatus.PREVIEWED, ManualImportBatchStatus.REJECTED),
            (ManualImportBatchStatus.AWAITING_REVIEW, ManualImportBatchStatus.REJECTED),
            (ManualImportBatchStatus.REJECTED, ManualImportBatchStatus.ARCHIVED),
            (ManualImportBatchStatus.ARCHIVED, ManualImportBatchStatus.MAPPED),
        ],
    )
    def test_allowed_transitions(
        self,
        from_state: ManualImportBatchStatus,
        to_state: ManualImportBatchStatus,
    ) -> None:
        assert can_transition(from_state, to_state) is True
        require_transition(from_state, to_state)  # must not raise

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (ManualImportBatchStatus.UPLOADED, ManualImportBatchStatus.APPROVED),
            (ManualImportBatchStatus.UPLOADED, ManualImportBatchStatus.POSTED),
            (ManualImportBatchStatus.AWAITING_REVIEW, ManualImportBatchStatus.POSTED),
            (ManualImportBatchStatus.APPROVED, ManualImportBatchStatus.REJECTED),
            (ManualImportBatchStatus.APPROVED, ManualImportBatchStatus.ARCHIVED),
            (ManualImportBatchStatus.POSTED, ManualImportBatchStatus.MAPPED),
            (ManualImportBatchStatus.POSTED, ManualImportBatchStatus.REJECTED),
            (ManualImportBatchStatus.REJECTED, ManualImportBatchStatus.MAPPED),
            (ManualImportBatchStatus.ARCHIVED, ManualImportBatchStatus.APPROVED),
            (ManualImportBatchStatus.ARCHIVED, ManualImportBatchStatus.REJECTED),
        ],
    )
    def test_forbidden_transitions_raise(
        self,
        from_state: ManualImportBatchStatus,
        to_state: ManualImportBatchStatus,
    ) -> None:
        assert can_transition(from_state, to_state) is False
        with pytest.raises(ManualImportInvalidStateError, match="cannot move"):
            require_transition(from_state, to_state)

    def test_posted_is_terminal(self) -> None:
        for target in ManualImportBatchStatus:
            assert can_transition(ManualImportBatchStatus.POSTED, target) is False


# -- preview + model round-trips ------------------------------------------------


class TestPreview:
    """Preview math + (de)serialisation of the batch models."""

    def test_statement_preview_from_totals(self) -> None:
        preview = ImportBatchPreview.from_statement_totals(
            total_deposits_irr=1_000_000,
            total_withdrawals_irr=250_000,
            new_rows=2,
            duplicate_rows=1,
            invalid_rows=0,
        )
        assert preview.gross_irr == 1_250_000
        assert preview.net_irr == 750_000
        assert preview.expected_deposit_irr == 1_000_000
        assert preview.fee_irr == 0
        assert preview.tax_irr == 0
        assert preview.refunds_irr == 0
        assert preview.receivable_before_irr is None
        assert preview.receivable_after_irr is None
        assert preview.new_rows == 2
        assert preview.duplicate_rows == 1

    def test_preview_round_trip(self) -> None:
        preview = ImportBatchPreview(
            gross_irr=25_000_000,
            fee_irr=2_500_000,
            tax_irr=250_000,
            refunds_irr=0,
            net_irr=22_250_000,
            deductions_irr=0,
            receivable_before_irr=89_000_000,
            receivable_after_irr=66_750_000,
            expected_deposit_irr=22_250_000,
            matched_rows=1,
        )
        assert ImportBatchPreview.from_dict(preview.to_dict()) == preview

    def test_stage_round_trip(self) -> None:
        stage = ManualImportStage(
            stage="awaiting_review", at=NOW, note="ready for review"
        )
        assert ManualImportStage.from_dict(stage.to_dict()) == stage

    def test_variance_report_round_trip(self) -> None:
        report = BatchVarianceReport(
            expected_net_irr=22_250_000,
            imported_net_irr=22_250_000,
            posted_net_irr=0,
            difference_irr=22_250_000,
            tieout_holds=None,
            tieout_difference_irr=None,
            tieout_tolerance_irr=None,
            gate_allowed=None,
            gate_reason="draft_hold",
            posting_status=POSTING_DRAFT_HOLD,
            generated_at=NOW,
        )
        assert BatchVarianceReport.from_dict(report.to_dict()) == report

    def test_batch_rerun_and_terminal_flags(self) -> None:
        batch = ManualImportBatch(
            id=str(uuid.uuid4()),
            tenant_id=TENANT,
            installation_id=INSTALLATION_ID,
            company_id=COMPANY_ID,
            kind=ManualImportKind.STATEMENT,
            source_key="melli",
            filename="baam.csv",
            file_format=StatementFileFormat.CSV,
            file_hash_sha256="abc",
            currency_unit=FileCurrencyUnit.RIAL,
            status=ManualImportBatchStatus.ARCHIVED,
        )
        assert batch.can_rerun is True
        assert batch.is_terminal is False
        posted = ManualImportBatch(
            **{**batch.__dict__, "status": ManualImportBatchStatus.POSTED}
        )
        assert posted.can_rerun is False
        assert posted.is_terminal is True


# -- detection ------------------------------------------------------------------


class TestDetect:
    """Format + column-mapping detection, per kind."""

    def test_detect_statement_delegates_to_bank_detector(
        self, world: dict[str, Any]
    ) -> None:
        detection = world["service"].detect_file(
            kind=ManualImportKind.STATEMENT, content=CSV_BYTES, filename="baam.csv"
        )
        assert detection.file_format == StatementFileFormat.CSV
        assert detection.suggested_mapping["date"] == "تاریخ"
        assert detection.suggested_mapping["description"] == "شرح"
        assert detection.missing_fields == ()

    def test_detect_settlement_suggests_columns_from_headers(
        self, world: dict[str, Any]
    ) -> None:
        world["reader"].grid = [HEADERS, _detail_row()]
        detection = world["service"].detect_file(
            kind=ManualImportKind.SETTLEMENT,
            content=b"fake-xlsx-bytes",
            filename="snapppay-1405-08.xlsx",
        )
        assert detection.file_format == StatementFileFormat.XLSX
        assert detection.suggested_mapping["transaction_id"] == "شماره تراکنش"
        assert detection.suggested_mapping["net"] == "مبلغ خالص"
        assert detection.missing_fields == ()
        assert detection.sample_rows

    def test_detect_settlement_empty_file_fails(self, world: dict[str, Any]) -> None:
        world["reader"].grid = []
        with pytest.raises(ManualImportFileError, match="no rows"):
            world["service"].detect_file(
                kind=ManualImportKind.SETTLEMENT,
                content=b"",
                filename="empty.xlsx",
            )


# -- settlement column mappings --------------------------------------------------


class TestSettlementMappings:
    """Saved per-channel settlement mappings (validated, supersedeable)."""

    def test_save_and_list_mapping(self, world: dict[str, Any]) -> None:
        mapping = world["service"].save_settlement_mapping(
            tenant_id=TENANT,
            channel_key="snapppay",
            name="SnappPay detail",
            file_format=StatementFileFormat.XLSX,
            file_type="detail",
            columns=dict(SETTLEMENT_COLUMNS),
        )
        assert mapping.id
        assert mapping.channel_key == "snapppay"
        found = world["service"].list_settlement_mappings(TENANT, "snapppay")
        assert [m.id for m in found] == [mapping.id]
        assert (
            world["service"].get_settlement_mapping(TENANT, mapping.id).id == mapping.id
        )

    def test_save_mapping_rejects_missing_required_columns(
        self, world: dict[str, Any]
    ) -> None:
        bad = {"transaction_id": "شماره تراکنش", "date": "تاریخ تراکنش"}
        with pytest.raises(
            SettlementColumnMappingInvalidError, match="invalid"
        ):
            world["service"].save_settlement_mapping(
                tenant_id=TENANT,
                channel_key="snapppay",
                name="broken",
                file_format=StatementFileFormat.XLSX,
                file_type="detail",
                columns=bad,
            )

    def test_save_mapping_rejects_bad_date_kind(self, world: dict[str, Any]) -> None:
        with pytest.raises(
            SettlementColumnMappingInvalidError, match="date_kind"
        ):
            world["service"].save_settlement_mapping(
                tenant_id=TENANT,
                channel_key="snapppay",
                name="broken",
                file_format=StatementFileFormat.XLSX,
                file_type="detail",
                columns=dict(SETTLEMENT_COLUMNS),
                date_kind="hijri",
            )

    def test_supersede_keeps_mapping_for_provenance(
        self, world: dict[str, Any]
    ) -> None:
        mapping = world["service"].save_settlement_mapping(
            tenant_id=TENANT,
            channel_key="snapppay",
            name="v1",
            file_format=StatementFileFormat.XLSX,
            file_type="detail",
            columns=dict(SETTLEMENT_COLUMNS),
        )
        superseded = world["service"].supersede_settlement_mapping(
            tenant_id=TENANT, mapping_id=mapping.id
        )
        assert superseded.status.value == "superseded"
        assert (
            world["service"].get_settlement_mapping(TENANT, mapping.id).status.value
            == "superseded"
        )

    def test_missing_mapping_is_404(self, world: dict[str, Any]) -> None:
        with pytest.raises(SettlementColumnMappingNotFoundError):
            world["service"].get_settlement_mapping(TENANT, "no-such-mapping")
        with pytest.raises(SettlementColumnMappingNotFoundError):
            world["service"].supersede_settlement_mapping(
                tenant_id=TENANT, mapping_id="no-such-mapping"
            )


# -- statement upload --------------------------------------------------------------


class TestStatementUpload:
    """Statement upload: batch born awaiting_review, with preview + stages."""

    def test_upload_statement_batch(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        batch = result.batch
        assert result.duplicate is False
        assert batch.kind == ManualImportKind.STATEMENT
        assert batch.status == ManualImportBatchStatus.AWAITING_REVIEW
        assert batch.attempt == 1
        assert batch.source_import_id is not None
        assert batch.review_item_id is not None
        assert [stage.stage for stage in batch.stages] == [
            "uploaded",
            "mapped",
            "validated",
            "previewed",
            "awaiting_review",
        ]
        preview = result.preview
        assert preview.gross_irr == 1_250_000
        assert preview.net_irr == 750_000
        assert preview.expected_deposit_irr == 1_000_000
        assert preview.new_rows == 2
        item = world["queue"].get_item(batch.review_item_id, TENANT)
        assert item is not None
        assert item.item_type == ReviewItemType.MANUAL_IMPORT_REVIEW

    def test_reupload_same_file_is_noop(self, world: dict[str, Any]) -> None:
        first = _upload_statement(world)
        second = _upload_statement(world)
        assert second.duplicate is True
        assert second.batch.id == first.batch.id
        assert len(world["batches"].batches) == 1

    def test_upload_with_save_mapping_saves_bank_mapping(
        self, world: dict[str, Any]
    ) -> None:
        result = _upload_statement(
            world, save_mapping=True, mapping_name="Melli Baam"
        )
        assert result.batch.column_mapping_id is not None
        saved = world["bank"].get_mapping(result.batch.column_mapping_id, TENANT)
        assert saved is not None
        assert saved.name == "Melli Baam"
        assert saved.bank_key == "melli"

    def test_upload_with_saved_mapping_id(self, world: dict[str, Any]) -> None:
        saved = world["bank_import"].save_mapping(
            tenant_id=TENANT,
            bank_key="melli",
            name="Melli Baam",
            file_format=StatementFileFormat.CSV,
            columns=BANK_COLUMNS,
            date_kind="jalali",
            currency_unit=FileCurrencyUnit.RIAL,
            header_row=0,
            company_id=COMPANY_ID,
        )
        result = _upload_statement(
            world, mapping_id=saved.id, mapping_columns=None
        )
        assert result.batch.column_mapping_id == saved.id
        assert result.preview.new_rows == 2


# -- settlement upload --------------------------------------------------------------


class TestSettlementUpload:
    """Settlement upload: the US-523 preview, wrapped in a batch."""

    def test_upload_settlement_batch(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        batch = result.batch
        assert result.duplicate is False
        assert batch.kind == ManualImportKind.SETTLEMENT
        assert batch.status == ManualImportBatchStatus.AWAITING_REVIEW
        assert batch.source_import_batch_id is not None
        assert batch.review_item_id is not None
        preview = result.preview
        assert preview.gross_irr == 25_000_000
        assert preview.fee_irr == 2_500_000
        assert preview.tax_irr == 250_000
        assert preview.net_irr == TRANCHE_NET
        assert preview.expected_deposit_irr == TRANCHE_NET
        assert preview.receivable_before_irr == 4 * TRANCHE_NET
        assert preview.receivable_after_irr == 3 * TRANCHE_NET
        assert preview.matched_rows == 1
        assert preview.new_rows == 0

    def test_reupload_same_file_is_noop(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        first = _upload_settlement(world)
        second = _upload_settlement(world)
        assert second.duplicate is True
        assert second.batch.id == first.batch.id
        assert len(world["batches"].batches) == 1

    def test_upload_with_save_mapping_saves_settlement_mapping(
        self, world: dict[str, Any]
    ) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world, save_mapping=True, mapping_name="SnappPay")
        assert result.batch.column_mapping_id is not None
        saved = world["service"].get_settlement_mapping(
            TENANT, result.batch.column_mapping_id
        )
        assert saved.name == "SnappPay"
        assert saved.channel_key == "snapppay"
        assert saved.columns == SETTLEMENT_COLUMNS

    def test_upload_with_saved_mapping_id(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        mapping = world["service"].save_settlement_mapping(
            tenant_id=TENANT,
            channel_key="snapppay",
            name="SnappPay detail",
            file_format=StatementFileFormat.XLSX,
            file_type="detail",
            columns=dict(SETTLEMENT_COLUMNS),
        )
        result = _upload_settlement(
            world, mapping_id=mapping.id, mapping_columns=None
        )
        assert result.batch.column_mapping_id == mapping.id
        assert result.preview.matched_rows == 1

    def test_upload_with_unknown_mapping_id_is_404(self, world: dict[str, Any]) -> None:
        with pytest.raises(SettlementColumnMappingNotFoundError):
            _upload_settlement(world, mapping_id="no-such-mapping")


# -- approve -------------------------------------------------------------------------


class TestApprove:
    """Approve: statement → draft statement; settlement → draft by default."""

    def test_approve_statement_posts_draft_statement(
        self, world: dict[str, Any]
    ) -> None:
        result = _upload_statement(world)
        approved = world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        assert approved.posted is True
        assert approved.batch.status == ManualImportBatchStatus.POSTED
        assert approved.batch.posting_status == POSTING_POSTED
        assert approved.batch.engine_ref_id is not None
        assert approved.batch.approved_at is not None
        assert approved.batch.posted_at is not None
        bank_batch = world["bank"].get_batch(result.batch.source_import_id, TENANT)
        assert bank_batch is not None
        assert bank_batch.status == ImportBatchStatus.CONFIRMED
        assert bank_batch.engine_statement_id == approved.batch.engine_ref_id

    def test_approve_from_wrong_state_raises(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        with pytest.raises(ManualImportInvalidStateError, match="cannot move"):
            world["service"].approve_batch(
                tenant_id=TENANT,
                batch_id=result.batch.id,
                company=COMPANY,
                actor="operator",
            )

    def test_approve_settlement_without_opt_in_is_draft_hold(
        self, world: dict[str, Any]
    ) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        approved = world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        assert approved.posted is False
        assert approved.batch.status == ManualImportBatchStatus.APPROVED
        assert approved.batch.posting_status == POSTING_DRAFT_HOLD
        assert approved.batch.auto_post_opt_in is False
        assert approved.settlement_event_id is not None
        assert len(approved.confirmed_tranche_ids) == 1
        assert len(world["posting"].calls) == 0  # nothing posted

    def test_approve_settlement_with_opt_in_posts_when_tieout_holds(
        self, world: dict[str, Any]
    ) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        _deposit(
            world,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
        )
        approved = world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            auto_post=True,
            actor="operator",
        )
        assert approved.posted is True
        assert approved.batch.status == ManualImportBatchStatus.POSTED
        assert approved.batch.posting_status == POSTING_POSTED
        assert approved.tryton_journal_id == 777
        assert approved.tieout is not None
        assert approved.tieout.holds is True
        assert len(world["posting"].calls) == 1
        assert world["posting"].calls[0].amount_irr == TRANCHE_NET

    def test_approve_settlement_with_opt_in_held_when_gate_denies(
        self, world: dict[str, Any]
    ) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        _deposit(
            world,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
        )
        world["gate"].allowed = False
        world["gate"].reason = "fiscal period closed"
        approved = world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            auto_post=True,
            actor="operator",
        )
        assert approved.posted is False
        assert approved.batch.status == ManualImportBatchStatus.APPROVED
        assert approved.batch.posting_status == POSTING_HELD_GATE
        assert len(world["posting"].calls) == 0

    def test_approve_settlement_with_opt_in_fails_without_deposit(
        self, world: dict[str, Any]
    ) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        approved = world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            auto_post=True,
            actor="operator",
        )
        assert approved.posted is False
        assert approved.batch.status == ManualImportBatchStatus.APPROVED
        assert approved.batch.posting_status == POSTING_TIEOUT_FAILED
        assert approved.tieout is not None
        assert approved.tieout.holds is False
        assert len(world["posting"].calls) == 0


# -- reject + re-run --------------------------------------------------------------------


class TestRejectAndRerun:
    """Reject archives with a reason; an archived batch re-runs."""

    def test_reject_statement_archives_with_reason(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        rejected = world["service"].reject_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            reason="Wrong month — re-export from Baam.",
            actor="operator",
        )
        assert rejected.status == ManualImportBatchStatus.ARCHIVED
        assert rejected.rejection_reason == "Wrong month — re-export from Baam."
        assert rejected.rejected_at is not None
        assert rejected.archived_at is not None
        stages = [stage.stage for stage in rejected.stages]
        assert stages[-2:] == ["rejected", "archived"]
        assert rejected.stages[-2].note == "Wrong month — re-export from Baam."
        bank_batch = world["bank"].get_batch(result.batch.source_import_id, TENANT)
        assert bank_batch is not None
        assert bank_batch.status == ImportBatchStatus.CANCELLED

    def test_reject_requires_a_reason(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        with pytest.raises(ManualImportInvalidStateError, match="needs a reason"):
            world["service"].reject_batch(
                tenant_id=TENANT, batch_id=result.batch.id, reason="  ", actor="op"
            )

    def test_reject_posted_batch_raises(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        with pytest.raises(ManualImportInvalidStateError, match="cannot move"):
            world["service"].reject_batch(
                tenant_id=TENANT,
                batch_id=result.batch.id,
                reason="too late",
                actor="op",
            )

    def test_rerun_archived_statement_batch(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        world["service"].reject_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            reason="Wrong month.",
            actor="operator",
        )
        rerun = world["service"].rerun_batch(
            tenant_id=TENANT, batch_id=result.batch.id, actor="operator"
        )
        assert rerun.status == ManualImportBatchStatus.AWAITING_REVIEW
        assert rerun.attempt == 2
        assert rerun.rejection_reason is None
        assert rerun.preview is not None
        assert rerun.preview.net_irr == 750_000
        assert rerun.review_item_id is not None
        assert rerun.review_item_id != result.batch.review_item_id
        stages = [stage.stage for stage in rerun.stages]
        assert stages[-4:] == ["mapped", "validated", "previewed", "awaiting_review"]
        # The rejection stays in the history — nothing is silently dropped.
        assert "rejected" in stages
        bank_batch = world["bank"].get_batch(result.batch.source_import_id, TENANT)
        assert bank_batch is not None
        assert bank_batch.status == ImportBatchStatus.AWAITING_REVIEW

    def test_rerun_non_archived_batch_raises(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        with pytest.raises(ManualImportInvalidStateError, match="cannot be re-run"):
            world["service"].rerun_batch(
                tenant_id=TENANT, batch_id=result.batch.id, actor="operator"
            )

    def test_reject_and_rerun_settlement_batch(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        rejected = world["service"].reject_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            reason="Gateway file was for the wrong period.",
            actor="operator",
        )
        assert rejected.status == ManualImportBatchStatus.ARCHIVED
        rerun = world["service"].rerun_batch(
            tenant_id=TENANT, batch_id=result.batch.id, actor="operator"
        )
        assert rerun.status == ManualImportBatchStatus.AWAITING_REVIEW
        assert rerun.attempt == 2
        assert rerun.preview is not None
        assert rerun.preview.net_irr == TRANCHE_NET
        assert rerun.preview.matched_rows == 1
        file_import = world["settlement_repo"].get_file_import(
            INSTALLATION_ID, result.batch.source_import_id
        )
        assert file_import is not None
        assert file_import.status == "awaiting_review"


# -- retry posting ------------------------------------------------------------------------


class TestRetryPosting:
    """Retry the posting step of an approved settlement batch."""

    def test_retry_statement_batch_raises(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        with pytest.raises(ManualImportInvalidStateError, match="settlement batch"):
            world["service"].retry_posting(
                tenant_id=TENANT,
                batch_id=result.batch.id,
                company=COMPANY,
                actor="operator",
            )

    def test_retry_unapproved_batch_raises(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        with pytest.raises(ManualImportInvalidStateError, match="only an approved"):
            world["service"].retry_posting(
                tenant_id=TENANT,
                batch_id=result.batch.id,
                company=COMPANY,
                actor="operator",
            )

    def test_retry_posts_once_deposit_is_recorded(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        _deposit(
            world,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
        )
        retried = world["service"].retry_posting(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        assert retried.posted is True
        assert retried.already_settled is False
        assert retried.batch.status == ManualImportBatchStatus.POSTED
        assert retried.tryton_journal_id == 777

    def test_retry_syncs_already_settled_batch_without_reposting(
        self, world: dict[str, Any]
    ) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        _deposit(
            world,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
        )
        # The import settles through the settlement tie-out endpoint
        # (not the batch) — the batch must sync, never re-post.
        run = world["settlement_import"].run_tieout(
            INSTALLATION_ID,
            RunSettlementTieOut(
                import_id=result.batch.source_import_id,
                actor="tester",
                tenant_id=TENANT,
            ),
            company=COMPANY,
        )
        assert run.posted is True
        assert len(world["posting"].calls) == 1
        retried = world["service"].retry_posting(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        assert retried.posted is True
        assert retried.already_settled is True
        assert retried.batch.status == ManualImportBatchStatus.POSTED
        assert len(world["posting"].calls) == 1  # no second posting


# -- variance report + reads ----------------------------------------------------------------


class TestVarianceAndReads:
    """Variance report (expected vs imported vs posted) + batch reads."""

    def test_variance_statement_awaiting_review(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        report = world["service"].variance_report(
            tenant_id=TENANT, batch_id=result.batch.id
        )
        assert report.expected_net_irr == 750_000
        assert report.imported_net_irr == 750_000
        assert report.posted_net_irr == 0
        assert report.difference_irr == 750_000
        assert report.tieout_holds is None
        assert report.gate_allowed is None

    def test_variance_statement_posted(self, world: dict[str, Any]) -> None:
        result = _upload_statement(world)
        world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        report = world["service"].variance_report(
            tenant_id=TENANT, batch_id=result.batch.id
        )
        assert report.expected_net_irr == 750_000
        assert report.imported_net_irr == 750_000
        assert report.posted_net_irr == 750_000
        assert report.difference_irr == 0
        assert report.gate_allowed is True

    def test_variance_settlement_draft_hold(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            actor="operator",
        )
        report = world["service"].variance_report(
            tenant_id=TENANT, batch_id=result.batch.id
        )
        assert report.expected_net_irr == TRANCHE_NET
        assert report.imported_net_irr == TRANCHE_NET
        assert report.posted_net_irr == 0
        assert report.difference_irr == TRANCHE_NET
        assert report.posting_status == POSTING_DRAFT_HOLD

    def test_variance_settlement_posted(self, world: dict[str, Any]) -> None:
        _record_payment(world["commands"])
        result = _upload_settlement(world)
        _deposit(
            world,
            amount_irr=TRANCHE_NET,
            deposit_date=datetime.date(2026, 11, 7),
        )
        world["service"].approve_batch(
            tenant_id=TENANT,
            batch_id=result.batch.id,
            company=COMPANY,
            auto_post=True,
            actor="operator",
        )
        report = world["service"].variance_report(
            tenant_id=TENANT, batch_id=result.batch.id
        )
        assert report.expected_net_irr == TRANCHE_NET
        assert report.imported_net_irr == TRANCHE_NET
        assert report.posted_net_irr == TRANCHE_NET
        assert report.difference_irr == 0
        assert report.tieout_holds is True
        assert report.gate_allowed is True

    def test_get_missing_batch_is_404(self, world: dict[str, Any]) -> None:
        with pytest.raises(ManualImportNotFoundError):
            world["service"].get_batch(TENANT, "no-such-batch")

    def test_list_batches_filters_by_kind_and_status(
        self, world: dict[str, Any]
    ) -> None:
        statement = _upload_statement(world)
        _record_payment(world["commands"])
        settlement = _upload_settlement(world)
        assert len(world["service"].list_batches(TENANT)) == 2
        statements = world["service"].list_batches(
            TENANT, kind=ManualImportKind.STATEMENT
        )
        assert [b.id for b in statements] == [statement.batch.id]
        settlements = world["service"].list_batches(
            TENANT, kind=ManualImportKind.SETTLEMENT
        )
        assert [b.id for b in settlements] == [settlement.batch.id]
        awaiting = world["service"].list_batches(
            TENANT, status=ManualImportBatchStatus.AWAITING_REVIEW
        )
        assert len(awaiting) == 2
        posted = world["service"].list_batches(
            TENANT, status=ManualImportBatchStatus.POSTED
        )
        assert posted == []
        by_source = world["service"].list_batches(
            TENANT, source_import_id=statement.batch.source_import_id
        )
        assert [b.id for b in by_source] == [statement.batch.id]
