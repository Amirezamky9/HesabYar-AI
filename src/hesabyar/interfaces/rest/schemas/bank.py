"""Request/response schemas for the bank import + reconcile endpoints (US-514, B1/B2).

Money follows the §9.3 contract: integer Rial amounts, never floats,
never Toman on the wire — a file whose unit is Toman is converted
(×10) at the import boundary and the batch records the source unit.
Dates are ISO Gregorian (``YYYY-MM-DD``).

Uploads are base64 JSON, not multipart: the admin BFF proxy parses
request bodies as JSON only, so the file travels as a base64 string
inside the request body (max 10 MB decoded).
"""

from __future__ import annotations

import base64
import binascii
import datetime

from pydantic import BaseModel, Field, field_validator

from hesabyar.application.commands.bank_import import FileDetection
from hesabyar.domain.accounting.value_objects import StatementJournalView
from hesabyar.domain.bank.models import (
    MAX_IMPORT_ROWS,
    BankColumnMapping,
    BankImportBatch,
    BankImportRow,
    BankMatchRule,
    BankReconciliationRow,
    BankReconciliationStatus,
    BankStatementMatch,
    MatchSuggestion,
    ReconcileLineResult,
    ReconcileResult,
)

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


class DetectFileRequest(BaseModel):
    """Upload a statement file for format/column detection (no persistence)."""

    filename: str = Field(description="Original file name (used for the format sniff)")
    content_base64: str = Field(description="File bytes, base64-encoded (max 10 MB decoded)")

    @field_validator("content_base64")
    @classmethod
    def _check_base64(cls, value: str) -> str:
        _decode_base64(value)
        return value

    def content_bytes(self) -> bytes:
        return _decode_base64(self.content_base64)


class FileDetectionResponse(BaseModel):
    """What detection found: format, columns, a sample, and a suggested mapping."""

    file_format: str = Field(description="Detected format: 'xlsx', 'csv' or 'pdf'")
    encoding: str | None = Field(default=None, description="Detected text encoding (CSV only)")
    columns: list[str] = Field(description="Header-row column names, in file order")
    sample_rows: list[list[str | None]] = Field(description="First five data rows")
    suggested_mapping: dict[str, str] = Field(
        description="Suggested canonical-field → column mapping"
    )
    missing_fields: list[str] = Field(
        description="Canonical fields the suggested mapping does not cover"
    )
    detection_notes: list[str] = Field(description="Reader notes (e.g. encoding fallback)")

    @classmethod
    def from_detection(cls, detection: FileDetection) -> FileDetectionResponse:
        return cls(
            file_format=detection.file_format.value,
            encoding=detection.encoding,
            columns=list(detection.columns),
            sample_rows=[list(row) for row in detection.sample_rows],
            suggested_mapping=dict(detection.suggested_mapping),
            missing_fields=list(detection.missing_fields),
            detection_notes=list(detection.detection_notes),
        )


class ColumnMappingCreateRequest(BaseModel):
    """Save a per-bank column mapping."""

    bank_key: str = Field(description="Bank key, e.g. 'melli'")
    name: str = Field(description="Operator-visible mapping name")
    file_format: str = Field(description="File format: 'xlsx', 'csv' or 'pdf'")
    columns: dict[str, str] = Field(
        description="Canonical field → source column (header name or '#<index>')"
    )
    date_kind: str = Field(default="jalali", description="'jalali' or 'gregorian'")
    currency_unit: str = Field(default="rial", description="File amounts unit: 'rial' or 'toman'")
    header_row: int = Field(default=0, ge=0, description="0-based header row index")
    company: str | None = Field(
        default=None, description="HesabYar company row id (None = all companies)"
    )


class ColumnMappingResponse(BaseModel):
    """A saved per-bank column mapping."""

    id: str = Field(description="Mapping UUID")
    bank_key: str = Field(description="Bank key, e.g. 'melli'")
    name: str = Field(description="Operator-visible mapping name")
    file_format: str = Field(description="File format: 'xlsx', 'csv' or 'pdf'")
    columns: dict[str, str] = Field(description="Canonical field → source column")
    date_kind: str = Field(description="'jalali' or 'gregorian'")
    currency_unit: str = Field(description="File amounts unit: 'rial' or 'toman'")
    header_row: int = Field(description="0-based header row index")
    company_id: str | None = Field(default=None, description="Company row id, or None (all)")
    status: str = Field(description="'active' or 'superseded'")
    created_at: datetime.datetime | None = Field(default=None)
    updated_at: datetime.datetime | None = Field(default=None)

    @classmethod
    def from_mapping(cls, mapping: BankColumnMapping) -> ColumnMappingResponse:
        return cls(
            id=mapping.id,
            bank_key=mapping.bank_key,
            name=mapping.name,
            file_format=mapping.file_format.value,
            columns=dict(mapping.columns),
            date_kind=mapping.date_kind,
            currency_unit=mapping.currency_unit.value,
            header_row=mapping.header_row,
            company_id=mapping.company_id,
            status=mapping.status.value,
            created_at=mapping.created_at,
            updated_at=mapping.updated_at,
        )


class CreateImportRequest(BaseModel):
    """Create an import batch from an uploaded statement file."""

    company: str = Field(description="HesabYar company row id")
    bank_key: str = Field(description="Bank key, e.g. 'melli'")
    statement_journal_id: int = Field(description="Engine id of the statement journal")
    filename: str = Field(description="Original file name (stored for provenance)")
    content_base64: str = Field(description="File bytes, base64-encoded (max 10 MB decoded)")
    currency_unit: str = Field(
        default="rial", description="File amounts unit: 'rial' or 'toman'"
    )
    default_account_id: int = Field(
        description="Engine id of the default (suspense) account for uncoded rows"
    )
    mapping_id: str | None = Field(
        default=None, description="Saved column mapping id (or inline 'columns')"
    )
    columns: dict[str, str] | None = Field(
        default=None, description="Inline canonical-field → column mapping"
    )
    date_kind: str = Field(default="jalali", description="'jalali' or 'gregorian'")
    header_row: int = Field(default=0, ge=0, description="0-based header row index")

    @field_validator("content_base64")
    @classmethod
    def _check_base64(cls, value: str) -> str:
        _decode_base64(value)
        return value

    def content_bytes(self) -> bytes:
        return _decode_base64(self.content_base64)


class BankImportRowResponse(BaseModel):
    """One parsed row of an import batch (the accounting-effect preview)."""

    id: str = Field(description="Row UUID")
    row_number: int = Field(description="1-based row number in the file")
    row_date: datetime.date | None = Field(default=None, description="Row date (Gregorian ISO)")
    amount_irr: int | None = Field(
        default=None, description="Signed amount in integer Rial (positive = money in)"
    )
    description: str = Field(description="Row description")
    bank_ref: str | None = Field(default=None, description="Bank reference, when present")
    balance_irr: int | None = Field(default=None, description="Running balance, when present")
    is_duplicate: bool = Field(description="True when the row duplicates an earlier import")
    duplicate_reason: str | None = Field(
        default=None, description="'bank_ref', 'match_key', or None"
    )
    parse_errors: list[str] = Field(description="Parse problems (empty when the row is valid)")
    was_rounded: bool = Field(description="True when a Toman→Rial conversion rounded")
    suggested_account_id: int | None = Field(
        default=None, description="Preview coding: engine account id"
    )
    suggested_party_id: int | None = Field(
        default=None, description="Preview coding: engine party id, when known"
    )
    suggested_invoice_id: int | None = Field(
        default=None, description="Preview coding: engine invoice id, when known"
    )
    suggestion_source: str | None = Field(
        default=None, description="'rule', 'default', or None"
    )

    @classmethod
    def from_row(cls, row: BankImportRow) -> BankImportRowResponse:
        return cls(
            id=row.id,
            row_number=row.row_number,
            row_date=row.row_date.gregorian_date if row.row_date is not None else None,
            amount_irr=row.amount_irr,
            description=row.description,
            bank_ref=row.bank_ref,
            balance_irr=row.balance_irr,
            is_duplicate=row.is_duplicate,
            duplicate_reason=row.duplicate_reason,
            parse_errors=list(row.parse_errors),
            was_rounded=row.was_rounded,
            suggested_account_id=row.suggested_account_id,
            suggested_party_id=row.suggested_party_id,
            suggested_invoice_id=row.suggested_invoice_id,
            suggestion_source=row.suggestion_source,
        )


class BankImportBatchResponse(BaseModel):
    """An import batch (summary; rows come from the detail endpoint)."""

    id: str = Field(description="Batch UUID (the import_batch_id)")
    company_id: str = Field(description="HesabYar company row id")
    bank_key: str = Field(description="Bank key, e.g. 'melli'")
    statement_journal_id: int = Field(description="Engine id of the statement journal")
    filename: str = Field(description="Original file name")
    file_format: str = Field(description="'xlsx', 'csv' or 'pdf'")
    currency_unit: str = Field(description="Source file amounts unit: 'rial' or 'toman'")
    status: str = Field(description="'awaiting_review', 'confirmed' or 'cancelled'")
    total_rows: int = Field(description="Rows parsed from the file")
    new_rows: int = Field(description="Valid, non-duplicate rows")
    duplicate_rows: int = Field(description="Rows skipped as duplicates")
    invalid_rows: int = Field(description="Rows with parse errors")
    total_deposits_irr: int = Field(description="Sum of new deposits in integer Rial")
    total_withdrawals_irr: int = Field(description="Sum of new withdrawals in integer Rial")
    default_account_id: int = Field(description="Engine id of the default (suspense) account")
    column_mapping_id: str | None = Field(default=None, description="Saved mapping id, if used")
    start_balance_irr: int | None = Field(default=None)
    end_balance_irr: int | None = Field(default=None)
    engine_statement_id: int | None = Field(
        default=None, description="Engine statement id, once confirmed"
    )
    review_item_id: str | None = Field(
        default=None, description="manual_import_review queue item id"
    )
    created_at: datetime.datetime | None = Field(default=None)
    confirmed_at: datetime.datetime | None = Field(default=None)
    cancelled_at: datetime.datetime | None = Field(default=None)
    max_rows: int = Field(
        default=MAX_IMPORT_ROWS, description="Bank-export row cap this import was checked against"
    )

    @classmethod
    def from_batch(cls, batch: BankImportBatch) -> BankImportBatchResponse:
        return cls(
            id=batch.id,
            company_id=batch.company_id,
            bank_key=batch.bank_key,
            statement_journal_id=batch.statement_journal_id,
            filename=batch.filename,
            file_format=batch.file_format.value,
            currency_unit=batch.currency_unit.value,
            status=batch.status.value,
            total_rows=batch.total_rows,
            new_rows=batch.new_rows,
            duplicate_rows=batch.duplicate_rows,
            invalid_rows=batch.invalid_rows,
            total_deposits_irr=batch.total_deposits_irr,
            total_withdrawals_irr=batch.total_withdrawals_irr,
            default_account_id=batch.default_account_id,
            column_mapping_id=batch.column_mapping_id,
            start_balance_irr=batch.start_balance_irr,
            end_balance_irr=batch.end_balance_irr,
            engine_statement_id=batch.engine_statement_id,
            review_item_id=batch.review_item_id,
            created_at=batch.created_at,
            confirmed_at=batch.confirmed_at,
            cancelled_at=batch.cancelled_at,
        )


class BankImportBatchDetailResponse(BankImportBatchResponse):
    """An import batch with its parsed rows (the accounting-effect preview)."""

    rows: list[BankImportRowResponse] = Field(description="Parsed rows, in file order")

    @classmethod
    def from_batch_and_rows(
        cls, batch: BankImportBatch, rows: list[BankImportRow]
    ) -> BankImportBatchDetailResponse:
        # NB: call the BASE classmethod explicitly — from_batch builds
        # cls(...), and building the detail class here would fail on the
        # missing (not yet supplied) rows field.
        base = BankImportBatchResponse.from_batch(batch)
        return cls(**base.model_dump(), rows=[BankImportRowResponse.from_row(r) for r in rows])


class StatementJournalResponse(BaseModel):
    """A statement journal with its bank account (for the import form)."""

    id: int = Field(description="Engine id of the statement journal")
    name: str = Field(description="Journal name")
    account_id: int = Field(description="Engine id of the journal's account")
    validation: str = Field(description="'balance', 'amount' or 'number_of_lines'")
    currency_code: str = Field(description="Journal currency (ISO code)")
    bank_account_number: str | None = Field(
        default=None, description="Destination Shaba (IBAN), when the journal has a bank account"
    )

    @classmethod
    def from_view(cls, view: StatementJournalView) -> StatementJournalResponse:
        return cls(
            id=view.id,
            name=view.name,
            account_id=view.account.engine_id,
            validation=view.validation,
            currency_code=view.currency_code,
            bank_account_number=view.bank_account_number,
        )


class MatchSuggestionResponse(BaseModel):
    """One P3 smart suggestion for a line (a human approves)."""

    target_type: str = Field(description="'invoice', 'payment', 'rule' or 'manual'")
    target_id: int | str = Field(description="Engine id, or the rule UUID")
    target_label: str = Field(description="Operator-visible target label")
    confidence: int = Field(description="Confidence 0–100")
    reason: str = Field(description="Why this was suggested")

    @classmethod
    def from_suggestion(cls, suggestion: MatchSuggestion) -> MatchSuggestionResponse:
        return cls(
            target_type=suggestion.target_type.value,
            target_id=suggestion.target_id,
            target_label=suggestion.target_label,
            confidence=suggestion.confidence,
            reason=suggestion.reason,
        )


class ReconcileLineResultResponse(BaseModel):
    """One line's reconcile outcome."""

    line_id: int = Field(description="Engine id of the statement line")
    line_date: datetime.date = Field(description="Line date (Gregorian ISO)")
    amount_irr: int = Field(description="Signed line amount in integer Rial")
    description: str = Field(description="Line description")
    outcome: str = Field(
        description="'already_matched', 'matched', 'suggested', 'ambiguous', "
        "'unmatched' or 'skipped_zero'"
    )
    priority: int | None = Field(default=None, description="Matching priority 1–3, when decided")
    confidence: int | None = Field(default=None, description="Confidence 0–100, when decided")
    target_type: str | None = Field(default=None)
    target_id: int | str | None = Field(default=None)
    target_label: str | None = Field(default=None)
    suggestions: list[MatchSuggestionResponse] = Field(default_factory=list)
    review_item_id: str | None = Field(
        default=None, description="bank_unmatched queue item id, when filed"
    )

    @classmethod
    def from_result(cls, result: ReconcileLineResult) -> ReconcileLineResultResponse:
        return cls(
            line_id=result.line_id,
            line_date=result.line_date.gregorian_date,
            amount_irr=result.amount_irr,
            description=result.description,
            outcome=result.outcome.value,
            priority=result.priority,
            confidence=result.confidence,
            target_type=result.target_type.value if result.target_type is not None else None,
            target_id=result.target_id,
            target_label=result.target_label,
            suggestions=[MatchSuggestionResponse.from_suggestion(s) for s in result.suggestions],
            review_item_id=result.review_item_id,
        )


class ReconcileResponse(BaseModel):
    """Summary of one reconcile run over a statement."""

    statement_id: int = Field(description="Engine id of the reconciled statement")
    company_id: str = Field(description="HesabYar company row id")
    total_lines: int = Field(description="Lines on the statement")
    already_matched_count: int = Field(description="Lines with a live match before this run")
    matched_count: int = Field(description="Lines auto-matched by this run")
    priority1_count: int = Field(description="Matched on exact reference")
    priority2_count: int = Field(description="Matched on amount + date + direction")
    suggested_count: int = Field(description="Lines with a human-approval suggestion")
    ambiguous_count: int = Field(description="Lines matching several candidates")
    unmatched_count: int = Field(description="Lines with no suggestion at all")
    matched_total_irr: int = Field(description="Absolute matched amount in integer Rial")
    unmatched_total_irr: int = Field(description="Absolute unmatched+ambiguous amount in Rial")
    review_items_filed: int = Field(description="bank_unmatched items filed by this run")
    lines: list[ReconcileLineResultResponse] = Field(description="Per-line outcomes")

    @classmethod
    def from_result(cls, result: ReconcileResult) -> ReconcileResponse:
        return cls(
            statement_id=result.statement_id,
            company_id=result.company_id,
            total_lines=result.total_lines,
            already_matched_count=result.already_matched_count,
            matched_count=result.matched_count,
            priority1_count=result.priority1_count,
            priority2_count=result.priority2_count,
            suggested_count=result.suggested_count,
            ambiguous_count=result.ambiguous_count,
            unmatched_count=result.unmatched_count,
            matched_total_irr=result.matched_total_irr,
            unmatched_total_irr=result.unmatched_total_irr,
            review_items_filed=result.review_items_filed,
            lines=[ReconcileLineResultResponse.from_result(line) for line in result.lines],
        )


class ReconcileRequest(BaseModel):
    """Options for a reconcile run (all optional)."""

    window_days: int | None = Field(
        default=None,
        ge=0,
        description="P2 date window in days (default: the installation setting, else 2)",
    )


class ManualMatchRequest(BaseModel):
    """Apply a human-confirmed match to one draft statement line."""

    target_type: str = Field(description="'invoice', 'payment' or 'manual'")
    target_id: str = Field(
        description="Engine id of the invoice/payment (as text), or 'manual'"
    )
    account_id: int | None = Field(
        default=None,
        description="Coding account (required for 'payment' and 'manual' targets)",
    )
    party_id: int | None = Field(
        default=None, description="Party override for a 'manual' match"
    )
    save_as_rule: bool = Field(
        default=False, description="Learn this mapping as a rule for future lines"
    )
    review_item_id: str | None = Field(
        default=None, description="bank_unmatched item this match resolves"
    )


class BankStatementMatchResponse(BaseModel):
    """A recorded reconciliation match for one statement line."""

    id: str = Field(description="Match UUID")
    statement_id: int = Field(description="Engine id of the statement")
    statement_line_id: int = Field(description="Engine id of the statement line")
    line_date: datetime.date = Field(description="Line date (Gregorian ISO)")
    line_amount_irr: int = Field(description="Signed line amount in integer Rial")
    line_description: str = Field(description="Line description at match time")
    target_type: str = Field(description="'invoice', 'payment', 'rule' or 'manual'")
    target_id: int | None = Field(default=None, description="Engine target id (None for rules)")
    target_ref: str = Field(description="Stable target key: engine id, or the rule UUID")
    target_label: str = Field(description="Operator-visible target label")
    priority: int = Field(description="Matching priority 1–3")
    confidence: int = Field(description="Confidence 0–100")
    status: str = Field(
        description="'auto_applied', 'suggested', 'confirmed', 'rejected' or 'unmatched'"
    )
    outcome: str | None = Field(
        default=None, description="Reconcile outcome that produced the record (None = manual)"
    )
    destination_account_id: int | None = Field(default=None)
    destination_shaba: str | None = Field(default=None)
    review_item_id: str | None = Field(default=None)
    matched_at: datetime.datetime | None = Field(default=None)
    matched_by: str | None = Field(default=None)

    @classmethod
    def from_match(cls, match: BankStatementMatch) -> BankStatementMatchResponse:
        return cls(
            id=match.id,
            statement_id=match.statement_id,
            statement_line_id=match.statement_line_id,
            line_date=match.line_date.gregorian_date,
            line_amount_irr=match.line_amount_irr,
            line_description=match.line_description,
            target_type=match.target_type.value,
            target_id=match.target_id,
            target_ref=match.target_ref,
            target_label=match.target_label,
            priority=match.priority,
            confidence=match.confidence,
            status=match.status.value,
            outcome=match.outcome.value if match.outcome is not None else None,
            destination_account_id=match.destination_account_id,
            destination_shaba=match.destination_shaba,
            review_item_id=match.review_item_id,
            matched_at=match.matched_at,
            matched_by=match.matched_by,
        )


class MatchRuleResponse(BaseModel):
    """A learned match rule (Suggest → Auto)."""

    id: str = Field(description="Rule UUID")
    bank_key: str = Field(description="Bank key, or '' for any bank")
    pattern: str = Field(description="Description substring this rule matches")
    direction: str = Field(description="'in', 'out' or 'any'")
    target_account_id: int = Field(description="Engine account id the rule codes to")
    target_party_id: int | None = Field(default=None, description="Engine party id, when known")
    target_kind: str = Field(description="'account', 'invoice' or 'payment'")
    mode: str = Field(description="'suggest' or 'auto'")
    confirmations: int = Field(description="Times a human confirmed this mapping")
    created_at: datetime.datetime | None = Field(default=None)
    updated_at: datetime.datetime | None = Field(default=None)

    @classmethod
    def from_rule(cls, rule: BankMatchRule) -> MatchRuleResponse:
        return cls(
            id=rule.id,
            bank_key=rule.bank_key,
            pattern=rule.pattern,
            direction=rule.direction,
            target_account_id=rule.target_account_id,
            target_party_id=rule.target_party_id,
            target_kind=rule.target_kind,
            mode=rule.mode.value,
            confirmations=rule.confirmations,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )


class RuleModeRequest(BaseModel):
    """Promote/demote a learned rule between Suggest and Auto."""

    mode: str = Field(description="'suggest' or 'auto'")


class ReconciliationRowResponse(BaseModel):
    """One bank account's reconciliation status in a period (§11.2)."""

    bank_key: str | None = Field(default=None, description="Bank key, when the journal came from an import")
    journal_id: int = Field(description="Engine id of the statement journal")
    journal_name: str = Field(description="Journal name")
    destination_shaba: str | None = Field(default=None)
    statements_count: int = Field(description="Statements with lines in the period")
    lines_total: int = Field(description="Lines in the period")
    matched_lines: int = Field(description="Lines matched to an invoice/payment")
    suggested_lines: int = Field(description="Lines with a suggestion awaiting approval")
    ambiguous_lines: int = Field(description="Lines matching several candidates")
    unmatched_lines: int = Field(description="Lines with no match and no suggestion")
    match_rate: float | None = Field(default=None, description="matched / total, or None when empty")
    unmatched_balance_irr: int = Field(description="Signed sum of unmatched line amounts in Rial")
    oldest_unmatched_age_days: int | None = Field(default=None)
    oldest_unmatched_line_id: int | None = Field(default=None)

    @classmethod
    def from_row(cls, row: BankReconciliationRow) -> ReconciliationRowResponse:
        return cls(
            bank_key=row.bank_key,
            journal_id=row.journal_id,
            journal_name=row.journal_name,
            destination_shaba=row.destination_shaba,
            statements_count=row.statements_count,
            lines_total=row.lines_total,
            matched_lines=row.matched_lines,
            suggested_lines=row.suggested_lines,
            ambiguous_lines=row.ambiguous_lines,
            unmatched_lines=row.unmatched_lines,
            match_rate=row.match_rate,
            unmatched_balance_irr=row.unmatched_balance_irr,
            oldest_unmatched_age_days=row.oldest_unmatched_age_days,
            oldest_unmatched_line_id=row.oldest_unmatched_line_id,
        )


class ReconciliationStatusResponse(BaseModel):
    """Bank reconciliation status report (spec §11.2, with §11.1 freshness)."""

    company_id: str = Field(description="HesabYar company row id")
    date_from: datetime.date = Field(description="Period start (Gregorian ISO)")
    date_to: datetime.date = Field(description="Period end (Gregorian ISO)")
    generated_at: datetime.datetime = Field(description="Report generation time (UTC)")
    source: str = Field(description="Data sources feeding the report")
    source_timestamp: datetime.datetime | None = Field(
        default=None, description="Newest source record feeding the report"
    )
    report_version: str = Field(description="Report format version")
    calculation_version: str = Field(description="Calculation version")
    freshness: str = Field(description="'live' when a source exists, else 'empty'")
    rows: list[ReconciliationRowResponse] = Field(description="Per-journal rows")
    statements_count: int = Field(description="Statements with lines in the period")
    lines_total: int = Field(description="Lines in the period")
    matched_lines: int = Field(description="Matched lines")
    suggested_lines: int = Field(description="Lines with a suggestion awaiting approval")
    ambiguous_lines: int = Field(description="Lines matching several candidates")
    unmatched_lines: int = Field(description="Lines with no match and no suggestion")
    match_rate: float | None = Field(default=None, description="matched / total, or None when empty")
    unmatched_balance_irr: int = Field(description="Signed sum of unmatched line amounts in Rial")

    @classmethod
    def from_status(cls, status: BankReconciliationStatus) -> ReconciliationStatusResponse:
        return cls(
            company_id=status.company_id,
            date_from=status.date_from.gregorian_date,
            date_to=status.date_to.gregorian_date,
            generated_at=status.generated_at,
            source=status.source,
            source_timestamp=status.source_timestamp,
            report_version=status.report_version,
            calculation_version=status.calculation_version,
            freshness=status.freshness,
            rows=[ReconciliationRowResponse.from_row(row) for row in status.rows],
            statements_count=status.statements_count,
            lines_total=status.lines_total,
            matched_lines=status.matched_lines,
            suggested_lines=status.suggested_lines,
            ambiguous_lines=status.ambiguous_lines,
            unmatched_lines=status.unmatched_lines,
            match_rate=status.match_rate,
            unmatched_balance_irr=status.unmatched_balance_irr,
        )
