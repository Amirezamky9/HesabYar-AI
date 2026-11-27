"""Unit tests for the Moadian services + reconciliation worker (US-404).

Services run against an in-memory fake of the repository port — no
database — following ``test_application_services.py`` (the engine
port is faked there the same way). The simulator is the real
deterministic one — no mocks — so the full lifecycle is exercised
end to end. The SQLAlchemy repository itself is covered by the REST
tests (``tests/interfaces/test_rest_moadian.py``), which run against
the real database through the app wiring.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from hesabyar.application.commands.moadian import (
    MOADIAN_SUBMISSION_QUEUED_EVENT,
    MoadianDocumentService,
    MoadianProfileService,
    MoadianReconciliationWorker,
    MoadianSubmissionService,
)
from hesabyar.application.errors import (
    MoadianDocumentNotFoundError,
    MoadianDuplicateTaxIdError,
    MoadianInvalidDocumentError,
    MoadianInvalidStateError,
    MoadianProductionNotConnectedError,
    MoadianProfileNotFoundError,
    MoadianProfileSlugConflictError,
    MoadianSubmissionNotFoundError,
)
from hesabyar.application.queries.moadian import MoadianQueryService
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
    SubmissionStatus,
)
from hesabyar.domain.moadian.ports import (
    IntegrationAttempt,
    OutboxEvent,
    OutboxEventStatus,
)
from hesabyar.infrastructure.moadian.simulator import DeterministicMoadianSimulator
from tests.moadian_fixtures import taxid_for, valid_invoice_payload

APPROVER = "client-approve-1"


@dataclass
class _Tenant:
    """A tenant id holder — the fake repository needs no tenant rows."""

    id: str


class FakeMoadianRepository:
    """In-memory MoadianRepositoryPort for unit tests (no database).

    Stores are public so tests can inspect outbox events and
    integration attempts directly, the way the old DB-backed tests
    queried the outbox tables.
    """

    def __init__(self) -> None:
        self.profiles: dict[str, MoadianProfile] = {}
        self.documents: dict[str, MoadianDocument] = {}
        self.submissions: dict[str, MoadianSubmission] = {}
        self.status_events: dict[str, MoadianStatusEvent] = {}
        self.outbox_events: dict[str, OutboxEvent] = {}
        self.integration_attempts: list[IntegrationAttempt] = []

    # -- profiles ----------------------------------------------------------

    def get_profile(self, profile_id: str, tenant_id: str) -> MoadianProfile | None:
        profile = self.profiles.get(profile_id)
        if profile is None or profile.tenant_id != tenant_id:
            return None
        return profile

    def list_profiles(
        self,
        tenant_id: str,
        *,
        environment: MoadianEnvironment | None = None,
        is_active: bool | None = None,
    ) -> Sequence[MoadianProfile]:
        profiles = [p for p in self.profiles.values() if p.tenant_id == tenant_id]
        if environment is not None:
            profiles = [p for p in profiles if p.environment == environment]
        if is_active is not None:
            profiles = [p for p in profiles if p.is_active == is_active]
        profiles.sort(key=lambda p: p.slug)
        return profiles

    def profile_slug_exists(
        self,
        tenant_id: str,
        slug: str,
        *,
        exclude_id: str | None = None,
    ) -> bool:
        return any(
            p.tenant_id == tenant_id and p.slug == slug and p.id != exclude_id
            for p in self.profiles.values()
        )

    def save_profile(self, profile: MoadianProfile) -> MoadianProfile:
        self.profiles[profile.id] = profile
        return profile

    # -- documents ---------------------------------------------------------

    def get_document(self, document_id: str, tenant_id: str) -> MoadianDocument | None:
        document = self.documents.get(document_id)
        if document is None or document.tenant_id != tenant_id:
            return None
        return document

    def list_documents(
        self,
        tenant_id: str,
        *,
        profile_id: str | None = None,
        status: DocumentStatus | None = None,
    ) -> Sequence[MoadianDocument]:
        documents = [d for d in self.documents.values() if d.tenant_id == tenant_id]
        if profile_id is not None:
            documents = [d for d in documents if d.profile_id == profile_id]
        if status is not None:
            documents = [d for d in documents if d.status == status]
        documents.sort(key=lambda d: (d.created_at, d.id))
        return documents

    def document_taxid_exists(self, tenant_id: str, taxid: str) -> bool:
        return any(
            d.tenant_id == tenant_id and d.taxid == taxid for d in self.documents.values()
        )

    def save_document(self, document: MoadianDocument) -> MoadianDocument:
        self.documents[document.id] = document
        return document

    # -- submissions -------------------------------------------------------

    def get_submission(self, submission_id: str, tenant_id: str) -> MoadianSubmission | None:
        submission = self.submissions.get(submission_id)
        if submission is None or submission.tenant_id != tenant_id:
            return None
        return submission

    def list_submissions(
        self,
        tenant_id: str,
        *,
        document_id: str | None = None,
        profile_id: str | None = None,
        status: SubmissionStatus | None = None,
    ) -> Sequence[MoadianSubmission]:
        submissions = [s for s in self.submissions.values() if s.tenant_id == tenant_id]
        if document_id is not None:
            submissions = [s for s in submissions if s.document_id == document_id]
        if profile_id is not None:
            submissions = [s for s in submissions if s.profile_id == profile_id]
        if status is not None:
            submissions = [s for s in submissions if s.status == status]
        submissions.sort(key=lambda s: (s.created_at, s.id))
        return submissions

    def count_submissions_for_document(self, document_id: str) -> int:
        return sum(1 for s in self.submissions.values() if s.document_id == document_id)

    def save_submission(self, submission: MoadianSubmission) -> MoadianSubmission:
        self.submissions[submission.id] = submission
        return submission

    # -- status events -----------------------------------------------------

    def list_status_events(self, submission_id: str) -> Sequence[MoadianStatusEvent]:
        events = [e for e in self.status_events.values() if e.submission_id == submission_id]
        events.sort(key=lambda e: (e.occurred_at, e.id))
        return events

    def count_status_events(self, submission_id: str) -> int:
        return sum(1 for e in self.status_events.values() if e.submission_id == submission_id)

    def save_status_event(self, event: MoadianStatusEvent) -> MoadianStatusEvent:
        self.status_events[event.id] = event
        return event

    # -- transactional outbox ----------------------------------------------

    def list_pending_outbox_events(self, tenant_id: str, event_type: str) -> Sequence[OutboxEvent]:
        events = [
            e
            for e in self.outbox_events.values()
            if e.tenant_id == tenant_id and e.event_type == event_type and e.status == OutboxEventStatus.PENDING
        ]
        events.sort(key=lambda e: (e.created_at, e.id))
        return events

    def save_outbox_event(self, event: OutboxEvent) -> OutboxEvent:
        self.outbox_events[event.id] = event
        return event

    def save_integration_attempt(self, attempt: IntegrationAttempt) -> IntegrationAttempt:
        self.integration_attempts.append(attempt)
        return attempt


@pytest.fixture()
def repository() -> FakeMoadianRepository:
    return FakeMoadianRepository()


@pytest.fixture()
def tenant() -> _Tenant:
    return _Tenant(id=str(uuid.uuid4()))


@pytest.fixture()
def other_tenant() -> _Tenant:
    return _Tenant(id=str(uuid.uuid4()))


@pytest.fixture()
def profiles(repository: FakeMoadianRepository) -> MoadianProfileService:
    return MoadianProfileService(repository)


@pytest.fixture()
def documents(repository: FakeMoadianRepository) -> MoadianDocumentService:
    return MoadianDocumentService(repository)


@pytest.fixture()
def submissions(repository: FakeMoadianRepository) -> MoadianSubmissionService:
    return MoadianSubmissionService(repository)


@pytest.fixture()
def queries(repository: FakeMoadianRepository) -> MoadianQueryService:
    return MoadianQueryService(repository)


@pytest.fixture()
def simulator() -> DeterministicMoadianSimulator:
    return DeterministicMoadianSimulator()


@pytest.fixture()
def worker(
    repository: FakeMoadianRepository, simulator: DeterministicMoadianSimulator
) -> MoadianReconciliationWorker:
    return MoadianReconciliationWorker(repository, simulator)


def _make_profile(
    profiles: MoadianProfileService,
    tenant_id: str,
    *,
    slug: str | None = None,
    environment: MoadianEnvironment = MoadianEnvironment.TEST,
):
    return profiles.create_profile(
        tenant_id=tenant_id,
        name="Test fiscal memory",
        slug=slug or f"test-{uuid.uuid4().hex[:8]}",
        environment=environment,
        sender_mode=SenderMode.SELF_TSP,
        fiscal_id="AA56CD",
        economic_code="32652362589632",
        source_authority="Iranian National Tax Administration (INTA)",
        source_url="https://www.intamedia.ir/The-regulation-of-store-terminals",
        key_reference="MOADIAN_TEST_PRIVATE_KEY",
        key_fingerprint="a" * 64,
    )


def _make_document(documents: MoadianDocumentService, tenant_id: str, profile_id: str, *, n: int = 1):
    return documents.create_document(
        tenant_id=tenant_id,
        profile_id=profile_id,
        payload=valid_invoice_payload(taxid=taxid_for(n)),
    )


def _queued_submission(
    submissions: MoadianSubmissionService,
    tenant_id: str,
    document_id: str,
    *,
    scenario: SimulationScenario = SimulationScenario.ACCEPT,
):
    """Create → approve → queue a submission; return the queued view."""
    draft = submissions.create_submission(tenant_id=tenant_id, document_id=document_id, scenario=scenario)
    submissions.approve_submission(draft.id, tenant_id, approved_by=APPROVER, approval_note="reviewed")
    return submissions.queue_submission(draft.id, tenant_id)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_create_profile_defaults_and_roundtrip(
    profiles: MoadianProfileService, queries: MoadianQueryService, tenant: _Tenant
) -> None:
    profile = _make_profile(profiles, tenant.id, slug="eyvan-test")
    assert profile.environment == MoadianEnvironment.TEST
    assert profile.sender_mode == SenderMode.SELF_TSP
    assert profile.packet_type == "INVOICE.V01"
    assert profile.api_base_url == "https://tp.tax.gov.ir/req/"
    assert profile.protocol_version == "1401-07-26"
    assert profile.verification_status.value == "UNVERIFIED"
    assert profile.is_active is True
    assert profile.key_reference == "MOADIAN_TEST_PRIVATE_KEY"
    fetched = queries.get_profile(profile.id, tenant.id)
    assert fetched == profile


def test_create_profile_duplicate_slug_conflicts(
    profiles: MoadianProfileService, tenant: _Tenant
) -> None:
    _make_profile(profiles, tenant.id, slug="dup-slug")
    with pytest.raises(MoadianProfileSlugConflictError):
        _make_profile(profiles, tenant.id, slug="dup-slug")


def test_create_profile_same_slug_allowed_in_another_tenant(
    profiles: MoadianProfileService, tenant: _Tenant, other_tenant: _Tenant
) -> None:
    _make_profile(profiles, tenant.id, slug="shared-slug")
    other = _make_profile(profiles, other_tenant.id, slug="shared-slug")
    assert other.tenant_id == other_tenant.id


def test_create_profile_rejects_bad_formats(profiles: MoadianProfileService, tenant: _Tenant) -> None:
    with pytest.raises(ValueError, match="slug must be"):
        _make_profile(profiles, tenant.id, slug="Bad Slug!")
    with pytest.raises(ValueError, match="fiscal_id must be 6"):
        profiles.create_profile(
            tenant_id=tenant.id, name="x", slug="ok-slug-1",
            environment=MoadianEnvironment.TEST, fiscal_id="TOOLONG",
        )
    with pytest.raises(ValueError, match="economic_code must be 10 or 14"):
        profiles.create_profile(
            tenant_id=tenant.id, name="x", slug="ok-slug-2",
            environment=MoadianEnvironment.TEST, economic_code="123",
        )
    with pytest.raises(ValueError, match="key_fingerprint must be"):
        profiles.create_profile(
            tenant_id=tenant.id, name="x", slug="ok-slug-3",
            environment=MoadianEnvironment.TEST, key_fingerprint="not-hex",
        )


def test_create_profile_refuses_key_material_as_reference(
    profiles: MoadianProfileService, tenant: _Tenant
) -> None:
    with pytest.raises(ValueError, match="must never contain key material"):
        profiles.create_profile(
            tenant_id=tenant.id, name="x", slug="pem-slug",
            environment=MoadianEnvironment.TEST,
            key_reference="-----BEGIN PRIVATE KEY-----\nMIIE...",
        )
    with pytest.raises(ValueError, match="single-line reference"):
        profiles.create_profile(
            tenant_id=tenant.id, name="x", slug="multiline-slug",
            environment=MoadianEnvironment.TEST, key_reference="line1\nline2",
        )


def test_update_profile_mutable_fields_only(
    profiles: MoadianProfileService, tenant: _Tenant
) -> None:
    profile = _make_profile(profiles, tenant.id)
    updated = profiles.update_profile(
        profile.id, tenant.id, name="Renamed", is_active=False, notes="deactivated for test"
    )
    assert updated.name == "Renamed"
    assert updated.is_active is False
    assert updated.notes == "deactivated for test"
    assert updated.environment == profile.environment
    assert updated.slug == profile.slug


def test_get_profile_of_another_tenant_is_not_found(
    profiles: MoadianProfileService, queries: MoadianQueryService, tenant: _Tenant, other_tenant: _Tenant
) -> None:
    profile = _make_profile(profiles, tenant.id)
    with pytest.raises(MoadianProfileNotFoundError):
        queries.get_profile(profile.id, other_tenant.id)
    with pytest.raises(MoadianProfileNotFoundError):
        queries.get_profile(str(uuid.uuid4()), tenant.id)


def test_list_profiles_filters(
    profiles: MoadianProfileService, queries: MoadianQueryService, tenant: _Tenant
) -> None:
    test_profile = _make_profile(profiles, tenant.id, slug="a-test")
    _make_profile(profiles, tenant.id, slug="b-prod", environment=MoadianEnvironment.PRODUCTION)
    profiles.update_profile(test_profile.id, tenant.id, is_active=False)
    assert [p.slug for p in queries.list_profiles(tenant.id)] == ["a-test", "b-prod"]
    assert [p.slug for p in queries.list_profiles(tenant.id, environment=MoadianEnvironment.PRODUCTION)] == ["b-prod"]
    assert [p.slug for p in queries.list_profiles(tenant.id, is_active=True)] == ["b-prod"]
    assert queries.list_profiles(tenant.id, environment=MoadianEnvironment.TEST, is_active=True) == []


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def test_create_document_validates_and_denormalizes(
    profiles: MoadianProfileService, documents: MoadianDocumentService, tenant: _Tenant
) -> None:
    profile = _make_profile(profiles, tenant.id)
    payload = valid_invoice_payload(taxid=taxid_for(11))
    document = documents.create_document(tenant_id=tenant.id, profile_id=profile.id, payload=payload)
    assert document.taxid == taxid_for(11)
    assert document.serial == "0002F2B4E7"
    assert document.invoice_type == 1
    assert document.invoice_pattern == 1
    assert document.invoice_subject == 1
    assert document.status == DocumentStatus.DRAFT
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)
    assert document.payload_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert document.payload == payload


def test_create_document_rejects_invalid_payload_with_problems(
    profiles: MoadianProfileService, documents: MoadianDocumentService, tenant: _Tenant
) -> None:
    profile = _make_profile(profiles, tenant.id)
    payload = valid_invoice_payload()
    del payload["header"]["tins"]
    payload["header"]["taxid"] = "SHORT"
    with pytest.raises(MoadianInvalidDocumentError) as excinfo:
        documents.create_document(tenant_id=tenant.id, profile_id=profile.id, payload=payload)
    assert excinfo.value.details["problems"] == [
        "header.tins is required.",
        "header.taxid must be a 22-character string.",
    ]


def test_create_document_rejects_duplicate_taxid(
    profiles: MoadianProfileService, documents: MoadianDocumentService, tenant: _Tenant
) -> None:
    profile = _make_profile(profiles, tenant.id)
    _make_document(documents, tenant.id, profile.id, n=21)
    with pytest.raises(MoadianDuplicateTaxIdError):
        _make_document(documents, tenant.id, profile.id, n=21)


def test_create_document_unknown_or_inactive_profile(
    profiles: MoadianProfileService, documents: MoadianDocumentService, tenant: _Tenant
) -> None:
    with pytest.raises(MoadianProfileNotFoundError):
        documents.create_document(
            tenant_id=tenant.id, profile_id=str(uuid.uuid4()), payload=valid_invoice_payload()
        )
    profile = _make_profile(profiles, tenant.id)
    profiles.update_profile(profile.id, tenant.id, is_active=False)
    with pytest.raises(MoadianInvalidStateError):
        documents.create_document(
            tenant_id=tenant.id, profile_id=profile.id, payload=valid_invoice_payload()
        )


def test_get_document_of_another_tenant_is_not_found(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    queries: MoadianQueryService,
    tenant: _Tenant,
    other_tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=31)
    with pytest.raises(MoadianDocumentNotFoundError):
        queries.get_document(document.id, other_tenant.id)


# ---------------------------------------------------------------------------
# Submissions: create / approve / queue
# ---------------------------------------------------------------------------


def test_create_submission_is_draft_with_deterministic_uid(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=41)
    submission = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    assert submission.status == SubmissionStatus.DRAFT
    assert submission.environment == MoadianEnvironment.TEST
    assert submission.scenario == SimulationScenario.ACCEPT
    assert submission.retry is False
    assert submission.approved_by is None
    assert submission.approved_at is None
    assert submission.reference_number is None
    expected_uid = str(
        uuid.uuid3(
            uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"),
            f"hesabyar:moadian:submission:{submission.id}",
        )
    )
    assert submission.uid == expected_uid
    assert uuid.UUID(submission.uid).version == 3


def test_second_submission_for_same_document_is_retry(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=42)
    first = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    second = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    assert first.retry is False
    assert second.retry is True
    assert first.uid != second.uid


def test_approve_records_approval_and_rejects_non_draft(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=43)
    draft = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    approved = submissions.approve_submission(draft.id, tenant.id, approved_by=APPROVER, approval_note="looks good")
    assert approved.status == SubmissionStatus.APPROVED
    assert approved.approved_by == APPROVER
    assert approved.approved_at is not None
    assert approved.approval_note == "looks good"
    with pytest.raises(MoadianInvalidStateError):
        submissions.approve_submission(draft.id, tenant.id, approved_by=APPROVER)


def test_queue_requires_approval(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=44)
    draft = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    with pytest.raises(MoadianInvalidStateError):
        submissions.queue_submission(draft.id, tenant.id)


def test_queue_writes_outbox_event_in_same_transaction(
    repository: FakeMoadianRepository,
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=45)
    draft = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    submissions.approve_submission(draft.id, tenant.id, approved_by=APPROVER)
    queued = submissions.queue_submission(draft.id, tenant.id)
    assert queued.status == SubmissionStatus.QUEUED
    assert queued.outbox_event_id is not None
    event = repository.outbox_events[queued.outbox_event_id]
    assert event.event_type == MOADIAN_SUBMISSION_QUEUED_EVENT
    assert event.aggregate_type == "moadian_submission"
    assert event.aggregate_id == queued.id
    assert event.status == "pending"
    assert event.correlation_id == queued.correlation_id
    assert event.payload["uid"] == queued.uid
    assert event.payload["scenario"] == "accept"
    canonical = json.dumps(event.payload, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)
    assert event.payload_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_queue_production_submission_is_refused_501(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id, environment=MoadianEnvironment.PRODUCTION)
    document = _make_document(documents, tenant.id, profile.id, n=46)
    draft = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    assert draft.environment == MoadianEnvironment.PRODUCTION
    submissions.approve_submission(draft.id, tenant.id, approved_by=APPROVER)
    with pytest.raises(MoadianProductionNotConnectedError) as excinfo:
        submissions.queue_submission(draft.id, tenant.id)
    assert excinfo.value.code == "MOADIAN_PRODUCTION_NOT_CONNECTED"


def test_get_submission_of_another_tenant_is_not_found(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    queries: MoadianQueryService,
    tenant: _Tenant,
    other_tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=47)
    draft = submissions.create_submission(tenant_id=tenant.id, document_id=document.id)
    with pytest.raises(MoadianSubmissionNotFoundError):
        queries.get_submission(draft.id, other_tenant.id)


# ---------------------------------------------------------------------------
# Reconciliation worker
# ---------------------------------------------------------------------------


def test_reconcile_accept_scenario_two_passes_to_accepted(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    queries: MoadianQueryService,
    worker: MoadianReconciliationWorker,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=51)
    queued = _queued_submission(submissions, tenant.id, document.id)

    first = worker.run_pass(tenant.id)
    assert first.dispatched == 1
    assert first.submit_failed == 0
    assert first.polled == 1
    assert first.accepted == 0
    assert first.still_pending == 1
    after_first = queries.get_submission(queued.id, tenant.id)
    assert after_first.status == SubmissionStatus.SUBMITTED
    assert after_first.reference_number is not None
    assert after_first.request_hash is not None
    assert after_first.response_hash is not None
    assert after_first.request_payload is not None
    assert after_first.request_payload["simulated"] is True
    assert after_first.request_payload["packet"]["uid"] == queued.uid
    assert after_first.request_payload["packet"]["packetType"] == "INVOICE.V01"
    assert after_first.request_payload["packet"]["fiscalId"] == "AA56CD"
    assert queries.get_document(document.id, tenant.id).status == DocumentStatus.SUBMITTED
    events = queries.list_status_events(queued.id, tenant.id)
    assert [event.status for event in events] == [OfficialStatus.PENDING]
    assert events[0].source.value == "simulator"
    assert events[0].correlation_id == queued.correlation_id
    assert events[0].evidence is not None
    assert events[0].evidence["status"] == "PENDING"
    assert events[0].evidence["referenceNumber"] == after_first.reference_number

    second = worker.run_pass(tenant.id)
    assert second.dispatched == 0
    assert second.polled == 1
    assert second.accepted == 1
    assert second.still_pending == 0
    after_second = queries.get_submission(queued.id, tenant.id)
    assert after_second.status == SubmissionStatus.ACCEPTED
    assert queries.get_document(document.id, tenant.id).status == DocumentStatus.ACCEPTED
    events = queries.list_status_events(queued.id, tenant.id)
    assert [event.status for event in events] == [OfficialStatus.PENDING, OfficialStatus.SUCCESS]
    assert events[1].evidence is not None
    assert events[1].evidence["data"] == {"simulated": True}

    # A third pass is a no-op: nothing queued, nothing in flight.
    third = worker.run_pass(tenant.id)
    assert third.dispatched == 0
    assert third.polled == 0


def test_reconcile_reject_scenario_ends_rejected_with_evidence(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    queries: MoadianQueryService,
    worker: MoadianReconciliationWorker,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=52)
    queued = _queued_submission(submissions, tenant.id, document.id, scenario=SimulationScenario.REJECT)
    worker.run_pass(tenant.id)
    report = worker.run_pass(tenant.id)
    assert report.rejected == 1
    assert report.accepted == 0
    final = queries.get_submission(queued.id, tenant.id)
    assert final.status == SubmissionStatus.REJECTED
    assert final.last_error_code == "SIMULATED_REJECTION"
    assert final.last_error_detail is not None
    assert "simulated" in final.last_error_detail.lower()
    assert queries.get_document(document.id, tenant.id).status == DocumentStatus.REJECTED
    events = queries.list_status_events(queued.id, tenant.id)
    assert [event.status for event in events] == [OfficialStatus.PENDING, OfficialStatus.FAILED]
    assert events[1].evidence is not None
    assert events[1].evidence["data"]["error"][0]["code"] == "SIMULATED_REJECTION"


def test_reconcile_marks_outbox_published_with_attempt(
    repository: FakeMoadianRepository,
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    worker: MoadianReconciliationWorker,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=53)
    queued = _queued_submission(submissions, tenant.id, document.id)
    worker.run_pass(tenant.id)
    assert queued.outbox_event_id is not None
    event = repository.outbox_events[queued.outbox_event_id]
    assert event.status == "published"
    assert event.published_at is not None
    assert event.attempts == 1
    attempts = [a for a in repository.integration_attempts if a.outbox_event_id == event.id]
    assert len(attempts) == 1
    assert attempts[0].status == "succeeded"
    assert attempts[0].attempt_number == 1


def test_reconcile_resumes_across_fresh_simulator_instances(
    repository: FakeMoadianRepository,
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    queries: MoadianQueryService,
    tenant: _Tenant,
) -> None:
    """Each pass uses a fresh simulator (like the REST layer does)."""
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=54)
    queued = _queued_submission(submissions, tenant.id, document.id)
    first_worker = MoadianReconciliationWorker(repository, DeterministicMoadianSimulator())
    first_worker.run_pass(tenant.id)
    assert queries.get_submission(queued.id, tenant.id).status == SubmissionStatus.SUBMITTED
    second_worker = MoadianReconciliationWorker(repository, DeterministicMoadianSimulator())
    report = second_worker.run_pass(tenant.id)
    assert report.accepted == 1
    assert queries.get_submission(queued.id, tenant.id).status == SubmissionStatus.ACCEPTED


def test_reconcile_empty_tenant_is_noop(
    worker: MoadianReconciliationWorker, tenant: _Tenant
) -> None:
    report = worker.run_pass(tenant.id)
    assert report.dispatched == 0
    assert report.submit_failed == 0
    assert report.polled == 0
    assert report.accepted == 0
    assert report.rejected == 0


def test_reconcile_stale_outbox_event_is_skipped_not_resubmitted(
    repository: FakeMoadianRepository,
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    worker: MoadianReconciliationWorker,
    tenant: _Tenant,
) -> None:
    """A pending outbox event whose submission already moved on is published as stale."""
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=55)
    queued = _queued_submission(submissions, tenant.id, document.id)
    assert queued.outbox_event_id is not None
    event = repository.outbox_events[queued.outbox_event_id]
    assert event.status == "pending"
    # Move the submission on without the worker (simulating an earlier pass
    # that crashed after the status change but before publishing).
    submission = repository.submissions[queued.id]
    repository.save_submission(dataclasses.replace(submission, status=SubmissionStatus.ACCEPTED))
    report = worker.run_pass(tenant.id)
    assert report.skipped == 1
    assert report.dispatched == 0
    assert repository.outbox_events[queued.outbox_event_id].status == "published"


def test_status_events_require_submission_visibility(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    queries: MoadianQueryService,
    worker: MoadianReconciliationWorker,
    tenant: _Tenant,
    other_tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    document = _make_document(documents, tenant.id, profile.id, n=56)
    queued = _queued_submission(submissions, tenant.id, document.id)
    worker.run_pass(tenant.id)
    assert len(queries.list_status_events(queued.id, tenant.id)) == 1
    with pytest.raises(MoadianSubmissionNotFoundError):
        queries.list_status_events(queued.id, other_tenant.id)


def test_list_submissions_filters(
    profiles: MoadianProfileService,
    documents: MoadianDocumentService,
    submissions: MoadianSubmissionService,
    queries: MoadianQueryService,
    tenant: _Tenant,
) -> None:
    profile = _make_profile(profiles, tenant.id)
    doc_a = _make_document(documents, tenant.id, profile.id, n=57)
    doc_b = _make_document(documents, tenant.id, profile.id, n=58)
    sub_a = submissions.create_submission(tenant_id=tenant.id, document_id=doc_a.id)
    submissions.create_submission(tenant_id=tenant.id, document_id=doc_b.id)
    submissions.approve_submission(sub_a.id, tenant.id, approved_by=APPROVER)
    assert len(queries.list_submissions(tenant.id)) == 2
    assert [s.id for s in queries.list_submissions(tenant.id, document_id=doc_a.id)] == [sub_a.id]
    assert [s.id for s in queries.list_submissions(tenant.id, status=SubmissionStatus.APPROVED)] == [sub_a.id]
    assert [s.id for s in queries.list_submissions(tenant.id, status=SubmissionStatus.DRAFT)][0] != sub_a.id
    assert len(queries.list_submissions(tenant.id, profile_id=profile.id)) == 2
