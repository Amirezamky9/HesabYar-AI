"""Unit tests for the bank import + reconcile + query services (US-514).

DB-free: in-memory fakes for the engine port, the bank repository
port, the review-queue ports (the REAL ReviewQueueCommandService runs
over them), and the integrations registry. The real file reader runs
over generated CSV bytes.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Sequence
from typing import cast

import pytest

from hesabyar.application.commands.bank_import import BankImportService
from hesabyar.application.commands.bank_reconcile import BankReconcileService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.bank import BankQueryService
from hesabyar.domain.accounting.commands import (
    CreateBankStatement,
    CreateEngineCheque,
    TransitionEngineCheque,
    UpdateBankStatementLine,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementRef,
    CheckbookEngineRef,
    ChequeEngineRef,
    CompanyRef,
    InvoiceRef,
    PartyRef,
    PaymentRef,
    StatementJournalRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountView,
    BankStatementLineView,
    BankStatementView,
    CheckbookEngineView,
    ChequeEngineView,
    InvoiceView,
    PaymentView,
    StatementJournalView,
)
from hesabyar.domain.bank.errors import (
    BankColumnMappingInvalidError,
    BankImportInvalidStateError,
    BankImportTooManyRowsError,
    BankLineAlreadyMatchedError,
    BankStatementJournalNotFoundError,
    BankStatementNotDraftError,
)
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
from hesabyar.domain.common.errors import AccountNotFoundError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.integrations.models import InstallationSettings, SuggestionMode
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.review_queue.models import (
    ReviewAuditEntry,
    ReviewItem,
    ReviewItemType,
    SlaPolicy,
)
from hesabyar.domain.review_queue.ports import (
    ReviewAuditPort,
    ReviewItemFilters,
    ReviewQueuePort,
)
from hesabyar.infrastructure.bank.file_readers import StatementFileReader

TENANT = "tenant-1"
COMPANY_ID = "company-1"
COMPANY = CompanyRef(engine_id=1)
DAY = datetime.date(2026, 11, 17)
NOW = datetime.datetime(2026, 11, 18, 9, 0, tzinfo=datetime.UTC)

CSV_BYTES = (
    "تاریخ,شرح,برداشت,واریز,مانده,شماره سند\n"
    "1405/08/26,واریز مشتری,,۱٬۰۰۰٬۰۰۰,۵٬۰۰۰٬۰۰۰,REF-1\n"
    "1405/08/26,پرداخت تأمین‌کننده,۲۵۰٬۰۰۰,,۴٬۷۵۰٬۰۰۰,REF-2\n"
).encode()

COLUMNS = {
    "date": "تاریخ",
    "description": "شرح",
    "debit": "برداشت",
    "credit": "واریز",
    "balance": "مانده",
    "bank_ref": "شماره سند",
}


def _fd(days: int = 0) -> FiscalDate:
    return FiscalDate(DAY + datetime.timedelta(days=days))


# -- fakes ------------------------------------------------------------------------


class FakeEngine:
    """In-memory engine port (only the methods the bank services use)."""

    def __init__(self) -> None:
        self.chart = [
            AccountView(
                ref=AccountRef(100),
                company=COMPANY,
                code="1.1.1",
                name="بانک ملی",
                type_name="asset",
                parent=None,
                closed=False,
                party_required=False,
                active=True,
            ),
            AccountView(
                ref=AccountRef(900),
                company=COMPANY,
                code="1.1.9",
                name="حساب معلق",
                type_name="asset",
                parent=None,
                closed=False,
                party_required=False,
                active=True,
            ),
            AccountView(
                ref=AccountRef(501),
                company=COMPANY,
                code="1.2.1",
                name="دریافتنی",
                type_name="asset",
                parent=None,
                closed=False,
                party_required=True,
                active=True,
            ),
            AccountView(
                ref=AccountRef(601),
                company=COMPANY,
                code="6.1.1",
                name="هزینه",
                type_name="expense",
                parent=None,
                closed=False,
                party_required=False,
                active=True,
            ),
        ]
        self.journals = [
            StatementJournalView(
                ref=StatementJournalRef(700),
                company=COMPANY,
                name="بانک ملی",
                account=AccountRef(100),
                validation="balance",
                currency_code="IRR",
                bank_account_number="IR062060000000100324200001",
            )
        ]
        self.statements: dict[int, BankStatementView] = {}
        self.invoices: list[InvoiceView] = []
        self.payments: list[PaymentView] = []
        self._next_statement_id = 1000
        self._next_line_id = 9000

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        return list(self.chart)

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return list(self.journals)

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        return self.statements.get(statement.engine_id)

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        views = list(self.statements.values())
        if state is not None:
            views = [view for view in views if view.state == state]
        return views

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        statement_id = self._next_statement_id
        self._next_statement_id += 1
        lines: list[BankStatementLineView] = []
        for line in command.lines:
            self._next_line_id += 1
            lines.append(
                BankStatementLineView(
                    id=self._next_line_id,
                    date=line.date,
                    amount=line.amount,
                    account=line.account,
                    description=line.description,
                    number=line.number,
                    party=line.party,
                    related_invoice=line.related_invoice,
                )
            )
        self.statements[statement_id] = BankStatementView(
            ref=BankStatementRef(statement_id),
            company=command.company,
            name=command.name,
            journal_name="بانک ملی",
            journal_id=command.journal.engine_id,
            date=command.date,
            state="draft",
            validation="balance",
            currency_code="IRR",
            start_balance=command.start_balance,
            end_balance=command.end_balance,
            balance=None,
            total_amount=None,
            number_of_lines=len(lines),
            lines=tuple(lines),
        )
        return BankStatementRef(statement_id)

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        view = self.statements[command.statement.engine_id]
        lines = []
        for line in view.lines:
            if line.id != command.line.engine_id:
                lines.append(line)
                continue
            lines.append(
                dataclasses.replace(
                    line,
                    account=command.account if command.account is not None else line.account,
                    party=command.party if command.party is not None else line.party,
                    related_invoice=(
                        None
                        if command.clear_related_invoice
                        else (
                            command.related_invoice
                            if command.related_invoice is not None
                            else line.related_invoice
                        )
                    ),
                )
            )
        self.statements[command.statement.engine_id] = dataclasses.replace(
            view, lines=tuple(lines)
        )

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        views = list(self.invoices)
        if invoice_type is not None:
            views = [v for v in views if v.invoice_type == invoice_type]
        if state is not None:
            views = [v for v in views if v.state == state]
        return views

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        return next((v for v in self.invoices if v.ref.engine_id == invoice.engine_id), None)

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        views = list(self.payments)
        if kind is not None:
            views = [v for v in views if v.kind == kind]
        if state is not None:
            views = [v for v in views if v.state == state]
        return views

    def get_payment(self, payment) -> PaymentView | None:
        return next((v for v in self.payments if v.ref.engine_id == payment.engine_id), None)

    # -- cheques (US-531): stubs, never exercised here --

    def create_engine_cheque(self, command: CreateEngineCheque) -> ChequeEngineView:
        raise NotImplementedError

    def get_engine_cheque(self, cheque: ChequeEngineRef) -> ChequeEngineView | None:
        raise NotImplementedError

    def list_engine_cheques(
        self, company: CompanyRef, *, state: str | None = None
    ) -> Sequence[ChequeEngineView]:
        raise NotImplementedError

    def transition_engine_cheque(self, command: TransitionEngineCheque) -> ChequeEngineView:
        raise NotImplementedError

    # -- cheques (US-532): stubs, never exercised here --

    def list_engine_checkbooks(self, company: CompanyRef) -> Sequence[CheckbookEngineView]:
        raise NotImplementedError

    def get_engine_checkbook(
        self, company: CompanyRef, checkbook: CheckbookEngineRef
    ) -> CheckbookEngineView | None:
        raise NotImplementedError

    def add_statement(self, *, state: str = "draft", lines: list[BankStatementLineView]) -> int:
        statement_id = self._next_statement_id
        self._next_statement_id += 1
        self.statements[statement_id] = BankStatementView(
            ref=BankStatementRef(statement_id),
            company=COMPANY,
            name=f"statement-{statement_id}",
            journal_name="بانک ملی",
            journal_id=700,
            date=_fd(),
            state=state,
            validation="balance",
            currency_code="IRR",
            start_balance=None,
            end_balance=None,
            balance=None,
            total_amount=None,
            number_of_lines=len(lines),
            lines=tuple(lines),
        )
        return statement_id


class FakeBankRepo(BankRepositoryPort):
    """In-memory bank repository port."""

    def __init__(self) -> None:
        self.mappings: dict[str, BankColumnMapping] = {}
        self.batches: dict[str, BankImportBatch] = {}
        self.rows: dict[str, list[BankImportRow]] = {}
        self.rules: dict[str, BankMatchRule] = {}
        self.matches: dict[str, BankStatementMatch] = {}

    def list_mappings(
        self, tenant_id: str, bank_key: str | None = None, company_id: str | None = None
    ) -> Sequence[BankColumnMapping]:
        return [
            m
            for m in self.mappings.values()
            if m.tenant_id == tenant_id
            and (bank_key is None or m.bank_key == bank_key)
            and (company_id is None or m.company_id in (None, company_id))
        ]

    def get_mapping(self, mapping_id: str, tenant_id: str) -> BankColumnMapping | None:
        mapping = self.mappings.get(mapping_id)
        return mapping if mapping is not None and mapping.tenant_id == tenant_id else None

    def save_mapping(self, mapping: BankColumnMapping) -> BankColumnMapping:
        self.mappings[mapping.id] = mapping
        return mapping

    def update_mapping(self, mapping: BankColumnMapping) -> BankColumnMapping:
        self.mappings[mapping.id] = mapping
        return mapping

    def list_batches(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankImportBatch]:
        return [
            b
            for b in self.batches.values()
            if b.tenant_id == tenant_id and (company_id is None or b.company_id == company_id)
        ]

    def get_batch(self, batch_id: str, tenant_id: str) -> BankImportBatch | None:
        batch = self.batches.get(batch_id)
        return batch if batch is not None and batch.tenant_id == tenant_id else None

    def save_batch(self, batch: BankImportBatch, rows: Sequence[BankImportRow]) -> BankImportBatch:
        self.batches[batch.id] = batch
        self.rows[batch.id] = list(rows)
        return batch

    def update_batch(self, batch: BankImportBatch) -> BankImportBatch:
        self.batches[batch.id] = batch
        return batch

    def list_rows(self, batch_id: str, tenant_id: str) -> Sequence[BankImportRow]:
        return list(self.rows.get(batch_id, []))

    def existing_bank_refs(
        self, tenant_id: str, company_id: str, bank_key: str, refs: Sequence[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        wanted = set(refs)
        found: set[tuple[str, str]] = set()
        for batch in self.batches.values():
            if (
                batch.tenant_id != tenant_id
                or batch.company_id != company_id
                or batch.bank_key != bank_key
            ):
                continue
            for row in self.rows.get(batch.id, []):
                if row.bank_ref is not None and row.row_date is not None:
                    pair = (row.bank_ref, row.row_date.gregorian_date.isoformat())
                    if pair in wanted:
                        found.add(pair)
        return found

    def existing_match_keys(
        self, tenant_id: str, company_id: str, bank_key: str, keys: Sequence[str]
    ) -> set[str]:
        wanted = set(keys)
        found: set[str] = set()
        for batch in self.batches.values():
            if (
                batch.tenant_id != tenant_id
                or batch.company_id != company_id
                or batch.bank_key != bank_key
            ):
                continue
            for row in self.rows.get(batch.id, []):
                if row.match_key is not None and row.match_key in wanted:
                    found.add(row.match_key)
        return found

    def list_rules(
        self, tenant_id: str, company_id: str | None = None
    ) -> Sequence[BankMatchRule]:
        return [
            r
            for r in self.rules.values()
            if r.tenant_id == tenant_id and (company_id is None or r.company_id == company_id)
        ]

    def get_rule(self, rule_id: str, tenant_id: str) -> BankMatchRule | None:
        rule = self.rules.get(rule_id)
        return rule if rule is not None and rule.tenant_id == tenant_id else None

    def find_rule(
        self,
        tenant_id: str,
        company_id: str,
        bank_key: str,
        pattern: str,
        direction: str,
        target_account_id: int,
    ) -> BankMatchRule | None:
        for rule in self.rules.values():
            if (
                rule.tenant_id == tenant_id
                and rule.company_id == company_id
                and rule.bank_key == bank_key
                and rule.pattern == pattern
                and rule.direction == direction
                and rule.target_account_id == target_account_id
            ):
                return rule
        return None

    def save_rule(self, rule: BankMatchRule) -> BankMatchRule:
        self.rules[rule.id] = rule
        return rule

    def update_rule(self, rule: BankMatchRule) -> BankMatchRule:
        self.rules[rule.id] = rule
        return rule

    def list_matches_for_statement(
        self, tenant_id: str, statement_id: int
    ) -> Sequence[BankStatementMatch]:
        return [
            m
            for m in self.matches.values()
            if m.tenant_id == tenant_id and m.statement_id == statement_id
        ]

    def list_matches_for_company(
        self, tenant_id: str, company_id: str
    ) -> Sequence[BankStatementMatch]:
        return [
            m
            for m in self.matches.values()
            if m.tenant_id == tenant_id and m.company_id == company_id
        ]

    def get_match_for_line(
        self, tenant_id: str, statement_id: int, statement_line_id: int
    ) -> BankStatementMatch | None:
        matches = [
            m
            for m in self.matches.values()
            if m.tenant_id == tenant_id
            and m.statement_id == statement_id
            and m.statement_line_id == statement_line_id
            and m.status != MatchStatus.REJECTED
        ]
        return matches[-1] if matches else None

    def save_match(self, match: BankStatementMatch) -> BankStatementMatch:
        self.matches[match.id] = match
        return match

    def update_match(self, match: BankStatementMatch) -> BankStatementMatch:
        self.matches[match.id] = match
        return match


class FakeReviewQueue(ReviewQueuePort, ReviewAuditPort):
    """In-memory review-queue + audit ports (one object, both ports)."""

    def __init__(self) -> None:
        self.items: dict[str, ReviewItem] = {}
        self.audit: list[ReviewAuditEntry] = []

    def list_items(self, tenant_id: str, filters: ReviewItemFilters) -> Sequence[ReviewItem]:
        return [item for item in self.items.values() if item.tenant_id == tenant_id]

    def get_item(self, item_id: str, tenant_id: str) -> ReviewItem | None:
        item = self.items.get(item_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    def save_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def update_item(self, item: ReviewItem) -> ReviewItem:
        self.items[item.id] = item
        return item

    def list_policies(self, tenant_id: str) -> Sequence[SlaPolicy]:
        return []

    def upsert_policy(self, tenant_id: str, policy: SlaPolicy) -> SlaPolicy:
        return policy

    def record(self, entry: ReviewAuditEntry) -> ReviewAuditEntry:
        self.audit.append(entry)
        return entry

    def list_for_item(self, item_id: str, tenant_id: str) -> Sequence[ReviewAuditEntry]:
        return [entry for entry in self.audit if entry.item_id == item_id]


class FakeRegistry:
    """In-memory integrations registry (settings only)."""

    def __init__(self, window_days: int | None = None) -> None:
        self.window_days = window_days

    def get_installation_settings(self, installation_id: str) -> InstallationSettings | None:
        if self.window_days is None:
            return None
        return InstallationSettings(
            installation_id=installation_id, matching_window_days=self.window_days
        )


@pytest.fixture
def world() -> dict:
    """A fresh fake world: engine + repos + services."""
    engine = FakeEngine()
    bank = FakeBankRepo()
    queue = FakeReviewQueue()
    review = ReviewQueueCommandService(queue, queue, now=NOW)
    registry = FakeRegistry()
    # The engine/registry fakes implement only the subset the bank
    # services call; cast them to the ports (the services never call
    # the unimplemented methods).
    engine_port = cast(AccountingEnginePort, engine)
    registry_port = cast(IntegrationsRegistryPort, registry)
    imports = BankImportService(engine_port, bank, review, StatementFileReader(), now=NOW)
    reconcile = BankReconcileService(engine_port, bank, review, queue, registry_port, now=NOW)
    queries = BankQueryService(engine_port, bank, now=NOW)
    return {
        "engine": engine,
        "bank": bank,
        "queue": queue,
        "review": review,
        "registry": registry,
        "imports": imports,
        "reconcile": reconcile,
        "queries": queries,
    }


def _create_batch(world: dict, content: bytes = CSV_BYTES, **overrides) -> BankImportBatch:
    kwargs = {
        "tenant_id": TENANT,
        "company": COMPANY,
        "company_id": COMPANY_ID,
        "bank_key": "melli",
        "statement_journal_id": 700,
        "filename": "baam.csv",
        "content": content,
        "currency_unit": FileCurrencyUnit.RIAL,
        "default_account_id": 900,
        "mapping_columns": COLUMNS,
        "date_kind": "jalali",
        "actor": "operator",
    }
    kwargs.update(overrides)
    return world["imports"].create_import(**kwargs)


def _invoice(**overrides) -> InvoiceView:
    base = {
        "ref": InvoiceRef(10),
        "company": COMPANY,
        "invoice_type": "out",
        "party": PartyRef(5),
        "party_name": "مشتری نمونه",
        "number": "INV-100",
        "invoice_date": _fd(),
        "accounting_date": _fd(),
        "state": "posted",
        "description": "",
        "reference": "INV-100",
        "journal": None,
        "account": AccountRef(501),
        "currency_code": "IRR",
        "untaxed_amount": MoneyIRR(1_000_000),
        "tax_amount": MoneyIRR(0),
        "total_amount": MoneyIRR(1_000_000),
        "lines": (),
    }
    base.update(overrides)
    return InvoiceView(**base)


def _payment(**overrides) -> PaymentView:
    base = {
        "ref": PaymentRef(20),
        "company": COMPANY,
        "number": "PAY-20",
        "kind": "receivable",
        "party": PartyRef(5),
        "party_name": "مشتری نمونه",
        "date": _fd(),
        "amount": MoneyIRR(1_000_000),
        "state": "succeeded",
        "reference": "PAY-20",
        "journal_name": "بانک ملی",
        "currency_code": "IRR",
    }
    base.update(overrides)
    return PaymentView(**base)


def _line(**overrides) -> BankStatementLineView:
    amount_irr = overrides.pop("amount_irr", None)
    base = {
        "id": 9001,
        "date": _fd(),
        "amount": MoneyIRR(amount_irr if amount_irr is not None else 1_000_000),
        "account": AccountRef(900),
        "description": "واریز مشتری",
        "number": None,
        "party": None,
        "party_name": None,
        "related_invoice": None,
    }
    base.update(overrides)
    return BankStatementLineView(**base)


# -- import: detection ---------------------------------------------------------------


def test_detect_file_suggests_mapping(world: dict) -> None:
    detection = world["imports"].detect_file(content=CSV_BYTES, filename="baam.csv")
    assert detection.file_format == StatementFileFormat.CSV
    assert detection.columns[0] == "تاریخ"
    assert detection.suggested_mapping["date"] == "تاریخ"
    assert detection.suggested_mapping["description"] == "شرح"
    assert detection.missing_fields == ()
    assert len(detection.sample_rows) == 2


def test_detect_file_unknown_format_is_quarantined(world: dict) -> None:
    detection = world["imports"].detect_file(content=b"\x00\x01\x02garbage", filename="x.bin")
    # CSV fallback reads the bytes; the grid has no parseable header.
    assert detection.file_format == StatementFileFormat.CSV
    assert detection.missing_fields  # no date/description columns found


# -- import: create --------------------------------------------------------------------


def test_create_import_builds_batch_with_preview(world: dict) -> None:
    batch = _create_batch(world)
    assert batch.status == ImportBatchStatus.AWAITING_REVIEW
    assert batch.total_rows == 2
    assert batch.new_rows == 2
    assert batch.duplicate_rows == 0
    assert batch.invalid_rows == 0
    assert batch.total_deposits_irr == 1_000_000
    assert batch.total_withdrawals_irr == 250_000
    assert batch.start_balance_irr == 4_000_000  # first balance minus first amount
    assert batch.end_balance_irr == 4_750_000
    rows = world["bank"].list_rows(batch.id, TENANT)
    assert rows[0].suggested_account_id == 900  # default (suspense) account
    assert rows[0].suggestion_source == "default"
    assert batch.review_item_id is not None
    item = world["queue"].get_item(batch.review_item_id, TENANT)
    assert item is not None
    assert item.item_type == ReviewItemType.MANUAL_IMPORT_REVIEW
    assert item.status.value == "open"


def test_create_import_dedups_by_bank_ref(world: dict) -> None:
    _create_batch(world)
    second = _create_batch(world)
    assert second.new_rows == 0
    assert second.duplicate_rows == 2
    rows = world["bank"].list_rows(second.id, TENANT)
    assert all(row.is_duplicate for row in rows)
    assert all(row.duplicate_reason == "bank_ref" for row in rows)


def test_create_import_dedups_by_match_key_without_bank_ref(world: dict) -> None:
    content = (
        "تاریخ,شرح,برداشت,واریز\n"
        "1405/08/26,واریز مشتری,,۱٬۰۰۰٬۰۰۰\n"
    ).encode()
    columns = {"date": "تاریخ", "description": "شرح", "debit": "برداشت", "credit": "واریز"}
    _create_batch(world, content=content, mapping_columns=columns)
    second = _create_batch(world, content=content, mapping_columns=columns)
    assert second.new_rows == 0
    rows = world["bank"].list_rows(second.id, TENANT)
    assert rows[0].duplicate_reason == "match_key"


def test_create_import_dedups_within_the_same_file(world: dict) -> None:
    content = (
        "تاریخ,شرح,برداشت,واریز,شماره سند\n"
        "1405/08/26,واریز مشتری,,۱٬۰۰۰٬۰۰۰,REF-1\n"
        "1405/08/26,واریز مشتری,,۱٬۰۰۰٬۰۰۰,REF-1\n"
    ).encode()
    columns = {
        "date": "تاریخ",
        "description": "شرح",
        "debit": "برداشت",
        "credit": "واریز",
        "bank_ref": "شماره سند",
    }
    batch = _create_batch(world, content=content, mapping_columns=columns)
    assert batch.new_rows == 1
    assert batch.duplicate_rows == 1


def test_create_import_counts_invalid_rows(world: dict) -> None:
    content = (
        "تاریخ,شرح,برداشت,واریز\n"
        "not-a-date,واریز,,۱٬۰۰۰٬۰۰۰\n"
        "1405/08/26,واریز درست,,۵۰۰٬۰۰۰\n"
    ).encode()
    columns = {"date": "تاریخ", "description": "شرح", "debit": "برداشت", "credit": "واریز"}
    batch = _create_batch(world, content=content, mapping_columns=columns)
    assert batch.total_rows == 2
    assert batch.new_rows == 1
    assert batch.invalid_rows == 1
    assert batch.status == ImportBatchStatus.AWAITING_REVIEW


def test_create_import_enforces_row_cap(world: dict) -> None:
    lines = ["تاریخ,شرح,مبلغ"] + [
        f"1405/08/26,ردیف {i},1000" for i in range(2001)
    ]
    content = "\n".join(lines).encode("utf-8")
    with pytest.raises(BankImportTooManyRowsError) as exc_info:
        _create_batch(
            world,
            content=content,
            mapping_columns={"date": "تاریخ", "description": "شرح", "amount": "مبلغ"},
        )
    assert exc_info.value.code == "BANK_IMPORT_TOO_MANY_ROWS"


def test_create_import_applies_learned_rule_coding(world: dict) -> None:
    world["bank"].save_rule(
        BankMatchRule(
            id="rule-1",
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            bank_key="",
            pattern="تأمین‌کننده",
            direction="out",
            target_account_id=601,
            target_party_id=None,
            target_kind="account",
            mode=SuggestionMode.SUGGEST,
            confirmations=3,
        )
    )
    batch = _create_batch(world)
    rows = world["bank"].list_rows(batch.id, TENANT)
    supplier = next(row for row in rows if "تأمین‌کننده" in row.description)
    assert supplier.suggested_account_id == 601
    assert supplier.suggestion_source == "rule"


def test_create_import_rejects_unknown_journal_and_account(world: dict) -> None:
    with pytest.raises(BankStatementJournalNotFoundError):
        _create_batch(world, statement_journal_id=9999)
    with pytest.raises(AccountNotFoundError):
        _create_batch(world, default_account_id=9999)


def test_create_import_rejects_bad_mapping(world: dict) -> None:
    with pytest.raises(BankColumnMappingInvalidError):
        _create_batch(world, mapping_columns={"date": "تاریخ"})
    with pytest.raises(BankColumnMappingInvalidError):
        _create_batch(world, mapping_columns=None, mapping_id=None)


def test_create_import_toman_unit_converts(world: dict) -> None:
    content = "تاریخ,شرح,مبلغ\n1405/08/26,واریز,۱۷۵\n".encode()
    batch = _create_batch(
        world,
        content=content,
        currency_unit=FileCurrencyUnit.TOMAN,
        mapping_columns={"date": "تاریخ", "description": "شرح", "amount": "مبلغ"},
    )
    assert batch.currency_unit == FileCurrencyUnit.TOMAN
    assert batch.total_deposits_irr == 1750


# -- import: confirm / cancel ------------------------------------------------------------


def test_confirm_import_writes_draft_statement(world: dict) -> None:
    batch = _create_batch(world)
    confirmed = world["imports"].confirm_import(
        tenant_id=TENANT, batch_id=batch.id, company=COMPANY, actor="operator"
    )
    assert confirmed.status == ImportBatchStatus.CONFIRMED
    assert confirmed.engine_statement_id is not None
    assert confirmed.confirmed_at == NOW
    statement = world["engine"].get_bank_statement(
        BankStatementRef(confirmed.engine_statement_id)
    )
    assert statement is not None
    assert statement.state == "draft"
    assert len(statement.lines) == 2
    assert statement.lines[0].amount.amount == 1_000_000
    assert statement.lines[0].account.engine_id == 900
    assert statement.lines[0].number == "REF-1"
    assert statement.start_balance.amount == 4_000_000
    item = world["queue"].get_item(batch.review_item_id, TENANT)
    assert item.status.value == "resolved"


def test_confirm_import_twice_is_a_conflict(world: dict) -> None:
    batch = _create_batch(world)
    world["imports"].confirm_import(
        tenant_id=TENANT, batch_id=batch.id, company=COMPANY, actor="operator"
    )
    with pytest.raises(BankImportInvalidStateError):
        world["imports"].confirm_import(
            tenant_id=TENANT, batch_id=batch.id, company=COMPANY, actor="operator"
        )


def test_confirm_import_with_no_new_rows_is_rejected(world: dict) -> None:
    _create_batch(world)
    second = _create_batch(world)  # all duplicates
    with pytest.raises(BankImportInvalidStateError, match="no new rows"):
        world["imports"].confirm_import(
            tenant_id=TENANT, batch_id=second.id, company=COMPANY, actor="operator"
        )


def test_cancel_import_resolves_the_review_item(world: dict) -> None:
    batch = _create_batch(world)
    cancelled = world["imports"].cancel_import(
        tenant_id=TENANT, batch_id=batch.id, actor="operator"
    )
    assert cancelled.status == ImportBatchStatus.CANCELLED
    assert cancelled.cancelled_at == NOW
    item = world["queue"].get_item(batch.review_item_id, TENANT)
    assert item.status.value == "resolved"
    assert world["engine"].statements == {}  # nothing reached the engine


def test_confirm_tolerates_an_already_resolved_review_item(world: dict) -> None:
    batch = _create_batch(world)
    # Resolve the item from the queue page first (operator action).
    world["review"].perform_action(
        item_id=batch.review_item_id,
        tenant_id=TENANT,
        action_code="approve_preview",
        actor="someone-else",
    )
    confirmed = world["imports"].confirm_import(
        tenant_id=TENANT, batch_id=batch.id, company=COMPANY, actor="operator"
    )
    assert confirmed.status == ImportBatchStatus.CONFIRMED


# -- mappings + rules ----------------------------------------------------------------------


def test_save_and_supersede_mapping(world: dict) -> None:
    mapping = world["imports"].save_mapping(
        tenant_id=TENANT,
        bank_key="melli",
        name="Baam",
        file_format=StatementFileFormat.CSV,
        columns=COLUMNS,
    )
    assert mapping.status == ColumnMappingStatus.ACTIVE
    superseded = world["imports"].supersede_mapping(
        tenant_id=TENANT, mapping_id=mapping.id
    )
    assert superseded.status == ColumnMappingStatus.SUPERSEDED


def test_save_mapping_rejects_bad_columns(world: dict) -> None:
    with pytest.raises(BankColumnMappingInvalidError):
        world["imports"].save_mapping(
            tenant_id=TENANT,
            bank_key="melli",
            name="bad",
            file_format=StatementFileFormat.CSV,
            columns={"date": "تاریخ"},
        )


def test_learn_rule_increments_confirmations(world: dict) -> None:
    first = world["imports"].learn_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        bank_key="",
        pattern="اسنپ",
        direction="out",
        target_account_id=601,
    )
    assert first.confirmations == 1
    assert first.mode == SuggestionMode.SUGGEST
    second = world["imports"].learn_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        bank_key="",
        pattern="اسنپ",
        direction="out",
        target_account_id=601,
    )
    assert second.id == first.id
    assert second.confirmations == 2


def test_set_rule_mode(world: dict) -> None:
    rule = world["imports"].learn_rule(
        tenant_id=TENANT,
        company_id=COMPANY_ID,
        bank_key="",
        pattern="اسنپ",
        direction="out",
        target_account_id=601,
    )
    updated = world["imports"].set_rule_mode(
        tenant_id=TENANT, rule_id=rule.id, mode=SuggestionMode.AUTO
    )
    assert updated.mode == SuggestionMode.AUTO


# -- manual match -----------------------------------------------------------------------


def test_manual_match_invoice_sets_related_invoice(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    statement_id = world["engine"].add_statement(lines=[_line()])
    match = world["imports"].apply_manual_match(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        statement_id=statement_id,
        line_id=9001,
        target_type=MatchTargetType.INVOICE,
        target_id="10",
        actor="operator",
    )
    assert match.status == MatchStatus.CONFIRMED
    assert match.confidence == 100
    assert match.destination_shaba == "IR062060000000100324200001"
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.related_invoice.engine_id == 10
    assert line.account.engine_id == 501
    assert line.party.engine_id == 5


def test_manual_match_payment_needs_an_account(world: dict) -> None:
    world["engine"].payments.append(_payment())
    statement_id = world["engine"].add_statement(lines=[_line()])
    with pytest.raises(BankImportInvalidStateError, match="coding account"):
        world["imports"].apply_manual_match(
            tenant_id=TENANT,
            company=COMPANY,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            line_id=9001,
            target_type=MatchTargetType.PAYMENT,
            target_id="20",
            actor="operator",
        )
    match = world["imports"].apply_manual_match(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        statement_id=statement_id,
        line_id=9001,
        target_type=MatchTargetType.PAYMENT,
        target_id="20",
        account_id=601,
        actor="operator",
    )
    assert match.status == MatchStatus.CONFIRMED
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.party.engine_id == 5
    assert line.account.engine_id == 601


def test_manual_match_twice_is_a_conflict(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    statement_id = world["engine"].add_statement(lines=[_line()])
    kwargs = {
        "tenant_id": TENANT,
        "company": COMPANY,
        "company_id": COMPANY_ID,
        "statement_id": statement_id,
        "line_id": 9001,
        "target_type": MatchTargetType.INVOICE,
        "target_id": "10",
        "actor": "operator",
    }
    world["imports"].apply_manual_match(**kwargs)
    with pytest.raises(BankLineAlreadyMatchedError):
        world["imports"].apply_manual_match(**kwargs)


def test_manual_match_on_validated_statement_is_rejected(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    statement_id = world["engine"].add_statement(state="validated", lines=[_line()])
    with pytest.raises(BankStatementNotDraftError):
        world["imports"].apply_manual_match(
            tenant_id=TENANT,
            company=COMPANY,
            company_id=COMPANY_ID,
            statement_id=statement_id,
            line_id=9001,
            target_type=MatchTargetType.INVOICE,
            target_id="10",
            actor="operator",
        )


def test_manual_match_with_save_as_rule_learns(world: dict) -> None:
    statement_id = world["engine"].add_statement(lines=[_line(description="پرداخت اسنپ")])
    world["imports"].apply_manual_match(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        statement_id=statement_id,
        line_id=9001,
        target_type=MatchTargetType.MANUAL,
        target_id="manual",
        account_id=601,
        save_as_rule=True,
        actor="operator",
    )
    rules = world["bank"].list_rules(TENANT, COMPANY_ID)
    assert len(rules) == 1
    assert rules[0].pattern == "پرداخت اسنپ"
    assert rules[0].target_account_id == 601
    assert rules[0].mode == SuggestionMode.SUGGEST


# -- reconcile ----------------------------------------------------------------------------


def _reconcile(world: dict, statement_id: int, **overrides):
    kwargs = {
        "tenant_id": TENANT,
        "installation_id": "install-1",
        "company": COMPANY,
        "company_id": COMPANY_ID,
        "statement_id": statement_id,
        "actor": "operator",
    }
    kwargs.update(overrides)
    return world["reconcile"].reconcile(**kwargs)


def test_reconcile_p2_auto_match_applies_and_records(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    statement_id = world["engine"].add_statement(lines=[_line()])
    result = _reconcile(world, statement_id)
    assert result.total_lines == 1
    assert result.matched_count == 1
    assert result.priority2_count == 1
    assert result.matched_total_irr == 1_000_000
    assert result.review_items_filed == 0
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.related_invoice.engine_id == 10
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    assert len(matches) == 1
    assert matches[0].status == MatchStatus.AUTO_APPLIED
    assert matches[0].destination_shaba == "IR062060000000100324200001"
    assert matches[0].outcome == ReconcileOutcome.MATCHED


def test_reconcile_p1_beats_p2(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    world["engine"].invoices.append(_invoice(ref=InvoiceRef(11), number="INV-200", reference="INV-200"))
    statement_id = world["engine"].add_statement(lines=[_line(number="INV-100")])
    result = _reconcile(world, statement_id)
    assert result.priority1_count == 1
    assert result.lines[0].target_id == 10


def test_reconcile_unmatched_files_one_review_item_and_is_idempotent(world: dict) -> None:
    statement_id = world["engine"].add_statement(lines=[_line()])
    first = _reconcile(world, statement_id)
    assert first.unmatched_count == 1
    assert first.review_items_filed == 1
    assert first.lines[0].review_item_id is not None
    item = world["queue"].get_item(first.lines[0].review_item_id, TENANT)
    assert item.item_type == ReviewItemType.BANK_UNMATCHED
    assert item.amount_irr == 1_000_000
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    assert len(matches) == 1
    assert matches[0].status == MatchStatus.UNMATCHED
    # Re-run: same outcome, no duplicate item, same item id.
    second = _reconcile(world, statement_id)
    assert second.unmatched_count == 1
    assert second.review_items_filed == 0
    assert second.lines[0].review_item_id == first.lines[0].review_item_id
    assert len(world["queue"].items) == 1


def test_reconcile_refiles_when_the_old_item_is_closed(world: dict) -> None:
    statement_id = world["engine"].add_statement(lines=[_line()])
    first = _reconcile(world, statement_id)
    world["review"].perform_action(
        item_id=first.lines[0].review_item_id,
        tenant_id=TENANT,
        action_code="manual_match",
        actor="operator",
    )
    second = _reconcile(world, statement_id)
    assert second.review_items_filed == 1
    assert second.lines[0].review_item_id != first.lines[0].review_item_id


def test_reconcile_suggested_line_records_and_files(world: dict) -> None:
    # Amount + party match, date outside the window: a P3 suggestion.
    world["engine"].invoices.append(_invoice(invoice_date=_fd(10), accounting_date=_fd(10)))
    statement_id = world["engine"].add_statement(
        lines=[_line(description="واریز مشتری نمونه")]
    )
    result = _reconcile(world, statement_id)
    assert result.suggested_count == 1
    assert result.lines[0].outcome == ReconcileOutcome.SUGGESTED
    assert result.lines[0].suggestions[0].target_id == 10
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    assert matches[0].status == MatchStatus.SUGGESTED
    assert matches[0].outcome == ReconcileOutcome.SUGGESTED
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.related_invoice is None  # suggestions never write the engine


def test_reconcile_ambiguous_line_needs_a_human(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    world["engine"].invoices.append(
        _invoice(ref=InvoiceRef(11), number="INV-200", reference="INV-200", invoice_date=_fd(1))
    )
    statement_id = world["engine"].add_statement(lines=[_line()])
    result = _reconcile(world, statement_id)
    assert result.ambiguous_count == 1
    assert result.review_items_filed == 1
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    assert matches[0].outcome == ReconcileOutcome.AMBIGUOUS


def test_reconcile_skips_already_matched_lines(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    statement_id = world["engine"].add_statement(lines=[_line()])
    _reconcile(world, statement_id)
    second = _reconcile(world, statement_id)
    assert second.already_matched_count == 1
    assert second.matched_count == 0
    assert second.review_items_filed == 0


def test_reconcile_respects_the_installation_window(world: dict) -> None:
    world["registry"].window_days = 5
    world["engine"].invoices.append(_invoice(invoice_date=_fd(4), accounting_date=_fd(4)))
    statement_id = world["engine"].add_statement(lines=[_line()])
    result = _reconcile(world, statement_id)
    assert result.matched_count == 1  # 4 days away, inside the 5-day window


def test_reconcile_applies_auto_rules(world: dict) -> None:
    world["bank"].save_rule(
        BankMatchRule(
            id="rule-1",
            tenant_id=TENANT,
            company_id=COMPANY_ID,
            bank_key="",
            pattern="اسنپ",
            direction="out",
            target_account_id=601,
            target_party_id=None,
            target_kind="account",
            mode=SuggestionMode.AUTO,
            confirmations=5,
        )
    )
    statement_id = world["engine"].add_statement(
        lines=[_line(amount_irr=-50_000, description="پرداخت اسنپ")]
    )
    result = _reconcile(world, statement_id)
    assert result.matched_count == 1
    assert result.lines[0].priority == 3
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.account.engine_id == 601


def test_reconcile_non_draft_is_rejected(world: dict) -> None:
    statement_id = world["engine"].add_statement(state="posted", lines=[_line()])
    with pytest.raises(BankStatementNotDraftError):
        _reconcile(world, statement_id)


def test_reconcile_payment_match_sets_party_only(world: dict) -> None:
    world["engine"].payments.append(_payment())
    statement_id = world["engine"].add_statement(lines=[_line()])
    result = _reconcile(world, statement_id)
    assert result.matched_count == 1
    assert result.lines[0].target_type == MatchTargetType.PAYMENT
    line = world["engine"].get_bank_statement(BankStatementRef(statement_id)).lines[0]
    assert line.party.engine_id == 5
    assert line.account.engine_id == 900  # the coded account is kept
    assert line.related_invoice is None  # related_to is invoices-only


def test_reconcile_skips_draft_invoices(world: dict) -> None:
    world["engine"].invoices.append(_invoice(state="draft"))
    statement_id = world["engine"].add_statement(lines=[_line()])
    result = _reconcile(world, statement_id)
    assert result.unmatched_count == 1  # a draft invoice cannot be linked


def test_reconcile_auto_match_supersedes_a_prior_suggestion(world: dict) -> None:
    # First run: only a suggestion (invoice date far away).
    world["engine"].invoices.append(_invoice(invoice_date=_fd(10), accounting_date=_fd(10)))
    statement_id = world["engine"].add_statement(
        lines=[_line(description="واریز مشتری نمونه")]
    )
    first = _reconcile(world, statement_id)
    assert first.suggested_count == 1
    old_item_id = first.lines[0].review_item_id
    # The invoice date is corrected to within the window; the next run auto-matches.
    world["engine"].invoices[0] = dataclasses.replace(
        world["engine"].invoices[0], invoice_date=_fd(), accounting_date=_fd()
    )
    second = _reconcile(world, statement_id)
    assert second.matched_count == 1
    matches = world["bank"].list_matches_for_statement(TENANT, statement_id)
    statuses = sorted(m.status for m in matches)
    assert statuses == [MatchStatus.AUTO_APPLIED, MatchStatus.REJECTED]
    old_item = world["queue"].get_item(old_item_id, TENANT)
    assert old_item.status.value == "resolved"  # the stale item is closed, not left open


# -- query service: reconciliation-status report ---------------------------------------


def test_reconciliation_status_report(world: dict) -> None:
    world["engine"].invoices.append(_invoice())
    matched_id = world["engine"].add_statement(
        lines=[_line(id=9001), _line(id=9002, amount_irr=777, description="واریز ناشناس")]
    )
    _reconcile(world, matched_id)
    cancelled_id = world["engine"].add_statement(
        state="cancelled", lines=[_line(id=9003, amount_irr=5)]
    )
    assert cancelled_id  # excluded from the report below
    status = world["queries"].reconciliation_status(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        date_from=_fd(-1),
        date_to=_fd(1),
    )
    assert status.company_id == COMPANY_ID
    assert status.freshness == "live"
    assert status.statements_count == 1  # the cancelled statement is excluded
    assert status.lines_total == 2
    assert status.matched_lines == 1
    assert status.unmatched_lines == 1
    assert status.match_rate == 0.5
    assert status.unmatched_balance_irr == 777
    assert len(status.rows) == 1
    row = status.rows[0]
    assert row.journal_id == 700
    assert row.destination_shaba == "IR062060000000100324200001"
    assert row.oldest_unmatched_age_days == 1  # line date 11-17, "now" is 11-18
    assert row.oldest_unmatched_line_id == 9002
    assert status.source_timestamp is not None


def test_reconciliation_status_empty_period(world: dict) -> None:
    status = world["queries"].reconciliation_status(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        date_from=_fd(30),
        date_to=_fd(31),
    )
    assert status.lines_total == 0
    assert status.match_rate is None
    assert status.freshness == "empty"
    assert status.rows == ()


def test_reconciliation_status_uses_engine_links(world: dict) -> None:
    # A line linked in the engine (e.g. from Sao) counts as matched
    # even with no HesabYar match record.
    statement_id = world["engine"].add_statement(
        lines=[_line(related_invoice=InvoiceRef(10))]
    )
    assert statement_id
    status = world["queries"].reconciliation_status(
        tenant_id=TENANT,
        company=COMPANY,
        company_id=COMPANY_ID,
        date_from=_fd(-1),
        date_to=_fd(1),
    )
    assert status.matched_lines == 1
    assert status.unmatched_lines == 0
