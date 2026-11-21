"""MCP tests for the US-605 draft-only mutation tools.

Four tools, each exercised the way a real client would use it: over
the in-memory streams, through the production middleware (auth,
scopes, company check, audit), against the real Postgres test
database — and, for the engine-backed tools, the live Tryton test
server when one is reachable (the plain ``python-tests`` CI job has
no trytond, so those tests skip there exactly like the contract
tests do).

Coverage per tool:

- conformance (listed with its narrow input schema; scope record);
- success: the draft is created, the result carries its ID +
  validation findings + the next human action, provenance complete,
  no binary floats anywhere in the JSON;
- the never-post SAFETY property: the draft exists AND its state is
  still ``draft`` AND no posted/finalized/approved/submitted object
  resulted (read back from the engine / database after the call);
- scope denial (a key missing only that tool's scope is refused);
- cross-company fail-closed (a key restricted to company A cannot
  call with company B);
- error shapes (unbalanced lines, unknown account, bad invoice
  type, invalid Moadian payload, unknown statement);
- Moadian never touched: no test key carries ``moadian:submit``
  (or ``ledger:post`` / ``invoice:finalize`` / ``compliance:submit``)
  and the tax tool creates no submission.

The tax-document tests are Postgres-only (no engine reads), so they
run in every CI job. The journal/invoice/reconcile tests need the
live Tryton test company and skip when it is unreachable.

Engine rows created by the live tests (draft moves, draft invoices,
draft statements, a customer party) are deleted in teardown; the
Postgres rows (Moadian documents/profiles, match records, review
items + their audit entries) are deleted the same way. The world's
tenant is unique per test, so parallel CI jobs sharing the test
database cannot see each other's rows.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from hesabyar.application.commands.moadian import MoadianProfileService
from hesabyar.domain.accounting.commands import CreateBankStatement, CreateBankStatementLine
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementRef,
    CompanyRef,
    InvoiceRef,
    JournalDraftRef,
    PartyRef,
    StatementJournalRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.moadian.models import MoadianEnvironment
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.moadian_repository import (
    SqlAlchemyMoadianRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    ApiClientModel,
    BankStatementMatchModel,
    EngineCompanyModel,
    EngineConnectionModel,
    MoadianDocumentModel,
    MoadianProfileModel,
    MoadianSubmissionModel,
    ReviewItemAuditModel,
    ReviewItemModel,
)
from hesabyar.interfaces.mcp import McpContext, create_mcp_server
from hesabyar.interfaces.mcp.errors import MCP_ERROR_FORBIDDEN
from hesabyar.interfaces.mcp.provenance import (
    MODEL_INTERPRETATION_NOTICE,
    PROVENANCE_FIELDS,
)
from hesabyar.interfaces.mcp.tools_drafts import DRAFT_TOOLS, register_draft_tools
from tests.mcp.conftest import INSTALLATION_ID, World
from tests.mcp.harness import authenticate_secret, connected_client, tool_text
from tests.moadian_fixtures import taxid_for, valid_invoice_payload

#: The live Tryton test company provisioned by scripts/ci/setup-tryton.sh.
TRYTON_TEST_COMPANY = "HesabYar Adapter Test"

#: Account codes of the test company (contract-test convention).
CASH_ACCOUNT_CODE = "1.1.1"
RECEIVABLE_ACCOUNT_CODE = "1.2.1"
REVENUE_ACCOUNT_CODE = "4.1.1"

#: All four draft scopes, plus the base + company scopes.
#: Deliberately NO moadian:submit, NO ledger:post, NO invoice:finalize,
#: NO compliance:submit — a draft-only tool must need none of them, and
#: every success test below proves it by succeeding without them.
FULL_SCOPES = (
    "mcp:connect",
    "company:read",
    "ledger:draft:create",
    "invoice:draft:create",
    "banking:reconcile",
    "compliance:draft:create",
)

#: Minimal arguments per tool (company A of the world). The denial
#: tests use these; the handler never runs there, so placeholder
#: engine ids are fine.
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "prepare_journal_draft": {
        "journal_date": "2026-11-20",
        "lines": [
            {"account_id": 1, "debit_irr": 1000},
            {"account_id": 2, "credit_irr": 1000},
        ],
    },
    "prepare_invoice_draft": {
        "invoice_type": "out",
        "party_id": 1,
        "invoice_date": "2026-11-20",
        "lines": [{"account_id": 1, "unit_price_irr": 1000}],
    },
    "prepare_reconciliation_suggestions": {"statement_id": 1},
    "prepare_tax_document_draft": {
        "profile_id": "00000000-0000-0000-0000-000000000000",
        "payload": {},
    },
}

#: Distinctive statement-line amounts (odd, non-round): no other
#: test's posted invoice or payment carries them, so the reconcile
#: test's lines are deterministically unmatched.
DEPOSIT_IRR = 5_000_001
WITHDRAWAL_IRR = 2_000_003


# ---------------------------------------------------------------------------
# Keys + server
# ---------------------------------------------------------------------------


@dataclass
class DraftKeys:
    """Secrets for the world's client, one per scope-denial case."""

    full: str
    no_ledger_draft: str
    no_invoice_draft: str
    no_reconcile: str
    no_compliance_draft: str
    restricted_a: str

    def missing_scope_key(self, tool: str) -> str:
        """The key that lacks exactly ``tool``'s scope."""
        return {
            "prepare_journal_draft": self.no_ledger_draft,
            "prepare_invoice_draft": self.no_invoice_draft,
            "prepare_reconciliation_suggestions": self.no_reconcile,
            "prepare_tax_document_draft": self.no_compliance_draft,
        }[tool]


def _issue_key(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    world: World,
    scopes: list[str],
    *,
    engine_company_id: str | None = None,
) -> str:
    """Issue one key for the world's client (world teardown removes it)."""
    session = session_factory()
    try:
        client = session.get(ApiClientModel, world.client_id)
        assert client is not None
        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        issued = service.issue_key(
            session,
            client=client,
            name="mcp drafts test key",
            scopes=scopes,
            installation_id=INSTALLATION_ID,
            engine_company_id=engine_company_id,
        )
        session.commit()
        return issued.secret
    finally:
        session.close()


@pytest.fixture()
def keys(world: World, session_factory: Callable[[], Session], settings: AppSettings) -> DraftKeys:
    """One full key, one key per missing scope, one company-restricted key."""
    full: list[str] = list(FULL_SCOPES)

    def _without(scope: str) -> list[str]:
        return [s for s in full if s != scope]

    return DraftKeys(
        full=_issue_key(session_factory, settings, world, full),
        no_ledger_draft=_issue_key(session_factory, settings, world, _without("ledger:draft:create")),
        no_invoice_draft=_issue_key(session_factory, settings, world, _without("invoice:draft:create")),
        no_reconcile=_issue_key(session_factory, settings, world, _without("banking:reconcile")),
        no_compliance_draft=_issue_key(session_factory, settings, world, _without("compliance:draft:create")),
        restricted_a=_issue_key(session_factory, settings, world, full, engine_company_id=world.company_a_id),
    )


@pytest.fixture(autouse=True)
def _clean_draft_audit_rows(world: World, session_factory: Callable[[], Session]) -> Generator[None, None, None]:
    """Remove this world's draft-tool audit rows (tool names are fixed)."""
    yield
    session = session_factory()
    try:
        session.execute(
            delete(AiAccessAuditModel).where(
                AiAccessAuditModel.tool.in_([name for name, _ in DRAFT_TOOLS]),
                AiAccessAuditModel.client_id == world.client_id,
            )
        )
        session.commit()
    finally:
        session.close()


def _draft_server(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    bootstrap_secret: str,
) -> tuple[Any, McpContext]:
    """A server with the four draft tools registered (no echo tool)."""
    context = McpContext(settings=settings, session_factory=session_factory, transport="memory")
    context.bootstrap_client = authenticate_secret(session_factory, settings, bootstrap_secret)
    server = create_mcp_server(context)
    register_draft_tools(server, context)
    return server, context


# ---------------------------------------------------------------------------
# Call + assertion helpers
# ---------------------------------------------------------------------------


def _call_tool(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    secret: str,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Call a draft tool; return its parsed JSON payload."""

    async def _run() -> dict[str, Any]:
        server, _ = _draft_server(world, session_factory, settings, secret)
        async with connected_client(server) as session:
            result = await session.call_tool(tool, arguments)
            assert not result.is_error, f"{tool} failed: {tool_text(result)}"
            return json.loads(tool_text(result))

    return asyncio.run(_run())


def _call_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    secret: str,
    tool: str,
    arguments: dict[str, Any],
) -> str:
    """Call a draft tool; it must fail as an is_error result."""

    async def _run() -> str:
        server, _ = _draft_server(world, session_factory, settings, secret)
        async with connected_client(server) as session:
            result = await session.call_tool(tool, arguments)
            assert result.is_error, f"{tool} unexpectedly succeeded: {tool_text(result)}"
            return tool_text(result)

    return asyncio.run(_run())


def _call_tool_denied_code(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    secret: str,
    tool: str,
    arguments: dict[str, Any],
) -> int:
    """Call a draft tool; it must fail with an MCP protocol error."""

    async def _run() -> int:
        server, _ = _draft_server(world, session_factory, settings, secret)
        async with connected_client(server) as session:
            with pytest.raises(MCPError) as excinfo:
                await session.call_tool(tool, arguments)
            return excinfo.value.code

    return asyncio.run(_run())


def _audit_rows_for_client(
    session_factory: Callable[[], Session], tool: str, client_id: str
) -> list[AiAccessAuditModel]:
    """This world's audit rows for ``tool``, oldest first."""
    session = session_factory()
    try:
        return list(
            session.execute(
                select(AiAccessAuditModel)
                .where(
                    AiAccessAuditModel.tool == tool,
                    AiAccessAuditModel.client_id == client_id,
                )
                .order_by(AiAccessAuditModel.created_at)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _assert_no_floats(value: Any, path: str = "$") -> None:
    """No binary floating-point anywhere in a tool result."""
    if isinstance(value, bool):
        return
    assert not isinstance(value, float), f"float at {path}: {value!r}"
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_floats(item, f"{path}[{index}]")


def _assert_provenance(payload: dict[str, Any], *, company_engine_id: int) -> None:
    """The §13.7 provenance block is complete on a tool result."""
    provenance = payload["provenance"]
    assert PROVENANCE_FIELDS <= set(provenance), (
        f"missing provenance fields: {sorted(PROVENANCE_FIELDS - set(provenance))}"
    )
    assert provenance["company_id"] == company_engine_id
    assert provenance["currency"] == "IRR"
    assert provenance["canonical_unit"] == "Rial"
    assert provenance["notice"] == MODEL_INTERPRETATION_NOTICE
    assert provenance["report_type"]
    assert provenance["period"]["label"]
    # generated_at is a parseable ISO timestamp.
    datetime.datetime.fromisoformat(provenance["generated_at"])


def _assert_draft_contract(payload: dict[str, Any], *, company_row_id: str, engine_id: int) -> None:
    """The §13.5 result contract shared by all four tools."""
    assert payload["company"]["id"] == company_row_id
    assert payload["company"]["engine_id"] == engine_id
    assert payload["draft_id"]
    assert isinstance(payload["findings"], list) and payload["findings"]
    assert all(isinstance(finding, str) and finding for finding in payload["findings"])
    assert isinstance(payload["next_action"], str) and payload["next_action"]
    _assert_no_floats(payload)
    _assert_provenance(payload, company_engine_id=engine_id)


def _count_moadian_rows(session_factory: Callable[[], Session], tenant_id: str) -> tuple[int, int]:
    """(documents, submissions) for a tenant — the Moadian-never-touched check."""
    session = session_factory()
    try:
        documents = session.execute(
            select(func.count())
            .select_from(MoadianDocumentModel)
            .where(MoadianDocumentModel.tenant_id == tenant_id)
        ).scalar_one()
        submissions = session.execute(
            select(func.count())
            .select_from(MoadianSubmissionModel)
            .where(MoadianSubmissionModel.tenant_id == tenant_id)
        ).scalar_one()
        return documents, submissions
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Live-Tryton company (engine-backed tests only)
# ---------------------------------------------------------------------------


@dataclass
class LiveCompany:
    """The world's mapping to the live Tryton test company."""

    engine_id: int
    row_id: str
    cash_account_id: int
    receivable_account_id: int
    revenue_account_id: int
    general_journal_id: int


def _tryton_client(settings: AppSettings) -> TrytonRestClient:
    """A raw Tryton REST client (the caller closes it)."""
    t = settings.tryton
    return TrytonRestClient(t.base_url, t.database, t.api_key, timeout=t.timeout_seconds)


@pytest.fixture()
def live_company(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> Generator[LiveCompany, None, None]:
    """Map the world's tenant to the live Tryton test company.

    Skips when no trytond is reachable (plain python-tests job) or
    the test company / accounts / general journal are not provisioned
    — the same skip-when-unreachable contract the contract tests use.
    """
    client = _tryton_client(settings)
    try:
        if not client.ping():
            pytest.skip(f"trytond server not reachable at {settings.tryton.base_url}")
        adapter = TrytonAccountingAdapter(client)
        engine_id: int | None = None
        for view in adapter.list_companies():
            if view.name == TRYTON_TEST_COMPANY:
                engine_id = view.ref.engine_id
                break
        if engine_id is None:
            pytest.skip(f"Test company {TRYTON_TEST_COMPANY!r} not found in the Tryton DB.")
        company = CompanyRef(engine_id)
        cash = adapter.get_account_by_code(company, CASH_ACCOUNT_CODE)
        receivable = adapter.get_account_by_code(company, RECEIVABLE_ACCOUNT_CODE)
        revenue = adapter.get_account_by_code(company, REVENUE_ACCOUNT_CODE)
        if cash is None or receivable is None or revenue is None:
            pytest.skip("Cash/receivable/revenue accounts missing from the test company.")
        journal = adapter.get_general_journal(company)
    finally:
        client.close()

    session = session_factory()
    connection_id = str(uuid.uuid4())
    row_id = str(uuid.uuid4())
    try:
        connection = EngineConnectionModel(
            id=connection_id,
            name=f"mcp605-{uuid.uuid4().hex[:8]}",
            engine="tryton",
            base_url="http://localhost:8000",
            database_name=f"hesabyar_tryton_test_{uuid.uuid4().hex[:8]}",
        )
        session.add(connection)
        session.flush()
        row = EngineCompanyModel(
            id=row_id,
            tenant_id=world.tenant_id,
            engine_connection_id=connection.id,
            external_company_id=str(engine_id),
            company_name=TRYTON_TEST_COMPANY,
        )
        session.add(row)
        session.commit()
        yield LiveCompany(
            engine_id=engine_id,
            row_id=row_id,
            cash_account_id=cash.ref.engine_id,
            receivable_account_id=receivable.ref.engine_id,
            revenue_account_id=revenue.ref.engine_id,
            general_journal_id=journal.engine_id,
        )
    finally:
        session.close()
        cleanup = session_factory()
        try:
            cleanup.execute(delete(EngineCompanyModel).where(EngineCompanyModel.id == row_id))
            cleanup.execute(delete(EngineConnectionModel).where(EngineConnectionModel.id == connection_id))
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture()
def customer_party(
    live_company: LiveCompany, settings: AppSettings
) -> Generator[PartyRef, None, None]:
    """A customer party with one address, created via REST (contract-test pattern).

    ``live_company`` is only taken for its skip-when-unreachable: the
    party itself is company-independent in Tryton.
    """
    client = _tryton_client(settings)
    party_id: int | None = None
    address_id: int | None = None
    try:
        party = client.create(
            "party.party", {"name": "US-605 MCP test customer"}, fields=["id"]
        )
        party_id = int(party["id"])
        address = client.create(
            "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
        )
        address_id = int(address["id"])
        yield PartyRef(party_id)
    finally:
        # Best-effort cleanup: a referencing invoice blocks deletion
        # (expected residue when a test fails mid-way — the invoice
        # cleanup runs first, so this normally succeeds).
        if address_id is not None:
            try:
                client.delete("party.address", address_id)
            except Exception:
                pass
        if party_id is not None:
            try:
                client.delete("party.party", party_id)
            except Exception:
                pass
        client.close()


# ---------------------------------------------------------------------------
# Moadian profile (Postgres-only tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def moadian_profile(
    world: World, session_factory: Callable[[], Session]
) -> Generator[str, None, None]:
    """An active TEST Moadian profile for the world's tenant; removed after."""
    session = session_factory()
    try:
        service = MoadianProfileService(SqlAlchemyMoadianRepository(session))
        profile = service.create_profile(
            tenant_id=world.tenant_id,
            name="US-605 MCP test profile",
            slug=f"mcp605-{uuid.uuid4().hex[:8]}",
            environment=MoadianEnvironment.TEST,
            fiscal_id="ABC123",
            economic_code="32652362589632",
        )
        session.commit()
        profile_id = profile.id
    finally:
        session.close()
    yield profile_id
    cleanup = session_factory()
    try:
        cleanup.execute(
            delete(MoadianDocumentModel).where(MoadianDocumentModel.profile_id == profile_id)
        )
        cleanup.execute(delete(MoadianProfileModel).where(MoadianProfileModel.id == profile_id))
        cleanup.commit()
    finally:
        cleanup.close()


# ---------------------------------------------------------------------------
# Conformance: the four tools are listed, with narrow input schemas
# ---------------------------------------------------------------------------


def test_tools_listed_with_narrow_schemas(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
) -> None:
    """All four tools are listed, with narrow input schemas and scope records.

    Subset, not equality: the factory also wires the 14 US-602 core
    tools, and US-603/US-604 wire more onto the same server.
    """

    async def _run() -> None:
        server, context = _draft_server(world, session_factory, settings, keys.full)
        async with connected_client(server) as session:
            listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {name for name, _ in DRAFT_TOOLS} <= names
        # NO approve_and_post tool in this release (spec §13.5).
        assert "approve_and_post" not in names
        assert context.registry.scopes_for_tool("approve_and_post") is None
        by_name = {tool.name: tool for tool in listed.tools}
        for name, _ in DRAFT_TOOLS:
            assert "ctx" not in by_name[name].input_schema["properties"]
            assert "company" in by_name[name].input_schema["properties"]
            assert "company" in by_name[name].input_schema["required"]
        assert set(by_name["prepare_journal_draft"].input_schema["properties"]) == {
            "company",
            "journal_date",
            "lines",
            "description",
            "journal_id",
        }
        assert set(by_name["prepare_journal_draft"].input_schema["required"]) == {
            "company",
            "journal_date",
            "lines",
        }
        assert set(by_name["prepare_invoice_draft"].input_schema["properties"]) == {
            "company",
            "invoice_type",
            "party_id",
            "invoice_date",
            "lines",
            "description",
            "reference",
            "journal_id",
            "account_id",
            "invoice_address_id",
        }
        assert set(by_name["prepare_invoice_draft"].input_schema["required"]) == {
            "company",
            "invoice_type",
            "party_id",
            "invoice_date",
            "lines",
        }
        assert set(by_name["prepare_reconciliation_suggestions"].input_schema["properties"]) == {
            "company",
            "statement_id",
            "window_days",
        }
        assert set(by_name["prepare_reconciliation_suggestions"].input_schema["required"]) == {
            "company",
            "statement_id",
        }
        assert set(by_name["prepare_tax_document_draft"].input_schema["properties"]) == {
            "company",
            "profile_id",
            "payload",
            "invoice_id",
        }
        assert set(by_name["prepare_tax_document_draft"].input_schema["required"]) == {
            "company",
            "profile_id",
            "payload",
        }
        for name, scopes in DRAFT_TOOLS:
            assert context.registry.scopes_for_tool(name) == frozenset(scopes)
            # A draft-only tool never needs a posting/submission scope.
            assert "moadian:submit" not in scopes
            assert "ledger:post" not in scopes
            assert "invoice:finalize" not in scopes
            assert "compliance:submit" not in scopes

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Scope denial + cross-company fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", [name for name, _ in DRAFT_TOOLS])
def test_tool_scope_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    tool: str,
) -> None:
    """A key missing only this tool's scope is refused (middleware, before the handler)."""
    code = _call_tool_denied_code(
        world,
        session_factory,
        settings,
        keys.missing_scope_key(tool),
        tool,
        {"company": world.company_a_id, **TOOL_ARGS[tool]},
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows_for_client(session_factory, tool, world.client_id)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "missing_scope"


@pytest.mark.parametrize("tool", [name for name, _ in DRAFT_TOOLS])
def test_cross_company_fails_closed(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    tool: str,
) -> None:
    """SAFETY: a key restricted to company A cannot call any tool with company B."""
    code = _call_tool_denied_code(
        world,
        session_factory,
        settings,
        keys.restricted_a,
        tool,
        {"company": world.company_b_id, **TOOL_ARGS[tool]},
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows_for_client(session_factory, tool, world.client_id)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_forbidden"


def test_unknown_company_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
) -> None:
    """A company id naming no row of the tenant is denied, not trusted."""
    code = _call_tool_denied_code(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_tax_document_draft",
        {"company": "00000000-0000-0000-0000-000000000000", **TOOL_ARGS["prepare_tax_document_draft"]},
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows_for_client(session_factory, "prepare_tax_document_draft", world.client_id)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_not_found"


def test_restricted_key_can_call_own_company(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    moadian_profile: str,
) -> None:
    """The same restricted key calling with company A succeeds (tax tool)."""
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.restricted_a,
        "prepare_tax_document_draft",
        {
            "company": world.company_a_id,
            "profile_id": moadian_profile,
            "payload": valid_invoice_payload(taxid=taxid_for(605001)),
        },
    )
    assert payload["state"] == "draft"
    assert payload["draft_id"]
    rows = _audit_rows_for_client(session_factory, "prepare_tax_document_draft", world.client_id)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].company_id == world.company_a_id


# ---------------------------------------------------------------------------
# prepare_tax_document_draft (Postgres-only — runs in every CI job)
# ---------------------------------------------------------------------------


def test_prepare_tax_document_draft_success(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    moadian_profile: str,
) -> None:
    """A valid payload creates a draft document — and NO submission."""
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_tax_document_draft",
        {
            "company": world.company_a_id,
            "profile_id": moadian_profile,
            "payload": valid_invoice_payload(taxid=taxid_for(605002)),
        },
    )
    _assert_draft_contract(
        payload,
        company_row_id=world.company_a_id,
        engine_id=int(world.company_a_external_id),
    )
    assert payload["state"] == "draft"
    assert payload["provenance"]["report_type"] == "tax_document_draft"
    assert payload["provenance"]["source_engine"] == "HesabYar"
    document = payload["document"]
    assert document["document_id"] == payload["draft_id"]
    assert document["profile_id"] == moadian_profile
    assert document["taxid"] == taxid_for(605002)
    assert document["status"] == "draft"
    assert len(document["payload_hash"]) == 64
    # The payload itself is NOT echoed back (it can carry float rates).
    assert "payload" not in document
    assert any("No submission was created" in finding for finding in payload["findings"])
    assert "Moadian" in payload["next_action"]
    # The draft is attributed to the MCP client in the audit log.
    rows = _audit_rows_for_client(session_factory, "prepare_tax_document_draft", world.client_id)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].client_id == world.client_id
    assert rows[0].key_id is not None
    assert rows[0].company_id == world.company_a_id
    # SAFETY: no submission exists for the document — Moadian was
    # never touched. The full key carries no moadian:submit scope.
    session = session_factory()
    try:
        repository = SqlAlchemyMoadianRepository(session)
        assert repository.count_submissions_for_document(payload["draft_id"]) == 0
        stored = repository.get_document(payload["draft_id"], world.tenant_id)
        assert stored is not None
        assert stored.status.value == "draft"
    finally:
        session.close()


def test_prepare_tax_document_draft_invalid_payload(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    moadian_profile: str,
) -> None:
    """An invalid payload is refused with the Table 7 problems; nothing is stored."""
    bad_payload = valid_invoice_payload(taxid=taxid_for(605003))
    del bad_payload["header"]["tins"]
    bad_payload["body"][0]["sstid"] = 123
    error = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_tax_document_draft",
        {
            "company": world.company_a_id,
            "profile_id": moadian_profile,
            "payload": bad_payload,
        },
    )
    assert "Table 7" in error
    assert "tins" in error
    documents, submissions = _count_moadian_rows(session_factory, world.tenant_id)
    assert documents == 0
    assert submissions == 0


def test_prepare_tax_document_draft_unknown_profile(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
) -> None:
    """An unknown profile is a tool error, not a draft."""
    error = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_tax_document_draft",
        {
            "company": world.company_a_id,
            "profile_id": str(uuid.uuid4()),
            "payload": valid_invoice_payload(taxid=taxid_for(605004)),
        },
    )
    assert "MOADIAN_PROFILE_NOT_FOUND" in error
    documents, submissions = _count_moadian_rows(session_factory, world.tenant_id)
    assert documents == 0
    assert submissions == 0


# ---------------------------------------------------------------------------
# prepare_journal_draft (live Tryton)
# ---------------------------------------------------------------------------


def test_prepare_journal_draft_success_live(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    live_company: LiveCompany,
) -> None:
    """A balanced draft is created — and stays a draft (never posted)."""
    today = datetime.date.today().isoformat()
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_journal_draft",
        {
            "company": live_company.row_id,
            "journal_date": today,
            "lines": [
                {
                    "account_id": live_company.cash_account_id,
                    "debit_irr": 1_000_000,
                    "description": "US-605 MCP test journal",
                },
                {
                    "account_id": live_company.revenue_account_id,
                    "credit_irr": 1_000_000,
                    "description": "US-605 MCP test journal",
                },
            ],
            "description": "US-605 MCP test journal",
            "journal_id": live_company.general_journal_id,
        },
    )
    draft_id = payload["draft_id"]
    try:
        _assert_draft_contract(
            payload,
            company_row_id=live_company.row_id,
            engine_id=live_company.engine_id,
        )
        assert payload["state"] == "draft"
        assert payload["provenance"]["report_type"] == "journal_draft"
        assert payload["provenance"]["source_engine"] == "Tryton"
        assert payload["validation"]["valid"] is True
        assert payload["validation"]["issues"] == []
        assert any("nothing was posted" in finding for finding in payload["findings"])
        assert "cannot post" in payload["next_action"]
        draft = payload["draft"]
        assert draft["id"] == draft_id
        assert draft["state"] == "draft"
        assert draft["total_debit"] == 1_000_000
        assert draft["total_credit"] == 1_000_000
        assert len(draft["lines"]) == 2
        # The draft is attributed to the MCP client in the audit log.
        rows = _audit_rows_for_client(session_factory, "prepare_journal_draft", world.client_id)
        assert len(rows) == 1
        assert rows[0].outcome == "allowed"
        assert rows[0].client_id == world.client_id
        # SAFETY, read back from the engine: the move exists AND is
        # still a draft — no posted move resulted.
        client = _tryton_client(settings)
        try:
            adapter = TrytonAccountingAdapter(client)
            view = adapter.get_journal_draft(JournalDraftRef(draft_id))
            assert view is not None
            assert view.state == "draft"
            move = client.read("account.move", draft_id, fields=["state"])
            assert move is not None and move["state"] == "draft"
        finally:
            client.close()
        # Moadian was never touched by this tool either.
        documents, submissions = _count_moadian_rows(session_factory, world.tenant_id)
        assert documents == 0
        assert submissions == 0
    finally:
        client = _tryton_client(settings)
        try:
            TrytonAccountingAdapter(client).delete_journal_draft(JournalDraftRef(draft_id))
        except Exception:
            pass
        finally:
            client.close()


def test_prepare_journal_draft_unbalanced_is_tool_error_live(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    live_company: LiveCompany,
) -> None:
    """Unbalanced lines are refused before any engine write; no draft is created.

    The no-draft check is by unique description, not by draft count:
    a count would race with any other test creating drafts in the
    shared Tryton company.
    """
    marker = f"US-605 unbalanced probe {uuid.uuid4().hex[:8]}"
    error = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_journal_draft",
        {
            "company": live_company.row_id,
            "journal_date": datetime.date.today().isoformat(),
            "lines": [
                {"account_id": live_company.cash_account_id, "debit_irr": 1_000_000},
                {"account_id": live_company.revenue_account_id, "credit_irr": 999_999},
            ],
            "description": marker,
            "journal_id": live_company.general_journal_id,
        },
    )
    assert "balanced" in error.lower() or "LEDGER" in error
    client = _tryton_client(settings)
    try:
        adapter = TrytonAccountingAdapter(client)
        drafts = adapter.list_journal_drafts(CompanyRef(live_company.engine_id))
    finally:
        client.close()
    assert all(draft.description != marker for draft in drafts)


def test_prepare_journal_draft_unknown_account_is_tool_error_live(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    live_company: LiveCompany,
) -> None:
    """An unknown account is refused by the engine-side verification."""
    error = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_journal_draft",
        {
            "company": live_company.row_id,
            "journal_date": datetime.date.today().isoformat(),
            "lines": [
                {"account_id": 999999999, "debit_irr": 1_000_000},
                {"account_id": live_company.revenue_account_id, "credit_irr": 1_000_000},
            ],
            "journal_id": live_company.general_journal_id,
        },
    )
    assert "LEDGER" in error or "account" in error.lower()


# ---------------------------------------------------------------------------
# prepare_invoice_draft (live Tryton)
# ---------------------------------------------------------------------------


def test_prepare_invoice_draft_success_live(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    live_company: LiveCompany,
    customer_party: PartyRef,
) -> None:
    """A priced draft invoice is created — and stays a draft (never finalized)."""
    today = datetime.date.today().isoformat()
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_invoice_draft",
        {
            "company": live_company.row_id,
            "invoice_type": "out",
            "party_id": customer_party.engine_id,
            "invoice_date": today,
            "lines": [
                {
                    "account_id": live_company.revenue_account_id,
                    "unit_price_irr": 500_000,
                    "quantity": 2,
                    "description": "US-605 MCP test goods",
                }
            ],
            "description": "US-605 MCP test invoice",
            "account_id": live_company.receivable_account_id,
        },
    )
    invoice_id = payload["draft_id"]
    try:
        _assert_draft_contract(
            payload,
            company_row_id=live_company.row_id,
            engine_id=live_company.engine_id,
        )
        assert payload["state"] == "draft"
        assert payload["provenance"]["report_type"] == "invoice_draft"
        assert payload["validation"]["valid"] is True
        assert payload["validation"]["issues"] == []
        assert any("not finalized or posted" in finding for finding in payload["findings"])
        assert "cannot" in payload["next_action"] and "finalize" in payload["next_action"]
        draft = payload["draft"]
        assert draft["id"] == invoice_id
        assert draft["state"] == "draft"
        assert draft["invoice_type"] == "out"
        assert draft["party_id"] == customer_party.engine_id
        assert draft["total_amount"] == 1_000_000
        assert len(draft["lines"]) == 1
        # The draft is attributed to the MCP client in the audit log.
        rows = _audit_rows_for_client(session_factory, "prepare_invoice_draft", world.client_id)
        assert len(rows) == 1
        assert rows[0].outcome == "allowed"
        assert rows[0].client_id == world.client_id
        # SAFETY, read back from the engine: the invoice exists AND
        # is still a draft — no validated/posted invoice resulted.
        client = _tryton_client(settings)
        try:
            adapter = TrytonAccountingAdapter(client)
            view = adapter.get_invoice(InvoiceRef(invoice_id))
            assert view is not None
            assert view.state == "draft"
        finally:
            client.close()
    finally:
        client = _tryton_client(settings)
        try:
            client.delete("account.invoice", invoice_id)
        except Exception:
            pass
        finally:
            client.close()


def test_prepare_invoice_draft_bad_type_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
) -> None:
    """An invoice type outside 'out'/'in' is refused before any service call."""
    error = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_invoice_draft",
        {
            "company": world.company_a_id,
            "invoice_type": "sale",
            "party_id": 1,
            "invoice_date": "2026-11-20",
            "lines": [{"account_id": 1, "unit_price_irr": 1000}],
        },
    )
    assert "invoice_type" in error


# ---------------------------------------------------------------------------
# prepare_reconciliation_suggestions (live Tryton)
# ---------------------------------------------------------------------------


def test_prepare_reconciliation_suggestions_success_live(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    live_company: LiveCompany,
) -> None:
    """A draft statement is reconciled for suggestions — and stays a draft."""
    client = _tryton_client(settings)
    try:
        adapter = TrytonAccountingAdapter(client)
        company = CompanyRef(live_company.engine_id)
        journals = adapter.list_statement_journals(company)
        if not journals:
            pytest.skip("No statement journal in the test company.")
        journal = journals[0]
        today = FiscalDate(datetime.date.today())
        statement_ref = adapter.create_bank_statement(
            CreateBankStatement(
                company=company,
                journal=StatementJournalRef(journal.ref.engine_id),
                name="US-605 MCP reconcile test",
                date=today,
                # The test company's cash journal is balance-validated:
                # the statement must carry its start/end balances.
                start_balance=MoneyIRR(0),
                end_balance=MoneyIRR(DEPOSIT_IRR - WITHDRAWAL_IRR),
                lines=(
                    CreateBankStatementLine(
                        date=today,
                        amount=MoneyIRR(DEPOSIT_IRR),
                        account=AccountRef(live_company.cash_account_id),
                        description="US-605 unmatched deposit",
                    ),
                    CreateBankStatementLine(
                        date=today,
                        amount=MoneyIRR(-WITHDRAWAL_IRR),
                        account=AccountRef(live_company.cash_account_id),
                        description="US-605 unmatched withdrawal",
                    ),
                ),
            )
        )
        statement_id = statement_ref.engine_id
    finally:
        client.close()

    try:
        payload = _call_tool(
            world,
            session_factory,
            settings,
            keys.full,
            "prepare_reconciliation_suggestions",
            {"company": live_company.row_id, "statement_id": statement_id},
        )
        assert payload["company"]["id"] == live_company.row_id
        assert payload["company"]["engine_id"] == live_company.engine_id
        assert payload["statement_id"] == statement_id
        assert payload["statement_state"] == "draft"
        assert payload["provenance"]["report_type"] == "reconciliation_suggestions"
        assert payload["provenance"]["source_engine"] == "Tryton"
        assert isinstance(payload["findings"], list) and payload["findings"]
        assert "cannot approve" in payload["next_action"]
        _assert_no_floats(payload)
        _assert_provenance(payload, company_engine_id=live_company.engine_id)
        reconciliation = payload["reconciliation"]
        assert reconciliation["statement_id"] == statement_id
        assert reconciliation["total_lines"] == 2
        assert reconciliation["unmatched_count"] == 2
        assert reconciliation["matched_count"] == 0
        assert reconciliation["review_items_filed"] == 2
        assert len(reconciliation["lines"]) == 2
        assert all(line["outcome"] == "unmatched" for line in reconciliation["lines"])
        review_item_ids = [
            line["review_item_id"]
            for line in reconciliation["lines"]
            if line["review_item_id"] is not None
        ]
        assert len(review_item_ids) == 2
        # The run is attributed to the MCP client in the audit log.
        rows = _audit_rows_for_client(
            session_factory, "prepare_reconciliation_suggestions", world.client_id
        )
        assert len(rows) == 1
        assert rows[0].outcome == "allowed"
        assert rows[0].client_id == world.client_id
        # SAFETY, read back from the engine: the statement exists AND
        # is still a draft — no validated statement resulted.
        client = _tryton_client(settings)
        try:
            adapter = TrytonAccountingAdapter(client)
            view = adapter.get_bank_statement(BankStatementRef(statement_id))
            assert view is not None
            assert view.state == "draft"
        finally:
            client.close()
        # The match records + review items are persisted (the handler
        # commits), and the review items' filed audit entries carry
        # the MCP client as the actor.
        session = session_factory()
        try:
            matches = list(
                session.execute(
                    select(BankStatementMatchModel).where(
                        BankStatementMatchModel.tenant_id == world.tenant_id,
                        BankStatementMatchModel.statement_id == statement_id,
                    )
                )
                .scalars()
                .all()
            )
            assert len(matches) == 2
            assert all(match.status == "unmatched" for match in matches)
            items = list(
                session.execute(
                    select(ReviewItemModel).where(ReviewItemModel.id.in_(review_item_ids))
                )
                .scalars()
                .all()
            )
            assert len(items) == 2
            assert all(item.item_type == "bank_unmatched" for item in items)
            assert all(item.status == "open" for item in items)
            entries = list(
                session.execute(
                    select(ReviewItemAuditModel).where(
                        ReviewItemAuditModel.item_id.in_(review_item_ids),
                        ReviewItemAuditModel.action == "filed",
                    )
                )
                .scalars()
                .all()
            )
            assert len(entries) == 2
            assert all(entry.actor == f"mcp:{world.client_id}" for entry in entries)
        finally:
            session.close()
    finally:
        client = _tryton_client(settings)
        try:
            client.delete("account.statement", statement_id)
        except Exception:
            pass
        finally:
            client.close()
        cleanup = session_factory()
        try:
            item_ids = [
                row[0]
                for row in cleanup.execute(
                    select(BankStatementMatchModel.review_item_id).where(
                        BankStatementMatchModel.tenant_id == world.tenant_id,
                        BankStatementMatchModel.statement_id == statement_id,
                    )
                ).all()
                if row[0] is not None
            ]
            if item_ids:
                cleanup.execute(
                    delete(ReviewItemAuditModel).where(
                        ReviewItemAuditModel.item_id.in_(item_ids)
                    )
                )
                cleanup.execute(
                    delete(ReviewItemModel).where(ReviewItemModel.id.in_(item_ids))
                )
            cleanup.execute(
                delete(BankStatementMatchModel).where(
                    BankStatementMatchModel.tenant_id == world.tenant_id,
                    BankStatementMatchModel.statement_id == statement_id,
                )
            )
            cleanup.commit()
        finally:
            cleanup.close()


def test_prepare_reconciliation_suggestions_unknown_statement_live(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
    live_company: LiveCompany,
) -> None:
    """A statement id naming no engine statement is a tool error."""
    error = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_reconciliation_suggestions",
        {"company": live_company.row_id, "statement_id": 999999999},
    )
    assert "LEDGER_STATEMENT_NOT_FOUND" in error


def test_prepare_reconciliation_suggestions_bad_window_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: DraftKeys,
) -> None:
    """A negative match window is refused before any service call."""
    error = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "prepare_reconciliation_suggestions",
        {"company": world.company_a_id, "statement_id": 1, "window_days": -1},
    )
    assert "window_days" in error
