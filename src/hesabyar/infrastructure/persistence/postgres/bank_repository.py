"""PostgreSQL implementation of the bank repository port (US-514).

Implements ``domain/bank/ports.py`` (``BankRepositoryPort``) over the
``bank_column_mappings`` / ``bank_import_batches`` / ``bank_import_rows``
/ ``bank_match_rules`` / ``bank_statement_matches`` tables. This is the
ONLY place that knows the bank tables' SQLAlchemy models — the
application layer talks to the port, never to these tables (§6.3).

Persistence contract (same as every other writer in the codebase):
``save_*`` / ``update_*`` flush so ids/timestamps exist but do NOT
commit — the caller (REST route) owns the transaction.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from hesabyar.domain.bank.models import (
    BankColumnMapping,
    BankImportBatch,
    BankImportRow,
    BankMatchRule,
    BankStatementMatch,
    ColumnMappingStatus,
    FileCurrencyUnit,
    ImportBatchStatus,
    MatchStatus,
    MatchTargetType,
    ReconcileOutcome,
    StatementFileFormat,
)
from hesabyar.domain.bank.ports import BankRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.models import SuggestionMode
from hesabyar.infrastructure.persistence.postgres.models import (
    BankColumnMappingModel,
    BankImportBatchModel,
    BankImportRowModel,
    BankMatchRuleModel,
    BankStatementMatchModel,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _mapping_from_row(row: BankColumnMappingModel) -> BankColumnMapping:
    return BankColumnMapping(
        id=row.id,
        tenant_id=row.tenant_id,
        bank_key=row.bank_key,
        name=row.name,
        file_format=StatementFileFormat(row.file_format),
        columns=dict(row.columns),
        date_kind=row.date_kind,
        currency_unit=FileCurrencyUnit(row.currency_unit),
        header_row=row.header_row,
        company_id=row.company_id,
        status=ColumnMappingStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _batch_from_row(row: BankImportBatchModel) -> BankImportBatch:
    return BankImportBatch(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        bank_key=row.bank_key,
        statement_journal_id=row.statement_journal_id,
        filename=row.filename,
        file_format=StatementFileFormat(row.file_format),
        currency_unit=FileCurrencyUnit(row.currency_unit),
        status=ImportBatchStatus(row.status),
        total_rows=row.total_rows,
        new_rows=row.new_rows,
        duplicate_rows=row.duplicate_rows,
        invalid_rows=row.invalid_rows,
        total_deposits_irr=row.total_deposits_irr,
        total_withdrawals_irr=row.total_withdrawals_irr,
        default_account_id=row.default_account_id,
        column_mapping_id=row.column_mapping_id,
        start_balance_irr=row.start_balance_irr,
        end_balance_irr=row.end_balance_irr,
        engine_statement_id=row.engine_statement_id,
        review_item_id=row.review_item_id,
        created_at=row.created_at,
        confirmed_at=row.confirmed_at,
        cancelled_at=row.cancelled_at,
    )


def _row_from_row(row: BankImportRowModel) -> BankImportRow:
    return BankImportRow(
        id=row.id,
        batch_id=row.batch_id,
        row_number=row.row_number,
        row_date=FiscalDate(row.row_date) if row.row_date is not None else None,
        amount_irr=row.amount_irr,
        description=row.description,
        bank_ref=row.bank_ref,
        balance_irr=row.balance_irr,
        match_key=row.match_key,
        is_duplicate=row.is_duplicate,
        duplicate_reason=row.duplicate_reason,
        parse_errors=tuple(str(item) for item in (row.parse_errors or [])),
        was_rounded=row.was_rounded,
        suggested_account_id=row.suggested_account_id,
        suggested_party_id=row.suggested_party_id,
        suggested_invoice_id=row.suggested_invoice_id,
        suggestion_source=row.suggestion_source,
    )


def _rule_from_row(row: BankMatchRuleModel) -> BankMatchRule:
    return BankMatchRule(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        bank_key=row.bank_key,
        pattern=row.pattern,
        direction=row.direction,
        target_account_id=row.target_account_id,
        target_party_id=row.target_party_id,
        target_kind=row.target_kind,
        mode=SuggestionMode(row.mode),
        confirmations=row.confirmations,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _match_from_row(row: BankStatementMatchModel) -> BankStatementMatch:
    return BankStatementMatch(
        id=row.id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        statement_id=row.statement_id,
        statement_line_id=row.statement_line_id,
        line_date=FiscalDate(row.line_date),
        line_amount_irr=row.line_amount_irr,
        line_description=row.line_description,
        target_type=MatchTargetType(row.target_type),
        target_id=row.target_id,
        target_ref=row.target_ref,
        target_label=row.target_label,
        priority=row.priority,
        confidence=row.confidence,
        status=MatchStatus(row.status),
        outcome=ReconcileOutcome(row.outcome) if row.outcome is not None else None,
        destination_account_id=row.destination_account_id,
        destination_shaba=row.destination_shaba,
        review_item_id=row.review_item_id,
        matched_at=row.matched_at,
        matched_by=row.matched_by,
    )


class SqlAlchemyBankRepository(BankRepositoryPort):
    """SQLAlchemy implementation of the bank repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- column mappings ---------------------------------------------------

    def list_mappings(
        self, tenant_id: str, bank_key: str | None = None, company_id: str | None = None
    ) -> Sequence[BankColumnMapping]:
        stmt = (
            select(BankColumnMappingModel)
            .where(BankColumnMappingModel.tenant_id == tenant_id)
            .order_by(
                BankColumnMappingModel.status.asc(),
                BankColumnMappingModel.created_at.desc(),
            )
        )
        if bank_key is not None:
            stmt = stmt.where(BankColumnMappingModel.bank_key == bank_key)
        if company_id is not None:
            stmt = stmt.where(
                (BankColumnMappingModel.company_id == company_id)
                | (BankColumnMappingModel.company_id.is_(None))
            )
        return [_mapping_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_mapping(self, mapping_id: str, tenant_id: str) -> BankColumnMapping | None:
        row = self._session.get(BankColumnMappingModel, mapping_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _mapping_from_row(row)

    def save_mapping(self, mapping: BankColumnMapping) -> BankColumnMapping:
        now = _now()
        row = BankColumnMappingModel(
            id=mapping.id or str(uuid.uuid4()),
            tenant_id=mapping.tenant_id,
            company_id=mapping.company_id,
            bank_key=mapping.bank_key,
            name=mapping.name,
            file_format=mapping.file_format.value,
            columns=dict(mapping.columns),
            date_kind=mapping.date_kind,
            currency_unit=mapping.currency_unit.value,
            header_row=mapping.header_row,
            status=mapping.status.value,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _mapping_from_row(row)

    def update_mapping(self, mapping: BankColumnMapping) -> BankColumnMapping:
        row = self._session.get(BankColumnMappingModel, mapping.id)
        if row is None or row.tenant_id != mapping.tenant_id:
            raise KeyError(f"Bank column mapping {mapping.id} does not exist.")
        row.company_id = mapping.company_id
        row.bank_key = mapping.bank_key
        row.name = mapping.name
        row.file_format = mapping.file_format.value
        row.columns = dict(mapping.columns)
        row.date_kind = mapping.date_kind
        row.currency_unit = mapping.currency_unit.value
        row.header_row = mapping.header_row
        row.status = mapping.status.value
        row.updated_at = _now()
        self._session.flush()
        return _mapping_from_row(row)

    # -- import batches + rows ----------------------------------------------

    def list_batches(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankImportBatch]:
        stmt = (
            select(BankImportBatchModel)
            .where(BankImportBatchModel.tenant_id == tenant_id)
            .order_by(BankImportBatchModel.created_at.desc())
        )
        if company_id is not None:
            stmt = stmt.where(BankImportBatchModel.company_id == company_id)
        return [_batch_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_batch(self, batch_id: str, tenant_id: str) -> BankImportBatch | None:
        row = self._session.get(BankImportBatchModel, batch_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _batch_from_row(row)

    def save_batch(self, batch: BankImportBatch, rows: Sequence[BankImportRow]) -> BankImportBatch:
        now = _now()
        batch_row = BankImportBatchModel(
            id=batch.id or str(uuid.uuid4()),
            tenant_id=batch.tenant_id,
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
            created_at=now,
            confirmed_at=batch.confirmed_at,
            cancelled_at=batch.cancelled_at,
        )
        self._session.add(batch_row)
        for item in rows:
            self._session.add(
                BankImportRowModel(
                    id=item.id or str(uuid.uuid4()),
                    batch_id=batch_row.id,
                    tenant_id=batch.tenant_id,
                    company_id=batch.company_id,
                    bank_key=batch.bank_key,
                    row_number=item.row_number,
                    row_date=item.row_date.gregorian_date if item.row_date is not None else None,
                    amount_irr=item.amount_irr,
                    description=item.description,
                    bank_ref=item.bank_ref,
                    balance_irr=item.balance_irr,
                    match_key=item.match_key,
                    is_duplicate=item.is_duplicate,
                    duplicate_reason=item.duplicate_reason,
                    parse_errors=list(item.parse_errors),
                    was_rounded=item.was_rounded,
                    suggested_account_id=item.suggested_account_id,
                    suggested_party_id=item.suggested_party_id,
                    suggested_invoice_id=item.suggested_invoice_id,
                    suggestion_source=item.suggestion_source,
                )
            )
        self._session.flush()
        return _batch_from_row(batch_row)

    def update_batch(self, batch: BankImportBatch) -> BankImportBatch:
        row = self._session.get(BankImportBatchModel, batch.id)
        if row is None or row.tenant_id != batch.tenant_id:
            raise KeyError(f"Bank import batch {batch.id} does not exist.")
        row.status = batch.status.value
        row.engine_statement_id = batch.engine_statement_id
        row.review_item_id = batch.review_item_id
        row.confirmed_at = batch.confirmed_at
        row.cancelled_at = batch.cancelled_at
        self._session.flush()
        return _batch_from_row(row)

    def list_rows(self, batch_id: str, tenant_id: str) -> Sequence[BankImportRow]:
        batch = self._session.get(BankImportBatchModel, batch_id)
        if batch is None or batch.tenant_id != tenant_id:
            return []
        stmt = (
            select(BankImportRowModel)
            .where(BankImportRowModel.batch_id == batch_id)
            .order_by(BankImportRowModel.row_number.asc())
        )
        return [_row_from_row(row) for row in self._session.scalars(stmt).all()]

    def existing_bank_refs(
        self, tenant_id: str, company_id: str, bank_key: str, refs: Sequence[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        if not refs:
            return set()
        stmt = (
            select(BankImportRowModel.bank_ref, BankImportRowModel.row_date)
            .where(
                and_(
                    BankImportRowModel.tenant_id == tenant_id,
                    BankImportRowModel.company_id == company_id,
                    BankImportRowModel.bank_key == bank_key,
                    BankImportRowModel.bank_ref.is_not(None),
                )
            )
            .distinct()
        )
        found = {
            (ref, row_date.isoformat())
            for ref, row_date in self._session.execute(stmt).all()
            if ref is not None and row_date is not None
        }
        return found & set(refs)

    def existing_match_keys(
        self, tenant_id: str, company_id: str, bank_key: str, keys: Sequence[str]
    ) -> set[str]:
        if not keys:
            return set()
        stmt = (
            select(BankImportRowModel.match_key)
            .where(
                and_(
                    BankImportRowModel.tenant_id == tenant_id,
                    BankImportRowModel.company_id == company_id,
                    BankImportRowModel.bank_key == bank_key,
                    BankImportRowModel.match_key.in_(list(keys)),
                )
            )
            .distinct()
        )
        return {key for (key,) in self._session.execute(stmt).all() if key is not None}

    # -- learned match rules -------------------------------------------------

    def list_rules(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankMatchRule]:
        stmt = (
            select(BankMatchRuleModel)
            .where(BankMatchRuleModel.tenant_id == tenant_id)
            .order_by(BankMatchRuleModel.created_at.desc())
        )
        if company_id is not None:
            stmt = stmt.where(BankMatchRuleModel.company_id == company_id)
        return [_rule_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_rule(self, rule_id: str, tenant_id: str) -> BankMatchRule | None:
        row = self._session.get(BankMatchRuleModel, rule_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return _rule_from_row(row)

    def find_rule(
        self,
        tenant_id: str,
        company_id: str,
        bank_key: str,
        pattern: str,
        direction: str,
        target_account_id: int,
    ) -> BankMatchRule | None:
        stmt = select(BankMatchRuleModel).where(
            and_(
                BankMatchRuleModel.tenant_id == tenant_id,
                BankMatchRuleModel.company_id == company_id,
                BankMatchRuleModel.bank_key == bank_key,
                BankMatchRuleModel.pattern == pattern,
                BankMatchRuleModel.direction == direction,
                BankMatchRuleModel.target_account_id == target_account_id,
            )
        )
        row = self._session.scalars(stmt).first()
        return _rule_from_row(row) if row is not None else None

    def save_rule(self, rule: BankMatchRule) -> BankMatchRule:
        now = _now()
        row = BankMatchRuleModel(
            id=rule.id or str(uuid.uuid4()),
            tenant_id=rule.tenant_id,
            company_id=rule.company_id,
            bank_key=rule.bank_key,
            pattern=rule.pattern,
            direction=rule.direction,
            target_account_id=rule.target_account_id,
            target_party_id=rule.target_party_id,
            target_kind=rule.target_kind,
            mode=rule.mode.value,
            confirmations=rule.confirmations,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _rule_from_row(row)

    def update_rule(self, rule: BankMatchRule) -> BankMatchRule:
        row = self._session.get(BankMatchRuleModel, rule.id)
        if row is None or row.tenant_id != rule.tenant_id:
            raise KeyError(f"Bank match rule {rule.id} does not exist.")
        row.mode = rule.mode.value
        row.confirmations = rule.confirmations
        row.target_party_id = rule.target_party_id
        row.updated_at = _now()
        self._session.flush()
        return _rule_from_row(row)

    # -- recorded matches ----------------------------------------------------

    def list_matches_for_statement(
        self, tenant_id: str, statement_id: int
    ) -> Sequence[BankStatementMatch]:
        stmt = (
            select(BankStatementMatchModel)
            .where(
                and_(
                    BankStatementMatchModel.tenant_id == tenant_id,
                    BankStatementMatchModel.statement_id == statement_id,
                )
            )
            .order_by(BankStatementMatchModel.matched_at.asc())
        )
        return [_match_from_row(row) for row in self._session.scalars(stmt).all()]

    def list_matches_for_company(
        self, tenant_id: str, company_id: str
    ) -> Sequence[BankStatementMatch]:
        stmt = (
            select(BankStatementMatchModel)
            .where(
                and_(
                    BankStatementMatchModel.tenant_id == tenant_id,
                    BankStatementMatchModel.company_id == company_id,
                )
            )
            .order_by(BankStatementMatchModel.matched_at.asc())
        )
        return [_match_from_row(row) for row in self._session.scalars(stmt).all()]

    def get_match_for_line(
        self, tenant_id: str, statement_id: int, statement_line_id: int
    ) -> BankStatementMatch | None:
        stmt = (
            select(BankStatementMatchModel)
            .where(
                and_(
                    BankStatementMatchModel.tenant_id == tenant_id,
                    BankStatementMatchModel.statement_id == statement_id,
                    BankStatementMatchModel.statement_line_id == statement_line_id,
                    BankStatementMatchModel.status != MatchStatus.REJECTED.value,
                )
            )
            .order_by(BankStatementMatchModel.matched_at.desc())
        )
        row = self._session.scalars(stmt).first()
        return _match_from_row(row) if row is not None else None

    def save_match(self, match: BankStatementMatch) -> BankStatementMatch:
        now = _now()
        row = BankStatementMatchModel(
            id=match.id or str(uuid.uuid4()),
            tenant_id=match.tenant_id,
            company_id=match.company_id,
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
            matched_at=match.matched_at or now,
            matched_by=match.matched_by,
        )
        self._session.add(row)
        self._session.flush()
        return _match_from_row(row)

    def update_match(self, match: BankStatementMatch) -> BankStatementMatch:
        row = self._session.get(BankStatementMatchModel, match.id)
        if row is None or row.tenant_id != match.tenant_id:
            raise KeyError(f"Bank statement match {match.id} does not exist.")
        row.status = match.status.value
        row.review_item_id = match.review_item_id
        row.matched_by = match.matched_by
        self._session.flush()
        return _match_from_row(row)
