"""REST schemas for the unified manual-import batches (US-554).

One batch cycle for statements + settlements: detect, upload,
preview, approve, reject, re-run, list, variance report — plus the
per-channel settlement column mappings.
"""

from __future__ import annotations

import base64
import binascii
import datetime

from pydantic import BaseModel, Field, field_validator

from hesabyar.application.commands.manual_imports import (
    ApproveBatchResult,
    RetryPostingResult,
    UploadBatchResult,
)
from hesabyar.domain.manual_imports.models import (
    BatchVarianceReport,
    ImportBatchPreview,
    ManualImportBatch,
    ManualImportStage,
    SettlementColumnMapping,
)
from hesabyar.domain.settlement.matching import TieOutResult

#: Decoded upload size cap (matches the file reader's MAX_FILE_BYTES).
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _decode_base64(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("content_base64 is not valid base64") from exc
    if len(decoded) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")
    return decoded


class DetectImportFileRequest(BaseModel):
    """Upload a file for format/column detection (no persistence)."""

    kind: str = Field(description="Batch kind: 'statement' or 'settlement'")
    filename: str = Field(description="Original file name (used for the format sniff)")
    content_base64: str = Field(description="File bytes, base64-encoded (max 10 MB decoded)")
    file_type: str = Field(
        default="detail",
        description="Settlement file kind: 'detail', 'payouts' or 'statement' (settlements only)",
    )

    @field_validator("content_base64")
    @classmethod
    def _check_base64(cls, value: str) -> str:
        _decode_base64(value)
        return value

    def content_bytes(self) -> bytes:
        return _decode_base64(self.content_base64)


class ImportBatchUploadRequest(BaseModel):
    """Upload a statement or settlement file into a batch awaiting review."""

    kind: str = Field(description="Batch kind: 'statement' or 'settlement'")
    source_key: str = Field(
        description="Bank key (statements, e.g. 'melli') or channel key (settlements, e.g. 'snapppay')"
    )
    company: str = Field(description="HesabYar company row id")
    filename: str = Field(description="Original file name (used for the format sniff)")
    content_base64: str = Field(description="File bytes, base64-encoded (max 10 MB decoded)")
    currency_unit: str = Field(
        default="rial",
        description="Per-import currency setting: 'rial' or 'toman' (Toman ×10 at the boundary)",
    )
    date_kind: str = Field(default="jalali", description="'jalali' or 'gregorian'")
    header_row: int = Field(default=0, ge=0, description="0-based header row index")
    mapping_id: str | None = Field(
        default=None,
        description="Saved column mapping id (bank mapping for statements, settlement mapping for settlements)",
    )
    columns: dict[str, str] | None = Field(
        default=None,
        description="Inline column mapping: canonical field → header text or '#<index>'",
    )
    save_mapping: bool = Field(
        default=False,
        description="Save the inline mapping for this source so the next import reuses it",
    )
    mapping_name: str | None = Field(
        default=None, description="Name for the saved mapping (defaults to '<source> — <filename>')"
    )
    statement_journal_id: int | None = Field(
        default=None, description="Engine statement journal id (statements only)"
    )
    default_account_id: int | None = Field(
        default=None, description="Engine account id for uncoded rows (statements only)"
    )
    file_type: str = Field(
        default="detail",
        description="Settlement file kind: 'detail', 'payouts' or 'statement' (settlements only)",
    )
    period_jalali: str | None = Field(
        default=None, description="Jalali period the file covers (YYYY/MM; settlements only)"
    )

    @field_validator("content_base64")
    @classmethod
    def _check_base64(cls, value: str) -> str:
        _decode_base64(value)
        return value

    def content_bytes(self) -> bytes:
        return _decode_base64(self.content_base64)


class BatchStageResponse(BaseModel):
    """One recorded stage transition (the batch's audit trail)."""

    stage: str = Field(description="Batch status value entered")
    at: datetime.datetime = Field(description="When the batch entered the stage (UTC)")
    note: str | None = Field(default=None, description="Free-text note (e.g. the rejection reason)")

    @classmethod
    def from_stage(cls, stage: ManualImportStage) -> BatchStageResponse:
        return cls(stage=stage.stage, at=stage.at, note=stage.note)


class ImportBatchPreviewResponse(BaseModel):
    """The accounting-effect preview (gross/fee/tax/refunds/net/receivable/deposit)."""

    gross_irr: int = Field(description="Preview gross in whole rials")
    fee_irr: int = Field(description="Preview fee in whole rials (0 for statements)")
    tax_irr: int = Field(description="Preview tax on the fee in whole rials (0 for statements)")
    refunds_irr: int = Field(description="Preview refunds in whole rials (0 for statements)")
    net_irr: int = Field(description="Preview net in whole rials")
    deductions_irr: int = Field(description="Preview documented deductions (0 for statements)")
    receivable_before_irr: int | None = Field(
        default=None, description="Channel receivable before the batch (null for statements)"
    )
    receivable_after_irr: int | None = Field(
        default=None, description="Channel receivable after the batch (null for statements)"
    )
    expected_deposit_irr: int = Field(description="Expected bank deposit in whole rials")
    matched_rows: int = Field(description="Rows matched to existing tranches/payments")
    new_rows: int = Field(description="New rows (statement rows / new payments)")
    duplicate_rows: int = Field(description="Rows skipped as duplicates")
    invalid_rows: int = Field(description="Rows that failed parsing")
    dropped_rows: int = Field(description="Summary rows dropped at parse (settlements only)")

    @classmethod
    def from_preview(cls, preview: ImportBatchPreview) -> ImportBatchPreviewResponse:
        return cls(
            gross_irr=preview.gross_irr,
            fee_irr=preview.fee_irr,
            tax_irr=preview.tax_irr,
            refunds_irr=preview.refunds_irr,
            net_irr=preview.net_irr,
            deductions_irr=preview.deductions_irr,
            receivable_before_irr=preview.receivable_before_irr,
            receivable_after_irr=preview.receivable_after_irr,
            expected_deposit_irr=preview.expected_deposit_irr,
            matched_rows=preview.matched_rows,
            new_rows=preview.new_rows,
            duplicate_rows=preview.duplicate_rows,
            invalid_rows=preview.invalid_rows,
            dropped_rows=preview.dropped_rows,
        )


class ManualImportBatchResponse(BaseModel):
    """One manual-import batch: cycle state, preview, stages, posting status."""

    id: str = Field(description="Batch UUID")
    kind: str = Field(description="'statement' or 'settlement'")
    source_key: str = Field(description="Bank key or channel key")
    company_id: str = Field(description="HesabYar company row id")
    filename: str = Field(description="Uploaded filename")
    file_format: str = Field(description="'xlsx', 'csv' or 'pdf'")
    currency_unit: str = Field(description="'rial' or 'toman'")
    status: str = Field(description="Cycle state")
    attempt: int = Field(description="Upload attempt number (1 = original)")
    source_import_id: str | None = Field(
        default=None, description="Underlying import id (bank batch / settlement file import)"
    )
    source_import_batch_id: str | None = Field(
        default=None, description="Settlement import_batch_id (the tie-out key)"
    )
    column_mapping_id: str | None = Field(
        default=None, description="Saved mapping used (null = inline mapping)"
    )
    mapping_columns: dict[str, str] = Field(description="Applied mapping")
    file_type: str | None = Field(default=None, description="Settlement file kind")
    period_jalali: str | None = Field(default=None, description="Jalali period (settlements)")
    preview: ImportBatchPreviewResponse | None = Field(
        default=None, description="Accounting-effect preview"
    )
    review_item_id: str | None = Field(
        default=None, description="manual_import_review queue item id"
    )
    rejection_reason: str | None = Field(default=None, description="Why the batch was rejected")
    auto_post_opt_in: bool = Field(description="Explicit auto-post opt-in for this batch")
    posting_status: str | None = Field(
        default=None,
        description="Posting step: draft_hold | held_gate | tieout_failed | posting_failed | posted",
    )
    posting_detail: str | None = Field(default=None, description="Why the posting step held/failed")
    engine_ref_id: int | None = Field(default=None, description="Engine id written on posting")
    stages: list[BatchStageResponse] = Field(description="Stage audit trail")
    created_at: datetime.datetime | None = Field(default=None)
    updated_at: datetime.datetime | None = Field(default=None)
    approved_at: datetime.datetime | None = Field(default=None)
    posted_at: datetime.datetime | None = Field(default=None)
    rejected_at: datetime.datetime | None = Field(default=None)
    archived_at: datetime.datetime | None = Field(default=None)

    @classmethod
    def from_batch(cls, batch: ManualImportBatch) -> ManualImportBatchResponse:
        return cls(
            id=batch.id,
            kind=batch.kind.value,
            source_key=batch.source_key,
            company_id=batch.company_id,
            filename=batch.filename,
            file_format=batch.file_format.value,
            currency_unit=batch.currency_unit.value,
            status=batch.status.value,
            attempt=batch.attempt,
            source_import_id=batch.source_import_id,
            source_import_batch_id=batch.source_import_batch_id,
            column_mapping_id=batch.column_mapping_id,
            mapping_columns=dict(batch.mapping_columns),
            file_type=batch.file_type,
            period_jalali=batch.period_jalali,
            preview=(
                ImportBatchPreviewResponse.from_preview(batch.preview)
                if batch.preview is not None
                else None
            ),
            review_item_id=batch.review_item_id,
            rejection_reason=batch.rejection_reason,
            auto_post_opt_in=batch.auto_post_opt_in,
            posting_status=batch.posting_status,
            posting_detail=batch.posting_detail,
            engine_ref_id=batch.engine_ref_id,
            stages=[BatchStageResponse.from_stage(stage) for stage in batch.stages],
            created_at=batch.created_at,
            updated_at=batch.updated_at,
            approved_at=batch.approved_at,
            posted_at=batch.posted_at,
            rejected_at=batch.rejected_at,
            archived_at=batch.archived_at,
        )


class UploadBatchResponse(BaseModel):
    """The outcome of an upload: the batch + its preview."""

    batch: ManualImportBatchResponse = Field(description="The batch (born awaiting_review)")
    preview: ImportBatchPreviewResponse = Field(description="Accounting-effect preview")
    duplicate: bool = Field(
        default=False, description="True when the file was already imported (a no-op re-upload)"
    )

    @classmethod
    def from_result(cls, result: UploadBatchResult) -> UploadBatchResponse:
        return cls(
            batch=ManualImportBatchResponse.from_batch(result.batch),
            preview=ImportBatchPreviewResponse.from_preview(result.preview),
            duplicate=result.duplicate,
        )


class TieOutFiguresResponse(BaseModel):
    """Tie-out figures (read-only recompute of the C6 tie-out)."""

    holds: bool = Field(description="True when the tie-out holds within tolerance")
    confirmed_total_irr: int = Field(description="Σ confirmed tranche nets")
    deductions_irr: int = Field(description="Σ documented period deductions")
    expected_deposits_irr: int = Field(description="Confirmed − deductions")
    matched_deposits_irr: int = Field(description="Σ allocated bank deposits")
    difference_irr: int = Field(description="Signed difference (expected − actual)")
    tolerance_irr: int = Field(description="Contract tie-out tolerance")

    @classmethod
    def from_tieout(cls, tieout: TieOutResult) -> TieOutFiguresResponse:
        return cls(
            holds=tieout.holds,
            confirmed_total_irr=tieout.confirmed_total_irr,
            deductions_irr=tieout.deductions_irr,
            expected_deposits_irr=tieout.expected_deposits_irr,
            matched_deposits_irr=tieout.matched_deposits_irr,
            difference_irr=tieout.difference_irr,
            tolerance_irr=tieout.tolerance_irr,
        )


class ApproveBatchRequest(BaseModel):
    """Approve a batch (draft by default; auto-post is an explicit opt-in)."""

    auto_post: bool = Field(
        default=False,
        description="Explicit opt-in: run the tie-out + auto-post after approval (settlements only)",
    )


class ApproveBatchResponse(BaseModel):
    """The outcome of approving a batch."""

    batch: ManualImportBatchResponse = Field(description="The batch after approval")
    posted: bool = Field(description="True when the engine write happened")
    tryton_journal_id: int | None = Field(
        default=None, description="Posted journal/statement engine id (when posted)"
    )
    settlement_event_id: str | None = Field(
        default=None, description="Settlement event id (settlements only)"
    )
    tieout: TieOutFiguresResponse | None = Field(
        default=None, description="Tie-out figures (settlements, when the tie-out ran)"
    )
    gate_reason: str | None = Field(default=None, description="Policy-gate verdict (when evaluated)")
    confirmed_tranche_ids: list[str] = Field(default_factory=list)
    created_payment_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: ApproveBatchResult) -> ApproveBatchResponse:
        return cls(
            batch=ManualImportBatchResponse.from_batch(result.batch),
            posted=result.posted,
            tryton_journal_id=result.tryton_journal_id,
            settlement_event_id=result.settlement_event_id,
            tieout=(
                TieOutFiguresResponse.from_tieout(result.tieout)
                if result.tieout is not None
                else None
            ),
            gate_reason=result.gate_reason,
            confirmed_tranche_ids=list(result.confirmed_tranche_ids),
            created_payment_ids=list(result.created_payment_ids),
        )


class RejectBatchRequest(BaseModel):
    """Reject a batch (a reason is required — the batch is archived with it)."""

    reason: str = Field(min_length=1, description="Why the batch is rejected")


class RetryPostingResponse(BaseModel):
    """The outcome of retrying the posting step of an approved batch."""

    batch: ManualImportBatchResponse = Field(description="The batch after the retry")
    posted: bool = Field(description="True when the engine write happened")
    tryton_journal_id: int | None = Field(
        default=None, description="Posted journal engine id (when posted)"
    )
    tieout: TieOutFiguresResponse | None = Field(
        default=None, description="Tie-out figures (when the tie-out ran)"
    )
    gate_reason: str | None = Field(default=None, description="Policy-gate verdict (when evaluated)")
    already_settled: bool = Field(
        default=False, description="True when the import had already settled (no re-post)"
    )

    @classmethod
    def from_result(cls, result: RetryPostingResult) -> RetryPostingResponse:
        return cls(
            batch=ManualImportBatchResponse.from_batch(result.batch),
            posted=result.posted,
            tryton_journal_id=result.tryton_journal_id,
            tieout=(
                TieOutFiguresResponse.from_tieout(result.tieout)
                if result.tieout is not None
                else None
            ),
            gate_reason=result.gate_reason,
            already_settled=result.already_settled,
        )


class BatchVarianceReportResponse(BaseModel):
    """Expected vs imported vs posted for one batch, with the tie-out result."""

    expected_net_irr: int = Field(description="Preview expected deposit/net in whole rials")
    imported_net_irr: int = Field(description="What the pipeline actually imported")
    posted_net_irr: int = Field(description="What reached the engine")
    difference_irr: int = Field(description="Imported − posted (money imported but not yet posted)")
    tieout_holds: bool | None = Field(
        default=None, description="Tie-out verdict (null for statements / tie-out not run)"
    )
    tieout_difference_irr: int | None = Field(default=None)
    tieout_tolerance_irr: int | None = Field(default=None)
    gate_allowed: bool | None = Field(default=None, description="Policy-gate verdict (when known)")
    gate_reason: str | None = Field(default=None)
    posting_status: str | None = Field(default=None)
    generated_at: datetime.datetime = Field(description="When the report was computed (UTC)")

    @classmethod
    def from_report(cls, report: BatchVarianceReport) -> BatchVarianceReportResponse:
        return cls(
            expected_net_irr=report.expected_net_irr,
            imported_net_irr=report.imported_net_irr,
            posted_net_irr=report.posted_net_irr,
            difference_irr=report.difference_irr,
            tieout_holds=report.tieout_holds,
            tieout_difference_irr=report.tieout_difference_irr,
            tieout_tolerance_irr=report.tieout_tolerance_irr,
            gate_allowed=report.gate_allowed,
            gate_reason=report.gate_reason,
            posting_status=report.posting_status,
            generated_at=report.generated_at,
        )


class SettlementColumnMappingCreateRequest(BaseModel):
    """Save a per-channel settlement column mapping."""

    channel_key: str = Field(description="Payment channel key (e.g. 'snapppay')")
    name: str = Field(description="Operator-visible mapping name")
    file_format: str = Field(description="File format: 'xlsx', 'csv' or 'pdf'")
    file_type: str = Field(description="Settlement file kind: 'detail', 'payouts' or 'statement'")
    columns: dict[str, str] = Field(
        description="Canonical settlement field → source column (header name or '#<index>')"
    )
    date_kind: str = Field(default="jalali", description="'jalali' or 'gregorian'")
    currency_unit: str = Field(default="rial", description="File amounts unit: 'rial' or 'toman'")
    header_row: int = Field(default=0, ge=0, description="0-based header row index")
    company: str | None = Field(
        default=None,
        description="HesabYar company row id when company-scoped (null = shared across companies)",
    )


class SettlementColumnMappingResponse(BaseModel):
    """One saved per-channel settlement column mapping."""

    id: str = Field(description="Mapping UUID")
    channel_key: str = Field(description="Payment channel key")
    name: str = Field(description="Operator-visible mapping name")
    file_format: str = Field(description="File format: 'xlsx', 'csv' or 'pdf'")
    file_type: str = Field(description="Settlement file kind: 'detail', 'payouts' or 'statement'")
    columns: dict[str, str] = Field(description="Canonical field → source column")
    date_kind: str = Field(description="'jalali' or 'gregorian'")
    currency_unit: str = Field(description="'rial' or 'toman'")
    header_row: int = Field(description="0-based header row index")
    company_id: str | None = Field(default=None, description="Company scope (null = shared)")
    status: str = Field(description="'active' or 'superseded'")
    created_at: datetime.datetime | None = Field(default=None)
    updated_at: datetime.datetime | None = Field(default=None)

    @classmethod
    def from_mapping(cls, mapping: SettlementColumnMapping) -> SettlementColumnMappingResponse:
        return cls(
            id=mapping.id,
            channel_key=mapping.channel_key,
            name=mapping.name,
            file_format=mapping.file_format.value,
            file_type=mapping.file_type,
            columns=dict(mapping.columns),
            date_kind=mapping.date_kind,
            currency_unit=mapping.currency_unit.value,
            header_row=mapping.header_row,
            company_id=mapping.company_id,
            status=mapping.status.value,
            created_at=mapping.created_at,
            updated_at=mapping.updated_at,
        )
