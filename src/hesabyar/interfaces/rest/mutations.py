"""Shared idempotent-mutation helpers for REST routers (US-205).

Mutating routes share one completion contract (spec §10.4 idempotency +
§14.3 audit):

- on success, the completed response is stored for idempotent replay
  and a success audit event is written, then the session commits;
- on failure, an error audit event is written (with the stable error
  code), the idempotency claim is released so the key can be retried,
  the session commits, and the error is translated into a typed
  problem response.

The journal router (US-202) keeps its own private copies of these
helpers — they predate this module and live in a file another story is
still editing; a later cleanup can unify them. New routers use this
module.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from hesabyar.domain.common.errors import HesabYarError
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.audit import AuditRecorder
from hesabyar.interfaces.rest.errors import ApiError, api_error_from_hesabyar_error
from hesabyar.interfaces.rest.idempotency import IdempotencyClaim
from hesabyar.interfaces.rest.middleware import get_current_request_id


def record_audit_event(
    session: Session,
    client: AuthenticatedClient,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    company_external_id: str | None,
    outcome: str,
    error_code: str | None = None,
    tryton_reference: str | None = None,
    payload_hash: str | None = None,
    idempotency_key: str | None = None,
    metadata_after: dict[str, Any] | None = None,
) -> None:
    """Append one audit event for the current request (flushes; caller commits)."""
    AuditRecorder(session).record(
        tenant_id=client.tenant_id,
        installation_id=client.installation_id,
        api_client_id=client.client_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        company_external_id=company_external_id,
        outcome=outcome,
        request_id=get_current_request_id(),
        error_code=error_code,
        tryton_reference=tryton_reference,
        payload_hash=payload_hash,
        idempotency_key=idempotency_key,
        metadata_after=metadata_after,
    )


def complete_mutation(
    session: Session,
    client: AuthenticatedClient,
    claim: IdempotencyClaim,
    *,
    response_status: int,
    response: BaseModel,
    response_reference: str,
    action: str,
    resource_type: str,
    resource_id: str,
    company_external_id: str,
    tryton_reference: str,
    metadata_after: dict[str, Any] | None = None,
) -> None:
    """Store the idempotent response + success audit event, then commit."""
    claim.complete(
        response_status=response_status,
        response_body=response.model_dump(mode="json"),
        response_reference=response_reference,
    )
    record_audit_event(
        session,
        client,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        company_external_id=company_external_id,
        outcome="success",
        tryton_reference=tryton_reference,
        payload_hash=claim.payload_hash,
        idempotency_key=claim.record.idempotency_key,
        metadata_after=metadata_after,
    )
    session.commit()


def fail_mutation(
    session: Session,
    client: AuthenticatedClient,
    claim: IdempotencyClaim,
    exc: HesabYarError,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    company_external_id: str | None,
    tryton_reference: str | None = None,
    metadata_after: dict[str, Any] | None = None,
) -> ApiError:
    """Record a failure audit event, release the claim, commit, translate the error.

    The claim is released so the same key can be retried; the failure
    audit event keeps the stable error code and — for a failed reversal
    post — the surviving ``reversal_draft_id`` for recovery.
    """
    metadata: dict[str, Any] = dict(metadata_after or {})
    reversal_draft_id = exc.details.get("reversal_draft_id")
    if isinstance(reversal_draft_id, int):
        metadata["reversal_draft_id"] = reversal_draft_id
    record_audit_event(
        session,
        client,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        company_external_id=company_external_id,
        outcome="error",
        error_code=exc.code,
        tryton_reference=tryton_reference,
        payload_hash=claim.payload_hash,
        idempotency_key=claim.record.idempotency_key,
        metadata_after=metadata or None,
    )
    claim.release()
    session.commit()
    return api_error_from_hesabyar_error(exc)
