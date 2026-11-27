"""In-memory MCP conformance harness (US-601).

No pytest-asyncio in this repo: tests are sync functions that drive
the async harness with ``asyncio.run()``. The harness connects a real
SDK ``ClientSession`` to a real ``MCPServer`` over the SDK's in-memory
streams — the same protocol path a remote client takes, minus the
socket.

The fixture echo tool is registered through the production
:func:`register_hesabyar_tool` helper with real scopes, so the
enforcement path the tests exercise is the production path. It takes
the narrow arguments the spec allows (``company`` + a payload) and
returns the transport it ran on, proving the lifespan context reached
the handler.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

from mcp.client.session import ClientSession
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import AiAccessAuditModel
from hesabyar.interfaces.mcp import McpContext, create_mcp_server, register_hesabyar_tool, run_over_streams
from tests.mcp.conftest import World


@asynccontextmanager
async def connected_client(server: MCPServer[Any]) -> AsyncIterator[ClientSession]:
    """Connect a real SDK client session to ``server`` over memory streams."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(
            run_over_streams(server, server_streams[0], server_streams[1])
        )
        try:
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                yield session
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def register_echo_tool(
    server: MCPServer[Any],
    context: McpContext,
    *,
    name: str,
    scopes: tuple[str, ...] = ("reports:read",),
) -> None:
    """Register the conformance echo tool (production registration path)."""

    async def echo(
        message: str = "hello",
        company: str | None = None,
        # Bare Context (not Context[McpContext, ...]): the SDK's context
        # detection only recognises the unparametrised annotation.
        ctx: Context | None = None,
    ) -> str:
        transport = "no-context"
        if ctx is not None:
            try:
                mcp_context = cast(McpContext, ctx.request_context.lifespan_context)
                transport = mcp_context.transport
            except (RuntimeError, AttributeError):
                transport = "no-context"
        return f"echo:{message}:{company}:{transport}"

    register_hesabyar_tool(
        server,
        context.registry,
        echo,
        name=name,
        scopes=scopes,
        description="Conformance echo tool (US-601 test harness).",
    )


def authenticate_secret(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    secret: str,
) -> AuthenticatedClient:
    """Authenticate ``secret`` and return the identity (bootstrap helper)."""
    session = session_factory()
    try:
        client = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper)).authenticate(
            session, secret=secret
        )
        session.commit()
        return client
    finally:
        session.close()


def make_server(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None = None,
    tool_scopes: tuple[str, ...] = ("reports:read",),
    transport: str = "memory",
    with_tool: bool = True,
) -> tuple[MCPServer[Any], McpContext]:
    """Build a server (+ context) for ``world`` with the echo tool registered."""
    context = McpContext(
        settings=settings,
        session_factory=session_factory,
        transport=transport,
    )
    if bootstrap_secret is not None:
        context.bootstrap_client = authenticate_secret(session_factory, settings, bootstrap_secret)
    server = create_mcp_server(context)
    if with_tool:
        register_echo_tool(server, context, name=world.tool_name, scopes=tool_scopes)
    return server, context


def audit_rows(
    session_factory: Callable[[], Session], tool_name: str
) -> list[AiAccessAuditModel]:
    """This world's audit rows for ``tool_name``, oldest first."""
    session = session_factory()
    try:
        return list(
            session.execute(
                select(AiAccessAuditModel)
                .where(AiAccessAuditModel.tool == tool_name)
                .order_by(AiAccessAuditModel.created_at)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def tool_text(result: Any) -> str:
    """The text of a CallToolResult's first content block."""
    assert result.content, "tool result has no content"
    return str(result.content[0].text)
