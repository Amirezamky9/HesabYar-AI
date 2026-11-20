"""SQLAlchemy implementation of the Moadian repository port (US-404).

Maps between the Moadian domain dataclasses and the ORM models. The
row-to-domain mappers live here — the application layer must never
see an ORM row (spec §6.3).

Follows the ``ApiKeyRepository`` style in ``repositories.py``: the
repository takes a session and flushes on every save, but never
commits — the caller (REST route) owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hesabyar.domain.moadian.models import (
    DocumentStatus,
    MoadianDocument,
    MoadianEnvironment,
    MoadianProfile,
    MoadianStatusEvent,
    MoadianSubmission,
    OfficialStatus,
    SenderMode,
    SimulationScenario,
    StatusEventSource,
    SubmissionStatus,
)
from hesabyar.domain.moadian.ports import (
    IntegrationAttempt,
    IntegrationAttemptStatus,
    OutboxEvent,
    OutboxEventStatus,
)
from hesabyar.domain.standards.models import VerificationStatus
from hesabyar.infrastructure.persistence.postgres.models import (
    IntegrationAttemptModel,
    MoadianDocumentModel,
    MoadianProfileModel,
    MoadianStatusEventModel,
    MoadianSubmissionModel,
    OutboxEventModel,
)

# ---------------------------------------------------------------------------
# Row -> domain mappers
# ---------------------------------------------------------------------------


def _profile_from_row(row: MoadianProfileModel) -> MoadianProfile:
    """Map a profile row onto the domain dataclass."""
    return MoadianProfile(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        slug=row.slug,
        environment=MoadianEnvironment(row.environment),
        sender_mode=SenderMode(row.sender_mode),
        fiscal_id=row.fiscal_id,
        economic_code=row.economic_code,
        packet_type=row.packet_type,
        api_base_url=row.api_base_url,
        protocol_version=row.protocol_version,
        source_authority=row.source_authority,
        source_url=row.source_url,
        snapshot_uri=row.snapshot_uri,
        verification_status=VerificationStatus(row.verification_status),
        key_reference=row.key_reference,
        key_fingerprint=row.key_fingerprint,
        is_active=row.is_active,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _document_from_row(row: MoadianDocumentModel) -> MoadianDocument:
    """Map a document row onto the domain dataclass."""
    return MoadianDocument(
        id=row.id,
        tenant_id=row.tenant_id,
        profile_id=row.profile_id,
        invoice_id=row.invoice_id,
        taxid=row.taxid,
        serial=row.serial,
        invoice_type=row.invoice_type,
        invoice_pattern=row.invoice_pattern,
        invoice_subject=row.invoice_subject,
        payload=dict(row.payload),
        payload_hash=row.payload_hash,
        status=DocumentStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _submission_from_row(row: MoadianSubmissionModel) -> MoadianSubmission:
    """Map a submission row onto the domain dataclass."""
    return MoadianSubmission(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        profile_id=row.profile_id,
        uid=row.uid,
        reference_number=row.reference_number,
        correlation_id=row.correlation_id,
        environment=MoadianEnvironment(row.environment),
        scenario=SimulationScenario(row.scenario),
        status=SubmissionStatus(row.status),
        retry=row.retry,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        approval_note=row.approval_note,
        request_hash=row.request_hash,
        response_hash=row.response_hash,
        request_payload=dict(row.request_payload) if row.request_payload is not None else None,
        response_payload=dict(row.response_payload) if row.response_payload is not None else None,
        outbox_event_id=row.outbox_event_id,
        last_error_code=row.last_error_code,
        last_error_detail=row.last_error_detail,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _status_event_from_row(row: MoadianStatusEventModel) -> MoadianStatusEvent:
    """Map a status-event row onto the domain dataclass."""
    return MoadianStatusEvent(
        id=row.id,
        submission_id=row.submission_id,
        status=OfficialStatus(row.status),
        source=StatusEventSource(row.source),
        evidence=dict(row.evidence) if row.evidence is not None else None,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
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


def _integration_attempt_from_row(row: IntegrationAttemptModel) -> IntegrationAttempt:
    """Map an integration-attempt row onto the domain dataclass."""
    return IntegrationAttempt(
        id=row.id,
        outbox_event_id=row.outbox_event_id,
        attempt_number=row.attempt_number,
        status=IntegrationAttemptStatus(row.status),
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Domain -> row appliers (shared by insert and update)
# ---------------------------------------------------------------------------


def _apply_profile(row: MoadianProfileModel, profile: MoadianProfile) -> None:
    row.tenant_id = profile.tenant_id
    row.name = profile.name
    row.slug = profile.slug
    row.environment = profile.environment.value
    row.sender_mode = profile.sender_mode.value
    row.fiscal_id = profile.fiscal_id
    row.economic_code = profile.economic_code
    row.packet_type = profile.packet_type
    row.api_base_url = profile.api_base_url
    row.protocol_version = profile.protocol_version
    row.source_authority = profile.source_authority
    row.source_url = profile.source_url
    row.snapshot_uri = profile.snapshot_uri
    row.verification_status = profile.verification_status.value
    row.key_reference = profile.key_reference
    row.key_fingerprint = profile.key_fingerprint
    row.is_active = profile.is_active
    row.notes = profile.notes
    row.created_at = profile.created_at
    row.updated_at = profile.updated_at


def _apply_document(row: MoadianDocumentModel, document: MoadianDocument) -> None:
    row.tenant_id = document.tenant_id
    row.profile_id = document.profile_id
    row.invoice_id = document.invoice_id
    row.taxid = document.taxid
    row.serial = document.serial
    row.invoice_type = document.invoice_type
    row.invoice_pattern = document.invoice_pattern
    row.invoice_subject = document.invoice_subject
    row.payload = dict(document.payload)
    row.payload_hash = document.payload_hash
    row.status = document.status.value
    row.created_at = document.created_at
    row.updated_at = document.updated_at


def _apply_submission(row: MoadianSubmissionModel, submission: MoadianSubmission) -> None:
    row.tenant_id = submission.tenant_id
    row.document_id = submission.document_id
    row.profile_id = submission.profile_id
    row.uid = submission.uid
    row.reference_number = submission.reference_number
    row.correlation_id = submission.correlation_id
    row.environment = submission.environment.value
    row.scenario = submission.scenario.value
    row.status = submission.status.value
    row.retry = submission.retry
    row.approved_by = submission.approved_by
    row.approved_at = submission.approved_at
    row.approval_note = submission.approval_note
    row.request_hash = submission.request_hash
    row.response_hash = submission.response_hash
    row.request_payload = dict(submission.request_payload) if submission.request_payload is not None else None
    row.response_payload = dict(submission.response_payload) if submission.response_payload is not None else None
    row.outbox_event_id = submission.outbox_event_id
    row.last_error_code = submission.last_error_code
    row.last_error_detail = submission.last_error_detail
    row.created_at = submission.created_at
    row.updated_at = submission.updated_at


def _apply_outbox_event(row: OutboxEventModel, event: OutboxEvent) -> None:
    row.tenant_id = event.tenant_id
    row.aggregate_type = event.aggregate_type
    row.aggregate_id = event.aggregate_id
    row.event_type = event.event_type
    row.payload = dict(event.payload)
    row.payload_hash = event.payload_hash
    row.status = event.status.value
    row.attempts = event.attempts
    row.last_attempt_at = event.last_attempt_at
    row.last_error = event.last_error
    row.published_at = event.published_at
    row.correlation_id = event.correlation_id
    row.created_at = event.created_at
    row.updated_at = event.updated_at


class SqlAlchemyMoadianRepository:
    """Moadian persistence over one SQLAlchemy session (US-404).

    Implements :class:`hesabyar.domain.moadian.ports.MoadianRepositoryPort`.
    Saves flush but never commit — the caller owns the transaction.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- profiles ----------------------------------------------------------

    def get_profile(self, profile_id: str, tenant_id: str) -> MoadianProfile | None:
        """Return one profile, or None when it does not exist in the tenant."""
        stmt = select(MoadianProfileModel).where(
            MoadianProfileModel.id == profile_id,
            MoadianProfileModel.tenant_id == tenant_id,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        return _profile_from_row(row) if row is not None else None

    def list_profiles(
        self,
        tenant_id: str,
        *,
        environment: MoadianEnvironment | None = None,
        is_active: bool | None = None,
    ) -> Sequence[MoadianProfile]:
        """Return the tenant's profiles, optionally filtered, ordered by slug."""
        stmt = select(MoadianProfileModel).where(MoadianProfileModel.tenant_id == tenant_id)
        if environment is not None:
            stmt = stmt.where(MoadianProfileModel.environment == environment.value)
        if is_active is not None:
            stmt = stmt.where(MoadianProfileModel.is_active == is_active)
        stmt = stmt.order_by(MoadianProfileModel.slug)
        return [_profile_from_row(row) for row in self.session.execute(stmt).scalars()]

    def profile_slug_exists(
        self,
        tenant_id: str,
        slug: str,
        *,
        exclude_id: str | None = None,
    ) -> bool:
        """True when the tenant already has a profile with this slug."""
        stmt = select(MoadianProfileModel.id).where(
            MoadianProfileModel.tenant_id == tenant_id,
            MoadianProfileModel.slug == slug,
        )
        if exclude_id is not None:
            stmt = stmt.where(MoadianProfileModel.id != exclude_id)
        return self.session.execute(stmt).scalar_one_or_none() is not None

    def save_profile(self, profile: MoadianProfile) -> MoadianProfile:
        """Insert or update a profile (by id); return the persisted profile."""
        row = self.session.get(MoadianProfileModel, profile.id)
        if row is None:
            row = MoadianProfileModel(id=profile.id)
            self.session.add(row)
        _apply_profile(row, profile)
        self.session.flush()
        return _profile_from_row(row)

    # -- documents ---------------------------------------------------------

    def get_document(self, document_id: str, tenant_id: str) -> MoadianDocument | None:
        """Return one document, or None when it does not exist in the tenant."""
        stmt = select(MoadianDocumentModel).where(
            MoadianDocumentModel.id == document_id,
            MoadianDocumentModel.tenant_id == tenant_id,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        return _document_from_row(row) if row is not None else None

    def list_documents(
        self,
        tenant_id: str,
        *,
        profile_id: str | None = None,
        status: DocumentStatus | None = None,
    ) -> Sequence[MoadianDocument]:
        """Return the tenant's documents, optionally filtered, oldest first."""
        stmt = select(MoadianDocumentModel).where(MoadianDocumentModel.tenant_id == tenant_id)
        if profile_id is not None:
            stmt = stmt.where(MoadianDocumentModel.profile_id == profile_id)
        if status is not None:
            stmt = stmt.where(MoadianDocumentModel.status == status.value)
        stmt = stmt.order_by(MoadianDocumentModel.created_at, MoadianDocumentModel.id)
        return [_document_from_row(row) for row in self.session.execute(stmt).scalars()]

    def document_taxid_exists(self, tenant_id: str, taxid: str) -> bool:
        """True when the tenant already has a document with this tax id."""
        stmt = select(MoadianDocumentModel.id).where(
            MoadianDocumentModel.tenant_id == tenant_id,
            MoadianDocumentModel.taxid == taxid,
        )
        return self.session.execute(stmt).scalar_one_or_none() is not None

    def save_document(self, document: MoadianDocument) -> MoadianDocument:
        """Insert or update a document (by id); return the persisted document."""
        row = self.session.get(MoadianDocumentModel, document.id)
        if row is None:
            row = MoadianDocumentModel(id=document.id)
            self.session.add(row)
        _apply_document(row, document)
        self.session.flush()
        return _document_from_row(row)

    # -- submissions -------------------------------------------------------

    def get_submission(self, submission_id: str, tenant_id: str) -> MoadianSubmission | None:
        """Return one submission, or None when it does not exist in the tenant."""
        stmt = select(MoadianSubmissionModel).where(
            MoadianSubmissionModel.id == submission_id,
            MoadianSubmissionModel.tenant_id == tenant_id,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        return _submission_from_row(row) if row is not None else None

    def list_submissions(
        self,
        tenant_id: str,
        *,
        document_id: str | None = None,
        profile_id: str | None = None,
        status: SubmissionStatus | None = None,
    ) -> Sequence[MoadianSubmission]:
        """Return the tenant's submissions, optionally filtered, oldest first."""
        stmt = select(MoadianSubmissionModel).where(MoadianSubmissionModel.tenant_id == tenant_id)
        if document_id is not None:
            stmt = stmt.where(MoadianSubmissionModel.document_id == document_id)
        if profile_id is not None:
            stmt = stmt.where(MoadianSubmissionModel.profile_id == profile_id)
        if status is not None:
            stmt = stmt.where(MoadianSubmissionModel.status == status.value)
        stmt = stmt.order_by(MoadianSubmissionModel.created_at, MoadianSubmissionModel.id)
        return [_submission_from_row(row) for row in self.session.execute(stmt).scalars()]

    def count_submissions_for_document(self, document_id: str) -> int:
        """Count every submission ever created for one document."""
        stmt = (
            select(func.count())
            .select_from(MoadianSubmissionModel)
            .where(MoadianSubmissionModel.document_id == document_id)
        )
        return self.session.execute(stmt).scalar_one()

    def save_submission(self, submission: MoadianSubmission) -> MoadianSubmission:
        """Insert or update a submission (by id); return the persisted submission."""
        row = self.session.get(MoadianSubmissionModel, submission.id)
        if row is None:
            row = MoadianSubmissionModel(id=submission.id)
            self.session.add(row)
        _apply_submission(row, submission)
        self.session.flush()
        return _submission_from_row(row)

    # -- status events -----------------------------------------------------

    def list_status_events(self, submission_id: str) -> Sequence[MoadianStatusEvent]:
        """Return a submission's status events, oldest first."""
        stmt = (
            select(MoadianStatusEventModel)
            .where(MoadianStatusEventModel.submission_id == submission_id)
            .order_by(MoadianStatusEventModel.occurred_at, MoadianStatusEventModel.id)
        )
        return [_status_event_from_row(row) for row in self.session.execute(stmt).scalars()]

    def count_status_events(self, submission_id: str) -> int:
        """Count a submission's recorded status events."""
        stmt = (
            select(func.count())
            .select_from(MoadianStatusEventModel)
            .where(MoadianStatusEventModel.submission_id == submission_id)
        )
        return self.session.execute(stmt).scalar_one()

    def save_status_event(self, event: MoadianStatusEvent) -> MoadianStatusEvent:
        """Append a status event; return the persisted event."""
        row = MoadianStatusEventModel(
            id=event.id,
            submission_id=event.submission_id,
            status=event.status.value,
            source=event.source.value,
            evidence=dict(event.evidence) if event.evidence is not None else None,
            correlation_id=event.correlation_id,
            occurred_at=event.occurred_at,
            created_at=event.created_at,
        )
        self.session.add(row)
        self.session.flush()
        return _status_event_from_row(row)

    # -- transactional outbox (spec §10.3) ---------------------------------

    def list_pending_outbox_events(self, tenant_id: str, event_type: str) -> Sequence[OutboxEvent]:
        """Return the tenant's pending outbox events of one type, oldest first."""
        stmt = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.tenant_id == tenant_id,
                OutboxEventModel.event_type == event_type,
                OutboxEventModel.status == OutboxEventStatus.PENDING.value,
            )
            .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
        )
        return [_outbox_event_from_row(row) for row in self.session.execute(stmt).scalars()]

    def save_outbox_event(self, event: OutboxEvent) -> OutboxEvent:
        """Insert or update an outbox event (by id); return the persisted event."""
        row = self.session.get(OutboxEventModel, event.id)
        if row is None:
            row = OutboxEventModel(id=event.id)
            self.session.add(row)
        _apply_outbox_event(row, event)
        self.session.flush()
        return _outbox_event_from_row(row)

    def save_integration_attempt(self, attempt: IntegrationAttempt) -> IntegrationAttempt:
        """Append an integration attempt; return the persisted attempt."""
        row = IntegrationAttemptModel(
            id=attempt.id,
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
        return _integration_attempt_from_row(row)
