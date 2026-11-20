"""Shared fixtures for the event-platform unit tests (US-501).

In-memory fakes of every event port + the engine port, following
``test_application_services.py`` (unused port methods raise
``NotImplementedError``). No database, no server.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from hesabyar.domain.accounting.commands import (
    CreateAnalyticAccount,
    CreateBankStatement,
    CreateConsumptionMove,
    CreateCreditNote,
    CreateEngineCheque,
    CreateInventory,
    CreateInvoiceDraft,
    CreateJournalDraft,
    CreatePackagingProduct,
    CreatePayment,
    CreateSale,
    PostJournal,
    ReconcileMoveLines,
    ReverseJournal,
    SucceedPayment,
    TransitionEngineCheque,
    UpdateBankStatementLine,
    UpdateInventoryLine,
    UpdateJournalDraft,
    UpsertOrderPoint,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    AnalyticAccountRef,
    BankStatementRef,
    CheckbookEngineRef,
    ChequeEngineRef,
    CompanyRef,
    FiscalYearRef,
    InventoryRef,
    InvoiceRef,
    JournalDraftRef,
    JournalRef,
    OutShipmentRef,
    PartyRef,
    PaymentJournalRef,
    PaymentRef,
    PostedJournalRef,
    ProductRef,
    SaleRef,
    StockLocationRef,
)
from hesabyar.domain.accounting.value_objects import (
    AccountBalanceView,
    AccountView,
    AnalyticAccountView,
    AnalyticLineView,
    BankStatementView,
    CheckbookEngineView,
    ChequeEngineView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
    InventoryView,
    InvoiceLineView,
    InvoiceTotalView,
    InvoiceView,
    JournalDraftView,
    MoveLineView,
    OrderPointView,
    OutShipmentCostView,
    PackagingProductView,
    PaymentJournalView,
    PaymentView,
    PostedJournalView,
    PurchaseRequestView,
    ReconciliationView,
    SaleLineView,
    SaleShipmentCostView,
    SaleView,
    StatementJournalView,
    StockLocationView,
    StockMoveView,
    TrialBalanceView,
    ValidationIssue,
    ValidationSummary,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import (
    HesabYarError,
    InvalidInvoiceError,
    InvoiceNotFoundError,
    PaymentNotFoundError,
    SaleNotFoundError,
)
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.events.envelope import (
    CanonicalEvent,
    CanonicalEventType,
    canonical_payload_hash,
)
from hesabyar.domain.events.manual_entries import ManualEntry, ManualEntryType
from hesabyar.domain.events.models import (
    AutomationDecision,
    ExternalIdMapping,
    IdempotencyRecord,
    InboxEvent,
    InboxEventStatus,
    InboxStoreOutcome,
    IntegrationAttempt,
    NormalizedEvent,
    OutboxEvent,
    OutboxEventStatus,
    PolicyDecision,
    ResolvedMapping,
    ReviewItemDraft,
    StoreSettings,
)

TENANT_ID = "tenant-event-tests"
INSTALLATION_ID = "installation-event-tests"

NOW = datetime.datetime(2026, 11, 17, 12, 0, 0, tzinfo=datetime.UTC)


def raw_envelope(
    *,
    source_system: str = "hayat",
    source_store_id: str = "store-1",
    source_event_id: str = "evt-1",
    payload: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a raw envelope dict (valid by default; overrides replace fields).

    Exactly the 15 §10.2 fields — no ``event_type``: the type
    travels alongside the envelope (see ``ingest_into``).
    """
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "source_system": source_system,
        "source_store_id": source_store_id,
        "source_event_id": source_event_id,
        "occurred_at": "2026-11-17T10:00:00Z",
        "currency": "IRR",
        "external_entity_type": "order",
        "external_entity_id": "order-1",
        "correlation_id": "corr-1",
        "payload": payload if payload is not None else {"total_amount": 175000},
    }
    envelope.update(overrides)
    return envelope


def ingest_into(
    inbox: FakeInboxRepository,
    event_type: CanonicalEventType | str = CanonicalEventType.SALES_ORDER_COMPLETED,
    **overrides: Any,
) -> str:
    """Store an event through a fake inbox and return its id.

    The event type is passed alongside the envelope, as the
    receiver does at ingest.
    """
    event = CanonicalEvent.from_raw(raw_envelope(**overrides))
    outcome = inbox.store(event, CanonicalEventType(event_type), TENANT_ID)
    assert outcome.status == "created"
    assert outcome.event is not None
    return outcome.event.id


def must_get(inbox: FakeInboxRepository, event_id: str) -> InboxEvent:
    """Fetch an inbox event, failing the test when it is missing."""
    event = inbox.get(event_id)
    assert event is not None
    return event


def must_get_outbox(outbox: FakeOutboxRepository, event_id: str) -> OutboxEvent:
    """Fetch an outbox event, failing the test when it is missing."""
    event = outbox.get(event_id)
    assert event is not None
    return event


def make_inbox_event(
    event: CanonicalEvent | None = None,
    *,
    event_type: CanonicalEventType | str = CanonicalEventType.SALES_ORDER_COMPLETED,
    tenant_id: str | None = TENANT_ID,
    installation_id: str | None = None,
    status: InboxEventStatus = InboxEventStatus.RECEIVED,
    attempts: int = 0,
    received_at: datetime.datetime | None = None,
    next_attempt_at: datetime.datetime | None = None,
    last_error: str | None = None,
) -> InboxEvent:
    """Build an InboxEvent from a canonical event (or a default one).

    The event type is passed alongside the envelope — callers
    rebuilding a stored event pass its stored ``event_type``.
    ``installation_id`` is the installation that ingested the event
    (US-511); None means pre-0012 / unknown.
    """
    canonical = event or CanonicalEvent.from_raw(raw_envelope())
    return InboxEvent(
        id=canonical.id,
        tenant_id=tenant_id,
        schema_version=canonical.schema_version,
        source_system=canonical.source_system,
        source_store_id=canonical.source_store_id,
        source_event_id=canonical.source_event_id,
        event_type=CanonicalEventType(event_type).value,
        occurred_at=canonical.occurred_at,
        received_at=received_at or canonical.received_at,
        company_mapping=canonical.company_mapping,
        currency=canonical.currency,
        external_entity_type=canonical.external_entity_type,
        external_entity_id=canonical.external_entity_id,
        correlation_id=canonical.correlation_id,
        idempotency_key=canonical.idempotency_key,
        payload=dict(canonical.payload),
        payload_hash=canonical.payload_hash,
        status=status,
        attempts=attempts,
        last_attempt_at=None,
        next_attempt_at=next_attempt_at,
        last_error=last_error,
        processed_at=None,
        dead_lettered_at=None,
        created_at=NOW,
        updated_at=NOW,
        installation_id=installation_id,
    )


class FakeInboxRepository:
    """In-memory EventInboxRepositoryPort."""

    def __init__(self) -> None:
        self.events: dict[str, InboxEvent] = {}

    def store(
        self,
        event: CanonicalEvent,
        event_type: CanonicalEventType,
        tenant_id: str | None,
        installation_id: str | None = None,
    ) -> InboxStoreOutcome:
        existing = self.get_by_source_event(
            event.source_system, event.source_store_id, event.source_event_id
        )
        if existing is not None:
            if existing.payload_hash == event.payload_hash:
                return InboxStoreOutcome(status="duplicate", event=existing, existing_event_id=existing.id)
            return InboxStoreOutcome(status="conflict", event=None, existing_event_id=existing.id)
        stored = make_inbox_event(
            event, event_type=event_type, tenant_id=tenant_id, installation_id=installation_id
        )
        self.events[stored.id] = stored
        return InboxStoreOutcome(status="created", event=stored)

    def get(self, event_id: str, tenant_id: str | None = None) -> InboxEvent | None:
        event = self.events.get(event_id)
        if event is None:
            return None
        if tenant_id is not None and event.tenant_id is not None and event.tenant_id != tenant_id:
            return None
        return event

    def get_by_source_event(
        self, source_system: str, source_store_id: str, source_event_id: str
    ) -> InboxEvent | None:
        for event in self.events.values():
            if (
                event.source_system == source_system
                and event.source_store_id == source_store_id
                and event.source_event_id == source_event_id
            ):
                return event
        return None

    def list_events(
        self,
        tenant_id: str | None = None,
        *,
        status: InboxEventStatus | None = None,
        source_system: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[InboxEvent]:
        events = list(self.events.values())
        if tenant_id is not None:
            events = [e for e in events if e.tenant_id == tenant_id]
        if status is not None:
            events = [e for e in events if e.status == status]
        if source_system is not None:
            events = [e for e in events if e.source_system == source_system]
        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]
        events.sort(key=lambda e: e.received_at, reverse=True)
        return events[offset : offset + limit]

    def count_events(
        self,
        tenant_id: str | None = None,
        *,
        status: InboxEventStatus | None = None,
        source_system: str | None = None,
        event_type: str | None = None,
    ) -> int:
        return len(
            self.list_events(
                tenant_id, status=status, source_system=source_system, event_type=event_type, limit=100000
            )
        )

    def claim_due(self, limit: int) -> Sequence[InboxEvent]:
        now = datetime.datetime.now(datetime.UTC)
        due = [
            e
            for e in self.events.values()
            if e.status == InboxEventStatus.RECEIVED
            and (e.next_attempt_at is None or e.next_attempt_at <= now)
        ]
        due.sort(key=lambda e: e.received_at)
        claimed: list[InboxEvent] = []
        for event in due[:limit]:
            claimed_event = dataclasses.replace(
                event,
                status=InboxEventStatus.PROCESSING,
                attempts=event.attempts + 1,
                last_attempt_at=now,
            )
            self.events[event.id] = claimed_event
            claimed.append(claimed_event)
        return claimed

    def claim_one(self, event_id: str) -> InboxEvent | None:
        event = self.events.get(event_id)
        if event is None:
            return None
        if event.status not in (InboxEventStatus.RECEIVED, InboxEventStatus.PROCESSING):
            return None
        now = datetime.datetime.now(datetime.UTC)
        if event.next_attempt_at is not None and event.next_attempt_at > now:
            return None
        claimed = dataclasses.replace(
            event,
            status=InboxEventStatus.PROCESSING,
            attempts=event.attempts + 1,
            last_attempt_at=now,
        )
        self.events[event.id] = claimed
        return claimed

    def save(self, event: InboxEvent) -> InboxEvent:
        self.events[event.id] = event
        return event

    def list_stale(self, older_than: datetime.timedelta) -> Sequence[InboxEvent]:
        cutoff = datetime.datetime.now(datetime.UTC) - older_than
        stale = [
            e
            for e in self.events.values()
            if e.status in (InboxEventStatus.RECEIVED, InboxEventStatus.PROCESSING)
            and e.received_at <= cutoff
        ]
        stale.sort(key=lambda e: e.received_at)
        return stale

    def count_stale(self, older_than: datetime.timedelta) -> int:
        return len(self.list_stale(older_than))


class FakeOutboxRepository:
    """In-memory EventOutboxRepositoryPort."""

    def __init__(self) -> None:
        self.events: dict[str, OutboxEvent] = {}
        self.attempts: list[IntegrationAttempt] = []

    def save(self, event: OutboxEvent) -> OutboxEvent:
        self.events[event.id] = event
        return event

    def get(self, event_id: str) -> OutboxEvent | None:
        return self.events.get(event_id)

    def list_events(
        self,
        tenant_id: str | None = None,
        *,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[OutboxEvent]:
        events = list(self.events.values())
        if tenant_id is not None:
            events = [e for e in events if e.tenant_id == tenant_id]
        if status is not None:
            events = [e for e in events if e.status.value == status]
        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]
        events.sort(key=lambda e: e.created_at, reverse=True)
        return events[offset : offset + limit]

    def claim_pending(self, limit: int) -> Sequence[OutboxEvent]:
        pending = [e for e in self.events.values() if e.status == OutboxEventStatus.PENDING]
        pending.sort(key=lambda e: e.created_at)
        return pending[:limit]

    def save_integration_attempt(self, attempt: IntegrationAttempt) -> IntegrationAttempt:
        self.attempts.append(attempt)
        return attempt


class FakeIdempotencyStore:
    """In-memory EventIdempotencyPort."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str], IdempotencyRecord] = {}

    def find(self, installation_id: str, operation: str, key: str) -> IdempotencyRecord | None:
        return self.records.get((installation_id, operation, key))

    def claim(
        self, installation_id: str, operation: str, key: str, request_hash: str
    ) -> IdempotencyRecord | None:
        if (installation_id, operation, key) in self.records:
            return None
        record = IdempotencyRecord(
            installation_id=installation_id,
            operation=operation,
            idempotency_key=key,
            request_hash=request_hash,
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=10),
        )
        self.records[(installation_id, operation, key)] = record
        return record

    def complete(
        self,
        record: IdempotencyRecord,
        *,
        response_status: int,
        response_body: Mapping[str, Any],
        response_reference: str | None = None,
    ) -> None:
        completed = dataclasses.replace(
            record,
            response_status=response_status,
            response_body=dict(response_body),
            response_reference=response_reference,
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
        )
        self.records[(record.installation_id, record.operation, record.idempotency_key)] = completed

    def release(self, record: IdempotencyRecord) -> None:
        self.records.pop((record.installation_id, record.operation, record.idempotency_key), None)

    def purge_expired(self, now: datetime.datetime) -> int:
        expired = [
            key for key, record in self.records.items() if record.expires_at is not None and record.expires_at <= now
        ]
        for key in expired:
            del self.records[key]
        return len(expired)


class FakeExternalIdMap:
    """In-memory ExternalIdMapPort."""

    def __init__(self) -> None:
        self.mappings: dict[tuple[str, str, str], ExternalIdMapping] = {}

    def get(
        self, source_system: str, external_entity_type: str, external_entity_id: str
    ) -> ExternalIdMapping | None:
        return self.mappings.get((source_system, external_entity_type, external_entity_id))

    def save(self, mapping: ExternalIdMapping) -> ExternalIdMapping:
        key = (mapping.source_system, mapping.external_entity_type, mapping.external_entity_id)
        existing = self.mappings.get(key)
        if existing is not None:
            return existing
        self.mappings[key] = mapping
        return mapping


class FakeAuditLog:
    """In-memory DecisionAuditLogPort."""

    def __init__(self) -> None:
        self.decisions: list[AutomationDecision] = []

    def record(self, decision: AutomationDecision) -> AutomationDecision:
        self.decisions.append(decision)
        return decision

    def list_decisions(
        self,
        tenant_id: str | None = None,
        *,
        inbox_event_id: str | None = None,
        decision: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AutomationDecision]:
        decisions = list(self.decisions)
        if tenant_id is not None:
            decisions = [d for d in decisions if d.tenant_id == tenant_id]
        if inbox_event_id is not None:
            decisions = [d for d in decisions if d.inbox_event_id == inbox_event_id]
        if decision is not None:
            decisions = [d for d in decisions if d.decision.value == decision]
        if event_type is not None:
            decisions = [d for d in decisions if d.event_type == event_type]
        decisions.sort(key=lambda d: d.decided_at, reverse=True)
        return decisions[offset : offset + limit]


class FakeStoreSettings:
    """In-memory StoreSettingsPort."""

    def __init__(self) -> None:
        self.settings: dict[tuple[str, str], StoreSettings] = {}

    def get(self, source_system: str, source_store_id: str) -> StoreSettings | None:
        return self.settings.get((source_system, source_store_id))

    def save(self, settings: StoreSettings) -> StoreSettings:
        self.settings[(settings.source_system, settings.source_store_id)] = settings
        return settings

    def list_settings(self) -> Sequence[StoreSettings]:
        return sorted(self.settings.values(), key=lambda s: (s.source_system, s.source_store_id))


class FakeWebhookSecrets:
    """In-memory WebhookSecretPort."""

    def __init__(self, secrets: dict[tuple[str, str], str] | None = None) -> None:
        self.secrets = dict(secrets or {})

    def get_secret(
        self,
        source_system: str,
        source_store_id: str,
        installation_id: str | None = None,
    ) -> str | None:
        return self.secrets.get((source_system, source_store_id))


class FakeManualEntryRepository:
    """In-memory ManualEntryRepositoryPort (US-513)."""

    def __init__(self) -> None:
        self.entries: dict[str, ManualEntry] = {}

    def save(self, entry: ManualEntry) -> ManualEntry:
        self.entries[entry.id] = entry
        return entry

    def get(self, entry_id: str, tenant_id: str | None = None) -> ManualEntry | None:
        entry = self.entries.get(entry_id)
        if entry is None:
            return None
        if tenant_id is not None and entry.tenant_id != tenant_id:
            return None
        return entry

    def list_entries(
        self,
        tenant_id: str | None = None,
        *,
        entry_type: ManualEntryType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ManualEntry]:
        entries = list(self.entries.values())
        if tenant_id is not None:
            entries = [e for e in entries if e.tenant_id == tenant_id]
        if entry_type is not None:
            entries = [e for e in entries if e.entry_type == entry_type]
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries[offset : offset + limit]


class FakeReviewQueue:
    """In-memory ReviewQueuePort."""

    def __init__(self) -> None:
        self.items: list[ReviewItemDraft] = []

    def enqueue(self, item: ReviewItemDraft) -> str:
        self.items.append(item)
        return f"review-{len(self.items)}"


def _fake_account(engine_id: int, code: str, name: str) -> AccountView:
    """Build one chart entry for the fake engine's chart of accounts."""
    return AccountView(
        ref=AccountRef(engine_id),
        company=CompanyRef(1),
        code=code,
        name=name,
        type_name="other",
        parent=None,
        closed=False,
        party_required=False,
        active=True,
    )


#: The fake engine's chart: receivable 1.2.1, revenue 4.1.1, shipping
#: revenue 4.1.2, payable 2.1.1 (mirrors the dev chart's codes).
FAKE_CHART: tuple[AccountView, ...] = (
    _fake_account(11, "1.2.1", "دریافتنی"),
    _fake_account(12, "4.1.1", "درآمد فروش"),
    _fake_account(13, "4.1.2", "درآمد حمل"),
    _fake_account(14, "2.1.1", "پرداختنی"),
)


class FakeEngine:
    """In-memory AccountingEnginePort for event-pipeline tests.

    Drafts live in dicts; ``fail_next`` injects one failure into the
    next engine call (transient or permanent, per the error given).
    Invoices, sales and payments are real in-memory documents with
    state transitions (US-511: the store pipeline builds them).
    """

    def __init__(self) -> None:
        self.drafts: dict[int, CreateJournalDraft] = {}
        self.invoices: dict[int, InvoiceView] = {}
        self.sales: dict[int, SaleView] = {}
        self.payments: dict[int, PaymentView] = {}
        self.posted: set[int] = set()
        self._next_id = 1
        self.fail_next: HesabYarError | None = None
        self.invalid_draft_ids: set[int] = set()
        self.create_calls = 0
        self.post_calls = 0

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise failure

    def _next_engine_id(self) -> int:
        engine_id = self._next_id
        self._next_id += 1
        return engine_id

    def list_companies(self) -> Sequence[CompanyView]:
        raise NotImplementedError("event tests never list companies")

    def get_chart_of_accounts(self, company: CompanyRef) -> Sequence[AccountView]:
        self._maybe_fail()
        return FAKE_CHART

    def create_journal_draft(self, command: CreateJournalDraft) -> JournalDraftRef:
        self._maybe_fail()
        self.create_calls += 1
        draft_id = self._next_id
        self._next_id += 1
        self.drafts[draft_id] = command
        return JournalDraftRef(draft_id)

    def validate_journal_draft(self, draft: JournalDraftRef) -> ValidationSummary:
        self._maybe_fail()
        if draft.engine_id in self.invalid_draft_ids:
            return ValidationSummary.failed(
                [ValidationIssue(code="DRAFT_INVALID", message="Seeded invalid draft.")]
            )
        return ValidationSummary.ok()

    def get_journal_draft(self, draft: JournalDraftRef) -> JournalDraftView | None:
        raise NotImplementedError("event tests never read drafts")

    def list_journal_drafts(self, company: CompanyRef) -> Sequence[JournalDraftView]:
        raise NotImplementedError("event tests never list drafts")

    def update_journal_draft(self, command: UpdateJournalDraft) -> JournalDraftView:
        raise NotImplementedError("event tests never update drafts")

    def list_invoice_totals(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceTotalView]:
        raise NotImplementedError

    def get_account_by_code(self, company: CompanyRef, code: str) -> AccountView | None:
        raise NotImplementedError

    def get_general_journal(self, company: CompanyRef) -> JournalRef:
        raise NotImplementedError

    def delete_journal_draft(self, draft: JournalDraftRef) -> None:
        raise NotImplementedError

    def get_account_balance(
        self, company: CompanyRef, account: AccountRef, as_of: FiscalDate
    ) -> AccountBalanceView:
        raise NotImplementedError

    def post_journal(self, command: PostJournal) -> PostedJournalRef:
        self._maybe_fail()
        self.post_calls += 1
        self.posted.add(command.draft.engine_id)
        return PostedJournalRef(command.draft.engine_id)

    def reverse_journal(self, command: ReverseJournal) -> PostedJournalRef:
        raise NotImplementedError("event tests never reverse journals")

    def get_journal(self, posted: PostedJournalRef) -> PostedJournalView | None:
        raise NotImplementedError("event tests never read journals")

    def list_fiscal_years(self, company: CompanyRef) -> Sequence[FiscalYearView]:
        raise NotImplementedError("event tests never list fiscal years")

    def list_periods(
        self, company: CompanyRef, fiscal_year: FiscalYearRef | None = None
    ) -> Sequence[FiscalPeriodView]:
        raise NotImplementedError("event tests never list periods")

    def get_trial_balance(
        self,
        company: CompanyRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
    ) -> TrialBalanceView:
        raise NotImplementedError("event tests never read trial balances")

    def create_invoice_draft(self, command: CreateInvoiceDraft) -> InvoiceRef:
        self._maybe_fail()
        self.create_calls += 1
        invoice_id = self._next_engine_id()
        total = command.total_amount
        lines = tuple(
            InvoiceLineView(
                account=line.account,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
                description=line.description,
            )
            for line in command.lines
        )
        self.invoices[invoice_id] = InvoiceView(
            ref=InvoiceRef(invoice_id),
            company=command.company,
            invoice_type=command.invoice_type,
            party=command.party,
            party_name="",
            number=None,
            invoice_date=command.date,
            accounting_date=None,
            state="draft",
            description=command.description,
            reference=command.reference,
            journal=command.journal,
            account=command.account or AccountRef(11),
            currency_code="IRR",
            untaxed_amount=total,
            tax_amount=MoneyIRR(0),
            total_amount=total,
            lines=lines,
            amount_to_pay=total,
        )
        return InvoiceRef(invoice_id)

    def validate_invoice_draft(self, invoice: InvoiceRef) -> ValidationSummary:
        self._maybe_fail()
        if invoice.engine_id in self.invalid_draft_ids:
            return ValidationSummary.failed(
                [ValidationIssue(code="DRAFT_INVALID", message="Seeded invalid draft.")]
            )
        return ValidationSummary.ok()

    def get_invoice(self, invoice: InvoiceRef) -> InvoiceView | None:
        self._maybe_fail()
        return self.invoices.get(invoice.engine_id)

    def list_invoices(
        self,
        company: CompanyRef,
        invoice_type: str | None = None,
        state: str | None = None,
    ) -> Sequence[InvoiceView]:
        return [
            view
            for view in self.invoices.values()
            if view.company == company
            and (invoice_type is None or view.invoice_type == invoice_type)
            and (state is None or view.state == state)
        ]

    def validate_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        self._maybe_fail()
        view = self.invoices.get(invoice.engine_id)
        if view is None:
            raise InvoiceNotFoundError(
                f"Invoice {invoice.engine_id} does not exist.",
                details={"invoice_id": invoice.engine_id},
            )
        if view.state != "draft":
            raise InvalidInvoiceError(
                f"Invoice {invoice.engine_id} is {view.state}; only a draft can be validated.",
                details={"invoice_id": invoice.engine_id, "state": view.state},
            )
        validated = dataclasses.replace(view, state="validated")
        self.invoices[invoice.engine_id] = validated
        return validated

    def post_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        self._maybe_fail()
        self.post_calls += 1
        view = self.invoices.get(invoice.engine_id)
        if view is None:
            raise InvoiceNotFoundError(
                f"Invoice {invoice.engine_id} does not exist.",
                details={"invoice_id": invoice.engine_id},
            )
        if view.state != "validated":
            raise InvalidInvoiceError(
                f"Invoice {invoice.engine_id} is {view.state}; only a validated invoice can be posted.",
                details={"invoice_id": invoice.engine_id, "state": view.state},
            )
        posted = dataclasses.replace(view, state="posted", number=f"INV-{invoice.engine_id}")
        self.invoices[invoice.engine_id] = posted
        return posted

    def cancel_invoice(self, invoice: InvoiceRef) -> InvoiceView:
        self._maybe_fail()
        view = self.invoices.get(invoice.engine_id)
        if view is None:
            raise InvoiceNotFoundError(
                f"Invoice {invoice.engine_id} does not exist.",
                details={"invoice_id": invoice.engine_id},
            )
        if view.state not in ("draft", "validated"):
            raise InvalidInvoiceError(
                f"Invoice {invoice.engine_id} is {view.state}; only a draft or validated "
                "invoice can be cancelled.",
                details={"invoice_id": invoice.engine_id, "state": view.state},
            )
        cancelled = dataclasses.replace(view, state="cancelled")
        self.invoices[invoice.engine_id] = cancelled
        return cancelled

    def create_credit_note(self, command: CreateCreditNote) -> InvoiceRef:
        self._maybe_fail()
        self.create_calls += 1
        source = self.invoices.get(command.invoice.engine_id)
        if source is None:
            raise InvoiceNotFoundError(
                f"Invoice {command.invoice.engine_id} does not exist in the engine.",
                details={"invoice_id": command.invoice.engine_id},
            )
        if source.state != "posted":
            raise InvalidInvoiceError(
                f"Invoice {command.invoice.engine_id} is not posted; only a posted invoice "
                "can be credited.",
                details={"invoice_id": command.invoice.engine_id, "state": source.state},
            )
        if command.partial_amount is not None:
            if command.partial_amount.amount >= source.total_amount.amount:
                raise InvalidInvoiceError(
                    f"Partial credit amount {command.partial_amount.amount} IRR must be "
                    f"less than the source invoice total {source.total_amount.amount} IRR.",
                    details={"invoice_id": command.invoice.engine_id},
                )
            accounts = {line.account for line in source.lines if line.line_type == "line"}
            if len(accounts) != 1:
                raise InvalidInvoiceError(
                    f"Partial credit needs a single revenue account; invoice "
                    f"{command.invoice.engine_id} lines use {len(accounts)} accounts.",
                    details={"invoice_id": command.invoice.engine_id},
                )
            total = MoneyIRR(-command.partial_amount.amount)
            lines = (
                InvoiceLineView(
                    account=accounts.pop(),
                    quantity=Decimal(-1),
                    unit_price=command.partial_amount,
                    amount=total,
                    description=f"Partial credit ({command.partial_amount.amount} IRR)",
                ),
            )
        else:
            total = MoneyIRR(-source.total_amount.amount)
            lines = tuple(
                InvoiceLineView(
                    account=line.account,
                    quantity=-line.quantity,
                    unit_price=line.unit_price,
                    amount=MoneyIRR(-line.amount.amount),
                    description=line.description,
                )
                for line in source.lines
            )
        credit_id = self._next_engine_id()
        self.invoices[credit_id] = InvoiceView(
            ref=InvoiceRef(credit_id),
            company=source.company,
            invoice_type=source.invoice_type,
            party=source.party,
            party_name=source.party_name,
            number=None,
            invoice_date=command.date,
            accounting_date=None,
            state="draft",
            description=command.description,
            reference="",
            journal=source.journal,
            account=source.account,
            currency_code=source.currency_code,
            untaxed_amount=total,
            tax_amount=MoneyIRR(0),
            total_amount=total,
            lines=lines,
            amount_to_pay=total,
        )
        return InvoiceRef(credit_id)

    def create_sale(self, command: CreateSale) -> SaleRef:
        self._maybe_fail()
        self.create_calls += 1
        sale_id = self._next_engine_id()
        total = command.total_amount
        lines = tuple(
            SaleLineView(
                product=line.product,
                product_name="",
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
                description=line.description,
            )
            for line in command.lines
        )
        self.sales[sale_id] = SaleView(
            ref=SaleRef(sale_id),
            company=command.company,
            party=command.party,
            party_name="",
            number=None,
            sale_date=None,
            state="draft",
            invoice_method=command.invoice_method,
            shipment_method=command.shipment_method,
            invoice_state="none",
            shipment_state="none",
            currency_code="IRR",
            untaxed_amount=total,
            total_amount=total,
            description=command.description,
            reference=command.reference,
            lines=lines,
        )
        return SaleRef(sale_id)

    def get_sale(self, sale: SaleRef) -> SaleView | None:
        self._maybe_fail()
        return self.sales.get(sale.engine_id)

    def list_sales(
        self,
        company: CompanyRef,
        state: str | None = None,
    ) -> Sequence[SaleView]:
        return [
            view
            for view in self.sales.values()
            if view.company == company and (state is None or view.state == state)
        ]

    def _transition_sale(self, sale: SaleRef, state: str) -> SaleView:
        view = self.sales.get(sale.engine_id)
        if view is None:
            raise SaleNotFoundError(
                f"Sale {sale.engine_id} does not exist.",
                details={"sale_id": sale.engine_id},
            )
        transitioned = dataclasses.replace(view, state=state)
        self.sales[sale.engine_id] = transitioned
        return transitioned

    def quote_sale(self, sale: SaleRef) -> SaleView:
        self._maybe_fail()
        return self._transition_sale(sale, "quotation")

    def confirm_sale(self, sale: SaleRef) -> SaleView:
        self._maybe_fail()
        return self._transition_sale(sale, "confirmed")

    def process_sale(self, sale: SaleRef) -> SaleView:
        self._maybe_fail()
        return self._transition_sale(sale, "processing")

    def cancel_sale(self, sale: SaleRef) -> SaleView:
        self._maybe_fail()
        return self._transition_sale(sale, "cancelled")

    def create_payment(self, command: CreatePayment) -> PaymentRef:
        self._maybe_fail()
        self.create_calls += 1
        payment_id = self._next_engine_id()
        self.payments[payment_id] = PaymentView(
            ref=PaymentRef(payment_id),
            company=command.company,
            number=f"PAY-{payment_id}",
            kind=command.kind,
            party=command.party,
            party_name="",
            date=command.date,
            amount=command.amount,
            state="draft",
            reference=command.reference,
            journal_name=(
                f"journal-{command.journal.engine_id}" if command.journal is not None else ""
            ),
            currency_code="IRR",
            move_line=command.line,
        )
        return PaymentRef(payment_id)

    def _transition_payment(self, payment: PaymentRef, state: str) -> PaymentView:
        view = self.payments.get(payment.engine_id)
        if view is None:
            raise PaymentNotFoundError(
                f"Payment {payment.engine_id} does not exist.",
                details={"payment_id": payment.engine_id},
            )
        transitioned = dataclasses.replace(view, state=state)
        self.payments[payment.engine_id] = transitioned
        return transitioned

    def submit_payment(self, payment: PaymentRef) -> PaymentView:
        self._maybe_fail()
        return self._transition_payment(payment, "submitted")

    def approve_payment(self, payment: PaymentRef) -> PaymentView:
        self._maybe_fail()
        return self._transition_payment(payment, "approved")

    def process_payment(self, payment: PaymentRef) -> PaymentView:
        self._maybe_fail()
        return self._transition_payment(payment, "processing")

    def succeed_payment(self, command: SucceedPayment) -> PaymentView:
        self._maybe_fail()
        return self._transition_payment(command.payment, "succeeded")

    def fail_payment(self, payment: PaymentRef) -> PaymentView:
        self._maybe_fail()
        return self._transition_payment(payment, "failed")

    def process_payments(self, payments: Sequence[PaymentRef]) -> Sequence[PaymentView]:
        raise NotImplementedError("event tests never batch-process payments")

    def get_payment_journal(
        self, journal: PaymentJournalRef
    ) -> PaymentJournalView | None:
        raise NotImplementedError("event tests never read payment journals")

    def list_move_lines(
        self,
        company: CompanyRef,
        *,
        move: JournalDraftRef | None = None,
        account: AccountRef | None = None,
        party: PartyRef | None = None,
        maturity_date: FiscalDate | None = None,
        unreconciled_only: bool = False,
    ) -> Sequence[MoveLineView]:
        raise NotImplementedError("event tests never list move lines")

    def reconcile_move_lines(self, command: ReconcileMoveLines) -> ReconciliationView:
        raise NotImplementedError("event tests never reconcile move lines")

    def get_payment(self, payment: PaymentRef) -> PaymentView | None:
        self._maybe_fail()
        return self.payments.get(payment.engine_id)

    def list_payments(
        self,
        company: CompanyRef,
        kind: str | None = None,
        state: str | None = None,
    ) -> Sequence[PaymentView]:
        return [
            view
            for view in self.payments.values()
            if view.company == company
            and (kind is None or view.kind == kind)
            and (state is None or view.state == state)
        ]

    def get_bank_statement(self, statement: BankStatementRef) -> BankStatementView | None:
        raise NotImplementedError("event tests never read statements")

    def create_bank_statement(self, command: CreateBankStatement) -> BankStatementRef:
        raise NotImplementedError("this fake never creates statements")

    def update_bank_statement_line(self, command: UpdateBankStatementLine) -> None:
        raise NotImplementedError("this fake never reconciles statements")

    def list_statement_journals(self, company: CompanyRef) -> Sequence[StatementJournalView]:
        return []

    def list_bank_statements(
        self, company: CompanyRef, state: str | None = None
    ) -> Sequence[BankStatementView]:
        raise NotImplementedError("event tests never list statements")

    # -- packaging & shipping (US-515): stubs, never exercised here --

    def list_stock_locations(self, company: CompanyRef) -> Sequence[StockLocationView]:
        raise NotImplementedError

    def get_stock_location(self, location: StockLocationRef) -> StockLocationView | None:
        raise NotImplementedError

    def create_packaging_product(self, command: CreatePackagingProduct) -> ProductRef:
        raise NotImplementedError

    def get_packaging_product(
        self,
        product: ProductRef,
        company: CompanyRef,
        location: StockLocationRef | None = None,
    ) -> PackagingProductView | None:
        raise NotImplementedError

    def list_packaging_products(
        self,
        company: CompanyRef,
        products: Sequence[ProductRef],
        location: StockLocationRef | None = None,
    ) -> Sequence[PackagingProductView]:
        raise NotImplementedError

    def get_product_quantity(
        self, product: ProductRef, location: StockLocationRef
    ) -> Decimal:
        raise NotImplementedError

    def get_product_cost_price(
        self, product: ProductRef, company: CompanyRef
    ) -> Decimal | None:
        raise NotImplementedError

    def upsert_order_point(self, command: UpsertOrderPoint) -> OrderPointView:
        raise NotImplementedError

    def get_order_point(
        self, product: ProductRef, warehouse: StockLocationRef, company: CompanyRef
    ) -> OrderPointView | None:
        raise NotImplementedError

    def list_order_points(self, company: CompanyRef) -> Sequence[OrderPointView]:
        raise NotImplementedError

    def create_consumption_move(self, command: CreateConsumptionMove) -> StockMoveView:
        raise NotImplementedError

    def list_consumption_moves(
        self,
        company: CompanyRef,
        from_location: StockLocationRef,
        to_location: StockLocationRef,
        date_from: FiscalDate,
        date_to: FiscalDate,
        product: ProductRef | None = None,
    ) -> Sequence[StockMoveView]:
        raise NotImplementedError

    def list_purchase_requests(
        self,
        company: CompanyRef,
        state: str | None = None,
        after_id: int | None = None,
    ) -> Sequence[PurchaseRequestView]:
        raise NotImplementedError

    def create_analytic_account(self, command: CreateAnalyticAccount) -> AnalyticAccountRef:
        raise NotImplementedError

    def get_analytic_account(
        self, account: AnalyticAccountRef
    ) -> AnalyticAccountView | None:
        raise NotImplementedError

    def list_analytic_lines(
        self, account: AnalyticAccountRef
    ) -> Sequence[AnalyticLineView]:
        raise NotImplementedError

    def create_inventory(self, command: CreateInventory) -> InventoryRef:
        raise NotImplementedError

    def get_inventory(self, inventory: InventoryRef) -> InventoryView | None:
        raise NotImplementedError

    def complete_inventory_lines(self, inventory: InventoryRef) -> InventoryView:
        raise NotImplementedError

    def update_inventory_line(self, command: UpdateInventoryLine) -> InventoryView:
        raise NotImplementedError

    def confirm_inventory(self, inventory: InventoryRef) -> InventoryView:
        raise NotImplementedError

    def cancel_inventory(self, inventory: InventoryRef) -> InventoryView:
        raise NotImplementedError

    def get_sale_shipment_costs(self, sale: SaleRef) -> Sequence[SaleShipmentCostView]:
        raise NotImplementedError

    def get_out_shipment_cost(self, shipment: OutShipmentRef) -> OutShipmentCostView | None:
        raise NotImplementedError

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


def resolving_mapping(
    *,
    version: str = "mapping-v3",
    company_id: int = 1,
) -> ResolvedMapping:
    """A complete mapping (company + two accounts + journal)."""
    return ResolvedMapping(
        mapping_version=version,
        company=CompanyRef(company_id),
        accounts={"receivable": AccountRef(11), "revenue": AccountRef(12)},
        journal=JournalRef(7),
        party=PartyRef(5),
        unresolved=(),
    )


def build_draft_command(event: NormalizedEvent, mapping: ResolvedMapping) -> CreateJournalDraft:
    """Build a balanced two-line draft from a normalized event's total."""
    total = event.total_amount_rial or MoneyIRR(0)
    if total.amount <= 0:
        total = MoneyIRR(1000)
    assert mapping.company is not None
    from hesabyar.domain.accounting.commands import JournalDraftLine

    return CreateJournalDraft(
        company=mapping.company,
        date=FiscalDate.from_gregorian(event.event.occurred_at.date()),
        lines=(
            JournalDraftLine(account=mapping.accounts["receivable"], debit=total),
            JournalDraftLine(account=mapping.accounts["revenue"], credit=total),
        ),
        description=f"Event {event.event_type.value} {event.event.source_event_id}",
        journal=mapping.journal,
    )


class FakeMappingResolver:
    """MappingResolverPort returning a fixed mapping (or raising)."""

    def __init__(self, mapping: ResolvedMapping | None = None, error: HesabYarError | None = None) -> None:
        self.mapping = mapping if mapping is not None else resolving_mapping()
        self.error = error
        self.calls = 0

    def resolve(self, event: NormalizedEvent) -> ResolvedMapping:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.mapping


class FakeDraftBuilder:
    """DraftBuilderPort building a balanced draft from the event total."""

    def __init__(self, error: HesabYarError | None = None) -> None:
        self.error = error
        self.calls = 0

    def build(self, event: NormalizedEvent, mapping: ResolvedMapping) -> CreateJournalDraft:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return build_draft_command(event, mapping)


class FakePolicyGate:
    """PolicyGatePort with a fixed verdict."""

    def __init__(self, allowed: bool, version: str = "policy-v2") -> None:
        self.allowed = allowed
        self.version = version
        self.calls = 0

    def evaluate(
        self,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        draft_total: MoneyIRR,
    ) -> PolicyDecision:
        from hesabyar.domain.events.models import GateResult

        self.calls += 1
        return PolicyDecision(
            allowed=self.allowed,
            policy_version=self.version,
            gate_results=(
                GateResult(name="event_type", passed=True, reason="Event type is allowed."),
                GateResult(name="amount_cap", passed=True, reason="Under the cap."),
                GateResult(name="period_lock", passed=True, reason="Period is open."),
            ),
            reason="Allowed by the test gate." if self.allowed else "Denied by the test gate.",
        )


def make_outbox_event(**overrides: Any) -> OutboxEvent:
    """Build an OutboxEvent (pending by default)."""
    payload = overrides.pop("payload", {"inbox_event_id": "evt-x"})
    event = OutboxEvent(
        id=str(uuid.uuid4()),
        tenant_id=TENANT_ID,
        aggregate_type="inbox_event",
        aggregate_id="evt-x",
        event_type="events.processed",
        payload=payload,
        payload_hash=canonical_payload_hash(payload),
        status=OutboxEventStatus.PENDING,
        attempts=0,
        last_attempt_at=None,
        last_error=None,
        published_at=None,
        correlation_id="corr-1",
        created_at=NOW,
        updated_at=NOW,
    )
    return dataclasses.replace(event, **overrides)
