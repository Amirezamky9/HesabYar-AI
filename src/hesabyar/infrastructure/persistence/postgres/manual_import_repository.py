"""PostgreSQL implementation of the manual-import repository port (US-554).

Implements ``domain/manual_imports/ports.py``
(``ManualImportRepositoryPort``) over the ``manual_import_batches`` /
``settlement_column_mappings`` tables. This is the ONLY place that
knows these tables' SQLAlchemy models — the application layer talks
to the port, never to these tables (§6.3).

Persistence contract (same as every other writer in the codebase):
``save_*`` / ``update_*`` flush so ids/timestamps exist but do NOT
commit — the caller (REST route) owns the transaction.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.domain.bank.models import (
    ColumnMappingStatus,
    FileCurrencyUnit,
    StatementFileFormat,
)
from hesabyar.domain.manual_imports.models import (
    ImportBatchPreview,
    ManualImportBatch,
    ManualImportBatchStatus,
    ManualImportKind,
    ManualImportStage,
    SettlementColumnMapping,
)
from hesabyar.domain.manual_imports.ports import ManualImportRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    ManualImportBatchModel,
    SettlementColumnMappingModel,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _stages_from_json(raw: Any) -> tuple[ManualImportStage, ...]:
    if not isinstance(raw, list):
        return ()
    stages: list[ManualImportStage] = []
    for item in raw:
        if isinstance(item, dict):
            stages.append(ManualImportStage.from_dict(item))
    return tuple(stages)


def _batch_from_row(row: ManualImportBatchModel) -> ManualImportBatch:
    preview = None
    if isinstance(row.preview_json, dict):
        preview = ImportBatchPreview.from_dict(row.preview_json)
    return ManualImportBatch(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        company_id=row.company_id,
        kind=ManualImportKind(row.kind),
        source_key=row.source_key,
        filename=row.filename,
        file_format=StatementFileFormat(row.file_format),
        file_hash_sha256=row.file_hash_sha256,
        currency_unit=FileCurrencyUnit(row.currency_unit),
        status=ManualImportBatchStatus(row.status),
        attempt=row.attempt,
        date_kind=row.date_kind,
        header_row=row.header_row,
        source_import_id=row.source_import_id,
        source_import_batch_id=row.source_import_batch_id,
        column_mapping_id=row.column_mapping_id,
        mapping_columns=dict(row.mapping_columns or {}),
        file_type=row.file_type,
        period_jalali=row.period_jalali,
        statement_journal_id=row.statement_journal_id,
        default_account_id=row.default_account_id,
        preview=preview,
        review_item_id=row.review_item_id,
        rejection_reason=row.rejection_reason,
        auto_post_opt_in=row.auto_post_opt_in,
        posting_status=row.posting_status,
        posting_detail=row.posting_detail,
        engine_ref_id=row.engine_ref_id,
        stages=_stages_from_json(row.stages_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
        approved_at=row.approved_at,
        posted_at=row.posted_at,
        rejected_at=row.rejected_at,
        archived_at=row.archived_at,
    )


def _mapping_from_row(row: SettlementColumnMappingModel) -> SettlementColumnMapping:
    return SettlementColumnMapping(
        id=row.id,
        tenant_id=row.tenant_id,
        channel_key=row.channel_key,
        name=row.name,
        file_format=StatementFileFormat(row.file_format),
        file_type=row.file_type,
        columns=dict(row.columns),
        date_kind=row.date_kind,
        currency_unit=FileCurrencyUnit(row.currency_unit),
        header_row=row.header_row,
        company_id=row.company_id,
        status=ColumnMappingStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyManualImportRepository(ManualImportRepositoryPort):
    """SQLAlchemy manual-import repository (flush, never commit)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- batches -----------------------------------------------------------

    def save_batch(self, batch: ManualImportBatch) -> ManualImportBatch:
        now = _now()
        row = ManualImportBatchModel(
            id=batch.id or str(uuid.uuid4()),
            tenant_id=batch.tenant_id,
            installation_id=batch.installation_id,
            company_id=batch.company_id,
            kind=batch.kind.value,
            source_key=batch.source_key,
            filename=batch.filename,
            file_format=batch.file_format.value,
            file_hash_sha256=batch.file_hash_sha256,
            currency_unit=batch.currency_unit.value,
            date_kind=batch.date_kind,
            header_row=batch.header_row,
            status=batch.status.value,
            attempt=batch.attempt,
            source_import_id=batch.source_import_id,
            source_import_batch_id=batch.source_import_batch_id,
            column_mapping_id=batch.column_mapping_id,
            mapping_columns=dict(batch.mapping_columns),
            file_type=batch.file_type,
            period_jalali=batch.period_jalali,
            statement_journal_id=batch.statement_journal_id,
            default_account_id=batch.default_account_id,
            preview_json=batch.preview.to_dict() if batch.preview is not None else None,
            review_item_id=batch.review_item_id,
            rejection_reason=batch.rejection_reason,
            auto_post_opt_in=batch.auto_post_opt_in,
            posting_status=batch.posting_status,
            posting_detail=batch.posting_detail,
            engine_ref_id=batch.engine_ref_id,
            stages_json=[stage.to_dict() for stage in batch.stages],
            created_at=now,
            updated_at=now,
            approved_at=batch.approved_at,
            posted_at=batch.posted_at,
            rejected_at=batch.rejected_at,
            archived_at=batch.archived_at,
        )
        self._session.add(row)
        self._session.flush()
        return _batch_from_row(row)

    def update_batch(self, batch: ManualImportBatch) -> ManualImportBatch:
        row = self._session.get(ManualImportBatchModel, batch.id)
        if row is None:
            raise ValueError(f"Manual import batch {batch.id!r} does not exist.")
        row.status = batch.status.value
        row.attempt = batch.attempt
        row.source_import_id = batch.source_import_id
        row.source_import_batch_id = batch.source_import_batch_id
        row.column_mapping_id = batch.column_mapping_id
        row.mapping_columns = dict(batch.mapping_columns)
        row.preview_json = batch.preview.to_dict() if batch.preview is not None else None
        row.review_item_id = batch.review_item_id
        row.rejection_reason = batch.rejection_reason
        row.auto_post_opt_in = batch.auto_post_opt_in
        row.posting_status = batch.posting_status
        row.posting_detail = batch.posting_detail
        row.engine_ref_id = batch.engine_ref_id
        row.stages_json = [stage.to_dict() for stage in batch.stages]
        row.updated_at = _now()
        row.approved_at = batch.approved_at
        row.posted_at = batch.posted_at
        row.rejected_at = batch.rejected_at
        row.archived_at = batch.archived_at
        self._session.flush()
        return _batch_from_row(row)

    def get_batch(self, batch_id: str, tenant_id: str) -> ManualImportBatch | None:
        row = self._session.get(ManualImportBatchModel, batch_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _batch_from_row(row)

    def list_batches(
        self,
        tenant_id: str,
        *,
        kind: ManualImportKind | None = None,
        status: ManualImportBatchStatus | None = None,
        company_id: str | None = None,
        source_import_id: str | None = None,
    ) -> Sequence[ManualImportBatch]:
        stmt = (
            select(ManualImportBatchModel)
            .where(ManualImportBatchModel.tenant_id == tenant_id)
            .order_by(
                ManualImportBatchModel.created_at.desc(),
                ManualImportBatchModel.id.desc(),
            )
        )
        if kind is not None:
            stmt = stmt.where(ManualImportBatchModel.kind == kind.value)
        if status is not None:
            stmt = stmt.where(ManualImportBatchModel.status == status.value)
        if company_id is not None:
            stmt = stmt.where(ManualImportBatchModel.company_id == company_id)
        if source_import_id is not None:
            stmt = stmt.where(ManualImportBatchModel.source_import_id == source_import_id)
        return [_batch_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_batch_by_file_hash(
        self,
        tenant_id: str,
        kind: ManualImportKind,
        source_key: str,
        file_hash_sha256: str,
    ) -> ManualImportBatch | None:
        stmt = select(ManualImportBatchModel).where(
            ManualImportBatchModel.tenant_id == tenant_id,
            ManualImportBatchModel.kind == kind.value,
            ManualImportBatchModel.source_key == source_key,
            ManualImportBatchModel.file_hash_sha256 == file_hash_sha256,
        )
        row = self._session.scalars(stmt).first()
        return _batch_from_row(row) if row is not None else None

    # -- settlement column mappings -----------------------------------------

    def list_settlement_mappings(
        self,
        tenant_id: str,
        channel_key: str | None = None,
        company_id: str | None = None,
    ) -> Sequence[SettlementColumnMapping]:
        stmt = (
            select(SettlementColumnMappingModel)
            .where(SettlementColumnMappingModel.tenant_id == tenant_id)
            .order_by(
                SettlementColumnMappingModel.status.asc(),
                SettlementColumnMappingModel.created_at.desc(),
            )
        )
        if channel_key is not None:
            stmt = stmt.where(SettlementColumnMappingModel.channel_key == channel_key)
        if company_id is not None:
            stmt = stmt.where(SettlementColumnMappingModel.company_id == company_id)
        return [_mapping_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_settlement_mapping(
        self, mapping_id: str, tenant_id: str
    ) -> SettlementColumnMapping | None:
        row = self._session.get(SettlementColumnMappingModel, mapping_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _mapping_from_row(row)

    def save_settlement_mapping(
        self, mapping: SettlementColumnMapping
    ) -> SettlementColumnMapping:
        now = _now()
        row = SettlementColumnMappingModel(
            id=mapping.id or str(uuid.uuid4()),
            tenant_id=mapping.tenant_id,
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
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _mapping_from_row(row)

    def update_settlement_mapping(
        self, mapping: SettlementColumnMapping
    ) -> SettlementColumnMapping:
        row = self._session.get(SettlementColumnMappingModel, mapping.id)
        if row is None:
            raise ValueError(f"Settlement column mapping {mapping.id!r} does not exist.")
        row.columns = dict(mapping.columns)
        row.status = mapping.status.value
        row.updated_at = _now()
        self._session.flush()
        return _mapping_from_row(row)
