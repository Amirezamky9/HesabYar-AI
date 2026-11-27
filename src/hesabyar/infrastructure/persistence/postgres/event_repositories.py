"""SQLAlchemy implementation of the event-platform repository ports (US-501).

Maps between the event domain dataclasses and the ORM models. The
row-to-domain mappers live here — the application layer must never
see an ORM row (spec §6.3).

Concurrency (spec §10.4, phased design §1.3):

- Ingest serializes concurrent duplicates of the same source event
  with a transaction-scoped advisory lock
  (``pg_advisory_xact_lock(hashtext(...))``) plus the unique
  constraint as a backstop.
- The worker claims due events with ``SELECT ... FOR UPDATE SKIP
  LOCKED`` so concurrent workers never process the same event.

Like every other repository, these flush on every save but never
commit — the caller (REST route, worker pass) owns the transaction.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hesabyar.domain.events.envelope import CanonicalEvent, CanonicalEventType
from hesabyar.domain.events.models import (
    AutomationDecision,
    AutomationDecisionKind,
    ExternalIdMapping,
    GateResult,
    IdempotencyRecord,
    InboxEvent,
    InboxEventStatus,
    InboxStoreOutcome,
    IntegrationAttempt,
    OutboxEvent,
    OutboxEventStatus,
    StoreSettings,
)
from hesabyar.domain.events.pipeline import CLAIM_TIMEOUT, IDEMPOTENCY_TTL
from hesabyar.infrastructure.persistence.postgres.idempotency import (
    IdempotencyStore,
    utcnow,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    AutomationDecisionLogModel,
    ConnectorStoreSettingsModel,
    ExternalIdMapModel,
    IdempotencyRecordModel,
    InboxEventModel,
    IntegrationAttemptModel,
    OutboxEventModel,
)

#: Target engine key for the §10.4 external-entity mapping.
TARGET_ENGINE_TRYTON = "tryton"


# ---------------------------------------------------------------------------
# Row -> domain mappers
# ---------------------------------------------------------------------------


def _inbox_event_from_row(row: InboxEventModel) -> InboxEvent:
    """Map an inbox-event row onto the domain dataclass."""
    return InboxEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        schema_version=row.schema_version,
        source_system=row.source_system,
        source_store_id=row.source_store_id,
        source_event_id=row.source_event_id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        company_mapping=dict(row.company_mapping) if row.company_mapping is not None else None,
        currency=row.currency,
        external_entity_type=row.external_entity_type,
        external_entity_id=row.external_entity_id,
        correlation_id=row.correlation_id,
        idempotency_key=row.idempotency_key,
        payload=dict(row.payload),
        payload_hash=row.payload_hash,
        status=InboxEventStatus(row.status),
        attempts=row.attempts,
        last_attempt_at=row.last_attempt_at,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        processed_at=row.processed_at,
        dead_lettered_at=row.dead_lettered_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        installation_id=row.installation_id,
    )


def _outbox_event_from_row(row: OutboxEventModel) -> OutboxEvent:
    """Map an outbox-event row onto the domain dataclass."""
    return OutboxEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        payload=dict(row.payload),
        payload_hash=row.payload_hash,
        status=OutboxEventStatus(row.status),
        attempts=row.attempts,
        last_attempt_at=row.last_attempt_at,
        last_error=row.last_error,
        published_at=row.published_at,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _decision_from_row(row: AutomationDecisionLogModel) -> AutomationDecision:
    """Map a decision-log row onto the domain dataclass."""
    return AutomationDecision(
        id=row.id,
        tenant_id=row.tenant_id,
        inbox_event_id=row.inbox_event_id,
        event_type=row.event_type,
        decision=AutomationDecisionKind(row.decision),
        policy_version=row.policy_version,
        mapping_version=row.mapping_version,
        gate_results=tuple(
            GateResult(
                name=str(item.get("name", "")),
                passed=bool(item.get("passed", False)),
                reason=str(item.get("reason", "")),
            )
            for item in (row.gate_results or [])
            if isinstance(item, dict)
        ),
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        correlation_id=row.correlation_id,
        details=dict(row.details or {}),
    )


def _settings_from_row(row: ConnectorStoreSettingsModel) -> StoreSettings:
    """Map a store-settings row onto the domain dataclass."""
    return StoreSettings(
        source_system=row.source_system,
        source_store_id=row.source_store_id,
        prices_in_toman=row.prices_in_toman,
        currency=row.currency,
        updated_at=row.updated_at,
    )


def _idempotency_record_from_row(row: Any) -> IdempotencyRecord:
    """Map an idempotency-record row onto the domain dataclass."""
    return IdempotencyRecord(
        installation_id=row.installation_id,
        operation=row.operation,
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        response_status=row.response_status,
        response_body=dict(row.response_body) if row.response_body is not None else None,
        response_reference=row.response_reference,
        expires_at=row.expires_at,
    )


def _external_id_mapping_from_row(row: ExternalIdMapModel) -> ExternalIdMapping:
    """Map an external-id-map row onto the domain dataclass."""
    return ExternalIdMapping(
        source_system=row.source_system,
        external_entity_type=row.external_entity_type,
        external_entity_id=row.external_entity_id,
        target_engine=row.target_engine,
        target_entity_type=row.target_entity_type,
        target_external_id=row.target_external_id,
        engine_company_id=row.engine_company_id,
    )


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


class SqlAlchemyEventInboxRepository:
    """Postgres inbox repository: ingest, claims, staleness, lists."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def store(
        self,
        event: CanonicalEvent,
        event_type: CanonicalEventType,
        tenant_id: str | None,
        installation_id: str | None = None,
    ) -> InboxStoreOutcome:
        """Store a raw event + inbox status in one transaction (§10.3 step 2).

        A transaction-scoped advisory lock on the source-event
        identity serializes concurrent duplicates; the §10.4 unique
        constraint is the backstop if the lock is ever bypassed.
        The canonical type travels alongside the envelope and is
        stored in the ``event_type`` column (migration 0004); the
        installation that ingested the event is stored in
        ``installation_id`` (migration 0012, US-511).
        """
        lock_key = f"inbox:{event.source_system}:{event.source_store_id}:{event.source_event_id}"
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": lock_key},
        )
        existing = self.get_by_source_event(
            event.source_system, event.source_store_id, event.source_event_id
        )
        if existing is not None:
            if existing.payload_hash == event.payload_hash:
                return InboxStoreOutcome(status="duplicate", event=existing, existing_event_id=existing.id)
            return InboxStoreOutcome(status="conflict", event=None, existing_event_id=existing.id)
        row = InboxEventModel(
            id=event.id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            schema_version=event.schema_version,
            source_system=event.source_system,
            source_store_id=event.source_store_id,
            source_event_id=event.source_event_id,
            event_type=event_type.value,
            occurred_at=event.occurred_at,
            received_at=event.received_at,
            company_mapping=dict(event.company_mapping) if event.company_mapping is not None else None,
            currency=event.currency,
            external_entity_type=event.external_entity_type,
            external_entity_id=event.external_entity_id,
            correlation_id=event.correlation_id,
            idempotency_key=event.idempotency_key,
            payload=dict(event.payload),
            payload_hash=event.payload_hash,
            status=InboxEventStatus.RECEIVED.value,
            attempts=0,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            # Lost a race the advisory lock did not serialize (e.g. a
            # second database connection inserting the same event):
            # re-read and decide duplicate vs conflict.
            existing = self.get_by_source_event(
                event.source_system, event.source_store_id, event.source_event_id
            )
            if existing is None:
                raise
            if existing.payload_hash == event.payload_hash:
                return InboxStoreOutcome(status="duplicate", event=existing, existing_event_id=existing.id)
            return InboxStoreOutcome(status="conflict", event=None, existing_event_id=existing.id)
        self.session.flush()
        return InboxStoreOutcome(status="created", event=_inbox_event_from_row(row))

    def get(self, event_id: str, tenant_id: str | None = None) -> InboxEvent | None:
        """Return one inbox event, or None when it does not exist (or is of another tenant)."""
        row = self.session.get(InboxEventModel, event_id)
        if row is None:
            return None
        if tenant_id is not None and row.tenant_id is not None and row.tenant_id != tenant_id:
            return None
        return _inbox_event_from_row(row)

    def get_by_source_event(
        self, source_system: str, source_store_id: str, source_event_id: str
    ) -> InboxEvent | None:
        """Return an inbox event by its §10.4 source-event identity, or None."""
        stmt = select(InboxEventModel).where(
            InboxEventModel.source_system == source_system,
            InboxEventModel.source_store_id == source_store_id,
            InboxEventModel.source_event_id == source_event_id,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        return _inbox_event_from_row(row) if row is not None else None

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
        """List inbox events, newest first, optionally filtered."""
        stmt = select(InboxEventModel)
        if tenant_id is not None:
            stmt = stmt.where(InboxEventModel.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(InboxEventModel.status == status.value)
        if source_system is not None:
            stmt = stmt.where(InboxEventModel.source_system == source_system)
        if event_type is not None:
            stmt = stmt.where(InboxEventModel.event_type == event_type)
        stmt = stmt.order_by(InboxEventModel.received_at.desc()).limit(limit).offset(offset)
        return [_inbox_event_from_row(row) for row in self.session.execute(stmt).scalars()]

    def count_events(
        self,
        tenant_id: str | None = None,
        *,
        status: InboxEventStatus | None = None,
        source_system: str | None = None,
        event_type: str | None = None,
    ) -> int:
        """Count inbox events matching the same filters as :meth:`list_events`."""
        stmt = select(func.count()).select_from(InboxEventModel)
        if tenant_id is not None:
            stmt = stmt.where(InboxEventModel.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(InboxEventModel.status == status.value)
        if source_system is not None:
            stmt = stmt.where(InboxEventModel.source_system == source_system)
        if event_type is not None:
            stmt = stmt.where(InboxEventModel.event_type == event_type)
        return int(self.session.execute(stmt).scalar_one())

    def claim_due(self, limit: int) -> Sequence[InboxEvent]:
        """Claim due inbox events (``FOR UPDATE SKIP LOCKED``), oldest first."""
        now = utcnow()
        stmt = (
            select(InboxEventModel)
            .where(
                or_(
                    (InboxEventModel.status == InboxEventStatus.RECEIVED.value)
                    & (
                        (InboxEventModel.next_attempt_at.is_(None))
                        | (InboxEventModel.next_attempt_at <= now)
                    ),
                    (InboxEventModel.status == InboxEventStatus.PROCESSING.value)
                    & (InboxEventModel.updated_at <= now - CLAIM_TIMEOUT),
                )
            )
            .order_by(InboxEventModel.received_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list(self.session.execute(stmt).scalars())
        claimed: list[InboxEvent] = []
        for row in rows:
            row.status = InboxEventStatus.PROCESSING.value
            row.attempts = row.attempts + 1
            row.last_attempt_at = now
            row.updated_at = now
            claimed.append(_inbox_event_from_row(row))
        if rows:
            self.session.flush()
        return claimed

    def claim_one(self, event_id: str) -> InboxEvent | None:
        """Claim one inbox event for processing (``FOR UPDATE SKIP LOCKED``).

        Returns None when the event does not exist, is not due, is
        already in a terminal/review state, or is locked by another
        worker — the caller skips it and the sweep is the backstop.
        """
        now = utcnow()
        stmt = (
            select(InboxEventModel)
            .where(
                InboxEventModel.id == event_id,
                InboxEventModel.status.in_(
                    [InboxEventStatus.RECEIVED.value, InboxEventStatus.PROCESSING.value]
                ),
                or_(
                    InboxEventModel.next_attempt_at.is_(None),
                    InboxEventModel.next_attempt_at <= now,
                ),
            )
            .with_for_update(skip_locked=True)
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        row.status = InboxEventStatus.PROCESSING.value
        row.attempts = row.attempts + 1
        row.last_attempt_at = now
        row.updated_at = now
        self.session.flush()
        return _inbox_event_from_row(row)

    def save(self, event: InboxEvent) -> InboxEvent:
        """Insert or update an inbox event (by id); return the persisted event."""
        row = self.session.get(InboxEventModel, event.id)
        if row is None:
            row = InboxEventModel(id=event.id)
            self.session.add(row)
        row.tenant_id = event.tenant_id
        row.installation_id = event.installation_id
        row.schema_version = event.schema_version
        row.source_system = event.source_system
        row.source_store_id = event.source_store_id
        row.source_event_id = event.source_event_id
        row.event_type = event.event_type
        row.occurred_at = event.occurred_at
        row.received_at = event.received_at
        row.company_mapping = dict(event.company_mapping) if event.company_mapping is not None else None
        row.currency = event.currency
        row.external_entity_type = event.external_entity_type
        row.external_entity_id = event.external_entity_id
        row.correlation_id = event.correlation_id
        row.idempotency_key = event.idempotency_key
        row.payload = dict(event.payload)
        row.payload_hash = event.payload_hash
        row.status = event.status.value
        row.attempts = event.attempts
        row.last_attempt_at = event.last_attempt_at
        row.next_attempt_at = event.next_attempt_at
        row.last_error = event.last_error
        row.processed_at = event.processed_at
        row.dead_lettered_at = event.dead_lettered_at
        row.updated_at = event.updated_at
        self.session.flush()
        return _inbox_event_from_row(row)

    def list_stale(self, older_than: datetime.timedelta) -> Sequence[InboxEvent]:
        """Return inbox events stuck in received/processing longer than the threshold."""
        cutoff = utcnow() - older_than
        stmt = (
            select(InboxEventModel)
            .where(
                InboxEventModel.status.in_(
                    [InboxEventStatus.RECEIVED.value, InboxEventStatus.PROCESSING.value]
                ),
                InboxEventModel.received_at <= cutoff,
            )
            .order_by(InboxEventModel.received_at.asc())
        )
        return [_inbox_event_from_row(row) for row in self.session.execute(stmt).scalars()]

    def count_stale(self, older_than: datetime.timedelta) -> int:
        """Count inbox events stuck longer than the threshold."""
        cutoff = utcnow() - older_than
        stmt = (
            select(func.count())
            .select_from(InboxEventModel)
            .where(
                InboxEventModel.status.in_(
                    [InboxEventStatus.RECEIVED.value, InboxEventStatus.PROCESSING.value]
                ),
                InboxEventModel.received_at <= cutoff,
            )
        )
        return int(self.session.execute(stmt).scalar_one())


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


class SqlAlchemyEventOutboxRepository:
    """Postgres outbox repository: result events + attempts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, event: OutboxEvent) -> OutboxEvent:
        """Insert or update an outbox event (by id); return the persisted event."""
        row = self.session.get(OutboxEventModel, event.id)
        if row is None:
            row = OutboxEventModel(id=event.id)
            self.session.add(row)
        row.tenant_id = event.tenant_id
        row.aggregate_type = event.aggregate_type
        row.aggregate_id = event.aggregate_id
        row.event_type = event.event_type
        row.payload = dict(event.payload)
        row.payload_hash = event.payload_hash
        row.status = event.status.value
        row.attempts = event.attempts
        row.last_attempt_at = event.last_attempt_at
        row.next_attempt_at = None
        row.last_error = event.last_error
        row.published_at = event.published_at
        row.correlation_id = event.correlation_id
        row.updated_at = event.updated_at
        self.session.flush()
        return _outbox_event_from_row(row)

    def get(self, event_id: str) -> OutboxEvent | None:
        """Return one outbox event, or None when it does not exist."""
        row = self.session.get(OutboxEventModel, event_id)
        return _outbox_event_from_row(row) if row is not None else None

    def list_events(
        self,
        tenant_id: str | None = None,
        *,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[OutboxEvent]:
        """List outbox events, newest first, optionally filtered."""
        stmt = select(OutboxEventModel)
        if tenant_id is not None:
            stmt = stmt.where(OutboxEventModel.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(OutboxEventModel.status == status)
        if event_type is not None:
            stmt = stmt.where(OutboxEventModel.event_type == event_type)
        stmt = stmt.order_by(OutboxEventModel.created_at.desc()).limit(limit).offset(offset)
        return [_outbox_event_from_row(row) for row in self.session.execute(stmt).scalars()]

    def claim_pending(self, limit: int) -> Sequence[OutboxEvent]:
        """Claim pending outbox events (``FOR UPDATE SKIP LOCKED``), oldest first."""
        now = utcnow()
        stmt = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.status == OutboxEventStatus.PENDING.value,
                or_(
                    OutboxEventModel.next_attempt_at.is_(None),
                    OutboxEventModel.next_attempt_at <= now,
                ),
            )
            .order_by(OutboxEventModel.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list(self.session.execute(stmt).scalars())
        return [_outbox_event_from_row(row) for row in rows]

    def save_integration_attempt(self, attempt: IntegrationAttempt) -> IntegrationAttempt:
        """Append an integration attempt; return the persisted attempt."""
        row = IntegrationAttemptModel(
            id=attempt.id,
            inbox_event_id=attempt.inbox_event_id,
            outbox_event_id=attempt.outbox_event_id,
            attempt_number=attempt.attempt_number,
            status=attempt.status.value,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            error_code=attempt.error_code,
            error_message=attempt.error_message,
            created_at=attempt.created_at,
        )
        self.session.add(row)
        self.session.flush()
        return attempt


# ---------------------------------------------------------------------------
# Idempotency / external-id map / audit log / store settings
# ---------------------------------------------------------------------------


class SqlAlchemyEventIdempotencyStore:
    """Idempotency-key store (§10.4, second constraint) over ``IdempotencyStore``.

    Completed records carry the 30-day event TTL
    (:data:`hesabyar.domain.events.pipeline.IDEMPOTENCY_TTL`); the
    underlying store's 24-hour default is overridden here because
    store webhooks retry for days, not minutes.
    """

    def __init__(self, session: Session) -> None:
        self._store = IdempotencyStore(session)
        self.session = session

    def find(self, installation_id: str, operation: str, key: str) -> IdempotencyRecord | None:
        """Return the record for a key, or None when the key is unused."""
        row = self._store.find(installation_id, operation, key)
        return _idempotency_record_from_row(row) if row is not None else None

    def claim(
        self, installation_id: str, operation: str, key: str, request_hash: str
    ) -> IdempotencyRecord | None:
        """Insert a pending record claiming the key; None when already held."""
        row = self._store.claim(installation_id, operation, key, request_hash)
        return _idempotency_record_from_row(row) if row is not None else None

    def complete(
        self,
        record: IdempotencyRecord,
        *,
        response_status: int,
        response_body: Mapping[str, Any],
        response_reference: str | None = None,
    ) -> None:
        """Store a completed response on a claimed record for replay (30-day TTL)."""
        row = self._store.find(record.installation_id, record.operation, record.idempotency_key)
        if row is None:
            return
        self._store.complete(
            row,
            response_status=response_status,
            response_body=dict(response_body),
            response_reference=response_reference,
        )
        row.expires_at = utcnow() + IDEMPOTENCY_TTL
        self.session.flush()

    def release(self, record: IdempotencyRecord) -> None:
        """Delete a pending record after a failed operation (its key frees up)."""
        row = self._store.find(record.installation_id, record.operation, record.idempotency_key)
        if row is not None and not IdempotencyStore.is_completed(row):
            self._store.release(row)

    def purge_expired(self, now: datetime.datetime) -> int:
        """Delete expired records; return how many were purged."""
        stmt = delete(IdempotencyRecordModel).where(
            IdempotencyRecordModel.expires_at.is_not(None),
            IdempotencyRecordModel.expires_at <= now,
        )
        result = self.session.execute(stmt)
        self.session.flush()
        if isinstance(result, CursorResult):
            return int(result.rowcount or 0)
        return 0


class SqlAlchemyExternalIdMapRepository:
    """Postgres external-id map (§10.4, third constraint)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, source_system: str, external_entity_type: str, external_entity_id: str
    ) -> ExternalIdMapping | None:
        """Return the engine record an external entity maps to, or None."""
        stmt = select(ExternalIdMapModel).where(
            ExternalIdMapModel.source_system == source_system,
            ExternalIdMapModel.external_entity_type == external_entity_type,
            ExternalIdMapModel.external_entity_id == external_entity_id,
            ExternalIdMapModel.target_engine == TARGET_ENGINE_TRYTON,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        return _external_id_mapping_from_row(row) if row is not None else None

    def save(self, mapping: ExternalIdMapping) -> ExternalIdMapping:
        """Insert a mapping; on conflict return the existing row (never a duplicate).

        A mapping without a tenant cannot be stored — the table is
        tenant-scoped — so it is returned unchanged without a row.
        In practice the pipeline always has a tenant (ingest sets it
        from the authenticated client); the inbox unique constraint
        still dedups the event itself for tenant-less events.
        """
        existing = self.get(
            mapping.source_system, mapping.external_entity_type, mapping.external_entity_id
        )
        if existing is not None:
            return existing
        if mapping.tenant_id is None:
            return mapping
        row = ExternalIdMapModel(
            id=str(uuid.uuid4()),
            tenant_id=mapping.tenant_id,
            source_system=mapping.source_system,
            external_entity_type=mapping.external_entity_type,
            external_entity_id=mapping.external_entity_id,
            target_engine=mapping.target_engine,
            target_entity_type=mapping.target_entity_type,
            target_external_id=mapping.target_external_id,
            engine_company_id=mapping.engine_company_id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            existing = self.get(
                mapping.source_system, mapping.external_entity_type, mapping.external_entity_id
            )
            if existing is None:
                raise
            return existing
        self.session.flush()
        return _external_id_mapping_from_row(row)


class SqlAlchemyDecisionAuditLogRepository:
    """Postgres automation-decision audit log (append-only)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, decision: AutomationDecision) -> AutomationDecision:
        """Append a decision; return the persisted decision."""
        row = AutomationDecisionLogModel(
            id=decision.id,
            tenant_id=decision.tenant_id,
            inbox_event_id=decision.inbox_event_id,
            event_type=decision.event_type,
            decision=decision.decision.value,
            policy_version=decision.policy_version,
            mapping_version=decision.mapping_version,
            gate_results=[
                {"name": gate.name, "passed": gate.passed, "reason": gate.reason}
                for gate in decision.gate_results
            ],
            decided_by=decision.decided_by,
            decided_at=decision.decided_at,
            correlation_id=decision.correlation_id,
            details=dict(decision.details),
        )
        self.session.add(row)
        self.session.flush()
        return _decision_from_row(row)

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
        """List decisions, newest first, optionally filtered."""
        stmt = select(AutomationDecisionLogModel)
        if tenant_id is not None:
            stmt = stmt.where(AutomationDecisionLogModel.tenant_id == tenant_id)
        if inbox_event_id is not None:
            stmt = stmt.where(AutomationDecisionLogModel.inbox_event_id == inbox_event_id)
        if decision is not None:
            stmt = stmt.where(AutomationDecisionLogModel.decision == decision)
        if event_type is not None:
            stmt = stmt.where(AutomationDecisionLogModel.event_type == event_type)
        stmt = stmt.order_by(AutomationDecisionLogModel.decided_at.desc()).limit(limit).offset(offset)
        return [_decision_from_row(row) for row in self.session.execute(stmt).scalars()]


class SqlAlchemyStoreSettingsRepository:
    """Postgres per-store connector settings."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, source_system: str, source_store_id: str) -> StoreSettings | None:
        """Return a store's settings, or None when the store has none stored."""
        stmt = select(ConnectorStoreSettingsModel).where(
            ConnectorStoreSettingsModel.source_system == source_system,
            ConnectorStoreSettingsModel.source_store_id == source_store_id,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        return _settings_from_row(row) if row is not None else None

    def save(self, settings: StoreSettings) -> StoreSettings:
        """Insert or update a store's settings (by source identity)."""
        stmt = select(ConnectorStoreSettingsModel).where(
            ConnectorStoreSettingsModel.source_system == settings.source_system,
            ConnectorStoreSettingsModel.source_store_id == settings.source_store_id,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = ConnectorStoreSettingsModel(
                id=str(uuid.uuid4()),
                source_system=settings.source_system,
                source_store_id=settings.source_store_id,
            )
            self.session.add(row)
        row.prices_in_toman = settings.prices_in_toman
        row.currency = settings.currency
        row.updated_at = settings.updated_at or utcnow()
        self.session.flush()
        return _settings_from_row(row)

    def list_settings(self) -> Sequence[StoreSettings]:
        """Return every stored store's settings, ordered by source identity."""
        stmt = select(ConnectorStoreSettingsModel).order_by(
            ConnectorStoreSettingsModel.source_system.asc(),
            ConnectorStoreSettingsModel.source_store_id.asc(),
        )
        return [_settings_from_row(row) for row in self.session.execute(stmt).scalars()]
