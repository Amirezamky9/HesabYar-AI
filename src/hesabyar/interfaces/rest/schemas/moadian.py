"""Request/response schemas for the Moadian endpoints (US-404).

Responses map from the domain views — no ORM object ever leaks.
Profiles never carry key material: ``key_reference`` is a reference
(env-var name / secret-manager path) and ``key_fingerprint`` the
SHA-256 of the public key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from hesabyar.application.commands.moadian import ReconciliationReport
from hesabyar.domain.moadian.models import (
    MOADIAN_API_BASE_URL,
    PROTOCOL_VERSION,
    MoadianDocument,
    MoadianEnvironment,
    MoadianProfile,
    MoadianStatusEvent,
    MoadianSubmission,
    SenderMode,
    SimulationScenario,
)
from hesabyar.domain.standards.models import VerificationStatus

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


class MoadianProfileResponse(BaseModel):
    """One Moadian protocol profile."""

    profile_id: str = Field(description="Profile id")
    name: str = Field(description="Human-readable profile name")
    slug: str = Field(description="URL-safe profile key, unique per tenant")
    environment: str = Field(description="Target environment: 'test' or 'production'")
    sender_mode: str = Field(description="Who sends: 'self-tsp' (direct) or 'tsp' (trusted company)")
    fiscal_id: str | None = Field(description="Fiscal memory unique id (6 characters)")
    economic_code: str | None = Field(description="Seller economic code (10 or 14 digits)")
    packet_type: str = Field(description="Packet type, e.g. 'INVOICE.V01'")
    api_base_url: str = Field(description="Official API base URL (profile metadata only)")
    protocol_version: str = Field(description="Version of the official instruction this profile follows")
    source_authority: str | None = Field(description="Issuing authority")
    source_url: str | None = Field(description="Official source document URL")
    snapshot_uri: str | None = Field(description="Repo-relative path of the snapshotted source")
    verification_status: str = Field(description="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)")
    key_reference: str | None = Field(description="Where the signing key lives (reference only, never key material)")
    key_fingerprint: str | None = Field(description="SHA-256 hex of the public key")
    is_active: bool = Field(description="Whether the profile may be used for new submissions")
    notes: str | None = Field(description="Free-form notes")
    created_at: datetime = Field(description="When the profile was created (UTC)")
    updated_at: datetime = Field(description="When the profile was last modified (UTC)")

    @classmethod
    def from_profile(cls, profile: MoadianProfile) -> MoadianProfileResponse:
        """Build a profile response from the domain view."""
        return cls(
            profile_id=profile.id,
            name=profile.name,
            slug=profile.slug,
            environment=profile.environment.value,
            sender_mode=profile.sender_mode.value,
            fiscal_id=profile.fiscal_id,
            economic_code=profile.economic_code,
            packet_type=profile.packet_type,
            api_base_url=profile.api_base_url,
            protocol_version=profile.protocol_version,
            source_authority=profile.source_authority,
            source_url=profile.source_url,
            snapshot_uri=profile.snapshot_uri,
            verification_status=profile.verification_status.value,
            key_reference=profile.key_reference,
            key_fingerprint=profile.key_fingerprint,
            is_active=profile.is_active,
            notes=profile.notes,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )


class CreateMoadianProfileRequest(BaseModel):
    """Request to create a Moadian profile."""

    name: str = Field(min_length=1, max_length=256, description="Human-readable profile name")
    slug: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,127}$",
        description="URL-safe profile key, unique per tenant (lowercase letters, digits, hyphens)",
    )
    environment: MoadianEnvironment = Field(description="Target environment: 'test' (simulator) or 'production'")
    sender_mode: SenderMode = Field(
        default=SenderMode.SELF_TSP,
        description="Who sends: 'self-tsp' (taxpayer directly) or 'tsp' (trusted company)",
    )
    fiscal_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9]{6}$",
        description="Fiscal memory unique id (6 alphanumeric characters)",
    )
    economic_code: str | None = Field(
        default=None,
        pattern=r"^([0-9]{10}|[0-9]{14})$",
        description="Seller economic code (10 or 14 digits)",
    )
    packet_type: str = Field(default="INVOICE.V01", description="Packet type, e.g. 'INVOICE.V01'")
    api_base_url: str = Field(
        default=MOADIAN_API_BASE_URL,
        description="Official API base URL (recorded as profile metadata only)",
    )
    protocol_version: str = Field(
        default=PROTOCOL_VERSION,
        description="Version of the official instruction this profile follows",
    )
    source_authority: str | None = Field(default=None, description="Issuing authority")
    source_url: str | None = Field(default=None, description="Official source document URL")
    snapshot_uri: str | None = Field(default=None, description="Repo-relative path of the snapshotted source")
    verification_status: VerificationStatus = Field(
        default=VerificationStatus.UNVERIFIED,
        description="Verification label (CURRENT_RULES_1405.md §1 + UNVERIFIED)",
    )
    key_reference: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Where the signing key lives: env-var name (test) or secret-manager path (production). "
            "Never key material."
        ),
    )
    key_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 hex of the public key",
    )
    notes: str | None = Field(default=None, description="Free-form notes")

    @field_validator("key_reference")
    @classmethod
    def _reject_key_material(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\n" in value or "\r" in value or "-----BEGIN" in value:
            raise ValueError("key_reference must name where the key lives; it must never contain key material.")
        return value


class UpdateMoadianProfileRequest(BaseModel):
    """Request to update a Moadian profile's mutable fields.

    Environment, sender mode, packet type and protocol version are
    immutable: a profile is a versioned protocol record, so changing
    any of them means creating a new profile.
    """

    name: str | None = Field(default=None, min_length=1, max_length=256, description="Human-readable profile name")
    is_active: bool | None = Field(default=None, description="Whether the profile may be used for new submissions")
    fiscal_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9]{6}$",
        description="Fiscal memory unique id (6 alphanumeric characters)",
    )
    economic_code: str | None = Field(
        default=None,
        pattern=r"^([0-9]{10}|[0-9]{14})$",
        description="Seller economic code (10 or 14 digits)",
    )
    key_reference: str | None = Field(
        default=None,
        max_length=512,
        description="Where the signing key lives (reference only, never key material)",
    )
    key_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 hex of the public key",
    )
    notes: str | None = Field(default=None, description="Free-form notes")

    @field_validator("key_reference")
    @classmethod
    def _reject_key_material(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\n" in value or "\r" in value or "-----BEGIN" in value:
            raise ValueError("key_reference must name where the key lives; it must never contain key material.")
        return value


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class MoadianDocumentResponse(BaseModel):
    """One invoice prepared for Moadian submission."""

    document_id: str = Field(description="Document id")
    profile_id: str = Field(description="Owning profile id")
    invoice_id: str | None = Field(description="HesabYar/Tryton invoice id, when linked")
    taxid: str | None = Field(description="Unique tax number (22 characters)")
    serial: str | None = Field(description="Invoice serial (inno, 10 characters)")
    invoice_type: int = Field(description="Invoice type (inty, 1-3)")
    invoice_pattern: int = Field(description="Invoice pattern (inp, 1-6)")
    invoice_subject: int = Field(description="Invoice subject (ins, 1-4)")
    payload: dict[str, Any] = Field(description="Moadian invoice JSON (header/body/payment)")
    payload_hash: str = Field(description="SHA-256 hex of the canonical invoice payload")
    status: str = Field(description="Document lifecycle: 'draft', 'submitted', 'accepted' or 'rejected'")
    created_at: datetime = Field(description="When the document was created (UTC)")
    updated_at: datetime = Field(description="When the document was last modified (UTC)")

    @classmethod
    def from_document(cls, document: MoadianDocument) -> MoadianDocumentResponse:
        """Build a document response from the domain view."""
        return cls(
            document_id=document.id,
            profile_id=document.profile_id,
            invoice_id=document.invoice_id,
            taxid=document.taxid,
            serial=document.serial,
            invoice_type=document.invoice_type,
            invoice_pattern=document.invoice_pattern,
            invoice_subject=document.invoice_subject,
            payload=dict(document.payload),
            payload_hash=document.payload_hash,
            status=document.status.value,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class CreateMoadianDocumentRequest(BaseModel):
    """Request to create a Moadian document (draft-only, no approval needed)."""

    profile_id: str = Field(description="Owning Moadian profile id")
    invoice_id: str | None = Field(
        default=None,
        description="HesabYar/Tryton invoice id this document was built from, when linked",
    )
    payload: dict[str, Any] = Field(
        description=(
            "Moadian invoice JSON (header/body/payment per instruction Table 7). "
            "Required header fields: taxid (22 chars), indatim, indati2m, inty (1-3), "
            "inno (10 chars), inp (1-6), ins (1-4), tins (10 or 14 digits), tob (1-5), "
            "tprdis, tdis, tadis, tvam, todam, tbill, setm (1-3), cap, insp, tvop. "
            "Each body line requires: sstid (13 digits), sstt, mu, am, fee, prdis, "
            "dis, adis, vra, vam, tsstam. Corrective/cancellation/return invoices "
            "(ins 2-4) also require irtaxid."
        )
    )


# ---------------------------------------------------------------------------
# Submissions + status events
# ---------------------------------------------------------------------------


class MoadianSubmissionResponse(BaseModel):
    """One submission attempt of a document to Moadian."""

    submission_id: str = Field(description="Submission id")
    document_id: str = Field(description="Owning document id")
    profile_id: str = Field(description="Profile used for this submission")
    uid: str = Field(description="Unique submission id (uuid3, client-generated)")
    reference_number: str | None = Field(description="Unique receipt id (uuid) issued on acceptance")
    correlation_id: str = Field(description="Correlation id across submission, outbox event and status events")
    environment: str = Field(description="Target environment: 'test' or 'production'")
    scenario: str = Field(description="Simulator fixture outcome: 'accept' or 'reject'")
    status: str = Field(
        description="Lifecycle: draft → approved → queued → submitted → accepted/rejected/failed"
    )
    retry: bool = Field(description="True when this is a resubmission of the same document")
    approved_by: str | None = Field(description="API client id that approved the submission")
    approved_at: datetime | None = Field(description="When the submission was approved (UTC)")
    approval_note: str | None = Field(description="Note recorded with the approval")
    request_hash: str | None = Field(description="SHA-256 hex of the canonical submitted packet")
    response_hash: str | None = Field(description="SHA-256 hex of the canonical (simulated) server response")
    request_payload: dict[str, Any] | None = Field(description="Submitted packet envelope")
    response_payload: dict[str, Any] | None = Field(description="Latest (simulated) server response")
    outbox_event_id: str | None = Field(description="Outbox event that dispatched this submission")
    last_error_code: str | None = Field(description="Stable error code of the last failure")
    last_error_detail: str | None = Field(description="Detail of the last failure")
    created_at: datetime = Field(description="When the submission was created (UTC)")
    updated_at: datetime = Field(description="When the submission was last modified (UTC)")

    @classmethod
    def from_submission(cls, submission: MoadianSubmission) -> MoadianSubmissionResponse:
        """Build a submission response from the domain view."""
        return cls(
            submission_id=submission.id,
            document_id=submission.document_id,
            profile_id=submission.profile_id,
            uid=submission.uid,
            reference_number=submission.reference_number,
            correlation_id=submission.correlation_id,
            environment=submission.environment.value,
            scenario=submission.scenario.value,
            status=submission.status.value,
            retry=submission.retry,
            approved_by=submission.approved_by,
            approved_at=submission.approved_at,
            approval_note=submission.approval_note,
            request_hash=submission.request_hash,
            response_hash=submission.response_hash,
            request_payload=dict(submission.request_payload) if submission.request_payload is not None else None,
            response_payload=dict(submission.response_payload) if submission.response_payload is not None else None,
            outbox_event_id=submission.outbox_event_id,
            last_error_code=submission.last_error_code,
            last_error_detail=submission.last_error_detail,
            created_at=submission.created_at,
            updated_at=submission.updated_at,
        )


class CreateMoadianSubmissionRequest(BaseModel):
    """Request to create a draft submission for a document."""

    document_id: str = Field(description="Document to submit")
    scenario: SimulationScenario = Field(
        default=SimulationScenario.ACCEPT,
        description="Simulator fixture outcome: 'accept' (ends SUCCESS) or 'reject' (ends FAILED)",
    )


class ApproveMoadianSubmissionRequest(BaseModel):
    """Request to record the human approval of a draft submission."""

    note: str | None = Field(default=None, description="Optional note recorded with the approval")


class MoadianStatusEventResponse(BaseModel):
    """One official status observation for a submission."""

    event_id: str = Field(description="Status event id")
    submission_id: str = Field(description="Owning submission id")
    status: str = Field(description="Official status: PENDING, SUCCESS, FAILED, IN_PROGRESS, NOT_FOUND or TIMEOUT")
    source: str = Field(description="Where the evidence came from: 'simulator', 'operator' or 'system'")
    evidence: dict[str, Any] | None = Field(description="Inquiry response item verbatim")
    correlation_id: str | None = Field(description="Correlation id of the submission")
    occurred_at: datetime = Field(description="When the status was observed (UTC)")
    created_at: datetime = Field(description="When the event was recorded (UTC)")

    @classmethod
    def from_event(cls, event: MoadianStatusEvent) -> MoadianStatusEventResponse:
        """Build a status-event response from the domain view."""
        return cls(
            event_id=event.id,
            submission_id=event.submission_id,
            status=event.status.value,
            source=event.source.value,
            evidence=dict(event.evidence) if event.evidence is not None else None,
            correlation_id=event.correlation_id,
            occurred_at=event.occurred_at,
            created_at=event.created_at,
        )


class ReconciliationReportResponse(BaseModel):
    """Outcome of one reconciliation pass."""

    dispatched: int = Field(description="Queued submissions submitted to the simulator")
    submit_failed: int = Field(description="Queued submissions the simulator refused (or that could not dispatch)")
    skipped: int = Field(description="Outbox events whose submission had already moved on")
    polled: int = Field(description="In-flight submissions polled")
    accepted: int = Field(description="Submissions that reached SUCCESS/accepted in this pass")
    rejected: int = Field(description="Submissions that reached FAILED/rejected in this pass")
    still_pending: int = Field(description="Polled submissions still in flight (PENDING/IN_PROGRESS/TIMEOUT/NOT_FOUND)")

    @classmethod
    def from_report(cls, report: ReconciliationReport) -> ReconciliationReportResponse:
        """Build a report response from the worker's report."""
        return cls(
            dispatched=report.dispatched,
            submit_failed=report.submit_failed,
            skipped=report.skipped,
            polled=report.polled,
            accepted=report.accepted,
            rejected=report.rejected,
            still_pending=report.still_pending,
        )
