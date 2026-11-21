"""Full-surface MCP conformance suite (US-606 — the phase exit criterion).

Spec §13.2 + §17 phase 6: every tool + resource + prompt against the
MCP 2026-07-28 spec, over the full production surface (23 tools, 6
resource templates, 6 prompts), with two independent clients — the
reference SDK client and a raw host-style JSON-RPC client (in-memory
+ real stdio). Live interop against a real AI host stays UNVERIFIED
(see docs/mcp-server.md); these clients prove the server speaks plain
protocol, not just SDK-to-SDK.

Also closes the US-604 follow-ups: live-Tryton conformance reads for
the engine-backed resources (chart-of-accounts, fiscal-periods) and
prompt↔tool integration — every prompt rendered against the REAL
US-602/US-603 tool results, proving figure containment still holds.

Two worlds, like the US-602 tests: the shared ``world`` fixture (fake
engine companies 301/302/303) for the tests that never reach the
engine, and ``live_world`` (the live Adapter Test company in Tryton)
for the success paths. Live tests skip when Tryton is down.

No pytest-asyncio in this repo: sync test functions drive the async
harness with ``asyncio.run()``.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    ApiClientModel,
    EngineCompanyModel,
    EngineConnectionModel,
    TenantModel,
)
from hesabyar.infrastructure.persistence.postgres.seed_standards import seed_standards
from hesabyar.infrastructure.persistence.postgres.seed_tax_policy import seed_tax_policy
from hesabyar.interfaces.mcp import SERVER_NAME
from hesabyar.interfaces.mcp.errors import MCP_ERROR_FORBIDDEN
from hesabyar.interfaces.mcp.prompts import PROMPT_DEFINITIONS
from hesabyar.interfaces.mcp.registry import ToolRegistry
from hesabyar.interfaces.mcp.resources import RESOURCE_DEFINITIONS, RESOURCE_MIME_TYPE
from tests.mcp.conformance import (
    EXPECTED_PROMPT_COUNT,
    EXPECTED_PROMPT_NAMES,
    EXPECTED_RESOURCE_COUNT,
    EXPECTED_RESOURCE_TEMPLATES,
    EXPECTED_TOOL_COUNT,
    EXPECTED_TOOL_NAMES,
    EXPECTED_TOOL_PROPERTIES,
    EXPECTED_TOOL_REQUIRED,
    MCP_NEGOTIATED_VERSION,
    MCP_SPEC_VERSION,
    STDIO_TIMEOUT_SECONDS,
    connect_raw_memory,
    connect_raw_stdio,
    make_full_surface_server,
)
from tests.mcp.conftest import World
from tests.mcp.harness import connected_client, tool_text

INSTALLATION_ID = "00000000-0000-0000-0000-000000000606"

TEST_COMPANY_NAME = "HesabYar Adapter Test"

# November 2026: a 1405 period with posted moves (same as test_rest_reports).
PERIOD_NOV_2026 = ("2026-11-01", "2026-11-30")
PERIOD_OCT_2026 = ("2026-10-01", "2026-10-31")
AS_OF_NOV_2026 = "2026-11-30"

#: Every scope the full surface needs (tools + resources + prompts).
ALL_CONFORMANCE_SCOPES = [
    "mcp:connect",
    "company:read",
    "account:read",
    "reports:read",
    "ledger:read",
    "banking:read",
    "compliance:read",
    "standards:read",
    "standards:validate",
    "workflow:read",
    "review:read",
    "settlement:read",
    "cheques:read",
    "packaging:read",
    "cashbox:read",
    "tax:read",
    "integrations:read",
    # US-605 draft tools (the conformance suite lists them; no
    # conformance test calls them, but the key must carry the scopes
    # the full surface needs).
    "ledger:draft:create",
    "invoice:draft:create",
    "banking:reconcile",
    "compliance:draft:create",
]

#: ASCII + Persian digit runs (same tokenization as test_mcp_prompts).
_DIGIT_RUN = re.compile(r"[0-9\u06f0-\u06f9]+")


def _digit_tokens(text: str) -> set[str]:
    return set(_DIGIT_RUN.findall(text))


def _assert_cites_only_given(rendered: str, given: list[str]) -> None:
    """Every figure in ``rendered`` appears in the ``given`` arguments."""
    allowed: set[str] = set()
    for text in given:
        allowed |= _digit_tokens(text)
    extra = _digit_tokens(rendered) - allowed
    assert not extra, f"rendered figures not in the given data: {sorted(extra)}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _seed_registries(engine: Engine) -> None:
    """Seed the standards + tax-policy registries once (idempotent upserts)."""
    session = sessionmaker(bind=engine)()
    try:
        seed_standards(session)
        seed_tax_policy(session)
        session.commit()
    finally:
        session.close()


@pytest.fixture()
def live_company(settings: AppSettings) -> CompanyRef:
    """Resolve the live Adapter Test company (skip when Tryton is down)."""
    tryton = settings.tryton
    client = TrytonRestClient(tryton.base_url, tryton.database, tryton.api_key)
    try:
        if not client.ping():
            pytest.skip(f"trytond server not reachable at {tryton.base_url}")
        for record in client.search("company.company", fields=["rec_name"]):
            if record["rec_name"] == TEST_COMPANY_NAME:
                return CompanyRef(engine_id=int(record["id"]))
    finally:
        client.close()
    pytest.skip(f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {tryton.database!r}.")


@dataclass
class LiveWorld:
    """Provisioned tenant, live-company mapping and keys for one test."""

    tenant_id: str
    company_row_id: str
    engine_id: int
    key_all_secret: str
    key_all_id: str


@pytest.fixture()
def live_world(
    engine: Engine, settings: AppSettings, live_company: CompanyRef
) -> Generator[LiveWorld, None, None]:
    """Provision a tenant mapped to the live company, plus a full-scope key."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_ids: list[str] = []
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"mcp-606-{uuid.uuid4().hex[:8]}", name="MCP US-606 Tenant"
        )
        session.add(tenant)
        session.flush()
        tenant_ids = [tenant.id]
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="mcp us-606 client")
        session.add(client)
        session.flush()
        connection = EngineConnectionModel(
            id=str(uuid.uuid4()),
            name=f"conn-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url=settings.tryton.base_url,
            # Suffixed per fixture (codebase convention): the unique
            # constraint is on (engine, database_name), and the live
            # adapter connects via settings.tryton, never via this row.
            database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
        )
        session.add(connection)
        session.flush()
        connection_ids.append(connection.id)
        live_row = EngineCompanyModel(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            engine_connection_id=connection.id,
            external_company_id=str(live_company.engine_id),
            company_name=f"Company {live_company.engine_id}",
        )
        session.add(live_row)
        session.flush()
        key_all = service.issue_key(
            session,
            client=client,
            name="mcp us-606 test key",
            scopes=ALL_CONFORMANCE_SCOPES,
            installation_id=INSTALLATION_ID,
        )
        session.commit()
        yield LiveWorld(
            tenant_id=tenant.id,
            company_row_id=live_row.id,
            engine_id=live_company.engine_id,
            key_all_secret=key_all.secret,
            key_all_id=key_all.record.id,
        )
    finally:
        session.close()
        cleanup = sessionmaker(bind=engine)()
        try:
            if tenant_ids:
                cleanup.query(TenantModel).filter(TenantModel.id.in_(tenant_ids)).delete(
                    synchronize_session=False
                )
            if connection_ids:
                cleanup.query(EngineConnectionModel).filter(
                    EngineConnectionModel.id.in_(connection_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_key(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    world: World,
    scopes: list[str],
    *,
    engine_company_id: str | None = None,
) -> tuple[str, str]:
    """Issue a key under the world's client; return (secret, key id)."""
    session = session_factory()
    try:
        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        client = session.get(ApiClientModel, world.client_id)
        assert client is not None
        issued = service.issue_key(
            session,
            client=client,
            name="us606 test key",
            scopes=scopes,
            installation_id=INSTALLATION_ID,
            engine_company_id=engine_company_id,
        )
        session.commit()
        return issued.secret, issued.record.id
    finally:
        session.close()


def _audit_rows(
    session_factory: Callable[[], Session], *, key_id: str, tool: str
) -> list[AiAccessAuditModel]:
    """This key's audit rows for ``tool``, oldest first."""
    session = session_factory()
    try:
        return list(
            session.execute(
                select(AiAccessAuditModel)
                .where(
                    AiAccessAuditModel.key_id == key_id,
                    AiAccessAuditModel.tool == tool,
                )
                .order_by(AiAccessAuditModel.created_at)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _uri(definition: Any, company: str, run_id: str = "00000000-0000-0000-0000-000000000000") -> str:
    """Fill a resource template's parameters."""
    return definition.uri_template.replace("{company}", company).replace("{run_id}", run_id)


# ---------------------------------------------------------------------------
# The spec version + the full surface (SDK client)
# ---------------------------------------------------------------------------


def test_sdk_targets_spec_version() -> None:
    """The pinned MCP SDK's latest protocol version is the target spec."""
    assert LATEST_PROTOCOL_VERSION == MCP_SPEC_VERSION


def test_protocol_version_negotiated(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Initialize negotiates the newest handshake version the pinned SDK offers.

    The spec target is MCP 2026-07-28, but the pinned SDK (mcp 2.3.0)
    only reaches 2026-07-28 through the stateless per-request
    envelope — the ``initialize`` handshake tops out at 2025-11-25
    (``HANDSHAKE_PROTOCOL_VERSIONS`` in ``mcp_types/version.py``).
    """

    async def _run() -> str | None:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            assert session.server_info is not None
            assert session.server_info.name == SERVER_NAME
            return session.protocol_version

    assert asyncio.run(_run()) == MCP_NEGOTIATED_VERSION


def test_full_surface_lists_exactly(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The full surface lists exactly 23 tools, 6 templates, 6 prompts."""

    async def _run() -> tuple[set[str], set[str], set[str]]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            tools = await session.list_tools()
            templates = await session.list_resource_templates()
            prompts = await session.list_prompts()
            return (
                {tool.name for tool in tools.tools},
                {t.uri_template for t in templates.resource_templates},
                {p.name for p in prompts.prompts},
            )

    tool_names, template_uris, prompt_names = asyncio.run(_run())
    assert tool_names == EXPECTED_TOOL_NAMES
    assert len(tool_names) == EXPECTED_TOOL_COUNT
    assert template_uris == EXPECTED_RESOURCE_TEMPLATES
    assert len(template_uris) == EXPECTED_RESOURCE_COUNT
    assert prompt_names == EXPECTED_PROMPT_NAMES
    assert len(prompt_names) == EXPECTED_PROMPT_COUNT


def test_full_surface_registry_counts(
    session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The registry records the same surface the server lists."""
    _, context = make_full_surface_server(session_factory, settings)
    assert context.registry.tool_names() == EXPECTED_TOOL_NAMES
    assert len(context.registry.tool_names()) == EXPECTED_TOOL_COUNT
    assert context.registry.resource_templates() == EXPECTED_RESOURCE_TEMPLATES
    assert len(context.registry.resource_templates()) == EXPECTED_RESOURCE_COUNT
    assert context.registry.prompt_names() == EXPECTED_PROMPT_NAMES
    assert len(context.registry.prompt_names()) == EXPECTED_PROMPT_COUNT


def test_every_tool_has_narrow_schema(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Every tool's input schema is narrow: exact properties + required."""

    async def _run() -> dict[str, Any]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            listed = await session.list_tools()
            return {tool.name: tool for tool in listed.tools}

    by_name = asyncio.run(_run())
    assert set(by_name) == EXPECTED_TOOL_NAMES
    for name, tool in by_name.items():
        schema = tool.input_schema
        assert schema.get("type") == "object", name
        properties = set(schema.get("properties", {}))
        assert "ctx" not in properties, name
        assert properties == EXPECTED_TOOL_PROPERTIES[name], name
        required = set(schema.get("required", []))
        assert required == EXPECTED_TOOL_REQUIRED[name], name
        assert required <= properties, name
        assert tool.description, name


def test_resource_templates_list_matches_definitions(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Listed templates carry the definitions' URIs, names and JSON mime."""

    async def _run() -> dict[str, Any]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            templates = await session.list_resource_templates()
            return {t.uri_template: t for t in templates.resource_templates}

    by_uri = asyncio.run(_run())
    assert set(by_uri) == EXPECTED_RESOURCE_TEMPLATES
    for definition in RESOURCE_DEFINITIONS:
        listed = by_uri[definition.uri_template]
        assert listed.name == definition.name
        assert listed.mime_type == RESOURCE_MIME_TYPE
        assert listed.description == definition.description


def test_prompts_list_matches_definitions(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Listed prompts carry the definitions' names and arguments."""

    async def _run() -> dict[str, dict[str, bool]]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            prompts = await session.list_prompts()
            return {
                p.name: {a.name: bool(a.required) for a in (p.arguments or [])}
                for p in prompts.prompts
            }

    listed = asyncio.run(_run())
    assert set(listed) == EXPECTED_PROMPT_NAMES
    for definition in PROMPT_DEFINITIONS:
        expected = {a.name: a.required for a in definition.arguments}
        assert listed[definition.name] == expected, definition.name


def test_server_capabilities_advertise_surface(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Initialize advertises tools + resources + prompts capabilities."""

    async def _run() -> Any:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connected_client(server) as session:
            result = session.initialize_result
            assert result is not None
            return result.capabilities

    capabilities = asyncio.run(_run())
    assert capabilities.tools is not None
    assert capabilities.resources is not None
    assert capabilities.prompts is not None


# ---------------------------------------------------------------------------
# Registry template matching (US-604 follow-up (a), unit level)
# ---------------------------------------------------------------------------


def test_registry_template_matching() -> None:
    """Concrete URIs match their templates; other shapes do not."""
    registry = ToolRegistry()
    registry.register_resource(
        "hesabyar://companies/{company}/chart-of-accounts", ["account:read"]
    )
    registry.register_resource(
        "hesabyar://companies/{company}/report-snapshots/{run_id}", ["reports:read"]
    )
    matched = registry.match_resource_uri("hesabyar://companies/abc/chart-of-accounts")
    assert matched is not None
    assert matched[0] == frozenset({"account:read"})
    assert matched[1] == {"company": "abc"}
    matched = registry.match_resource_uri(
        "hesabyar://companies/abc/report-snapshots/run-9"
    )
    assert matched is not None
    assert matched[0] == frozenset({"reports:read"})
    assert matched[1] == {"company": "abc", "run_id": "run-9"}
    # A parameter never spans a slash, and unknown shapes do not match.
    assert registry.match_resource_uri("hesabyar://companies/a/b/chart-of-accounts") is None
    assert registry.match_resource_uri("hesabyar://companies/abc/unknown") is None
    assert registry.match_resource_uri("hesabyar://other/abc/chart-of-accounts") is None
    assert registry.scopes_for_resource_uri("hesabyar://companies/abc/chart-of-accounts") == (
        frozenset({"account:read"})
    )
    assert registry.scopes_for_resource_uri("hesabyar://companies/abc/unknown") is None


# ---------------------------------------------------------------------------
# Host-style client: raw JSON-RPC over memory streams
# ---------------------------------------------------------------------------


def test_raw_client_initialize_and_lists(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A raw JSON-RPC client handshakes and lists the full surface."""

    async def _run() -> tuple[dict[str, Any], set[str], set[str], set[str]]:
        secret, _ = _issue_key(
            session_factory, settings, world, list(ALL_CONFORMANCE_SCOPES)
        )
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=secret
        )
        async with connect_raw_memory(server) as raw:
            init = await raw.initialize()
            tools = await raw.request("tools/list")
            templates = await raw.request("resources/templates/list")
            prompts = await raw.request("prompts/list")
            return (
                init,
                {t["name"] for t in tools["tools"]},
                {t["uriTemplate"] for t in templates["resourceTemplates"]},
                {p["name"] for p in prompts["prompts"]},
            )

    init, tool_names, template_uris, prompt_names = asyncio.run(_run())
    assert init["protocolVersion"] == MCP_NEGOTIATED_VERSION
    assert init["serverInfo"]["name"] == SERVER_NAME
    assert init.get("instructions")
    assert tool_names == EXPECTED_TOOL_NAMES
    assert template_uris == EXPECTED_RESOURCE_TEMPLATES
    assert prompt_names == EXPECTED_PROMPT_NAMES


def test_raw_client_calls_tool_reads_resource_gets_prompt(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A raw client calls a tool, reads a resource and gets a prompt.

    All three are DB-backed (no Tryton): the review queue, the
    standards registry and a prompt render over fixture JSON.
    """

    async def _run() -> tuple[dict[str, Any], dict[str, Any], str]:
        secret, _ = _issue_key(
            session_factory, settings, world, list(ALL_CONFORMANCE_SCOPES)
        )
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=secret
        )
        async with connect_raw_memory(server) as raw:
            await raw.initialize()
            await raw.request("ping")
            tool_result = await raw.request(
                "tools/call",
                {
                    "name": "list_pending_review_items",
                    "arguments": {"company": world.company_a_id},
                },
            )
            resource_result = await raw.request(
                "resources/read",
                {"uri": f"hesabyar://companies/{world.company_a_id}/standards"},
            )
            prompt_result = await raw.request(
                "prompts/get",
                {
                    "name": "summarize-overdue-receivables",
                    "arguments": {
                        "company": world.company_a_id,
                        "as_of": "1405-08-30",
                        "receivables_aging": json.dumps(
                            {"as_of": "2026-11-30", "buckets": [], "provenance": {}}
                        ),
                    },
                },
            )
            return (
                tool_result,
                resource_result,
                str(prompt_result["messages"][0]["content"]["text"]),
            )

    tool_result, resource_result, rendered = asyncio.run(_run())
    assert tool_result.get("isError") is not True
    assert tool_result["content"], "tool result has no content"
    payload = json.loads(str(tool_result["content"][0]["text"]))
    assert "result" in payload and "provenance" in payload
    assert resource_result["contents"], "resource read has no content"
    assert resource_result["contents"][0]["mimeType"] == RESOURCE_MIME_TYPE
    resource_payload = json.loads(str(resource_result["contents"][0]["text"]))
    assert resource_payload["provenance"]["report_type"] == "standards"
    assert "خلاصهٔ مطالبات معوق" in rendered


def test_raw_client_unknown_tool_is_error_result(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A raw client calling an unknown tool gets an isError result.

    NOT JSON-RPC -32601: that code is for an unknown METHOD, and
    ``tools/call`` is a known method — the unknown thing is the tool
    name inside it. The SDK raises ToolError for an unknown tool and
    converts it to ``CallToolResult(is_error=True)`` (only MCPError
    propagates as a protocol error).
    """

    async def _run() -> dict[str, Any]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=world.key_full_secret
        )
        async with connect_raw_memory(server) as raw:
            await raw.initialize()
            return await raw.request(
                "tools/call",
                {"name": "no_such_tool", "arguments": {}},
            )

    result = asyncio.run(_run())
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Unknown tool: no_such_tool"


def test_raw_client_invalid_arguments(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A raw client calling a tool without its required args gets an isError result.

    NOT JSON-RPC -32602: pydantic signature validation fails before
    the handler runs, the SDK wraps the ValidationError in a
    ToolError, and that becomes ``CallToolResult(is_error=True)``
    whose text names the rejected fields.
    """

    async def _run() -> dict[str, Any]:
        secret, _ = _issue_key(
            session_factory, settings, world, list(ALL_CONFORMANCE_SCOPES)
        )
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=secret
        )
        async with connect_raw_memory(server) as raw:
            await raw.initialize()
            return await raw.request(
                "tools/call",
                {"name": "get_trial_balance", "arguments": {}},
            )

    result = asyncio.run(_run())
    assert result["isError"] is True
    text = str(result["content"][0]["text"])
    assert text.startswith("Error executing tool get_trial_balance:")
    assert "company" in text
    assert "date_from" in text
    assert "date_to" in text


# ---------------------------------------------------------------------------
# Host-style client: raw JSON-RPC over the real stdio subprocess
# ---------------------------------------------------------------------------


def test_raw_stdio_client_lists_full_surface(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The real stdio server speaks raw JSON-RPC to a non-SDK client."""

    async def _run() -> tuple[dict[str, Any], set[str], set[str], set[str]]:
        secret, _ = _issue_key(
            session_factory, settings, world, list(ALL_CONFORMANCE_SCOPES)
        )
        async with connect_raw_stdio(secret) as raw:
            init = await raw.initialize()
            tools = await raw.request("tools/list")
            templates = await raw.request("resources/templates/list")
            prompts = await raw.request("prompts/list")
            return (
                init,
                {t["name"] for t in tools["tools"]},
                {t["uriTemplate"] for t in templates["resourceTemplates"]},
                {p["name"] for p in prompts["prompts"]},
            )

    init, tool_names, template_uris, prompt_names = asyncio.run(
        asyncio.wait_for(_run(), timeout=STDIO_TIMEOUT_SECONDS)
    )
    assert init["protocolVersion"] == MCP_NEGOTIATED_VERSION
    assert init["serverInfo"]["name"] == SERVER_NAME
    assert tool_names == EXPECTED_TOOL_NAMES
    assert template_uris == EXPECTED_RESOURCE_TEMPLATES
    assert prompt_names == EXPECTED_PROMPT_NAMES


def test_raw_stdio_client_calls_tool(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A raw stdio client calls a DB-backed tool end to end."""

    async def _run() -> dict[str, Any]:
        secret, _ = _issue_key(
            session_factory, settings, world, list(ALL_CONFORMANCE_SCOPES)
        )
        async with connect_raw_stdio(secret) as raw:
            await raw.initialize()
            return await raw.request(
                "tools/call",
                {
                    "name": "list_pending_review_items",
                    "arguments": {"company": world.company_a_id},
                },
            )

    result = asyncio.run(asyncio.wait_for(_run(), timeout=STDIO_TIMEOUT_SECONDS))
    assert result.get("isError") is not True
    assert result["content"], "tool result has no content"
    payload = json.loads(str(result["content"][0]["text"]))
    assert "result" in payload and "provenance" in payload


# ---------------------------------------------------------------------------
# Enforcement + audit on the full surface (US-604 follow-ups (a) + (b))
# ---------------------------------------------------------------------------


def test_full_surface_scope_denial(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A key missing scopes is denied on a tool, a resource and a prompt."""

    async def _run() -> tuple[int, int, int]:
        # company:read only (plus the base scope): no reports, no
        # account, no review — all three calls must fail closed.
        secret, _ = _issue_key(
            session_factory, settings, world, ["mcp:connect", "company:read"]
        )
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=secret
        )
        codes: list[int] = []
        async with connected_client(server) as session:
            for coro in (
                session.call_tool(
                    "get_trial_balance",
                    {
                        "company": world.company_a_id,
                        "date_from": PERIOD_NOV_2026[0],
                        "date_to": PERIOD_NOV_2026[1],
                    },
                ),
                session.read_resource(
                    f"hesabyar://companies/{world.company_a_id}/chart-of-accounts"
                ),
                session.get_prompt(
                    "monthly-close-review",
                    {
                        "company": world.company_a_id,
                        "period": "1405-08",
                        "trial_balance": "{}",
                        "income_statement": "{}",
                    },
                ),
            ):
                with pytest.raises(MCPError) as excinfo:
                    await coro
                codes.append(excinfo.value.code)
        return codes[0], codes[1], codes[2]

    tool_code, resource_code, prompt_code = asyncio.run(_run())
    assert tool_code == MCP_ERROR_FORBIDDEN
    assert resource_code == MCP_ERROR_FORBIDDEN
    assert prompt_code == MCP_ERROR_FORBIDDEN


def test_resource_scope_denied_by_middleware_for_run_template(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Central enforcement covers the two-parameter template too.

    The report-snapshot template (``{company}`` + ``{run_id}``) is
    matched by the middleware, so a scope-missing read audits as
    ``missing_scope`` — and the denial happens before the handler
    (and its snapshot lookup) runs.
    """
    scopes = [scope for scope in ALL_CONFORMANCE_SCOPES if scope != "reports:read"]
    secret, key_id = _issue_key(session_factory, settings, world, scopes)
    uri = _uri(RESOURCE_DEFINITIONS[4], world.company_a_id)

    async def _run() -> int:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=secret
        )
        async with connected_client(server) as session:
            with pytest.raises(MCPError) as excinfo:
                await session.read_resource(uri)
            return excinfo.value.code

    assert asyncio.run(_run()) == MCP_ERROR_FORBIDDEN
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].kind == "resource"
    assert rows[0].error_code == "missing_scope"
    assert rows[0].company_id == world.company_a_id


def test_full_surface_audit_rows_carry_company(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Allowed tool, resource and prompt calls all audit their company.

    US-604 follow-up (b): resource and prompt rows used to carry
    company_id NULL; the middleware now extracts the company from
    the URI / arguments for the audit record.
    """
    secret, key_id = _issue_key(
        session_factory, settings, world, list(ALL_CONFORMANCE_SCOPES)
    )
    uri = f"hesabyar://companies/{world.company_a_id}/standards"

    async def _run() -> None:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=secret
        )
        async with connected_client(server) as session:
            await session.call_tool(
                "list_pending_review_items", {"company": world.company_a_id}
            )
            await session.read_resource(uri)
            await session.get_prompt(
                "summarize-overdue-receivables",
                {
                    "company": world.company_a_id,
                    "as_of": "1405-08-30",
                    "receivables_aging": json.dumps({"as_of": "2026-11-30", "buckets": []}),
                },
            )

    asyncio.run(_run())
    tool_rows = _audit_rows(
        session_factory, key_id=key_id, tool="list_pending_review_items"
    )
    assert len(tool_rows) == 1
    assert tool_rows[0].outcome == "allowed"
    assert tool_rows[0].company_id == world.company_a_id
    resource_rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(resource_rows) == 1
    assert resource_rows[0].outcome == "allowed"
    assert resource_rows[0].kind == "resource"
    assert resource_rows[0].company_id == world.company_a_id
    prompt_rows = _audit_rows(
        session_factory, key_id=key_id, tool="summarize-overdue-receivables"
    )
    assert len(prompt_rows) == 1
    assert prompt_rows[0].outcome == "allowed"
    assert prompt_rows[0].kind == "prompt"
    assert prompt_rows[0].company_id == world.company_a_id


# ---------------------------------------------------------------------------
# Live-Tryton conformance reads (US-604 follow-up (c))
# ---------------------------------------------------------------------------


def test_live_chart_of_accounts_resource(
    live_world: LiveWorld, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The chart-of-accounts resource reads live Tryton data end to end."""

    async def _run() -> dict[str, Any]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=live_world.key_all_secret
        )
        uri = f"hesabyar://companies/{live_world.company_row_id}/chart-of-accounts"
        async with connected_client(server) as session:
            result: Any = await session.read_resource(uri)
            assert result.contents, "resource read has no content"
            return json.loads(str(result.contents[0].text))

    payload = asyncio.run(_run())
    assert payload["company"]["engine_id"] == live_world.engine_id
    assert payload["active_only"] is True
    assert payload["account_count"] == len(payload["accounts"])
    assert payload["account_count"] > 0, "the Adapter Test chart is empty"
    # Every account has a name; every account except the chart root
    # (code "", no parent) has a code. Only postable (leaf) accounts
    # carry a type: group accounts — the "1" Assets group, the 7.x
    # memorandum groups — have NO type in Tryton by design (REST
    # asserts the same: root["type"] is None), so a typeless account
    # is correct engine data, not a mapping gap. The chart root itself
    # is the one exception: it carries the chart template's ROOT type
    # ("Universal Chart of Account Types" — the type tree's root node,
    # mirrored in REST's own fixture in test_reports_us550.py).
    codeless = [account for account in payload["accounts"] if not account["code"]]
    assert len(codeless) <= 1, "at most one account (the chart root) has no code"
    if codeless:
        assert codeless[0]["parent_code"] is None
        assert codeless[0]["type"] == "Universal Chart of Account Types"
        assert codeless[0]["postable"] is False
    for account in payload["accounts"]:
        assert account["name"], f"account {account['code']!r} has no name"
        if account["postable"]:
            assert account["type"], f"postable account {account['code']} has no type"
    assert any(
        account["type"] is None and not account["postable"]
        for account in payload["accounts"]
    ), "the live chart has no typeless group account"
    cash = next(
        account for account in payload["accounts"] if account["code"] == "1.1.1"
    )
    assert cash["name"] == "Cash and Cash Equivalents"
    assert cash["type"]
    assert cash["postable"] is True
    provenance = payload["provenance"]
    assert provenance["company_id"] == live_world.engine_id
    assert provenance["report_type"] == "chart-of-accounts"
    assert provenance["source_engine"] == "Tryton"


def test_live_fiscal_periods_resource(
    live_world: LiveWorld, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The fiscal-periods resource reads live Tryton data end to end."""

    async def _run() -> dict[str, Any]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=live_world.key_all_secret
        )
        uri = f"hesabyar://companies/{live_world.company_row_id}/fiscal-periods"
        async with connected_client(server) as session:
            result: Any = await session.read_resource(uri)
            assert result.contents, "resource read has no content"
            return json.loads(str(result.contents[0].text))

    payload = asyncio.run(_run())
    assert payload["company"]["engine_id"] == live_world.engine_id
    assert payload["company"]["name"] == TEST_COMPANY_NAME
    assert payload["fiscal_years"], "the Adapter Test company has no fiscal years"
    for year in payload["fiscal_years"]:
        assert year["start_date"]["gregorian"] and year["start_date"]["jalali"]
    assert payload["periods"], "the Adapter Test company has no fiscal periods"
    provenance = payload["provenance"]
    assert provenance["company_id"] == live_world.engine_id
    assert provenance["report_type"] == "fiscal-periods"


# ---------------------------------------------------------------------------
# Prompt <-> tool integration (US-604 follow-up (d))
# ---------------------------------------------------------------------------
#
# The US-604 prompt tests render against fixture tool results. Here
# every prompt is rendered against the REAL tool results, called live
# through the same session — the host flow — and figure containment
# must still hold: every figure in the render appears in the tool
# results (or the period labels / company name the host supplied).
#
# The company argument is the ENGINE id (not the UUID row id) so the
# allowed digit set stays tight — a UUID would widen it and weaken
# the proof (see test_mcp_prompts.test_prompt_renders_end_to_end).


async def _call_tool_json(session: Any, name: str, arguments: dict[str, Any]) -> str:
    """Call a tool; return its result text (must not be an error)."""
    result = await session.call_tool(name, arguments)
    assert not result.is_error, f"{name} returned is_error: {result.content}"
    return tool_text(result)


def _prompt_cases(live_world: LiveWorld) -> dict[str, Any]:
    """Per-prompt live tool calls + the arguments they feed.

    Each entry is (tool calls, argument builder): the tool calls run
    first (name -> arguments), then the builder turns their JSON
    texts into the prompt's arguments.
    """
    company = str(live_world.engine_id)
    nov = {"date_from": PERIOD_NOV_2026[0], "date_to": PERIOD_NOV_2026[1]}
    return {
        "monthly-close-review": (
            [
                ("get_trial_balance", {"company": company, **nov}),
                ("get_income_statement", {"company": company, **nov}),
                ("list_pending_review_items", {"company": company}),
            ],
            lambda r: {
                "company": company,
                "period": "1405-08",
                "trial_balance": r["get_trial_balance"],
                "income_statement": r["get_income_statement"],
                "pending_review_items": r["list_pending_review_items"],
            },
        ),
        "explain-unusual-balance-movement": (
            [
                ("get_general_ledger", {"company": company, **nov}),
                ("get_trial_balance", {"company": company, **nov}),
            ],
            lambda r: {
                "company": company,
                "account": "1000",
                "period": "1405-08",
                "general_ledger": r["get_general_ledger"],
                "trial_balance": r["get_trial_balance"],
            },
        ),
        "summarize-overdue-receivables": (
            [
                ("get_receivables_aging", {"company": company, "as_of": AS_OF_NOV_2026}),
            ],
            lambda r: {
                "company": company,
                "as_of": "1405-08-30",
                "receivables_aging": r["get_receivables_aging"],
            },
        ),
        "review-unreconciled-bank-activity": (
            [
                ("get_bank_reconciliation_status", {"company": company, **nov}),
            ],
            lambda r: {
                "company": company,
                "period": "1405-08",
                "bank_reconciliation_status": r["get_bank_reconciliation_status"],
            },
        ),
        "validate-financial-statements": (
            [
                (
                    "validate_financial_statement",
                    {
                        "company": company,
                        "period_start": PERIOD_NOV_2026[0],
                        "period_end": PERIOD_NOV_2026[1],
                        "standard_codes": ["1"],
                    },
                ),
                ("get_balance_sheet", {"company": company, "as_of": AS_OF_NOV_2026}),
                ("get_income_statement", {"company": company, **nov}),
            ],
            lambda r: {
                "company": company,
                "period": "1405-08",
                "validation": r["validate_financial_statement"],
                "balance_sheet": r["get_balance_sheet"],
                "income_statement": r["get_income_statement"],
            },
        ),
        # compare-month-over-month calls get_income_statement twice
        # (two periods, one tool name) — it has its own test below.
    }


@pytest.mark.parametrize(
    "prompt_name",
    [
        "monthly-close-review",
        "explain-unusual-balance-movement",
        "summarize-overdue-receivables",
        "review-unreconciled-bank-activity",
        "validate-financial-statements",
    ],
)
def test_prompt_renders_real_tool_results(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    prompt_name: str,
) -> None:
    """The prompt renders the REAL tool results; containment still holds."""
    calls, build_arguments = _prompt_cases(live_world)[prompt_name]
    assert build_arguments is not None

    async def _run() -> tuple[str, list[str]]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=live_world.key_all_secret
        )
        async with connected_client(server) as session:
            results: dict[str, str] = {}
            for tool_name, arguments in calls:
                results[tool_name] = await _call_tool_json(session, tool_name, arguments)
            arguments_map = build_arguments(results)
            result: Any = await session.get_prompt(prompt_name, arguments_map)
            assert result.messages, "prompt has no messages"
            rendered = str(result.messages[0].content.text)
            # Given: the tool results + the host-supplied labels
            # (company, period, account) + the resolved company name.
            given = [
                *results.values(),
                *(v for v in arguments_map.values() if isinstance(v, str)),
                f"Company {live_world.engine_id}",
            ]
            return rendered, given

    rendered, given = asyncio.run(_run())
    _assert_cites_only_given(rendered, given)


def test_prompt_compare_month_over_month_renders_real_results(
    live_world: LiveWorld, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """compare-month-over-month renders two REAL income statements."""
    company = str(live_world.engine_id)

    async def _run() -> tuple[str, list[str]]:
        server, _ = make_full_surface_server(
            session_factory, settings, bootstrap_secret=live_world.key_all_secret
        )
        async with connected_client(server) as session:
            current = await _call_tool_json(
                session,
                "get_income_statement",
                {
                    "company": company,
                    "date_from": PERIOD_NOV_2026[0],
                    "date_to": PERIOD_NOV_2026[1],
                },
            )
            prior = await _call_tool_json(
                session,
                "get_income_statement",
                {
                    "company": company,
                    "date_from": PERIOD_OCT_2026[0],
                    "date_to": PERIOD_OCT_2026[1],
                },
            )
            result: Any = await session.get_prompt(
                "compare-month-over-month",
                {
                    "company": company,
                    "current_period": "1405-08",
                    "prior_period": "1405-07",
                    "current_income_statement": current,
                    "prior_income_statement": prior,
                },
            )
            assert result.messages, "prompt has no messages"
            rendered = str(result.messages[0].content.text)
            return rendered, [current, prior, "1405-08", "1405-07", f"Company {live_world.engine_id}"]

    rendered, given = asyncio.run(_run())
    _assert_cites_only_given(rendered, given)
