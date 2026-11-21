"""Tests for the 14 read-only MCP tools of spec §13.4 (US-602).

Conformance per tool (arguments, scopes, error shapes), scope denial
per tool, the cross-company fail-closed property, provenance
completeness, and MCP-vs-REST equality per report tool (spec §13.3 +
§19: the same report via MCP and via REST produces the same figures
for the same company, date range and source snapshot).

Two worlds:

- the shared ``world`` fixture (fake engine companies 301/302/303)
  for the tests that never reach the engine: scope denial (the
  middleware refuses before the handler runs), cross-company
  fail-closed, unknown-company fail-closed, and argument validation;
- ``live_world`` (the live Adapter Test company in Tryton) for the
  success paths, provenance and equality tests. It skips when Tryton
  is down, following ``tests/interfaces/test_rest_reports.py``.

Audit assertions filter by the world's tenant id: parallel CI jobs
share the test database, and production tool names (unlike the
harness's unique echo names) cannot distinguish jobs.

No pytest-asyncio in this repo: sync test functions drive the async
harness with ``asyncio.run()``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
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
from hesabyar.interfaces.mcp import McpContext, create_mcp_server
from hesabyar.interfaces.mcp.errors import MCP_ERROR_FORBIDDEN, MCP_ERROR_INTERNAL
from hesabyar.interfaces.mcp.provenance import PROVENANCE_FIELDS
from hesabyar.interfaces.mcp.tools_core import (
    CORE_TOOL_NAMES,
    CORE_TOOL_SCOPES,
    MCP_ERROR_INVALID_PARAMS,
)
from hesabyar.interfaces.rest.app import create_app
from tests.mcp.conftest import World
from tests.mcp.harness import authenticate_secret, connected_client, make_server, tool_text

INSTALLATION_ID = "00000000-0000-0000-0000-000000000602"

TEST_COMPANY_NAME = "HesabYar Adapter Test"

# November 2026: a 1405 period with posted moves (same as test_rest_reports).
PERIOD_NOV_2026 = ("2026-11-01", "2026-11-30")
AS_OF_NOV_2026 = "2026-11-30"

#: Every scope the 14 tools need (plus the base scope).
ALL_TOOL_SCOPES = [
    "mcp:connect",
    "company:read",
    "reports:read",
    "ledger:read",
    "banking:read",
    "compliance:read",
    "standards:read",
    "standards:validate",
    "workflow:read",
]

#: Extra REST-only scopes the equality tests need on the comparison key
#: (the REST endpoints the MCP results are compared against).
REST_COMPARISON_SCOPES = ["tax:read", "moadian:read", "review:read", "review:file"]

#: Valid-shaped arguments per tool (company filled in by the caller).
_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "list_companies": {},
    "get_accounting_summary": {"date_from": PERIOD_NOV_2026[0], "date_to": PERIOD_NOV_2026[1]},
    "get_trial_balance": {"date_from": PERIOD_NOV_2026[0], "date_to": PERIOD_NOV_2026[1]},
    "get_general_ledger": {"date_from": PERIOD_NOV_2026[0], "date_to": PERIOD_NOV_2026[1]},
    "get_balance_sheet": {"as_of": AS_OF_NOV_2026},
    "get_income_statement": {"date_from": PERIOD_NOV_2026[0], "date_to": PERIOD_NOV_2026[1]},
    "get_cash_flow": {"date_from": PERIOD_NOV_2026[0], "date_to": PERIOD_NOV_2026[1]},
    "get_receivables_aging": {"as_of": AS_OF_NOV_2026},
    "get_payables_aging": {"as_of": AS_OF_NOV_2026},
    "get_bank_reconciliation_status": {
        "date_from": PERIOD_NOV_2026[0],
        "date_to": PERIOD_NOV_2026[1],
    },
    "get_tax_compliance_status": {"as_of": AS_OF_NOV_2026},
    "validate_financial_statement": {
        "period_start": PERIOD_NOV_2026[0],
        "period_end": PERIOD_NOV_2026[1],
        "standard_codes": ["1"],
    },
    "explain_accounting_rule": {"standard_code": "1"},
    "list_pending_review_items": {},
}

#: Tools that take a ``company`` argument (all but list_companies and
#: explain_accounting_rule).
_COMPANY_TOOLS = [name for name in CORE_TOOL_NAMES if name not in {"list_companies", "explain_accounting_rule"}]

#: Tools whose results carry §13.7 provenance (all company-scoped tools).
_PROVENANCE_TOOLS = _COMPANY_TOOLS

#: Expected input-schema properties per tool (narrow arguments only).
_TOOL_SCHEMA_PROPERTIES: dict[str, set[str]] = {
    "list_companies": set(),
    "get_accounting_summary": {"company", "date_from", "date_to"},
    "get_trial_balance": {"company", "date_from", "date_to"},
    "get_general_ledger": {"company", "date_from", "date_to", "account_id"},
    "get_balance_sheet": {"company", "as_of", "comparison_as_of"},
    "get_income_statement": {"company", "date_from", "date_to", "comparison_from", "comparison_to"},
    "get_cash_flow": {"company", "date_from", "date_to"},
    "get_receivables_aging": {"company", "as_of", "party_id"},
    "get_payables_aging": {"company", "as_of", "party_id"},
    "get_bank_reconciliation_status": {"company", "date_from", "date_to"},
    "get_tax_compliance_status": {"company", "as_of"},
    "validate_financial_statement": {"company", "period_start", "period_end", "standard_codes"},
    "explain_accounting_rule": {"standard_code", "rule_id"},
    "list_pending_review_items": {"company", "item_type", "severity", "limit"},
}


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


@pytest.fixture(scope="module")
def live_company(settings: AppSettings) -> CompanyRef:
    """Resolve the live Adapter Test company (skip when Tryton is down).

    Uses the app settings' Tryton connection — the same Tryton the MCP
    server and the REST app under test talk to.
    """
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
    other_tenant_id: str
    company_row_id: str
    engine_id: int
    other_company_row_id: str  # same tenant, different (fake) engine company
    foreign_company_row_id: str  # belongs to the OTHER tenant
    key_all_secret: str  # mcp:connect + every tool scope + REST comparison scopes
    key_mcp_only_secret: str  # mcp:connect only (no tool scope at all)
    key_reports_only_secret: str  # mcp:connect + reports:read
    key_standards_only_secret: str  # mcp:connect + standards:read
    key_restricted_secret: str  # all scopes, restricted to the live company


@pytest.fixture()
def live_world(
    engine: Engine, settings: AppSettings, live_company: CompanyRef
) -> Generator[LiveWorld, None, None]:
    """Provision a tenant mapped to the live company, plus keys; clean up after."""
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    tenant_ids: list[str] = []
    connection_ids: list[str] = []
    try:
        tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"mcp-602-{uuid.uuid4().hex[:8]}", name="MCP US-602 Tenant"
        )
        other_tenant = TenantModel(
            id=str(uuid.uuid4()), slug=f"mcp-602-{uuid.uuid4().hex[:8]}", name="MCP US-602 Other Tenant"
        )
        session.add_all([tenant, other_tenant])
        session.flush()
        tenant_ids = [tenant.id, other_tenant.id]
        client = ApiClientModel(id=str(uuid.uuid4()), tenant_id=tenant.id, name="mcp us-602 client")
        session.add(client)
        session.flush()

        def _company(tenant_id: str, external_id: str) -> EngineCompanyModel:
            # One company per (tenant, connection) — unique constraint.
            connection = EngineConnectionModel(
                id=str(uuid.uuid4()),
                name=f"conn-{uuid.uuid4().hex[:8]}",
                engine="tryton",
                base_url=settings.tryton.base_url,
                database_name=settings.tryton.database,
            )
            session.add(connection)
            session.flush()
            connection_ids.append(connection.id)
            row = EngineCompanyModel(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                engine_connection_id=connection.id,
                external_company_id=external_id,
                company_name=f"Company {external_id}",
            )
            session.add(row)
            session.flush()
            return row

        live_row = _company(tenant.id, str(live_company.engine_id))
        other_row = _company(tenant.id, "999901")
        foreign_row = _company(other_tenant.id, "999902")

        def _key(scopes: list[str], **kwargs: object):
            return service.issue_key(
                session,
                client=client,
                name="mcp us-602 test key",
                scopes=scopes,
                installation_id=INSTALLATION_ID,
                **kwargs,  # type: ignore[arg-type]
            )

        key_all = _key(ALL_TOOL_SCOPES + REST_COMPARISON_SCOPES)
        key_mcp_only = _key(["mcp:connect"])
        key_reports_only = _key(["mcp:connect", "reports:read"])
        key_standards_only = _key(["mcp:connect", "standards:read"])
        key_restricted = _key(ALL_TOOL_SCOPES, engine_company_id=live_row.id)
        session.commit()
        yield LiveWorld(
            tenant_id=tenant.id,
            other_tenant_id=other_tenant.id,
            company_row_id=live_row.id,
            engine_id=live_company.engine_id,
            other_company_row_id=other_row.id,
            foreign_company_row_id=foreign_row.id,
            key_all_secret=key_all.secret,
            key_mcp_only_secret=key_mcp_only.secret,
            key_reports_only_secret=key_reports_only.secret,
            key_standards_only_secret=key_standards_only.secret,
            key_restricted_secret=key_restricted.secret,
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


@pytest.fixture()
def extra_keys(
    world: World, engine: Engine, settings: AppSettings
) -> Generator[dict[str, str], None, None]:
    """Extra keys on the shared world's tenant (no Tryton needed).

    The shared ``world`` keys carry only company:read + reports:read;
    the scope tests need narrower keys. The world's teardown deletes
    the tenant, cascading to these keys.
    """
    session = sessionmaker(bind=engine)()
    service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
    client = session.get(ApiClientModel, world.client_id)
    assert client is not None

    def _key(scopes: list[str]) -> str:
        issued = service.issue_key(
            session,
            client=client,
            name="mcp us-602 extra key",
            scopes=scopes,
            installation_id=INSTALLATION_ID,
        )
        return issued.secret

    keys = {
        "mcp_only": _key(["mcp:connect"]),
        "standards_only": _key(["mcp:connect", "standards:read"]),
        "workflow_only": _key(["mcp:connect", "workflow:read"]),
    }
    session.commit()
    yield keys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core_server(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str,
) -> MCPServer[Any]:
    """A real MCP server with the 14 core tools (no harness echo tool)."""
    context = McpContext(settings=settings, session_factory=session_factory, transport="memory")
    context.bootstrap_client = authenticate_secret(session_factory, settings, bootstrap_secret)
    return create_mcp_server(context)


def _rest_client(settings: AppSettings) -> TestClient:
    """Real REST app with real wiring (no overrides)."""
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _args_for(tool: str, company: str | None) -> dict[str, Any]:
    """Valid-shaped arguments for ``tool`` (company injected when it takes one)."""
    args = dict(_TOOL_ARGS[tool])
    if tool in _COMPANY_TOOLS:
        assert company is not None
        args["company"] = company
    return args


async def _call_json(session: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a tool and parse its JSON envelope (asserting success)."""
    result = await session.call_tool(tool, args)
    assert not result.is_error, f"{tool} failed: {tool_text(result)}"
    return json.loads(tool_text(result))


async def _call_error_code(session: Any, tool: str, args: dict[str, Any]) -> int:
    """Call a tool expecting an MCPError; return its code."""
    with pytest.raises(MCPError) as excinfo:
        await session.call_tool(tool, args)
    return excinfo.value.code


async def _call_error_message(session: Any, tool: str, args: dict[str, Any]) -> str:
    """Call a tool expecting an MCPError; return its message."""
    with pytest.raises(MCPError) as excinfo:
        await session.call_tool(tool, args)
    return str(excinfo.value)


def _audit_rows_for_tenant(
    session_factory: Callable[[], Session], tool: str, tenant_id: str
) -> list[AiAccessAuditModel]:
    """This tenant's audit rows for ``tool``, oldest first.

    Filtered by tenant: production tool names are shared across
    parallel CI jobs, so the tool name alone cannot isolate a test.
    """
    session = session_factory()
    try:
        return list(
            session.execute(
                select(AiAccessAuditModel)
                .where(
                    AiAccessAuditModel.tool == tool,
                    AiAccessAuditModel.tenant_id == tenant_id,
                )
                .order_by(AiAccessAuditModel.created_at)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _strip_timestamps(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the per-call timestamps from a report payload (equality helper).

    MCP and REST read the same live data at different instants, so
    ``generated_at`` / ``source_timestamp`` legitimately differ; every
    figure must still match.
    """
    cleaned = json.loads(json.dumps(payload, default=str))
    freshness = cleaned.get("freshness")
    if isinstance(freshness, dict):
        freshness.pop("generated_at", None)
        freshness.pop("source_timestamp", None)
    cleaned.pop("generated_at", None)
    cleaned.pop("source_timestamp", None)
    return cleaned


def _assert_no_floats(value: Any, *, path: str = "$") -> None:
    """No binary floating-point anywhere except known ratio/duration fields.

    ``match_rate`` (a ratio) and ``age_hours`` (a duration) are not
    amounts; REST serializes them as floats and MCP matches REST.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"match_rate", "age_hours"}:
                continue
            _assert_no_floats(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_floats(item, path=f"{path}[{index}]")
    else:
        assert not isinstance(value, float), f"binary float at {path}: {value!r}"


# ---------------------------------------------------------------------------
# Conformance: the 14 tools are listed, with narrow input schemas
# ---------------------------------------------------------------------------


def test_list_tools_includes_all_14(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The server lists the 14 US-602 tools (plus the harness echo tool)."""

    async def _run() -> None:
        server, _ = make_server(
            world,
            session_factory,
            settings,
            bootstrap_secret=world.key_full_secret,
            with_tool=True,
        )
        async with connected_client(server) as session:
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert CORE_TOOL_NAMES <= names
            assert world.tool_name in names

    asyncio.run(_run())


def test_tool_input_schemas_are_narrow(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """Each tool's input schema carries only its narrow arguments (no ctx)."""

    async def _run() -> None:
        server = _core_server(session_factory, settings, bootstrap_secret=world.key_full_secret)
        async with connected_client(server) as session:
            tools = await session.list_tools()
            by_name = {tool.name: tool for tool in tools.tools}
            assert set(by_name) == CORE_TOOL_NAMES
            for name, expected in _TOOL_SCHEMA_PROPERTIES.items():
                schema = by_name[name].input_schema
                assert set(schema["properties"]) == expected, name
                assert "ctx" not in schema["properties"]

    asyncio.run(_run())


def test_tool_scope_records_match_spec(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The registry records exactly the §13.4 scope per tool."""
    _server, context = make_server(
        world,
        session_factory,
        settings,
        bootstrap_secret=world.key_full_secret,
        with_tool=False,
    )
    assert context.registry.tool_names() == CORE_TOOL_NAMES
    for name, scopes in CORE_TOOL_SCOPES.items():
        assert context.registry.scopes_for_tool(name) == frozenset(scopes), name


# ---------------------------------------------------------------------------
# Scope denial per tool (no engine call: the middleware refuses first)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(CORE_TOOL_NAMES))
def test_tool_denied_without_its_scope(
    tool: str,
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    extra_keys: dict[str, str],
) -> None:
    """A key with only mcp:connect is denied every tool (missing scope)."""

    async def _run() -> int:
        server = _core_server(session_factory, settings, bootstrap_secret=extra_keys["mcp_only"])
        async with connected_client(server) as session:
            return await _call_error_code(session, tool, _args_for(tool, world.company_a_id))

    assert asyncio.run(_run()) == MCP_ERROR_FORBIDDEN
    rows = _audit_rows_for_tenant(session_factory, tool, world.tenant_id)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "missing_scope"


def test_reports_key_cannot_call_ledger_tool(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """reports:read does not imply ledger:read (exact scopes, no wildcard)."""

    async def _run() -> int:
        # key_full: mcp:connect + company:read + reports:read (no ledger:read).
        server = _core_server(session_factory, settings, bootstrap_secret=world.key_full_secret)
        async with connected_client(server) as session:
            return await _call_error_code(
                session,
                "get_general_ledger",
                _args_for("get_general_ledger", world.company_a_id),
            )

    assert asyncio.run(_run()) == MCP_ERROR_FORBIDDEN


def test_standards_key_passes_scope_gate(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    extra_keys: dict[str, str],
) -> None:
    """A standards:read key reaches the handler (unknown standard → not forbidden)."""

    async def _run() -> tuple[int, str]:
        server = _core_server(
            session_factory, settings, bootstrap_secret=extra_keys["standards_only"]
        )
        async with connected_client(server) as session:
            try:
                await session.call_tool("explain_accounting_rule", {"standard_code": "99999"})
            except MCPError as exc:
                return exc.code, str(exc)
            raise AssertionError("expected STANDARD_NOT_FOUND")

    code, message = asyncio.run(_run())
    assert code == MCP_ERROR_INTERNAL  # not forbidden: the scope gate passed
    assert "STANDARD_NOT_FOUND" in message


# ---------------------------------------------------------------------------
# Company fail-closed (middleware resolves the company against the grant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["get_trial_balance", "get_bank_reconciliation_status", "list_pending_review_items"]
)
def test_restricted_key_cannot_call_other_company(
    tool: str,
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """A key restricted to company A is refused company B (same tenant)."""

    async def _run() -> int:
        server = _core_server(
            session_factory, settings, bootstrap_secret=world.key_restricted_a_secret
        )
        async with connected_client(server) as session:
            return await _call_error_code(session, tool, _args_for(tool, world.company_b_id))

    assert asyncio.run(_run()) == MCP_ERROR_FORBIDDEN
    rows = _audit_rows_for_tenant(session_factory, tool, world.tenant_id)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_forbidden"


@pytest.mark.parametrize("tool", ["get_trial_balance", "get_cash_flow"])
def test_other_tenant_company_looks_nonexistent(
    tool: str,
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """A company of another tenant fails closed exactly like an unknown one."""

    async def _run() -> int:
        server = _core_server(session_factory, settings, bootstrap_secret=world.key_full_secret)
        async with connected_client(server) as session:
            return await _call_error_code(session, tool, _args_for(tool, world.company_c_id))

    assert asyncio.run(_run()) == MCP_ERROR_FORBIDDEN
    rows = _audit_rows_for_tenant(session_factory, tool, world.tenant_id)
    assert rows[0].error_code == "company_not_found"


def test_unknown_company_fails_closed(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """An unmapped company id is refused (never trusted as authorization)."""

    async def _run() -> int:
        server = _core_server(session_factory, settings, bootstrap_secret=world.key_full_secret)
        async with connected_client(server) as session:
            return await _call_error_code(
                session, "get_trial_balance", _args_for("get_trial_balance", "999999")
            )

    assert asyncio.run(_run()) == MCP_ERROR_FORBIDDEN


# ---------------------------------------------------------------------------
# Argument validation (error shapes)
# ---------------------------------------------------------------------------


def test_reversed_date_range_is_invalid_params(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """date_to before date_from is a -32602 (the service raises before any engine read)."""

    async def _run() -> int:
        server = _core_server(session_factory, settings, bootstrap_secret=world.key_full_secret)
        async with connected_client(server) as session:
            return await _call_error_code(
                session,
                "get_trial_balance",
                {
                    "company": world.company_a_id,
                    "date_from": PERIOD_NOV_2026[1],
                    "date_to": PERIOD_NOV_2026[0],
                },
            )

    assert asyncio.run(_run()) == MCP_ERROR_INVALID_PARAMS


def test_bad_review_filter_is_invalid_params(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    extra_keys: dict[str, str],
) -> None:
    """An unknown severity value is a -32602 (key carries workflow:read)."""

    async def _run() -> int:
        server = _core_server(
            session_factory, settings, bootstrap_secret=extra_keys["workflow_only"]
        )
        async with connected_client(server) as session:
            return await _call_error_code(
                session,
                "list_pending_review_items",
                {"company": world.company_a_id, "severity": "dire"},
            )

    assert asyncio.run(_run()) == MCP_ERROR_INVALID_PARAMS


def test_malformed_date_is_rejected(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A non-date string for a date argument fails (pydantic validation)."""

    async def _run() -> None:
        server = _core_server(session_factory, settings, bootstrap_secret=world.key_full_secret)
        async with connected_client(server) as session:
            with pytest.raises(MCPError):
                await session.call_tool(
                    "get_trial_balance",
                    {
                        "company": world.company_a_id,
                        "date_from": "not-a-date",
                        "date_to": PERIOD_NOV_2026[1],
                    },
                )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Live success paths + provenance + no-floats
# ---------------------------------------------------------------------------


def _expected_company_id(tool: str, live_world: LiveWorld) -> tuple[list[str], str]:
    """Where a tool's result carries the company id, and its expected value.

    Most results carry the engine company id at ``result.company_id``;
    the bank report carries the HesabYar row id (as REST does), and
    the validation run carries it inside ``result.run``.
    """
    if tool == "get_bank_reconciliation_status":
        return ["company_id"], live_world.company_row_id
    if tool == "validate_financial_statement":
        return ["run", "company_id"], str(live_world.engine_id)
    return ["company_id"], str(live_world.engine_id)


@pytest.mark.parametrize("tool", sorted(_COMPANY_TOOLS))
def test_live_tool_success_shape(
    tool: str,
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """Each company-scoped tool succeeds live and returns result + provenance."""

    async def _run() -> dict[str, Any]:
        server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
        async with connected_client(server) as session:
            return await _call_json(session, tool, _args_for(tool, str(live_world.engine_id)))

    payload = asyncio.run(_run())
    assert "result" in payload
    assert "provenance" in payload
    path, expected = _expected_company_id(tool, live_world)
    node: Any = payload["result"]
    for key in path:
        node = node[key]
    assert node == expected


def test_live_list_companies_and_explain(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """The two non-company tools succeed live (and carry no provenance)."""

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
        async with connected_client(server) as session:
            companies = await _call_json(session, "list_companies", {})
            explained = await _call_json(
                session, "explain_accounting_rule", {"standard_code": "1"}
            )
            return companies, explained

    companies, explained = asyncio.run(_run())
    assert "provenance" not in companies
    assert companies["result"]["count"] >= 1
    assert any(
        item["engine_id"] == live_world.engine_id
        for item in companies["result"]["companies"]
    )
    assert "provenance" not in explained
    assert explained["result"]["standard_code"] == "1"
    assert explained["result"]["rules"], "standard 1 carries machine-readable rules"


def test_live_restricted_key_lists_only_its_company(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """A company-restricted key sees exactly its own company in list_companies."""

    async def _run() -> dict[str, Any]:
        server = _core_server(
            session_factory, settings, bootstrap_secret=live_world.key_restricted_secret
        )
        async with connected_client(server) as session:
            return await _call_json(session, "list_companies", {})

    payload = asyncio.run(_run())
    assert payload["result"]["count"] == 1
    assert payload["result"]["companies"][0]["engine_id"] == live_world.engine_id


@pytest.mark.parametrize("tool", sorted(_PROVENANCE_TOOLS))
def test_provenance_complete(
    tool: str,
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """Every financial result carries all §13.7 provenance fields."""

    async def _run() -> dict[str, Any]:
        server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
        async with connected_client(server) as session:
            return await _call_json(session, tool, _args_for(tool, str(live_world.engine_id)))

    payload = asyncio.run(_run())
    provenance = payload["provenance"]
    assert set(provenance) == set(PROVENANCE_FIELDS)
    assert provenance["company_id"] == live_world.engine_id
    assert provenance["currency"] == "IRR"
    assert provenance["canonical_unit"] == "Rial"
    assert provenance["generated_at"]
    assert provenance["source_data_freshness"]
    assert "interprets the supplied data" in provenance["notice"]
    assert isinstance(provenance["validation_warnings"], list)
    assert isinstance(provenance["incomplete_data_warnings"], list)


@pytest.mark.parametrize("tool", sorted(_COMPANY_TOOLS))
def test_no_binary_floats_in_amounts(
    tool: str,
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """Amounts serialize as ints / decimal strings — never binary floats."""

    async def _run() -> dict[str, Any]:
        server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
        async with connected_client(server) as session:
            return await _call_json(session, tool, _args_for(tool, str(live_world.engine_id)))

    payload = asyncio.run(_run())
    _assert_no_floats(payload["result"])
    _assert_no_floats(payload["provenance"])


def test_successful_call_is_audited(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """A successful tool call writes one allowed audit row with the company."""

    async def _run() -> None:
        server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
        async with connected_client(server) as session:
            await _call_json(
                session,
                "get_trial_balance",
                _args_for("get_trial_balance", str(live_world.engine_id)),
            )

    asyncio.run(_run())
    rows = _audit_rows_for_tenant(session_factory, "get_trial_balance", live_world.tenant_id)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].company_id == live_world.company_row_id


# ---------------------------------------------------------------------------
# MCP-vs-REST equality (spec §13.3 + §19)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,rest_path",
    [
        (
            "get_trial_balance",
            "/api/v1/reports/trial-balance?company={company}&date_from={date_from}&date_to={date_to}",
        ),
        (
            "get_general_ledger",
            "/api/v1/reports/general-ledger?company={company}&date_from={date_from}&date_to={date_to}",
        ),
        (
            "get_balance_sheet",
            "/api/v1/reports/balance-sheet?company={company}&as_of={as_of}",
        ),
        (
            "get_income_statement",
            "/api/v1/reports/income-statement?company={company}&date_from={date_from}&date_to={date_to}",
        ),
        (
            "get_cash_flow",
            "/api/v1/reports/cash-flow?company={company}&date_from={date_from}&date_to={date_to}",
        ),
        (
            "get_receivables_aging",
            "/api/v1/reports/notes-balance?company={company}&as_of={as_of}&instrument=receivable",
        ),
        (
            "get_payables_aging",
            "/api/v1/reports/notes-balance?company={company}&as_of={as_of}&instrument=payable",
        ),
        (
            "get_bank_reconciliation_status",
            "/api/v1/bank/reconciliation-status?company={company}&date_from={date_from}&date_to={date_to}",
        ),
    ],
)
def test_report_equality_mcp_vs_rest(
    tool: str,
    rest_path: str,
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """Same company + period via MCP and via REST produces the same figures."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(session, tool, _args_for(tool, str(live_world.engine_id)))

    payload = asyncio.run(_run())
    url = rest_path.format(
        company=live_world.engine_id,
        date_from=PERIOD_NOV_2026[0],
        date_to=PERIOD_NOV_2026[1],
        as_of=AS_OF_NOV_2026,
    )
    response = rest.get(url, headers=_auth(live_world.key_all_secret))
    assert response.status_code == 200, response.text
    mcp_result = dict(payload["result"])
    # The aging tools add MCP-only coverage keys on top of the REST shape.
    mcp_result.pop("aging_scope", None)
    mcp_result.pop("coverage_note", None)
    assert _strip_timestamps(mcp_result) == _strip_timestamps(response.json())


def test_general_ledger_detail_equality(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """General-ledger DETAIL mode (one account's lines) matches REST too."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            summary = await _call_json(
                session,
                "get_general_ledger",
                _args_for("get_general_ledger", str(live_world.engine_id)),
            )
            assert summary["result"]["accounts"], "the period has posted activity"
            account_id = summary["result"]["accounts"][0]["account_id"]
            return await _call_json(
                session,
                "get_general_ledger",
                _args_for("get_general_ledger", str(live_world.engine_id))
                | {"account_id": account_id},
            )

    detail = asyncio.run(_run())
    account_id = detail["result"]["accounts"][0]["account_id"]
    assert detail["result"]["accounts"][0]["lines"], "detail mode carries lines"
    url = (
        f"/api/v1/reports/general-ledger?company={live_world.engine_id}"
        f"&date_from={PERIOD_NOV_2026[0]}&date_to={PERIOD_NOV_2026[1]}"
        f"&account_id={account_id}"
    )
    response = rest.get(url, headers=_auth(live_world.key_all_secret))
    assert response.status_code == 200, response.text
    assert _strip_timestamps(detail["result"]) == _strip_timestamps(response.json())


def test_summary_components_match_rest_reports(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """The summary's four components equal the four REST reports' totals."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(
                session,
                "get_accounting_summary",
                _args_for("get_accounting_summary", str(live_world.engine_id)),
            )

    payload = asyncio.run(_run())
    headers = _auth(live_world.key_all_secret)
    company = live_world.engine_id

    def _get(path: str) -> dict[str, Any]:
        response = rest.get(path, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    trial_balance = _get(
        f"/api/v1/reports/trial-balance?company={company}"
        f"&date_from={PERIOD_NOV_2026[0]}&date_to={PERIOD_NOV_2026[1]}"
    )
    balance_sheet = _get(
        f"/api/v1/reports/balance-sheet?company={company}&as_of={AS_OF_NOV_2026}"
    )
    income_statement = _get(
        f"/api/v1/reports/income-statement?company={company}"
        f"&date_from={PERIOD_NOV_2026[0]}&date_to={PERIOD_NOV_2026[1]}"
    )
    cash_flow = _get(
        f"/api/v1/reports/cash-flow?company={company}"
        f"&date_from={PERIOD_NOV_2026[0]}&date_to={PERIOD_NOV_2026[1]}"
    )
    summary = payload["result"]
    assert summary["trial_balance"]["total_debit"] == trial_balance["total_debit"]
    assert summary["trial_balance"]["total_credit"] == trial_balance["total_credit"]
    assert summary["trial_balance"]["balanced"] == trial_balance["balanced"]
    assert summary["balance_sheet"]["total_assets"] == balance_sheet["total_assets"]
    assert summary["balance_sheet"]["total_liabilities"] == balance_sheet["total_liabilities"]
    assert summary["balance_sheet"]["total_equity"] == balance_sheet["total_equity"]
    assert summary["income_statement"]["total_revenue"] == income_statement["total_revenue"]
    assert summary["income_statement"]["total_expense"] == income_statement["total_expense"]
    assert summary["income_statement"]["net_income"] == income_statement["net_income"]
    assert summary["cash_flow"]["opening_cash"] == cash_flow["opening_cash"]
    assert summary["cash_flow"]["closing_cash"] == cash_flow["closing_cash"]
    assert summary["cash_flow"]["net_change"] == cash_flow["net_change"]
    assert set(summary["sources"]) == {
        "trial_balance",
        "balance_sheet",
        "income_statement",
        "cash_flow",
    }


def test_list_companies_equality_mcp_vs_rest(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """list_companies matches the REST companies list (same tenant join)."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(session, "list_companies", {})

    payload = asyncio.run(_run())
    response = rest.get("/api/v1/companies", headers=_auth(live_world.key_all_secret))
    assert response.status_code == 200, response.text
    rest_items = {
        (item["id"], item["engine_id"], item["name"], item["currency_code"])
        for item in response.json()["items"]
    }
    mcp_items = {
        (item["id"], item["engine_id"], item["name"], item["currency_code"])
        for item in payload["result"]["companies"]
    }
    assert mcp_items == rest_items
    assert payload["result"]["count"] == len(rest_items)


def test_tax_compliance_matches_rest_registries(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """Tax status: active versions + Moadian counts match the REST registries."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(
                session,
                "get_tax_compliance_status",
                _args_for("get_tax_compliance_status", str(live_world.engine_id)),
            )

    payload = asyncio.run(_run())
    headers = _auth(live_world.key_all_secret)

    versions = rest.get(
        f"/api/v1/tax-policy/versions?effective_on={AS_OF_NOV_2026}&limit=100",
        headers=headers,
    )
    assert versions.status_code == 200, versions.text
    active_rest = {
        item["id"] for item in versions.json()["items"] if item["status"] == "active"
    }
    active_mcp = {item["id"] for item in payload["result"]["active_policy_versions"]}
    assert active_mcp == active_rest
    assert payload["result"]["active_policy_version_count"] == len(active_rest)

    profiles = rest.get("/api/v1/moadian/profiles?is_active=true&limit=100", headers=headers)
    assert profiles.status_code == 200, profiles.text
    assert {item["profile_id"] for item in profiles.json()["items"]} == {
        item["profile_id"] for item in payload["result"]["moadian_profiles"]
    }

    documents = rest.get("/api/v1/moadian/documents?limit=100", headers=headers)
    assert documents.status_code == 200, documents.text
    doc_counts: dict[str, int] = {}
    for item in documents.json()["items"]:
        doc_counts[item["status"]] = doc_counts.get(item["status"], 0) + 1
    assert payload["result"]["moadian_documents_by_status"] == doc_counts

    submissions = rest.get("/api/v1/moadian/submissions?limit=100", headers=headers)
    assert submissions.status_code == 200, submissions.text
    sub_counts: dict[str, int] = {}
    for item in submissions.json()["items"]:
        sub_counts[item["status"]] = sub_counts.get(item["status"], 0) + 1
    assert payload["result"]["moadian_submissions_by_status"] == sub_counts


def test_validate_equality_mcp_vs_rest(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """validate_financial_statement: same per-rule outcomes as the REST run."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(
                session,
                "validate_financial_statement",
                _args_for("validate_financial_statement", str(live_world.engine_id)),
            )

    payload = asyncio.run(_run())
    headers = _auth(live_world.key_all_secret)
    body = {
        "company_id": str(live_world.engine_id),
        "period_start": PERIOD_NOV_2026[0],
        "period_end": PERIOD_NOV_2026[1],
        "standard_codes": ["1"],
    }
    response = rest.post("/api/v1/report-validation-runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    rest_run = response.json()
    mcp_run = payload["result"]["run"]
    rest_run_inner = rest_run["run"]
    # Two runs, two ids — the OUTCOMES must match, not the ids.
    assert mcp_run["overall_outcome"] == rest_run_inner["overall_outcome"]
    assert mcp_run["is_valid"] == rest_run_inner["is_valid"]
    assert mcp_run["rules_executed"] == rest_run_inner["rules_executed"]
    assert mcp_run["rules_passed"] == rest_run_inner["rules_passed"]
    assert mcp_run["rules_failed"] == rest_run_inner["rules_failed"]
    assert mcp_run["rules_errored"] == rest_run_inner["rules_errored"]
    assert mcp_run["standard_codes"] == rest_run_inner["standard_codes"]
    assert mcp_run["ruleset_version"] == rest_run_inner["ruleset_version"]
    mcp_results = {item["rule_id"]: item["outcome"] for item in mcp_run["results"]}
    rest_results = {item["rule_id"]: item["outcome"] for item in rest_run_inner["results"]}
    assert mcp_results == rest_results
    assert payload["result"]["provenance"]["payload_hash"] == rest_run["provenance"]["payload_hash"]
    assert payload["result"]["provenance"]["total_debit"] == rest_run["provenance"]["total_debit"]
    assert payload["result"]["provenance"]["total_credit"] == rest_run["provenance"]["total_credit"]
    # The MCP-created run is a real run: REST serves it back by id.
    run_id = mcp_run["run_id"]
    fetched = rest.get(f"/api/v1/report-validation-runs/{run_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["provenance"]["snapshot_id"] == payload["provenance"]["snapshot_ref"]


def test_validate_blocked_period_fails_like_rest(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """A 1405-period validation without standard codes hits the Standard 15 gate."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)

    async def _run() -> str:
        async with connected_client(server) as session:
            return await _call_error_message(
                session,
                "validate_financial_statement",
                {
                    "company": str(live_world.engine_id),
                    "period_start": "2026-03-21",
                    "period_end": "2026-03-31",
                },
            )

    assert "STANDARD_SOURCE_BLOCKED" in asyncio.run(_run())


def test_explain_equality_mcp_vs_rest(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """explain_accounting_rule matches GET /standards/{code} exactly."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(
                session, "explain_accounting_rule", {"standard_code": "1"}
            )

    payload = asyncio.run(_run())
    response = rest.get("/api/v1/standards/1", headers=_auth(live_world.key_all_secret))
    assert response.status_code == 200, response.text
    assert payload["result"] == response.json()


def test_explain_single_rule_filters(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """explain_accounting_rule with rule_id returns only that rule."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        async with connected_client(server) as session:
            full = await _call_json(
                session, "explain_accounting_rule", {"standard_code": "1"}
            )
            rule_id = full["result"]["rules"][0]["rule_id"]
            single = await _call_json(
                session,
                "explain_accounting_rule",
                {"standard_code": "1", "rule_id": rule_id},
            )
            return full, single

    full, single = asyncio.run(_run())
    assert len(single["result"]["rules"]) == 1
    assert single["result"]["rules"] == full["result"]["rules"][:1]
    assert single["result"]["standard_code"] == "1"


def test_explain_unknown_rule_is_invalid_params(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """An unknown rule id for a known standard is a -32602."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)

    async def _run() -> int:
        async with connected_client(server) as session:
            return await _call_error_code(
                session,
                "explain_accounting_rule",
                {"standard_code": "1", "rule_id": "NOPE-000"},
            )

    assert asyncio.run(_run()) == MCP_ERROR_INVALID_PARAMS


def _file_review_item(
    rest: TestClient,
    secret: str,
    company_row_id: str | None,
    *,
    item_type: str = "bank_unmatched",
    severity: str = "warning",
    amount_irr: int | None = 1_500_000,
) -> dict[str, Any]:
    """File one review item through the REST endpoint; return its JSON."""
    body = {
        "item_type": item_type,
        "severity": severity,
        "reason": "MCP US-602 test item",
        "suggested_action": "Review it",
        "company_id": company_row_id,
        "amount_irr": amount_irr,
    }
    response = rest.post("/api/v1/review-queue/items", json=body, headers=_auth(secret))
    assert response.status_code == 201, response.text
    return response.json()


def test_review_items_equality_mcp_vs_rest(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """list_pending_review_items matches the REST queue for the company.

    Items filed for another company (or no company) must not leak in.
    """
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)
    headers = _auth(live_world.key_all_secret)
    mine_a = _file_review_item(rest, live_world.key_all_secret, live_world.company_row_id)
    mine_b = _file_review_item(
        rest,
        live_world.key_all_secret,
        live_world.company_row_id,
        item_type="cheque_exception",
        severity="critical",
        amount_irr=None,
    )
    _file_review_item(rest, live_world.key_all_secret, live_world.other_company_row_id)
    _file_review_item(rest, live_world.key_all_secret, None)

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(
                session,
                "list_pending_review_items",
                {"company": str(live_world.engine_id)},
            )

    payload = asyncio.run(_run())
    result = payload["result"]
    assert result["total_pending"] == 2
    assert result["count"] == 2
    assert {item["id"] for item in result["items"]} == {mine_a["id"], mine_b["id"]}
    # Critical first, then oldest (the service's default sort).
    assert result["items"][0]["id"] == mine_b["id"]

    rest_response = rest.get(
        f"/api/v1/review-queue/items?company={live_world.company_row_id}&limit=100",
        headers=headers,
    )
    assert rest_response.status_code == 200, rest_response.text
    rest_items = {
        item["id"]: item for item in rest_response.json()["items"] if item["status"] == "open"
    }
    assert set(rest_items) == {mine_a["id"], mine_b["id"]}
    for item in result["items"]:
        rest_item = rest_items[item["id"]]
        for field in (
            "item_type",
            "severity",
            "status",
            "company_id",
            "reason",
            "amount_irr",
            "suggested_action_code",
            "suggested_reason_key",
        ):
            assert item[field] == rest_item[field], field


def test_review_items_filters(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """item_type / severity filters narrow the pending list."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)
    rest = _rest_client(settings)
    _file_review_item(rest, live_world.key_all_secret, live_world.company_row_id)
    critical = _file_review_item(
        rest,
        live_world.key_all_secret,
        live_world.company_row_id,
        item_type="cheque_exception",
        severity="critical",
    )

    async def _run() -> dict[str, Any]:
        async with connected_client(server) as session:
            return await _call_json(
                session,
                "list_pending_review_items",
                {"company": str(live_world.engine_id), "severity": "critical"},
            )

    payload = asyncio.run(_run())
    assert payload["result"]["total_pending"] == 1
    assert payload["result"]["items"][0]["id"] == critical["id"]


def test_aging_tools_carry_coverage_note(
    live_world: LiveWorld,
    session_factory: Callable[[], Session],
    settings: AppSettings,
) -> None:
    """The aging tools say plainly they return NOTES aging (the honest gap)."""
    server = _core_server(session_factory, settings, bootstrap_secret=live_world.key_all_secret)

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        async with connected_client(server) as session:
            receivables = await _call_json(
                session,
                "get_receivables_aging",
                _args_for("get_receivables_aging", str(live_world.engine_id)),
            )
            payables = await _call_json(
                session,
                "get_payables_aging",
                _args_for("get_payables_aging", str(live_world.engine_id)),
            )
            return receivables, payables

    receivables, payables = asyncio.run(_run())
    assert receivables["result"]["aging_scope"] == "notes_receivable_only"
    assert payables["result"]["aging_scope"] == "notes_payable_only"
    assert "NOTES" in receivables["result"]["coverage_note"]
    assert receivables["result"]["instrument"] == "receivable"
    assert payables["result"]["instrument"] == "payable"
