"""Ports for the Moadian boundary (spec §12.3, US-404).

The application layer depends on these ports — never on a concrete
simulator, on SQLAlchemy, or on any real Moadian endpoint.

- :class:`MoadianSimulatorPort` — the deterministic fixture
  simulator. The only implementation in this phase is
  :mod:`hesabyar.infrastructure.moadian.simulator`; a production
  connector is a separate, later implementation of a different port,
  and must never be reachable through this one.
- :class:`MoadianRepositoryPort` — persistence for Moadian
  aggregates (profiles, documents, submissions, status events) and
  the transactional outbox they move through. The SQLAlchemy
  implementation lives in
  :mod:`hesabyar.infrastructure.persistence.postgres.moadian_repository`.

Shapes follow the official instruction: the async submit response
(``AsyncResponse`` — instruction Tables 8-9: ``uid``,
``referenceNumber``, ``errorCode``, ``errorDetail``) and the inquiry
response items (Table 10: ``uid``, ``referenceNumber``, ``status``,
``data``, ``packetType``, ``fiscalId``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hesabyar.domain.events.models import (
    IntegrationAttempt,
    IntegrationAttemptStatus,
    OutboxEvent,
    OutboxEventStatus,
)
from hesabyar.domain.moadian.models import (
    INVOICE_PACKET_TYPE,
    DocumentStatus,
    MoadianDocument,
    MoadianEnvironment,
    MoadianProfile,
    MoadianStatusEvent,
    MoadianSubmission,
    OfficialStatus,
    SimulationScenario,
    SubmissionStatus,
)


@dataclass(frozen=True)
class SimulatorSubmitResult:
    """Outcome of one simulated async submit (instruction Tables 8-9)."""

    uid: str
    reference_number: str | None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def accepted(self) -> bool:
        """True when the packet entered the (simulated) queue."""
        return self.reference_number is not None and self.error_code is None


@dataclass(frozen=True)
class SimulatorInquiryItem:
    """One simulated inquiry response item (instruction Table 10)."""

    uid: str
    reference_number: str | None
    status: OfficialStatus
    data: dict[str, Any] | None
    packet_type: str | None
    fiscal_id: str | None


class MoadianSimulatorPort(Protocol):
    """Deterministic, fixture-based stand-in for the Moadian API.

    Implementations MUST NOT perform network calls. ``submit_packet``
    registers one packet and returns its receipt; ``inquiry_by_uid``
    reports the simulated processing status of previously submitted
    packets. Both calls are pure functions of their inputs plus the
    simulator's in-memory registry — the same inputs always produce
    the same outputs.
    """

    def submit_packet(
        self,
        *,
        uid: str,
        packet: dict[str, Any],
        scenario: SimulationScenario,
    ) -> SimulatorSubmitResult:
        """Submit one packet; return its (simulated) receipt or error."""
        ...

    def register_submitted(
        self,
        *,
        uid: str,
        reference_number: str,
        scenario: SimulationScenario,
        packet_type: str = INVOICE_PACKET_TYPE,
        fiscal_id: str | None = None,
        polls: int = 0,
    ) -> None:
        """Re-seed a packet submitted in an earlier process.

        Lets a fresh simulator instance poll submissions whose receipt
        lives in the database. ``polls`` is the number of inquiries
        already recorded, so the lifecycle resumes where it left off.
        """
        ...

    def inquiry_by_uid(self, uids: list[str]) -> list[SimulatorInquiryItem]:
        """Report the simulated status of each uid (unknown → NOT_FOUND)."""
        ...


# ---------------------------------------------------------------------------
# Transactional outbox (spec §10.3) — the Moadian view
#
# The outbox tables are generic integration infrastructure. The transfer
# shapes now live in :mod:`hesabyar.domain.events.models` (US-501: the
# event platform is the outbox's second user after Moadian) and are
# re-exported here so existing Moadian importers keep working.
# ---------------------------------------------------------------------------

__all__ = [
    "IntegrationAttempt",
    "IntegrationAttemptStatus",
    "MoadianRepositoryPort",
    "MoadianSimulatorPort",
    "OutboxEvent",
    "OutboxEventStatus",
    "SimulatorInquiryItem",
    "SimulatorSubmitResult",
]


class MoadianRepositoryPort(Protocol):
    """Persistence for Moadian aggregates (spec §6.3, §12.3).

    The application layer depends on this port — never on SQLAlchemy
    or the ORM models. Every read is tenant-scoped: an aggregate of
    another tenant looks exactly like one that does not exist.

    Implementations flush but never commit: the caller (REST route)
    owns the transaction, like every other service that writes
    through a request session. ``save_*`` inserts or updates by id
    and returns the persisted aggregate.
    """

    # -- profiles ----------------------------------------------------------

    def get_profile(self, profile_id: str, tenant_id: str) -> MoadianProfile | None:
        """Return one profile, or None when it does not exist in the tenant."""
        ...

    def list_profiles(
        self,
        tenant_id: str,
        *,
        environment: MoadianEnvironment | None = None,
        is_active: bool | None = None,
    ) -> Sequence[MoadianProfile]:
        """Return the tenant's profiles, optionally filtered, ordered by slug."""
        ...

    def profile_slug_exists(
        self,
        tenant_id: str,
        slug: str,
        *,
        exclude_id: str | None = None,
    ) -> bool:
        """True when the tenant already has a profile with this slug."""
        ...

    def save_profile(self, profile: MoadianProfile) -> MoadianProfile:
        """Insert or update a profile (by id); return the persisted profile."""
        ...

    # -- documents ---------------------------------------------------------

    def get_document(self, document_id: str, tenant_id: str) -> MoadianDocument | None:
        """Return one document, or None when it does not exist in the tenant."""
        ...

    def list_documents(
        self,
        tenant_id: str,
        *,
        profile_id: str | None = None,
        status: DocumentStatus | None = None,
    ) -> Sequence[MoadianDocument]:
        """Return the tenant's documents, optionally filtered, oldest first."""
        ...

    def document_taxid_exists(self, tenant_id: str, taxid: str) -> bool:
        """True when the tenant already has a document with this tax id."""
        ...

    def save_document(self, document: MoadianDocument) -> MoadianDocument:
        """Insert or update a document (by id); return the persisted document."""
        ...

    # -- submissions -------------------------------------------------------

    def get_submission(self, submission_id: str, tenant_id: str) -> MoadianSubmission | None:
        """Return one submission, or None when it does not exist in the tenant."""
        ...

    def list_submissions(
        self,
        tenant_id: str,
        *,
        document_id: str | None = None,
        profile_id: str | None = None,
        status: SubmissionStatus | None = None,
    ) -> Sequence[MoadianSubmission]:
        """Return the tenant's submissions, optionally filtered, oldest first."""
        ...

    def count_submissions_for_document(self, document_id: str) -> int:
        """Count every submission ever created for one document."""
        ...

    def save_submission(self, submission: MoadianSubmission) -> MoadianSubmission:
        """Insert or update a submission (by id); return the persisted submission."""
        ...

    # -- status events -----------------------------------------------------

    def list_status_events(self, submission_id: str) -> Sequence[MoadianStatusEvent]:
        """Return a submission's status events, oldest first."""
        ...

    def count_status_events(self, submission_id: str) -> int:
        """Count a submission's recorded status events."""
        ...

    def save_status_event(self, event: MoadianStatusEvent) -> MoadianStatusEvent:
        """Append a status event; return the persisted event."""
        ...

    # -- transactional outbox (spec §10.3) ---------------------------------

    def list_pending_outbox_events(self, tenant_id: str, event_type: str) -> Sequence[OutboxEvent]:
        """Return the tenant's pending outbox events of one type, oldest first."""
        ...

    def save_outbox_event(self, event: OutboxEvent) -> OutboxEvent:
        """Insert or update an outbox event (by id); return the persisted event."""
        ...

    def save_integration_attempt(self, attempt: IntegrationAttempt) -> IntegrationAttempt:
        """Append an integration attempt; return the persisted attempt."""
        ...
