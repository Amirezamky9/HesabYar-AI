"""MCP middleware: authentication, authorization, audit (US-601).

One :class:`HesabYarMcpMiddleware` sits in front of every MCP request
on every transport (the SDK's ``ServerMiddleware`` runs before
validation, lookup or handshake dispatch), so enforcement cannot be
forgotten by a future tool:

1. **Authentication** — every request except ``initialize``, ``ping``
   and notifications needs an authenticated identity (bearer token for
   Streamable HTTP, bootstrap client for stdio) carrying the base
   scope ``mcp:connect``. The HTTP layer already enforces this at the
   transport (401/403); the middleware enforces it again at the
   protocol layer so stdio and in-memory transports get the same
   guarantee.
2. **Scopes** — a ``tools/call`` needs the scopes its tool
   registered (``ToolRegistry``); a ``resources/read`` needs the
   scopes of the template its URI matches (US-606: template-aware
   matching, so resource scopes are enforced centrally too — the
   handler check stays as defence in depth); a ``prompts/get``
   needs the prompt's scopes. A key missing any of them is denied.
3. **Company check** — a ``tools/call`` whose arguments carry a
   ``company`` value has it resolved against the grant
   (:func:`hesabyar.interfaces.mcp.auth.resolve_company_row`): the
   caller-provided id is never proof of authorization. A company the
   key cannot access fails closed. Resource and prompt handlers
   apply the same check to the ``company`` URI segment / argument.
4. **Audit** — every ``tools/call``, ``resources/read`` and
   ``prompts/get`` is audit-logged (allowed, denied or error) through
   :class:`AiAccessAuditWriter`, with the company the call touched:
   the ``company`` argument for tools and prompts, the ``{company}``
   URI segment for resources (US-606 — resolved tenant-scoped for
   the audit record only, never as authorization). A tool call whose
   result carries ``is_error`` (unknown tool, invalid arguments —
   the SDK returns these as normal results, not protocol errors) is
   audited as an error, never as allowed. List operations
   (``tools/list``, ``resources/list``, ``prompts/list``) and
   ``initialize``/``ping`` are NOT audited: they access no company
   data. An audit-write failure fails the call closed
   (``MCP_ERROR_INTERNAL``) rather than serving an unaudited call —
   the result is withheld. Known limitation: the tool has already run
   by then, so a failed audit does not roll back side effects;
   US-601 tools are read-only, and US-605 draft tools document this
   in their own safety notes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.mcp.audit import AuditKind, AuditOutcome
from hesabyar.interfaces.mcp.auth import MCP_BASE_SCOPE, find_company_row, resolve_company_row
from hesabyar.interfaces.mcp.context import McpContext, current_client
from hesabyar.interfaces.mcp.errors import (
    CompanyAccessError,
    forbidden,
    internal_error,
    is_auth_error,
    unauthenticated,
)

logger = logging.getLogger(__name__)

#: Methods that pass through without an identity check: the handshake
#: itself, liveness, and notifications (which carry no data access).
_PASSTHROUGH_METHODS = frozenset({"initialize", "ping"})

#: Methods whose attempts are audit-logged (data access).
_AUDITABLE_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})


def _is_error_result(result: HandlerResult) -> bool:
    """True when a tool-call result reports failure (``is_error``).

    The SDK converts an unknown tool or invalid arguments into a
    ``CallToolResult(is_error=True)`` returned NORMALLY — only
    ``MCPError`` propagates as a protocol error — so a successful
    return is not necessarily a success. ``HandlerResult`` is
    ``BaseModel | dict | None``; the SDK returns a ``CallToolResult``
    model, but read defensively so a dict-shaped result is handled
    the same way.
    """
    if isinstance(result, dict):
        return bool(result.get("is_error"))
    return bool(getattr(result, "is_error", False))


class HesabYarMcpMiddleware:
    """Enforce auth + scopes + company checks; audit every data call."""

    def __init__(self, context: McpContext) -> None:
        self._context = context

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        method = ctx.method
        if method in _PASSTHROUGH_METHODS or method.startswith("notifications/"):
            return await call_next(ctx)

        client = current_client(self._context)
        if client is None:
            if method in _AUDITABLE_METHODS:
                await self._audit(ctx, None, method, outcome="denied", error_code="unauthenticated")
            raise unauthenticated()
        if not client.has_scope(MCP_BASE_SCOPE):
            if method in _AUDITABLE_METHODS:
                await self._audit(ctx, client, method, outcome="denied", error_code="missing_scope")
            raise forbidden(f"The API key lacks the required scope: {MCP_BASE_SCOPE}.")

        if method == "tools/call":
            return await self._handle_tool_call(ctx, call_next, client)
        if method == "resources/read":
            return await self._handle_audited(
                ctx, call_next, client, kind="resource", name=self._param(ctx, "uri")
            )
        if method == "prompts/get":
            return await self._handle_audited(
                ctx, call_next, client, kind="prompt", name=self._param(ctx, "name")
            )
        # tools/list, resources/list, prompts/list, ...: authenticated
        # (and base-scoped) above; no company data is accessed, so no
        # audit row and no further check.
        return await call_next(ctx)

    # -- tool calls -----------------------------------------------------

    async def _handle_tool_call(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
        client: AuthenticatedClient,
    ) -> HandlerResult:
        tool_name = self._param(ctx, "name")
        required = self._context.registry.scopes_for_tool(tool_name)
        if required is not None:
            missing = client.missing_scopes(*sorted(required))
            if missing:
                await self._audit(
                    ctx,
                    client,
                    "tools/call",
                    outcome="denied",
                    error_code="missing_scope",
                )
                raise forbidden(
                    f"The API key lacks the required scope(s): {', '.join(missing)}."
                )
        company_id = await self._checked_company(ctx, client)
        try:
            result = await call_next(ctx)
        except MCPError as exc:
            outcome: AuditOutcome = "denied" if is_auth_error(exc) else "error"
            error_code = "forbidden" if is_auth_error(exc) else "tool_error"
            await self._audit(
                ctx, client, "tools/call", outcome=outcome, error_code=error_code, company_id=company_id
            )
            raise
        except ValidationError:
            await self._audit(
                ctx,
                client,
                "tools/call",
                outcome="error",
                error_code="invalid_arguments",
                company_id=company_id,
            )
            raise
        except Exception:
            await self._audit(
                ctx, client, "tools/call", outcome="error", error_code="tool_error", company_id=company_id
            )
            raise
        # A returned result is not necessarily a success: the SDK turns
        # an unknown tool or invalid arguments into a CallToolResult
        # with is_error=True instead of raising, so audit such a result
        # as an error — never record a failed call as "allowed".
        if _is_error_result(result):
            await self._audit(
                ctx, client, "tools/call", outcome="error", error_code="tool_error", company_id=company_id
            )
        else:
            await self._audit(
                ctx, client, "tools/call", outcome="allowed", company_id=company_id
            )
        return result

    async def _checked_company(
        self,
        ctx: ServerRequestContext[Any, Any],
        client: AuthenticatedClient,
    ) -> str | None:
        """Resolve the call's ``company`` argument against the grant.

        Returns the canonical company row id for the audit record, or
        None when the call carries no company argument. A non-string
        company value is left for the tool's own argument validation
        (it fails there with ``invalid_arguments``). A company the key
        cannot access is audited as denied and refused.
        """
        params = ctx.params
        if not isinstance(params, dict):
            return None
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return None
        raw_company = arguments.get("company")
        if not isinstance(raw_company, str) or not raw_company:
            return None

        def _resolve() -> str:
            session = self._context.session_factory()
            try:
                return resolve_company_row(session, client, raw_company).id
            finally:
                session.close()

        try:
            return await asyncio.to_thread(_resolve)
        except CompanyAccessError as exc:
            await self._audit(
                ctx, client, "tools/call", outcome="denied", error_code=exc.error_code
            )
            raise forbidden(exc.message) from None

    # -- resources / prompts --------------------------------------------

    async def _handle_audited(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
        client: AuthenticatedClient,
        *,
        kind: str,
        name: str,
    ) -> HandlerResult:
        method = "resources/read" if kind == "resource" else "prompts/get"
        required = (
            # US-606: the concrete URI is matched against the
            # registered templates, so resource scopes are enforced
            # centrally like tool and prompt scopes.
            self._context.registry.scopes_for_resource_uri(name)
            if kind == "resource"
            else self._context.registry.scopes_for_prompt(name)
        )
        # The company for the audit record, resolved before the scope
        # check so even a scope-denied call names the company it
        # touched (audit only — never authorization; the handler
        # enforces the grant).
        company_id = await self._audit_company(ctx, client, kind=kind, name=name)
        if required is not None:
            missing = client.missing_scopes(*sorted(required))
            if missing:
                await self._audit(
                    ctx,
                    client,
                    method,
                    outcome="denied",
                    error_code="missing_scope",
                    company_id=company_id,
                )
                raise forbidden(
                    f"The API key lacks the required scope(s): {', '.join(missing)}."
                )
        try:
            result = await call_next(ctx)
        except MCPError as exc:
            outcome: AuditOutcome = "denied" if is_auth_error(exc) else "error"
            error_code = "forbidden" if is_auth_error(exc) else "tool_error"
            await self._audit(
                ctx, client, method, outcome=outcome, error_code=error_code, company_id=company_id
            )
            raise
        except ValidationError:
            await self._audit(
                ctx,
                client,
                method,
                outcome="error",
                error_code="invalid_arguments",
                company_id=company_id,
            )
            raise
        except Exception:
            await self._audit(
                ctx, client, method, outcome="error", error_code="tool_error", company_id=company_id
            )
            raise
        await self._audit(
            ctx, client, method, outcome="allowed", company_id=company_id
        )
        return result

    async def _audit_company(
        self,
        ctx: ServerRequestContext[Any, Any],
        client: AuthenticatedClient,
        *,
        kind: str,
        name: str,
    ) -> str | None:
        """Best-effort company row id for the audit record.

        Resources: the ``{company}`` segment of the URI (matched
        against the registered templates). Prompts: the ``company``
        argument. The value is resolved tenant-scoped — a row of
        another tenant, or an unresolvable value, yields None. This
        is AUDIT ONLY: it never authorizes anything (the handler
        enforces the grant), so a denied call still records the
        company it tried to touch.
        """
        raw: str | None = None
        if kind == "resource":
            matched = self._context.registry.match_resource_uri(name)
            if matched is not None:
                raw = matched[1].get("company")
        else:
            params = ctx.params
            if isinstance(params, dict):
                arguments = params.get("arguments")
                if isinstance(arguments, dict):
                    value = arguments.get("company")
                    if isinstance(value, str) and value:
                        raw = value
        if not raw:
            return None
        company_value: str = raw
        tenant_id = client.tenant_id

        def _lookup() -> str | None:
            session = self._context.session_factory()
            try:
                row = find_company_row(session, tenant_id, company_value)
                return row.id if row is not None else None
            finally:
                session.close()

        return await asyncio.to_thread(_lookup)

    # -- helpers ----------------------------------------------------------

    def _param(self, ctx: ServerRequestContext[Any, Any], key: str) -> str:
        """Read a raw string param, tolerating malformed requests."""
        params = ctx.params
        if isinstance(params, dict):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        return "<unknown>"

    def _client_info(self, ctx: ServerRequestContext[Any, Any]) -> str | None:
        """MCP client name/version from initialize, when available."""
        try:
            params = ctx.session.client_params
        except AttributeError:
            return None
        if params is None:
            return None
        info = params.client_info
        return f"{info.name}/{info.version}"

    async def _audit(
        self,
        ctx: ServerRequestContext[Any, Any],
        client: AuthenticatedClient | None,
        method: str,
        *,
        outcome: AuditOutcome,
        error_code: str | None = None,
        company_id: str | None = None,
    ) -> None:
        """Write one audit row; an audit failure fails the call closed."""
        if method == "tools/call":
            kind: AuditKind = "tool"
            name = self._param(ctx, "name")
        elif method == "resources/read":
            kind = "resource"
            name = self._param(ctx, "uri")
        elif method == "prompts/get":
            kind = "prompt"
            name = self._param(ctx, "name")
        else:  # pragma: no cover - only auditable methods reach here
            return
        request_id = ctx.request_id
        try:
            await self._context.audit_writer.record_async(
                client=client,
                tool=name,
                kind=kind,
                company_id=company_id,
                outcome=outcome,
                error_code=error_code,
                request_id=str(request_id) if request_id is not None else None,
                transport=self._context.transport,
                client_info=self._client_info(ctx),
            )
        except Exception:
            logger.critical(
                "AI access audit write failed for %s %r (outcome=%s); the call is refused.",
                method,
                name,
                outcome,
                exc_info=True,
            )
            raise internal_error(
                "Audit logging failed; the result is withheld."
            ) from None
