"""Schemas for the MCP client-management endpoints (US-606, spec §13.8).

The registry record (``mcp_authorized_clients``) is the management
view of one AI client's MCP access; the credential itself is an
ordinary API key. No secret ever appears in a response except the
one-time ``secret`` field of the create/rotate responses — and the
admin UI must show that exactly once, then drop it.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from hesabyar.infrastructure.auth.scopes import parse_scopes
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    McpAuthorizedClientModel,
)


class McpClientCreateRequest(BaseModel):
    """Create an authorized MCP client (issues its API key)."""

    name: str = Field(
        min_length=1,
        max_length=128,
        description="Client name (unique per tenant)",
    )
    description: str | None = Field(
        default=None, description="What this client is for (which AI host, which team)"
    )
    scopes: list[str] = Field(
        min_length=1,
        description="Scopes for the client's key; must include 'mcp:connect'",
    )
    company_id: str | None = Field(
        default=None,
        description=(
            "Company grant: HesabYar company row id or engine company id. "
            "Null means every company of the tenant."
        ),
    )
    expires_at: datetime.datetime | None = Field(
        default=None, description="Key expiry (UTC); null means no expiry"
    )


class McpClientRevokeRequest(BaseModel):
    """Revoke an authorized MCP client (its key stops working at once)."""

    reason: str | None = Field(
        default=None,
        max_length=256,
        description="Why the client is revoked (recorded on the row and its key)",
    )


class McpClientResponse(BaseModel):
    """One authorized MCP client (never carries the secret)."""

    id: str = Field(description="Registry row id (UUID)")
    api_client_id: str = Field(
        description=(
            "The underlying api_clients row id — the client_id the audit "
            "log records, so the admin UI can filter the log by client"
        )
    )
    name: str = Field(description="Client name")
    description: str | None = Field(default=None, description="What this client is for")
    key_prefix: str = Field(description="Visible prefix of the current key (safe to show)")
    scopes: list[str] = Field(description="Scopes the client's key carries (sorted)")
    company_id: str | None = Field(
        default=None,
        description="Company grant (HesabYar row id); null means every company of the tenant",
    )
    company_name: str | None = Field(
        default=None, description="Granted company name, when the grant names one company"
    )
    status: str = Field(description="'active' or 'revoked'")
    created_at: datetime.datetime = Field(description="Creation timestamp (UTC)")
    updated_at: datetime.datetime = Field(description="Last-modified timestamp (UTC)")
    last_used_at: datetime.datetime | None = Field(
        default=None, description="Last successful use of the current key (UTC)"
    )
    expires_at: datetime.datetime | None = Field(
        default=None, description="Current key expiry (UTC); null means no expiry"
    )
    revoked_at: datetime.datetime | None = Field(
        default=None, description="Revocation timestamp (UTC); null means not revoked"
    )
    revoked_reason: str | None = Field(
        default=None, description="Why the client was revoked"
    )

    @classmethod
    def from_record(
        cls, record: McpAuthorizedClientModel, *, company_name: str | None
    ) -> McpClientResponse:
        """Build a response from a registry row (+ its current key)."""
        key = record.key
        return cls(
            id=record.id,
            api_client_id=record.client_id,
            name=record.name,
            description=record.description,
            key_prefix=key.key_prefix,
            scopes=sorted(parse_scopes(record.scopes)),
            company_id=record.engine_company_id,
            company_name=company_name,
            status="revoked" if record.revoked_at is not None else "active",
            created_at=record.created_at,
            updated_at=record.updated_at,
            last_used_at=key.last_used_at,
            expires_at=key.expires_at,
            revoked_at=record.revoked_at,
            revoked_reason=record.revoked_reason,
        )


class McpClientWithSecretResponse(McpClientResponse):
    """Create/rotate response: the client plus its secret, shown ONCE.

    The secret is the full ``hya_live_`` key. It is returned only by
    the create and rotate endpoints, only in the response to the
    request that issued it, and must never be stored, logged or
    returned again.
    """

    secret: str = Field(
        description="The full API key secret — shown exactly once, never again"
    )


class McpAuditRowResponse(BaseModel):
    """One AI access audit row: who called what, when, with what outcome."""

    id: str = Field(description="Audit row id (UUID)")
    client_id: str | None = Field(
        default=None, description="API client id (null when unauthenticated)"
    )
    key_prefix: str | None = Field(
        default=None, description="Visible key prefix (null when unauthenticated)"
    )
    tool: str = Field(
        description="Tool name, resource URI, prompt name or lifecycle action"
    )
    kind: str = Field(description="'tool' | 'resource' | 'prompt' | 'management'")
    company_id: str | None = Field(
        default=None, description="HesabYar company row id the call touched"
    )
    company_name: str | None = Field(
        default=None, description="Company name, when the row names a known company"
    )
    scopes: list[str] = Field(
        default_factory=list, description="Scopes the key carried at call time"
    )
    outcome: str = Field(description="'allowed' | 'denied' | 'error'")
    error_code: str | None = Field(
        default=None, description="Machine-readable failure reason (null when allowed)"
    )
    request_id: str | None = Field(default=None, description="MCP request id, when known")
    transport: str = Field(description="'stdio' | 'streamable-http' | 'memory' | 'rest'")
    client_info: str | None = Field(
        default=None, description="MCP client name/version from initialize, when known"
    )
    created_at: datetime.datetime = Field(description="Call timestamp (UTC)")

    @classmethod
    def from_model(
        cls, row: AiAccessAuditModel, *, company_name: str | None
    ) -> McpAuditRowResponse:
        """Build a response from an audit row."""
        return cls(
            id=row.id,
            client_id=row.client_id,
            key_prefix=row.key_prefix,
            tool=row.tool,
            kind=row.kind,
            company_id=row.company_id,
            company_name=company_name,
            scopes=sorted(parse_scopes(row.scopes)),
            outcome=row.outcome,
            error_code=row.error_code,
            request_id=row.request_id,
            transport=row.transport,
            client_info=row.client_info,
            created_at=row.created_at,
        )
