"""Response schemas for the audit-trail query endpoints (US-205, spec §14.3).

Audit events are immutable HesabYar-side records (``audit_events``),
not engine data: the response maps the ORM model, never a Tryton
record. No secret ever appears — audit rows store only the API client
id, hashes and evidence pointers, never key material.
"""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, Field

from hesabyar.infrastructure.persistence.postgres.models import AuditEventModel


class AuditEventResponse(BaseModel):
    """One audit event: who did what, to which resource, with what outcome."""

    id: str = Field(description="Audit event id (UUID)")
    tenant_id: str = Field(description="Owning tenant id")
    installation_id: str = Field(description="Installation id the event belongs to")
    actor_id: str | None = Field(
        default=None, description="Acting actor id; null for non-actor events"
    )
    api_client_id: str | None = Field(
        default=None, description="Source API client id; null for non-API actions"
    )
    action: str = Field(
        description="Action performed, e.g. 'ledger.draft.create'"
    )
    resource_type: str = Field(
        description="Resource type acted on, e.g. 'journal_draft'"
    )
    resource_id: str | None = Field(
        default=None, description="Resource id acted on"
    )
    company_external_id: str | None = Field(
        default=None, description="Engine company id — external reference"
    )
    occurred_at: datetime.datetime = Field(
        description="Event timestamp (UTC)"
    )
    request_id: str | None = Field(
        default=None, description="Request id that caused the event"
    )
    source_system: str | None = Field(
        default=None, description="Source system key, e.g. 'api'"
    )
    authorization_result: str = Field(
        description="Authorization outcome: 'allowed' or 'denied'"
    )
    outcome: str = Field(description="Event outcome: 'success' or 'error'")
    error_code: str | None = Field(
        default=None, description="Stable error code when the outcome is not success"
    )
    tryton_reference: str | None = Field(
        default=None,
        description="Engine reference for accounting operations, e.g. 'account.move:123'",
    )
    payload_hash: str | None = Field(
        default=None, description="SHA-256 hex of the request payload (tamper evidence)"
    )
    metadata_after: dict[str, Any] | None = Field(
        default=None, description="After-change metadata (incl. the idempotency key)"
    )

    @classmethod
    def from_model(cls, event: AuditEventModel) -> AuditEventResponse:
        """Build a response from an audit event row."""
        return cls(
            id=event.id,
            tenant_id=event.tenant_id,
            installation_id=event.installation_id,
            actor_id=event.actor_id,
            api_client_id=event.api_client_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            company_external_id=event.company_external_id,
            occurred_at=event.occurred_at,
            request_id=event.request_id,
            source_system=event.source_system,
            authorization_result=event.authorization_result,
            outcome=event.outcome,
            error_code=event.error_code,
            tryton_reference=event.tryton_reference,
            payload_hash=event.payload_hash,
            metadata_after=event.metadata_after,
        )
