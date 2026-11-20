"""Bank read use cases (US-514): imports, mappings, rules, matches, and the §11.2 report."""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import StatementJournalView
from hesabyar.domain.bank.errors import (
    BankColumnMappingNotFoundError,
    BankImportNotFoundError,
)
from hesabyar.domain.bank.models import (
    BankColumnMapping,
    BankImportBatch,
    BankImportRow,
    BankMatchRule,
    BankReconciliationRow,
    BankReconciliationStatus,
    BankStatementMatch,
    MatchStatus,
    ReconcileOutcome,
)
from hesabyar.domain.bank.ports import BankRepositoryPort
from hesabyar.domain.common.dates import FiscalDate

#: §11.1 freshness metadata for the reconciliation-status report.
REPORT_SOURCE = "tryton:account.statement + hesabyar:bank_statement_matches/bank_import_batches"
REPORT_VERSION = "1.0"
CALCULATION_VERSION = "us-514.1"


class BankQueryService:
    """Read use cases for bank imports, mappings, rules, matches, and the report."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        bank: BankRepositoryPort,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self._engine = engine
        self._bank = bank
        self._now = now

    def _clock(self) -> datetime.datetime:
        if self._now is not None:
            return self._now
        return datetime.datetime.now(datetime.UTC)

    # -- imports ---------------------------------------------------------------

    def list_batches(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankImportBatch]:
        return self._bank.list_batches(tenant_id, company_id)

    def get_batch(self, tenant_id: str, batch_id: str) -> BankImportBatch:
        batch = self._bank.get_batch(batch_id, tenant_id)
        if batch is None:
            raise BankImportNotFoundError(
                f"Import batch {batch_id!r} does not exist.",
                details={"batch_id": batch_id},
            )
        return batch

    def list_rows(self, tenant_id: str, batch_id: str) -> Sequence[BankImportRow]:
        self.get_batch(tenant_id, batch_id)
        return self._bank.list_rows(batch_id, tenant_id)

    # -- mappings / rules / matches ----------------------------------------------

    def list_mappings(
        self,
        tenant_id: str,
        bank_key: str | None = None,
        company_id: str | None = None,
    ) -> Sequence[BankColumnMapping]:
        return self._bank.list_mappings(tenant_id, bank_key, company_id)

    def get_mapping(self, tenant_id: str, mapping_id: str) -> BankColumnMapping:
        mapping = self._bank.get_mapping(mapping_id, tenant_id)
        if mapping is None:
            raise BankColumnMappingNotFoundError(
                f"Column mapping {mapping_id!r} does not exist.",
                details={"mapping_id": mapping_id},
            )
        return mapping

    def list_rules(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankMatchRule]:
        return self._bank.list_rules(tenant_id, company_id)

    def list_matches_for_statement(
        self, tenant_id: str, statement_id: int
    ) -> Sequence[BankStatementMatch]:
        return self._bank.list_matches_for_statement(tenant_id, statement_id)

    def list_statement_journals(
        self, company: CompanyRef
    ) -> Sequence[StatementJournalView]:
        return self._engine.list_statement_journals(company)

    # -- §11.2 reconciliation-status report ---------------------------------------

    def reconciliation_status(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> BankReconciliationStatus:
        """Bank reconciliation status per journal over a period (spec §11.2).

        Lines are classified from the engine (``related_to`` set ⇒
        matched) plus the latest non-rejected HesabYar match record:
        auto-applied/confirmed ⇒ matched, suggested ⇒ suggested,
        ambiguous outcome ⇒ ambiguous, unmatched record or no record
        at all ⇒ unmatched. Cancelled statements are excluded.
        """
        now = self._clock()
        journals = {journal.id: journal for journal in self._engine.list_statement_journals(company)}
        batches = self._bank.list_batches(tenant_id, company_id)
        bank_by_statement = {
            batch.engine_statement_id: batch.bank_key
            for batch in batches
            if batch.engine_statement_id is not None
        }
        matches = self._bank.list_matches_for_company(tenant_id, company_id)
        latest: dict[tuple[int, int], BankStatementMatch] = {}
        for match in matches:
            if match.status == MatchStatus.REJECTED:
                continue
            latest[(match.statement_id, match.statement_line_id)] = match
        buckets: dict[int, _JournalBucket] = {}
        source_times: list[datetime.datetime] = [
            batch.created_at for batch in batches if batch.created_at is not None
        ]
        for match in matches:
            if match.matched_at is not None:
                source_times.append(match.matched_at)
        for statement in self._engine.list_bank_statements(company):
            if statement.state == "cancelled":
                continue
            journal = journals.get(statement.journal_id)
            bucket = buckets.setdefault(
                statement.journal_id,
                _JournalBucket(
                    bank_key=bank_by_statement.get(statement.ref.engine_id),
                    journal_id=statement.journal_id,
                    journal_name=statement.journal_name,
                    destination_shaba=journal.bank_account_number if journal else None,
                ),
            )
            seen_statement = False
            for line in statement.lines:
                if not (date_from.gregorian_date <= line.date.gregorian_date <= date_to.gregorian_date):
                    continue
                seen_statement = True
                bucket.lines_total += 1
                record = latest.get((statement.ref.engine_id, line.id))
                if line.related_invoice is not None or (
                    record is not None
                    and record.status in (MatchStatus.AUTO_APPLIED, MatchStatus.CONFIRMED)
                ):
                    bucket.matched_lines += 1
                elif record is not None and record.status == MatchStatus.SUGGESTED:
                    if record.outcome == ReconcileOutcome.AMBIGUOUS:
                        bucket.ambiguous_lines += 1
                    else:
                        bucket.suggested_lines += 1
                    bucket.unmatched_balance_irr += line.amount.amount
                    bucket.note_unmatched(line.id, line.date)
                else:
                    bucket.unmatched_lines += 1
                    bucket.unmatched_balance_irr += line.amount.amount
                    bucket.note_unmatched(line.id, line.date)
            if seen_statement:
                bucket.statements_count += 1
        rows = tuple(
            bucket.to_row(now) for bucket in sorted(buckets.values(), key=lambda b: b.journal_name)
        )
        lines_total = sum(row.lines_total for row in rows)
        matched_lines = sum(row.matched_lines for row in rows)
        return BankReconciliationStatus(
            company_id=company_id,
            date_from=date_from,
            date_to=date_to,
            generated_at=now,
            source=REPORT_SOURCE,
            source_timestamp=max(source_times) if source_times else None,
            report_version=REPORT_VERSION,
            calculation_version=CALCULATION_VERSION,
            rows=rows,
            statements_count=sum(row.statements_count for row in rows),
            lines_total=lines_total,
            matched_lines=matched_lines,
            suggested_lines=sum(row.suggested_lines for row in rows),
            ambiguous_lines=sum(row.ambiguous_lines for row in rows),
            unmatched_lines=sum(row.unmatched_lines for row in rows),
            match_rate=(matched_lines / lines_total) if lines_total else None,
            unmatched_balance_irr=sum(row.unmatched_balance_irr for row in rows),
        )


class _JournalBucket:
    """Mutable per-journal accumulator for the §11.2 report."""

    def __init__(
        self,
        *,
        bank_key: str | None,
        journal_id: int,
        journal_name: str,
        destination_shaba: str | None,
    ) -> None:
        self.bank_key = bank_key
        self.journal_id = journal_id
        self.journal_name = journal_name
        self.destination_shaba = destination_shaba
        self.statements_count = 0
        self.lines_total = 0
        self.matched_lines = 0
        self.suggested_lines = 0
        self.ambiguous_lines = 0
        self.unmatched_lines = 0
        self.unmatched_balance_irr = 0
        self._oldest_unmatched: tuple[FiscalDate, int] | None = None

    def note_unmatched(self, line_id: int, line_date: FiscalDate) -> None:
        if self._oldest_unmatched is None or line_date.gregorian_date < self._oldest_unmatched[0].gregorian_date:
            self._oldest_unmatched = (line_date, line_id)

    def to_row(self, now: datetime.datetime) -> BankReconciliationRow:
        oldest_age = None
        oldest_id = None
        if self._oldest_unmatched is not None:
            oldest_age = (now.date() - self._oldest_unmatched[0].gregorian_date).days
            oldest_id = self._oldest_unmatched[1]
        return BankReconciliationRow(
            bank_key=self.bank_key,
            journal_id=self.journal_id,
            journal_name=self.journal_name,
            destination_shaba=self.destination_shaba,
            statements_count=self.statements_count,
            lines_total=self.lines_total,
            matched_lines=self.matched_lines,
            suggested_lines=self.suggested_lines,
            ambiguous_lines=self.ambiguous_lines,
            unmatched_lines=self.unmatched_lines,
            match_rate=(self.matched_lines / self.lines_total) if self.lines_total else None,
            unmatched_balance_irr=self.unmatched_balance_irr,
            oldest_unmatched_age_days=oldest_age,
            oldest_unmatched_line_id=oldest_id,
        )
