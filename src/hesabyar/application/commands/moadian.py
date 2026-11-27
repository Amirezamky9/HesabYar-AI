"""Moadian command use cases + reconciliation worker (US-404).

Profiles, documents and submissions, and the worker that dispatches
queued submissions to the simulator through the transactional outbox
and reconciles their status by polling (spec §12.3, §10.3).

Lifecycle (spec §19 rule 11 — no LLM-initiated submission):

- A document is created draft-only: creation validates the invoice
  payload against instruction Table 7 and needs no approval.
- A submission is created as a draft. Approving it records the human
  approval (approver client id, timestamp, optional note) — the
  approval is a recorded step, not a flag that automation can set as
  a side effect.
- Queueing requires the recorded approval and writes the outbox event
  in the same transaction as the status change (spec §10.3). A
  production submission cannot be queued: there is no production
  connector in this phase, so queueing fails with a typed 501.
- The reconciliation worker dispatches pending outbox events to the
  simulator, then polls in-flight submissions and persists every
  inquiry response as a status event with its evidence, plus the
  request/response hashes, timestamps and correlation ids (spec
  §12.3).

Persistence: services persist through the repository port
(:class:`hesabyar.domain.moadian.ports.MoadianRepositoryPort`) and
never see SQLAlchemy or ORM rows (spec §6.3). The repository flushes
but does NOT commit — the caller (REST route) owns the transaction,
like every other service that writes through a request session.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

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
from hesabyar.domain.moadian.models import (
    MOADIAN_API_BASE_URL,
    PROTOCOL_VERSION,
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
    validate_invoice_payload,
)
from hesabyar.domain.moadian.ports import (
    IntegrationAttempt,
    IntegrationAttemptStatus,
    MoadianRepositoryPort,
    MoadianSimulatorPort,
    OutboxEvent,
    OutboxEventStatus,
)
from hesabyar.domain.standards.models import VerificationStatus

#: Outbox event type for a queued Moadian submission (spec §10.3).
MOADIAN_SUBMISSION_QUEUED_EVENT = "moadian.submission.queued"

#: Aggregate type for Moadian submissions in the outbox.
MOADIAN_SUBMISSION_AGGREGATE = "moadian_submission"

#: Slug shape: lowercase letters, digits, hyphens; must start alphanumerically.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")

#: Fiscal memory id: 6 alphanumeric characters (instruction).
_FISCAL_ID_RE = re.compile(r"^[A-Za-z0-9]{6}$")

#: Economic code: 10 digits (Table 7) or 14 digits (new economic code
#: in the instruction's own example — see docs/moadian.md §"Assumptions").
_ECONOMIC_CODE_RE = re.compile(r"^([0-9]{10}|[0-9]{14})$")

#: Public-key fingerprint: SHA-256 hex.
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

#: Namespace for deterministic submission uids (uuid3 per the
#: instruction: the unique submission id is generated client-side).
_UID_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


def _canonical_hash(payload: Any) -> str:
    """SHA-256 hex of the canonical JSON of a payload."""
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json(payload: Any) -> str:
    """Canonical JSON string of a payload (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)


def _submission_uid(submission_id: str) -> str:
    """Deterministic uuid3 submission uid for a submission id."""
    return str(uuid.uuid3(_UID_NAMESPACE, f"hesabyar:moadian:submission:{submission_id}"))


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True)
class ReconciliationReport:
    """Outcome of one reconciliation pass (one worker run)."""

    dispatched: int
    submit_failed: int
    skipped: int
    polled: int
    accepted: int
    rejected: int
    still_pending: int


def _check_key_reference(key_reference: str | None) -> None:
    """Fail closed when a key reference looks like key material.

    A reference names where a key lives (an env-var name, a
    secret-manager path). A PEM block, a newline, or a suspiciously
    long blob is key material smuggled into the reference field — it
    is refused, never stored.
    """
    if key_reference is None:
        return
    if "-----BEGIN" in key_reference:
        raise ValueError("key_reference must name where the key lives; it must never contain key material.")
    if "\n" in key_reference or "\r" in key_reference:
        raise ValueError("key_reference must be a single-line reference (env-var name or secret-manager path).")
    if len(key_reference) > 512:
        raise ValueError("key_reference is too long.")


class MoadianProfileService:
    """Create and update Moadian profiles."""

    def __init__(self, repository: MoadianRepositoryPort) -> None:
        self._repository = repository

    def _require_profile(self, profile_id: str, tenant_id: str) -> MoadianProfile:
        profile = self._repository.get_profile(profile_id, tenant_id)
        if profile is None:
            raise MoadianProfileNotFoundError(
                f"Moadian profile {profile_id!r} does not exist.",
                details={"moadian_profile_id": profile_id},
            )
        return profile

    def create_profile(
        self,
        *,
        tenant_id: str,
        name: str,
        slug: str,
        environment: MoadianEnvironment,
        sender_mode: SenderMode = SenderMode.SELF_TSP,
        fiscal_id: str | None = None,
        economic_code: str | None = None,
        packet_type: str = "INVOICE.V01",
        api_base_url: str = MOADIAN_API_BASE_URL,
        protocol_version: str = PROTOCOL_VERSION,
        source_authority: str | None = None,
        source_url: str | None = None,
        snapshot_uri: str | None = None,
        verification_status: VerificationStatus = VerificationStatus.UNVERIFIED,
        key_reference: str | None = None,
        key_fingerprint: str | None = None,
        notes: str | None = None,
    ) -> MoadianProfile:
        """Create a profile. Flushes; the caller commits."""
        if not _SLUG_RE.match(slug):
            raise ValueError("slug must be lowercase letters, digits and hyphens, starting with a letter or digit.")
        if fiscal_id is not None and not _FISCAL_ID_RE.match(fiscal_id):
            raise ValueError("fiscal_id must be 6 alphanumeric characters.")
        if economic_code is not None and not _ECONOMIC_CODE_RE.match(economic_code):
            raise ValueError("economic_code must be 10 or 14 digits.")
        if key_fingerprint is not None and not _FINGERPRINT_RE.match(key_fingerprint):
            raise ValueError("key_fingerprint must be a 64-character lowercase hex SHA-256.")
        _check_key_reference(key_reference)
        if self._repository.profile_slug_exists(tenant_id, slug):
            raise MoadianProfileSlugConflictError(
                f"Profile slug {slug!r} is already used in this tenant.",
                details={"slug": slug},
            )
        now = _now()
        profile = MoadianProfile(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            environment=environment,
            sender_mode=sender_mode,
            fiscal_id=fiscal_id,
            economic_code=economic_code,
            packet_type=packet_type,
            api_base_url=api_base_url,
            protocol_version=protocol_version,
            source_authority=source_authority,
            source_url=source_url,
            snapshot_uri=snapshot_uri,
            verification_status=verification_status,
            key_reference=key_reference,
            key_fingerprint=key_fingerprint,
            is_active=True,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        return self._repository.save_profile(profile)

    def update_profile(
        self,
        profile_id: str,
        tenant_id: str,
        *,
        name: str | None = None,
        is_active: bool | None = None,
        fiscal_id: str | None = None,
        economic_code: str | None = None,
        key_reference: str | None = None,
        key_fingerprint: str | None = None,
        notes: str | None = None,
    ) -> MoadianProfile:
        """Update a profile's mutable fields. Flushes; the caller commits.

        Environment, sender mode, packet type and protocol version are
        immutable: a profile is a versioned protocol record, so a
        change to any of them means creating a new profile.
        """
        profile = self._require_profile(profile_id, tenant_id)
        if fiscal_id is not None and not _FISCAL_ID_RE.match(fiscal_id):
            raise ValueError("fiscal_id must be 6 alphanumeric characters.")
        if economic_code is not None and not _ECONOMIC_CODE_RE.match(economic_code):
            raise ValueError("economic_code must be 10 or 14 digits.")
        if key_fingerprint is not None and not _FINGERPRINT_RE.match(key_fingerprint):
            raise ValueError("key_fingerprint must be a 64-character lowercase hex SHA-256.")
        _check_key_reference(key_reference)
        updates: dict[str, Any] = {"updated_at": _now()}
        if name is not None:
            updates["name"] = name
        if is_active is not None:
            updates["is_active"] = is_active
        if fiscal_id is not None:
            updates["fiscal_id"] = fiscal_id
        if economic_code is not None:
            updates["economic_code"] = economic_code
        if key_reference is not None:
            updates["key_reference"] = key_reference
        if key_fingerprint is not None:
            updates["key_fingerprint"] = key_fingerprint
        if notes is not None:
            updates["notes"] = notes
        return self._repository.save_profile(dataclasses.replace(profile, **updates))


class MoadianDocumentService:
    """Create Moadian documents (draft-only, no approval needed)."""

    def __init__(self, repository: MoadianRepositoryPort) -> None:
        self._repository = repository

    def create_document(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        payload: dict[str, Any],
        invoice_id: str | None = None,
    ) -> MoadianDocument:
        """Validate the invoice payload and persist the document.

        The payload is the source of truth: taxid/serial/type/pattern/
        subject are read from its header and denormalized onto the
        document for querying. An invalid payload is refused with the
        full problem list — never stored. Flushes; the caller commits.
        """
        profile = self._repository.get_profile(profile_id, tenant_id)
        if profile is None:
            raise MoadianProfileNotFoundError(
                f"Moadian profile {profile_id!r} does not exist.",
                details={"moadian_profile_id": profile_id},
            )
        if not profile.is_active:
            raise MoadianInvalidStateError(
                f"Moadian profile {profile_id!r} is not active.",
                details={"moadian_profile_id": profile_id},
            )
        problems = validate_invoice_payload(payload)
        if problems:
            raise MoadianInvalidDocumentError(
                "The invoice payload fails Moadian Table 7 validation.",
                details={"problems": problems},
            )
        header = payload["header"]
        taxid = str(header["taxid"])
        if self._repository.document_taxid_exists(tenant_id, taxid):
            raise MoadianDuplicateTaxIdError(
                f"Tax id {taxid!r} is already used by a document in this tenant.",
                details={"taxid": taxid},
            )
        now = _now()
        document = MoadianDocument(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            profile_id=profile_id,
            invoice_id=invoice_id,
            taxid=taxid,
            serial=str(header["inno"]),
            invoice_type=int(header["inty"]),
            invoice_pattern=int(header["inp"]),
            invoice_subject=int(header["ins"]),
            payload=dict(payload),
            payload_hash=_canonical_hash(payload),
            status=DocumentStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        return self._repository.save_document(document)


class MoadianSubmissionService:
    """Create, approve and queue Moadian submissions."""

    def __init__(self, repository: MoadianRepositoryPort) -> None:
        self._repository = repository

    def _require_submission(self, submission_id: str, tenant_id: str) -> MoadianSubmission:
        submission = self._repository.get_submission(submission_id, tenant_id)
        if submission is None:
            raise MoadianSubmissionNotFoundError(
                f"Moadian submission {submission_id!r} does not exist.",
                details={"moadian_submission_id": submission_id},
            )
        return submission

    def create_submission(
        self,
        *,
        tenant_id: str,
        document_id: str,
        scenario: SimulationScenario = SimulationScenario.ACCEPT,
    ) -> MoadianSubmission:
        """Create a draft submission for a document. Flushes; the caller commits.

        A second submission for the same document is a resubmission
        (``retry`` true, per the instruction). Draft creation needs no
        approval — only queueing does.
        """
        document = self._repository.get_document(document_id, tenant_id)
        if document is None:
            raise MoadianDocumentNotFoundError(
                f"Moadian document {document_id!r} does not exist.",
                details={"moadian_document_id": document_id},
            )
        profile = self._repository.get_profile(document.profile_id, tenant_id)
        if profile is None or not profile.is_active:
            raise MoadianInvalidStateError(
                f"Moadian profile {document.profile_id!r} is not active.",
                details={"moadian_profile_id": document.profile_id},
            )
        prior = self._repository.count_submissions_for_document(document_id)
        submission_id = str(uuid.uuid4())
        now = _now()
        submission = MoadianSubmission(
            id=submission_id,
            tenant_id=tenant_id,
            document_id=document_id,
            profile_id=document.profile_id,
            uid=_submission_uid(submission_id),
            reference_number=None,
            correlation_id=str(uuid.uuid4()),
            environment=profile.environment,
            scenario=scenario,
            status=SubmissionStatus.DRAFT,
            retry=prior > 0,
            approved_by=None,
            approved_at=None,
            approval_note=None,
            request_hash=None,
            response_hash=None,
            request_payload=None,
            response_payload=None,
            outbox_event_id=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=now,
            updated_at=now,
        )
        return self._repository.save_submission(submission)

    def approve_submission(
        self,
        submission_id: str,
        tenant_id: str,
        *,
        approved_by: str,
        approval_note: str | None = None,
    ) -> MoadianSubmission:
        """Record the human approval of a draft submission.

        The approval is a recorded step (approver, timestamp, note):
        only an approved submission can be queued. Approving anything
        but a draft is a 409. Flushes; the caller commits.
        """
        submission = self._require_submission(submission_id, tenant_id)
        if submission.status != SubmissionStatus.DRAFT:
            raise MoadianInvalidStateError(
                f"Submission {submission_id!r} is {submission.status.value!r}; only a draft can be approved.",
                details={"moadian_submission_id": submission_id, "status": submission.status.value},
            )
        return self._repository.save_submission(
            dataclasses.replace(
                submission,
                approved_by=approved_by,
                approved_at=_now(),
                approval_note=approval_note,
                status=SubmissionStatus.APPROVED,
                updated_at=_now(),
            )
        )

    def queue_submission(self, submission_id: str, tenant_id: str) -> MoadianSubmission:
        """Queue an approved submission via the transactional outbox.

        Writes the outbox event in the same transaction as the status
        change (spec §10.3). A production submission is refused with
        a typed 501: there is no production connector in this phase.
        Flushes; the caller commits.
        """
        submission = self._require_submission(submission_id, tenant_id)
        if submission.status != SubmissionStatus.APPROVED:
            raise MoadianInvalidStateError(
                f"Submission {submission_id!r} is {submission.status.value!r}; "
                "only an approved submission can be queued.",
                details={"moadian_submission_id": submission_id, "status": submission.status.value},
            )
        if submission.environment != MoadianEnvironment.TEST:
            raise MoadianProductionNotConnectedError(
                "Production Moadian connectivity is not built in this phase: "
                "the submission is approved and recorded, but it cannot be "
                "queued until the production connector lands.",
                details={
                    "moadian_submission_id": submission_id,
                    "environment": submission.environment.value,
                },
            )
        profile = self._repository.get_profile(submission.profile_id, tenant_id)
        if profile is None or not profile.is_active:
            raise MoadianInvalidStateError(
                f"Moadian profile {submission.profile_id!r} is not active.",
                details={"moadian_profile_id": submission.profile_id},
            )
        outbox_payload = {
            "submission_id": submission.id,
            "uid": submission.uid,
            "document_id": submission.document_id,
            "profile_id": submission.profile_id,
            "environment": submission.environment.value,
            "scenario": submission.scenario.value,
            "retry": submission.retry,
        }
        now = _now()
        event = OutboxEvent(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            aggregate_type=MOADIAN_SUBMISSION_AGGREGATE,
            aggregate_id=submission.id,
            event_type=MOADIAN_SUBMISSION_QUEUED_EVENT,
            payload=outbox_payload,
            payload_hash=_canonical_hash(outbox_payload),
            status=OutboxEventStatus.PENDING,
            attempts=0,
            last_attempt_at=None,
            last_error=None,
            published_at=None,
            correlation_id=submission.correlation_id,
            created_at=now,
            updated_at=now,
        )
        event = self._repository.save_outbox_event(event)
        return self._repository.save_submission(
            dataclasses.replace(
                submission,
                outbox_event_id=event.id,
                status=SubmissionStatus.QUEUED,
                updated_at=_now(),
            )
        )


class MoadianReconciliationWorker:
    """Dispatch queued submissions to the simulator and poll their status.

    One pass (spec §12.3): (1) publish every pending
    ``moadian.submission.queued`` outbox event by submitting its packet
    to the simulator; (2) poll every in-flight (``submitted``)
    submission and persist each inquiry response as a status event
    with its evidence. Request/response hashes, timestamps and
    correlation ids are persisted on the submission at every step.

    The worker is deterministic and idempotent across passes: the
    simulator is re-seeded from the database (poll counts come from
    the persisted status events), and an outbox event whose submission
    already moved on is published as stale instead of resubmitted.
    """

    def __init__(self, repository: MoadianRepositoryPort, simulator: MoadianSimulatorPort) -> None:
        self._repository = repository
        self._simulator = simulator

    def run_pass(self, tenant_id: str) -> ReconciliationReport:
        """Run one dispatch + poll pass for the tenant.

        Flushes; the caller commits.
        """
        dispatched, submit_failed, skipped = self._dispatch_pending(tenant_id)
        polled, accepted, rejected, still_pending = self._poll_in_flight(tenant_id)
        return ReconciliationReport(
            dispatched=dispatched,
            submit_failed=submit_failed,
            skipped=skipped,
            polled=polled,
            accepted=accepted,
            rejected=rejected,
            still_pending=still_pending,
        )

    # -- dispatch ----------------------------------------------------------

    def _dispatch_pending(self, tenant_id: str) -> tuple[int, int, int]:
        """Submit every pending queued-submission outbox event."""
        events = self._repository.list_pending_outbox_events(tenant_id, MOADIAN_SUBMISSION_QUEUED_EVENT)
        dispatched = 0
        submit_failed = 0
        skipped = 0
        for event in events:
            outcome = self._dispatch_one(event, tenant_id)
            if outcome == "dispatched":
                dispatched += 1
            elif outcome == "failed":
                submit_failed += 1
            else:
                skipped += 1
        return dispatched, submit_failed, skipped

    def _dispatch_one(self, event: OutboxEvent, tenant_id: str) -> str:
        """Dispatch one outbox event. Returns 'dispatched' | 'failed' | 'skipped'."""
        started_at = _now()
        submission = self._repository.get_submission(event.aggregate_id, tenant_id)
        if submission is None:
            self._fail_outbox(event, started_at, "MOADIAN_SUBMISSION_NOT_FOUND", "Submission no longer exists.")
            return "failed"
        if submission.status != SubmissionStatus.QUEUED:
            # Already handled (e.g. by an earlier pass): publish as stale.
            self._publish_outbox(event, started_at)
            return "skipped"
        if submission.environment != MoadianEnvironment.TEST:
            self._fail_outbox(
                event,
                started_at,
                "MOADIAN_PRODUCTION_NOT_CONNECTED",
                "Production Moadian connectivity is not built in this phase.",
            )
            return "failed"
        document = self._repository.get_document(submission.document_id, tenant_id)
        profile = self._repository.get_profile(submission.profile_id, tenant_id)
        if document is None or profile is None:
            self._fail_outbox(event, started_at, "MOADIAN_INVALID_STATE", "Document or profile no longer exists.")
            return "failed"

        # Packet per instruction Tables 5-6. The invoice JSON travels in
        # clear inside a simulated packet: this phase performs no signing
        # or encryption (those land with the production connector), and
        # the envelope marks the packet simulated.
        packet = {
            "uid": submission.uid,
            "packetType": profile.packet_type,
            "retry": submission.retry,
            "data": _canonical_json(document.payload),
            "encryptionKeyId": None,
            "symmetrickey": None,
            "iv": None,
            "fiscalId": profile.fiscal_id,
            "dataSignature": None,
        }
        request_payload = {
            "simulated": True,
            "scenario": submission.scenario.value,
            "packet": packet,
        }
        result = self._simulator.submit_packet(
            uid=submission.uid,
            packet=packet,
            scenario=submission.scenario,
        )
        if not result.accepted:
            response_payload = {
                "simulated": True,
                "uid": result.uid,
                "referenceNumber": None,
                "errorCode": result.error_code,
                "errorDetail": result.error_detail,
            }
            self._repository.save_submission(
                dataclasses.replace(
                    submission,
                    request_payload=request_payload,
                    request_hash=_canonical_hash(request_payload),
                    response_payload=response_payload,
                    response_hash=_canonical_hash(response_payload),
                    status=SubmissionStatus.FAILED,
                    last_error_code=result.error_code,
                    last_error_detail=result.error_detail,
                    updated_at=_now(),
                )
            )
            self._fail_outbox(event, started_at, result.error_code or "SIMULATOR_ERROR", result.error_detail)
            return "failed"
        response_payload = {
            "simulated": True,
            "uid": result.uid,
            "referenceNumber": result.reference_number,
            "errorCode": None,
            "errorDetail": None,
        }
        self._repository.save_submission(
            dataclasses.replace(
                submission,
                request_payload=request_payload,
                request_hash=_canonical_hash(request_payload),
                response_payload=response_payload,
                response_hash=_canonical_hash(response_payload),
                reference_number=result.reference_number,
                status=SubmissionStatus.SUBMITTED,
                last_error_code=None,
                last_error_detail=None,
                updated_at=_now(),
            )
        )
        self._repository.save_document(
            dataclasses.replace(document, status=DocumentStatus.SUBMITTED, updated_at=_now())
        )
        self._publish_outbox(event, started_at)
        return "dispatched"

    def _publish_outbox(self, event: OutboxEvent, started_at: datetime.datetime) -> None:
        """Mark an outbox event published and log the attempt."""
        now = _now()
        published = self._repository.save_outbox_event(
            dataclasses.replace(
                event,
                attempts=event.attempts + 1,
                last_attempt_at=now,
                status=OutboxEventStatus.PUBLISHED,
                published_at=now,
                last_error=None,
                updated_at=now,
            )
        )
        self._repository.save_integration_attempt(
            IntegrationAttempt(
                id=str(uuid.uuid4()),
                outbox_event_id=event.id,
                attempt_number=published.attempts,
                status=IntegrationAttemptStatus.SUCCEEDED,
                started_at=started_at,
                finished_at=_now(),
                error_code=None,
                error_message=None,
                created_at=_now(),
            )
        )

    def _fail_outbox(
        self,
        event: OutboxEvent,
        started_at: datetime.datetime,
        error_code: str,
        error_message: str | None,
    ) -> None:
        """Mark an outbox event failed and log the attempt."""
        now = _now()
        failed = self._repository.save_outbox_event(
            dataclasses.replace(
                event,
                attempts=event.attempts + 1,
                last_attempt_at=now,
                status=OutboxEventStatus.FAILED,
                last_error=error_message,
                updated_at=now,
            )
        )
        self._repository.save_integration_attempt(
            IntegrationAttempt(
                id=str(uuid.uuid4()),
                outbox_event_id=event.id,
                attempt_number=failed.attempts,
                status=IntegrationAttemptStatus.FAILED,
                started_at=started_at,
                finished_at=_now(),
                error_code=error_code,
                error_message=error_message,
                created_at=_now(),
            )
        )

    # -- polling -----------------------------------------------------------

    def _poll_in_flight(self, tenant_id: str) -> tuple[int, int, int, int]:
        """Poll every in-flight submission and persist the evidence."""
        submissions = [
            submission
            for submission in self._repository.list_submissions(tenant_id, status=SubmissionStatus.SUBMITTED)
            if submission.reference_number is not None
        ]
        if not submissions:
            return 0, 0, 0, 0
        # Re-seed the (possibly fresh) simulator from the database so
        # polling resumes where the last pass left off.
        for submission in submissions:
            prior_polls = self._repository.count_status_events(submission.id)
            assert submission.reference_number is not None
            self._simulator.register_submitted(
                uid=submission.uid,
                reference_number=submission.reference_number,
                scenario=submission.scenario,
                fiscal_id=self._fiscal_id_for(submission, tenant_id),
                polls=prior_polls,
            )
        items = self._simulator.inquiry_by_uid([submission.uid for submission in submissions])
        by_uid = {submission.uid: submission for submission in submissions}
        polled = 0
        accepted = 0
        rejected = 0
        still_pending = 0
        for item in items:
            submission = by_uid.get(item.uid)
            if submission is None:
                continue
            polled += 1
            evidence = {
                "uid": item.uid,
                "referenceNumber": item.reference_number,
                "status": item.status.value,
                "data": item.data,
                "packetType": item.packet_type,
                "fiscalId": item.fiscal_id,
            }
            self._repository.save_status_event(
                MoadianStatusEvent(
                    id=str(uuid.uuid4()),
                    submission_id=submission.id,
                    status=item.status,
                    source=StatusEventSource.SIMULATOR,
                    evidence=evidence,
                    correlation_id=submission.correlation_id,
                    occurred_at=_now(),
                    created_at=_now(),
                )
            )
            response_payload = {"simulated": True, "inquiry": evidence}
            updated = dataclasses.replace(
                submission,
                response_payload=response_payload,
                response_hash=_canonical_hash(response_payload),
                updated_at=_now(),
            )
            if item.status == OfficialStatus.SUCCESS:
                updated = dataclasses.replace(updated, status=SubmissionStatus.ACCEPTED)
                self._set_document_status(submission.document_id, DocumentStatus.ACCEPTED, tenant_id)
                accepted += 1
            elif item.status == OfficialStatus.FAILED:
                error_code, error_detail = _rejection_error(item.data)
                updated = dataclasses.replace(
                    updated,
                    status=SubmissionStatus.REJECTED,
                    last_error_code=error_code,
                    last_error_detail=error_detail,
                )
                self._set_document_status(submission.document_id, DocumentStatus.REJECTED, tenant_id)
                rejected += 1
            else:
                still_pending += 1
            self._repository.save_submission(updated)
        return polled, accepted, rejected, still_pending

    def _fiscal_id_for(self, submission: MoadianSubmission, tenant_id: str) -> str | None:
        profile = self._repository.get_profile(submission.profile_id, tenant_id)
        return profile.fiscal_id if profile is not None else None

    def _set_document_status(self, document_id: str, status: DocumentStatus, tenant_id: str) -> None:
        document = self._repository.get_document(document_id, tenant_id)
        if document is not None:
            self._repository.save_document(
                dataclasses.replace(document, status=status, updated_at=_now())
            )


def _rejection_error(data: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Extract the first simulated rejection error (code, detail)."""
    if not isinstance(data, dict):
        return None, None
    errors = data.get("error")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            code = first.get("code")
            message = first.get("message")
            return (
                code if isinstance(code, str) else None,
                message if isinstance(message, str) else None,
            )
    return None, None
