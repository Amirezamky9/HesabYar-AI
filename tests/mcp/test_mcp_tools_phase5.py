"""MCP tests for the US-603 read-only Phase 5 report tools.

Five tools, each exercised the way a real client would use it: over
the in-memory streams, through the production middleware (auth,
scopes, company check, audit), against the real Postgres test
database — and, for the engine-backed tools, the live Tryton test
server when one is reachable (the plain ``python-tests`` CI job has
no trytond, so those tests skip there exactly like the contract
tests do).

Coverage per tool:

- conformance (listed with its narrow input schema; scope record);
- success on empty data (honest empty, provenance complete, no
  binary floats anywhere in the JSON);
- scope denial (a key missing only that tool's scope is refused);
- cross-company fail-closed (a key restricted to company A cannot
  call with company B);
- MCP-vs-REST equality: the same data + period through the MCP tool
  and through the matching REST endpoint(s) produce the same
  figures;
- error shapes (unknown channel/state, bad limit, reversed range).

The equality tests drive the REAL REST app (``TestClient`` over
``create_app`` with no dependency overrides) with the SAME API key
the MCP server uses, so both interfaces read the same database and
the same engine. Seeded rows are tracked by id and deleted in
teardown; the fixed test installation id is shared across worlds,
so settlement seeds use a unique channel key per test and cheque
seeds use unique Sayad ids (the live-Sayad partial unique index).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import jdatetime
import pytest
from fastapi.testclient import TestClient
from mcp.shared.exceptions import MCPError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from hesabyar.application.commands.integrations import (
    ChannelRegistryService,
    GatewayContractService,
)
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.domain.cashbox.models import (
    CashBox,
    CashBoxKind,
    CountSession,
    CountSessionStatus,
)
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.models import TrancheLeg
from hesabyar.domain.packaging.models import (
    ConsumptionRecord,
    ConsumptionSource,
    PackagingMaterial,
    ShipmentCostMethod,
    ShipmentCostRecord,
)
from hesabyar.domain.settlement.commands import CreateGatewayPayment
from hesabyar.domain.settlement.models import PaymentSource, TrancheState
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.cashbox_repository import (
    SqlAlchemyCashBoxRepository,
)
from hesabyar.infrastructure.persistence.postgres.cheques_repository import (
    SqlAlchemyChequeRepository,
)
from hesabyar.infrastructure.persistence.postgres.integrations_repository import (
    SqlAlchemyIntegrationsRepository,
)
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    ApiClientModel,
    CashBoxModel,
    ChequeModel,
    CountSessionModel,
    EngineCompanyModel,
    EngineConnectionModel,
    GatewayContractModel,
    GatewayPaymentModel,
    PackagingConsumptionModel,
    PackagingMaterialModel,
    PackagingShipmentCostModel,
    PaymentChannelModel,
    SettlementFeeRecordModel,
    SettlementTrancheModel,
)
from hesabyar.infrastructure.persistence.postgres.packaging_repository import (
    SqlAlchemyPackagingRepository,
)
from hesabyar.infrastructure.persistence.postgres.settlement_repository import (
    SqlAlchemySettlementAudit,
    SqlAlchemySettlementRepository,
)
from hesabyar.interfaces.mcp import McpContext, create_mcp_server
from hesabyar.interfaces.mcp.errors import MCP_ERROR_FORBIDDEN
from hesabyar.interfaces.mcp.provenance import (
    MODEL_INTERPRETATION_NOTICE,
    PROVENANCE_FIELDS,
)
from hesabyar.interfaces.mcp.tools_phase5 import PHASE5_TOOLS, register_phase5_tools
from hesabyar.interfaces.rest.app import create_app
from tests.mcp.conftest import INSTALLATION_ID, World
from tests.mcp.harness import authenticate_secret, connected_client, tool_text

#: The live Tryton test company provisioned by scripts/ci/setup-tryton.sh.
TRYTON_TEST_COMPANY = "HesabYar Adapter Test"

#: Cash account of the test company (contract-test convention).
CASH_ACCOUNT_CODE = "1.1.1"

#: All five report scopes, plus the base + company scopes.
FULL_SCOPES = (
    "mcp:connect",
    "company:read",
    "reports:read",
    "settlement:read",
    "cheques:read",
    "packaging:read",
    "cashbox:read",
)

#: Minimal valid arguments per tool (company A of the world).
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "get_settlement_status": {},
    "get_cheque_status": {},
    "get_packaging_costs": {},
    "get_cashbox_status": {"date_from": "2026-11-01", "date_to": "2026-11-30"},
    "get_cash_forecast": {"date_from": "2026-11-20", "date_to": "2027-03-20"},
}

#: Tools whose success path reads the Tryton engine (skip when unreachable).
ENGINE_TOOLS = frozenset({"get_cashbox_status", "get_cash_forecast"})

#: The four-tranche SnappPay-style schedule (contract-test convention).
FOUR_TRANCHES = (
    TrancheLeg(month_offset=1, payout_day=5, percent=Decimal("25")),
    TrancheLeg(month_offset=2, payout_day=5, percent=Decimal("25")),
    TrancheLeg(month_offset=3, payout_day=5, percent=Decimal("25")),
    TrancheLeg(month_offset=4, payout_day=5, percent=Decimal("25")),
)

SALE_DATE = datetime.date(2026, 11, 10)
GROSS_IRR = 100_000_000


# ---------------------------------------------------------------------------
# Keys + server + REST client
# ---------------------------------------------------------------------------


@dataclass
class Phase5Keys:
    """Secrets for the world's client, one per scope-denial case."""

    full: str
    no_settlement: str
    no_cheques: str
    no_packaging: str
    no_cashbox: str
    no_reports: str
    restricted_a: str

    def missing_scope_key(self, tool: str) -> str:
        """The key that lacks exactly ``tool``'s scope."""
        return {
            "get_settlement_status": self.no_settlement,
            "get_cheque_status": self.no_cheques,
            "get_packaging_costs": self.no_packaging,
            "get_cashbox_status": self.no_cashbox,
            "get_cash_forecast": self.no_reports,
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
            name="mcp phase5 test key",
            scopes=scopes,
            installation_id=INSTALLATION_ID,
            engine_company_id=engine_company_id,
        )
        session.commit()
        return issued.secret
    finally:
        session.close()


@pytest.fixture()
def keys(world: World, session_factory: Callable[[], Session], settings: AppSettings) -> Phase5Keys:
    """One full key, one key per missing scope, one company-restricted key."""
    full: list[str] = list(FULL_SCOPES)

    def _without(scope: str) -> list[str]:
        return [s for s in full if s != scope]

    return Phase5Keys(
        full=_issue_key(session_factory, settings, world, full),
        no_settlement=_issue_key(session_factory, settings, world, _without("settlement:read")),
        no_cheques=_issue_key(session_factory, settings, world, _without("cheques:read")),
        no_packaging=_issue_key(session_factory, settings, world, _without("packaging:read")),
        no_cashbox=_issue_key(session_factory, settings, world, _without("cashbox:read")),
        no_reports=_issue_key(session_factory, settings, world, _without("reports:read")),
        restricted_a=_issue_key(session_factory, settings, world, full, engine_company_id=world.company_a_id),
    )


@pytest.fixture(autouse=True)
def _clean_phase5_audit_rows(world: World, session_factory: Callable[[], Session]) -> Generator[None, None, None]:
    """Remove this world's Phase 5 audit rows (tool names are fixed)."""
    yield
    session = session_factory()
    try:
        session.execute(
            delete(AiAccessAuditModel).where(
                AiAccessAuditModel.tool.in_([name for name, _ in PHASE5_TOOLS]),
                AiAccessAuditModel.client_id == world.client_id,
            )
        )
        session.commit()
    finally:
        session.close()


def _phase5_server(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    bootstrap_secret: str,
) -> tuple[Any, McpContext]:
    """A server with the five Phase 5 tools registered (no echo tool)."""
    context = McpContext(settings=settings, session_factory=session_factory, transport="memory")
    context.bootstrap_client = authenticate_secret(session_factory, settings, bootstrap_secret)
    server = create_mcp_server(context)
    register_phase5_tools(server, context)
    return server, context


def _rest_client(settings: AppSettings) -> TestClient:
    """The real REST app (no overrides) over the same DB + engine."""
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def _rest_get(client: TestClient, secret: str, url: str) -> dict[str, Any]:
    """GET with the API key; the call must succeed."""
    response = client.get(url, headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200, f"GET {url}: {response.status_code} {response.text}"
    data = response.json()
    assert isinstance(data, dict)
    return data


def _rest_get_list(client: TestClient, secret: str, url: str) -> list[dict[str, Any]]:
    """GET a list endpoint; the call must succeed."""
    response = client.get(url, headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200, f"GET {url}: {response.status_code} {response.text}"
    data = response.json()
    assert isinstance(data, list)
    return data


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
    """Call a Phase 5 tool; return its parsed JSON payload."""

    async def _run() -> dict[str, Any]:
        server, _ = _phase5_server(world, session_factory, settings, secret)
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
    """Call a Phase 5 tool; it must fail as an is_error result."""

    async def _run() -> str:
        server, _ = _phase5_server(world, session_factory, settings, secret)
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
    """Call a Phase 5 tool; it must fail with an MCP protocol error."""

    async def _run() -> int:
        server, _ = _phase5_server(world, session_factory, settings, secret)
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


def _values_equal(left: Any, right: Any) -> bool:
    """JSON values equal, with Decimal-aware numeric comparison.

    MCP serializes Decimals as strings (never binary floats); REST
    serializes them as JSON numbers. ``"10"`` and ``10.0`` are the
    same figure — compare through Decimal, falling back to plain
    equality for non-numeric values.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float, str)) and isinstance(right, (int, float, str)):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except InvalidOperation:
            return left == right
    return left == right


def _assert_json_equal(left: Any, right: Any, *, path: str = "$") -> None:
    """Recursive JSON equality with Decimal-aware numbers."""
    if isinstance(left, dict) and isinstance(right, dict):
        assert set(left) == set(right), f"keys differ at {path}: {sorted(set(left) ^ set(right))}"
        for key in left:
            _assert_json_equal(left[key], right[key], path=f"{path}.{key}")
    elif isinstance(left, list) and isinstance(right, list):
        assert len(left) == len(right), f"length differs at {path}: {len(left)} != {len(right)}"
        for index, (l_item, r_item) in enumerate(zip(left, right)):
            _assert_json_equal(l_item, r_item, path=f"{path}[{index}]")
    else:
        assert _values_equal(left, right), f"values differ at {path}: {left!r} != {right!r}"


def _assert_rows_equal(
    mcp_rows: list[dict[str, Any]],
    rest_rows: list[dict[str, Any]],
    *,
    key: str,
    fields: list[str],
) -> None:
    """Two row lists carry the same figures (order-independent)."""
    assert len(mcp_rows) == len(rest_rows), f"row counts differ: MCP {len(mcp_rows)} vs REST {len(rest_rows)}"
    rest_by_key = {row[key]: row for row in rest_rows}
    assert len(rest_by_key) == len(rest_rows), f"duplicate {key} in REST rows"
    for mcp_row in mcp_rows:
        rest_row = rest_by_key.get(mcp_row[key])
        assert rest_row is not None, f"REST has no row with {key}={mcp_row[key]!r}"
        for field in fields:
            assert _values_equal(mcp_row[field], rest_row[field]), (
                f"{key}={mcp_row[key]!r} field {field}: MCP {mcp_row[field]!r} != REST {rest_row[field]!r}"
            )


# ---------------------------------------------------------------------------
# Live-Tryton company (engine-backed tests only)
# ---------------------------------------------------------------------------


@dataclass
class LiveCompany:
    """The world's mapping to the live Tryton test company."""

    engine_id: int
    row_id: str
    cash_account_id: int


@pytest.fixture()
def live_company(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> Generator[LiveCompany, None, None]:
    """Map the world's tenant to the live Tryton test company.

    Skips when no trytond is reachable (plain python-tests job) or
    the test company / cash account is not provisioned — the same
    skip-when-unreachable contract the contract tests use.
    """
    t = settings.tryton
    client = TrytonRestClient(t.base_url, t.database, t.api_key, timeout=t.timeout_seconds)
    try:
        if not client.ping():
            pytest.skip(f"trytond server not reachable at {t.base_url}")
        adapter = TrytonAccountingAdapter(client)
        engine_id: int | None = None
        for view in adapter.list_companies():
            if view.name == TRYTON_TEST_COMPANY:
                engine_id = view.ref.engine_id
                break
        if engine_id is None:
            pytest.skip(f"Test company {TRYTON_TEST_COMPANY!r} not found in the Tryton DB.")
        from hesabyar.domain.accounting.refs import CompanyRef

        account = adapter.get_account_by_code(CompanyRef(engine_id), CASH_ACCOUNT_CODE)
        if account is None:
            pytest.skip(f"Cash account {CASH_ACCOUNT_CODE} missing from the test company.")
    finally:
        client.close()

    session = session_factory()
    connection_id = str(uuid.uuid4())
    row_id = str(uuid.uuid4())
    try:
        connection = EngineConnectionModel(
            id=connection_id,
            name=f"mcp603-{uuid.uuid4().hex[:8]}",
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
        yield LiveCompany(engine_id=engine_id, row_id=row_id, cash_account_id=account.ref.engine_id)
    finally:
        session.close()
        cleanup = session_factory()
        try:
            cleanup.execute(delete(EngineCompanyModel).where(EngineCompanyModel.id == row_id))
            cleanup.execute(delete(EngineConnectionModel).where(EngineConnectionModel.id == connection_id))
            cleanup.commit()
        finally:
            cleanup.close()


def _require_tryton(settings: AppSettings) -> None:
    """Skip unless a trytond answers (for parametrized engine tests)."""
    t = settings.tryton
    client = TrytonRestClient(t.base_url, t.database, t.api_key, timeout=t.timeout_seconds)
    try:
        if not client.ping():
            pytest.skip(f"trytond server not reachable at {t.base_url}")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Seeders (each returns the ids its teardown must delete)
# ---------------------------------------------------------------------------


@dataclass
class SettlementSeed:
    channel_key: str
    channel_id: str
    payment_id: str
    tranche_ids: list[str]


def _seed_settlement(
    session_factory: Callable[[], Session],
    world: World,
    *,
    sale_date: datetime.date = SALE_DATE,
    mark_progress: bool = True,
) -> SettlementSeed:
    """One 100M payment + 4 tranches on a unique channel.

    With ``mark_progress`` (default): tranche 0 confirmed in full,
    tranche 1 received 50,000 short (variance -50,000). Without it
    all four tranches stay EXPECTED — for the cash forecast, where a
    received tranche is already cash and correctly leaves the
    forecast.
    """
    session = session_factory()
    try:
        repository = SqlAlchemySettlementRepository(session)
        registry = SqlAlchemyIntegrationsRepository(session)
        audit = SqlAlchemySettlementAudit(session)
        channels = ChannelRegistryService(registry)
        contracts = GatewayContractService(registry)
        settlement = SettlementCommandService(repository, registry, audit)
        channel_key = f"mcp603-{uuid.uuid4().hex[:8]}"
        channel = channels.create_channel(INSTALLATION_ID, channel_key, "کانال تست", "bnpl")
        contracts.create_version(
            INSTALLATION_ID,
            channel_key,
            valid_from=sale_date - datetime.timedelta(days=30),
            fee_percent=Decimal("10"),
            fee_vat_rate=Decimal("10"),
            settlement_model="tranches_4",
            tranche_legs=FOUR_TRANCHES,
            payout_window_start_day=1,
            payout_window_end_day=5,
            grace_days_after_due=3,
        )
        session.commit()
        result = settlement.record_payment(
            CreateGatewayPayment(
                installation_id=INSTALLATION_ID,
                channel_key=channel_key,
                gateway_transaction_id=f"TXN-603-{uuid.uuid4().hex[:8]}",
                gross_irr=GROSS_IRR,
                gateway_confirmed_at=datetime.datetime.combine(sale_date, datetime.time(12, 0), tzinfo=datetime.UTC),
                source=PaymentSource.STORE_WEBHOOK,
                store_order_id="ORD-603",
            )
        )
        session.commit()
        tranches = sorted(result.tranches, key=lambda t: t.tranche_index)
        assert len(tranches) == 4
        if mark_progress:
            # Tranche 0: confirmed in full. Tranche 1: received 50,000 short.
            repository.update_tranche(
                tranches[0].id,
                state=TrancheState.CONFIRMED,
                confirmed_net_irr=tranches[0].net_irr,
            )
            repository.update_tranche(
                tranches[1].id,
                state=TrancheState.RECEIVED,
                confirmed_net_irr=tranches[1].net_irr,
                received_net_irr=tranches[1].net_irr - 50_000,
                variance_irr=-50_000,
                variance_reason="short payment",
            )
            session.commit()
        return SettlementSeed(
            channel_key=channel_key,
            channel_id=channel.id,
            payment_id=result.payment.id,
            tranche_ids=[t.id for t in tranches],
        )
    finally:
        session.close()


def _clean_settlement(session_factory: Callable[[], Session], seed: SettlementSeed) -> None:
    session = session_factory()
    try:
        session.execute(delete(SettlementTrancheModel).where(SettlementTrancheModel.id.in_(seed.tranche_ids)))
        session.execute(
            delete(SettlementFeeRecordModel).where(SettlementFeeRecordModel.gateway_payment_id == seed.payment_id)
        )
        session.execute(delete(GatewayPaymentModel).where(GatewayPaymentModel.id == seed.payment_id))
        session.execute(delete(GatewayContractModel).where(GatewayContractModel.channel_id == seed.channel_id))
        session.execute(delete(PaymentChannelModel).where(PaymentChannelModel.id == seed.channel_id))
        session.commit()
    finally:
        session.close()


@dataclass
class ChequeSeed:
    cheque_ids: list[str]
    due_soon_ids: list[str]
    collateral_id: str
    in_clearing_id: str


def _cheque(
    world: World,
    *,
    cheque_id: str,
    kind: ChequeKind,
    purpose: ChequePurpose,
    state: ChequeState,
    amount_irr: int,
    due: datetime.date,
    deposit_date: datetime.date | None = None,
    secured_obligation: str | None = None,
) -> Cheque:
    return Cheque(
        id=cheque_id,
        tenant_id=world.tenant_id,
        installation_id=INSTALLATION_ID,
        company_engine_id=int(world.company_a_external_id),
        kind=kind,
        purpose=purpose,
        sayad_id=f"SAYAD-603-{uuid.uuid4().hex[:12]}",
        amount_irr=amount_irr,
        issue_date=FiscalDate(datetime.date(2026, 11, 1)),
        due_date=FiscalDate(due),
        issuer_name="صادرکننده",
        issuer_national_id="0012345678",
        issuer_bank="بانک ملی",
        payee_name="گیرنده",
        payee_national_id="0012345679",
        order_id="ORDER-603",
        customer_ref="CUST-603",
        sayad_registered=True,
        state=state,
        deposit_date=FiscalDate(deposit_date) if deposit_date is not None else None,
        secured_obligation=secured_obligation,
    )


def _seed_cheques(session_factory: Callable[[], Session], world: World) -> ChequeSeed:
    """Received-held (due soon), issued-held (due later), collateral, in-clearing."""
    today = datetime.date.today()
    session = session_factory()
    try:
        repository = SqlAlchemyChequeRepository(session)
        received = _cheque(
            world,
            cheque_id=f"ch-603-r-{uuid.uuid4().hex[:8]}",
            kind=ChequeKind.RECEIVED,
            purpose=ChequePurpose.PAYMENT,
            state=ChequeState.HELD,
            amount_irr=10_000_000,
            due=today + datetime.timedelta(days=10),
        )
        issued = _cheque(
            world,
            cheque_id=f"ch-603-i-{uuid.uuid4().hex[:8]}",
            kind=ChequeKind.ISSUED,
            purpose=ChequePurpose.PAYMENT,
            state=ChequeState.HELD,
            amount_irr=5_000_000,
            due=today + datetime.timedelta(days=40),
        )
        collateral = _cheque(
            world,
            cheque_id=f"ch-603-c-{uuid.uuid4().hex[:8]}",
            kind=ChequeKind.RECEIVED,
            purpose=ChequePurpose.COLLATERAL,
            state=ChequeState.HELD,
            amount_irr=20_000_000,
            due=today + datetime.timedelta(days=20),
            secured_obligation="loan-603",
        )
        in_clearing = _cheque(
            world,
            cheque_id=f"ch-603-x-{uuid.uuid4().hex[:8]}",
            kind=ChequeKind.RECEIVED,
            purpose=ChequePurpose.PAYMENT,
            state=ChequeState.IN_CLEARING,
            amount_irr=7_000_000,
            due=today - datetime.timedelta(days=2),
            deposit_date=today - datetime.timedelta(days=5),
        )
        for cheque in (received, issued, collateral, in_clearing):
            repository.create(cheque)
        session.commit()
        return ChequeSeed(
            cheque_ids=[c.id for c in (received, issued, collateral, in_clearing)],
            due_soon_ids=[received.id],
            collateral_id=collateral.id,
            in_clearing_id=in_clearing.id,
        )
    finally:
        session.close()


def _clean_cheques(session_factory: Callable[[], Session], seed: ChequeSeed) -> None:
    session = session_factory()
    try:
        session.execute(delete(ChequeModel).where(ChequeModel.id.in_(seed.cheque_ids)))
        session.commit()
    finally:
        session.close()


@dataclass
class PackagingSeed:
    order_ref: str
    material_id: str
    shipment_ids: list[str]
    consumption_ids: list[str]


def _seed_packaging(session_factory: Callable[[], Session], world: World) -> PackagingSeed:
    """Two shipment costs + two counted consumptions for one order (plus one uncounted)."""
    order_ref = f"ORD-603-{uuid.uuid4().hex[:6]}"
    session = session_factory()
    try:
        repository = SqlAlchemyPackagingRepository(session)
        material = PackagingMaterial(
            id=f"mat-603-{uuid.uuid4().hex[:8]}",
            tenant_id=world.tenant_id,
            company_id=world.company_a_id,
            tryton_product_id=900001,
            tryton_template_id=900001,
            name_fa="کارتن تست",
        )
        repository.save_material(material)
        shipment_ids: list[str] = []
        for invoiced, actual in ((120_000, 150_000), (80_000, 80_000)):
            record = ShipmentCostRecord(
                id=f"ship-603-{uuid.uuid4().hex[:8]}",
                tenant_id=world.tenant_id,
                company_id=world.company_a_id,
                order_ref=order_ref,
                method=ShipmentCostMethod.ON_ORDER,
                invoiced_irr=invoiced,
                actual_irr=actual,
                carrier_name="پست تست",
            )
            repository.save_shipment_cost(record)
            shipment_ids.append(record.id)
        # One shipment for another order: the order filter must exclude it.
        other = ShipmentCostRecord(
            id=f"ship-603-{uuid.uuid4().hex[:8]}",
            tenant_id=world.tenant_id,
            company_id=world.company_a_id,
            order_ref=f"ORD-OTHER-{uuid.uuid4().hex[:6]}",
            method=ShipmentCostMethod.ON_ORDER,
            invoiced_irr=999_000,
            actual_irr=999_000,
        )
        repository.save_shipment_cost(other)
        shipment_ids.append(other.id)
        consumption_ids: list[str] = []
        for quantity, unit_cost, move_id in (
            (Decimal("2"), Decimal("50000"), 910001),
            (Decimal("3"), Decimal("50000"), 910002),
            # No engine move: counted in the list, excluded from the
            # order-cost packaging sum (the service's own rule).
            (Decimal("1"), Decimal("50000"), None),
        ):
            record = ConsumptionRecord(
                id=f"cons-603-{uuid.uuid4().hex[:8]}",
                tenant_id=world.tenant_id,
                company_id=world.company_a_id,
                material_id=material.id,
                order_ref=order_ref,
                shipment_ref="SHIP-603",
                quantity=quantity,
                unit_cost_irr=unit_cost,
                total_cost_irr=int(quantity * unit_cost),
                rounding_delta_irr=0,
                idempotency_key=f"key-{uuid.uuid4().hex[:12]}",
                source=ConsumptionSource.MANUAL,
                tryton_move_id=move_id,
            )
            repository.save_consumption(record)
            consumption_ids.append(record.id)
        session.commit()
        return PackagingSeed(
            order_ref=order_ref,
            material_id=material.id,
            shipment_ids=shipment_ids,
            consumption_ids=consumption_ids,
        )
    finally:
        session.close()


def _clean_packaging(session_factory: Callable[[], Session], seed: PackagingSeed) -> None:
    session = session_factory()
    try:
        session.execute(delete(PackagingConsumptionModel).where(PackagingConsumptionModel.id.in_(seed.consumption_ids)))
        session.execute(delete(PackagingShipmentCostModel).where(PackagingShipmentCostModel.id.in_(seed.shipment_ids)))
        session.execute(delete(PackagingMaterialModel).where(PackagingMaterialModel.id == seed.material_id))
        session.commit()
    finally:
        session.close()


@dataclass
class CashboxSeed:
    box_id: str
    session_ids: list[str]


def _seed_cashbox(
    session_factory: Callable[[], Session],
    world: World,
    live: LiveCompany,
) -> CashboxSeed:
    """One cash box on the live 1.1.1 account + a shortage and an overage session."""
    session = session_factory()
    try:
        repository = SqlAlchemyCashBoxRepository(session)
        box = CashBox(
            id=f"box-603-{uuid.uuid4().hex[:8]}",
            tenant_id=world.tenant_id,
            company_id=live.row_id,
            name_fa="صندوق تست",
            kind=CashBoxKind.STORE,
            statement_journal_id=1,
            cash_account_id=live.cash_account_id,
            cash_account_code=CASH_ACCOUNT_CODE,
            engine_company_id=live.engine_id,
        )
        repository.save_cash_box(box)
        session_ids: list[str] = []
        for counted, book in ((900_000, 1_000_000), (1_050_000, 1_000_000)):
            count = CountSession(
                id=f"sess-603-{uuid.uuid4().hex[:8]}",
                tenant_id=world.tenant_id,
                company_id=live.row_id,
                cash_box_id=box.id,
                cashier="cashier-603",
                supervisor="supervisor-603",
                status=CountSessionStatus.ARCHIVED,
                book_balance_irr=book,
                counted_amount_irr=counted,
                engine_company_id=live.engine_id,
                created_at=datetime.datetime(2026, 11, 15, 10, 0, tzinfo=datetime.UTC),
            )
            repository.save_session(count)
            session_ids.append(count.id)
        session.commit()
        return CashboxSeed(box_id=box.id, session_ids=session_ids)
    finally:
        session.close()


def _clean_cashbox(session_factory: Callable[[], Session], seed: CashboxSeed) -> None:
    session = session_factory()
    try:
        session.execute(delete(CountSessionModel).where(CountSessionModel.id.in_(seed.session_ids)))
        session.execute(delete(CashBoxModel).where(CashBoxModel.id == seed.box_id))
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def test_tools_listed_with_narrow_schemas(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """All five tools are listed, with narrow input schemas and scope records."""

    async def _run() -> None:
        server, context = _phase5_server(world, session_factory, settings, keys.full)
        async with connected_client(server) as session:
            listed = await session.list_tools()
        # Subset, not equality: create_mcp_server also registers the 14
        # US-602 core tools on the same server (and the schema checks
        # below apply to the five Phase 5 tools only — e.g.
        # list_companies takes no company argument by design).
        assert {name for name, _ in PHASE5_TOOLS} <= {tool.name for tool in listed.tools}
        by_name = {tool.name: tool for tool in listed.tools}
        for name, _ in PHASE5_TOOLS:
            tool = by_name[name]
            assert "ctx" not in tool.input_schema["properties"]
            assert "company" in tool.input_schema["properties"]
            assert "company" in tool.input_schema["required"]
        assert set(by_name["get_settlement_status"].input_schema["properties"]) == {
            "company",
            "channel_key",
            "state",
            "sales_period",
            "due_from",
            "due_to",
            "include_tranches",
            "limit",
        }
        assert set(by_name["get_cheque_status"].input_schema["properties"]) == {
            "company",
            "kind",
            "state",
            "due_within_days",
            "include_collateral",
            "limit",
        }
        assert set(by_name["get_packaging_costs"].input_schema["properties"]) == {
            "company",
            "order_ref",
            "include_consumptions",
            "include_shipment_costs",
            "limit",
        }
        assert set(by_name["get_cashbox_status"].input_schema["properties"]) == {
            "company",
            "date_from",
            "date_to",
            "cash_box_id",
            "include_boxes",
            "include_sessions",
            "limit",
        }
        assert set(by_name["get_cash_forecast"].input_schema["properties"]) == {
            "company",
            "date_from",
            "date_to",
        }
        assert set(by_name["get_cashbox_status"].input_schema["required"]) == {
            "company",
            "date_from",
            "date_to",
        }
        assert set(by_name["get_cash_forecast"].input_schema["required"]) == {
            "company",
            "date_from",
            "date_to",
        }
        for name, scopes in PHASE5_TOOLS:
            assert context.registry.scopes_for_tool(name) == frozenset(scopes)

    asyncio.run(_run())


# The engine-backed tools need a company that exists in Tryton; the
# plain world's companies (301/302) do not. Their empty state is
# covered by the live-company tests below instead.
_NON_ENGINE_TOOLS = [name for name, _ in PHASE5_TOOLS if name not in ENGINE_TOOLS]


@pytest.mark.parametrize("tool", _NON_ENGINE_TOOLS)
def test_empty_state_is_honest_empty(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
    tool: str,
) -> None:
    """No data: honest empty figures, complete provenance, no floats."""
    company = world.company_a_id
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.full,
        tool,
        {"company": company, **TOOL_ARGS[tool]},
    )
    _assert_no_floats(payload)
    _assert_provenance(payload, company_engine_id=int(world.company_a_external_id))
    assert payload["company"]["engine_id"] == int(world.company_a_external_id)
    assert payload["company"]["id"] == company
    if tool == "get_settlement_status":
        assert payload["totals"]["tranche_count"] == 0
        assert payload["totals"]["expected_net_irr"] == 0
        assert payload["totals"]["received_net_irr"] == 0
        assert payload["tranches"] == []
    elif tool == "get_cheque_status":
        assert payload["totals"]["registry_count"] == 0
        assert payload["totals"]["due_soon_count"] == 0
        assert payload["totals"]["in_clearing_count"] == 0
        assert payload["registry"] == []
        assert payload["collateral"]["received_total_irr"] == 0
        assert payload["collateral"]["cheques"] == []
    elif tool == "get_packaging_costs":
        assert payload["totals"]["shipment_cost_count"] == 0
        assert payload["totals"]["consumption_count"] == 0
        assert payload["order_cost"] is None
        assert payload["shipment_costs"] == []
        assert payload["consumptions"] == []


def test_empty_cashbox_status_live_company(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
    live_company: LiveCompany,
) -> None:
    """No boxes for the live company: an honest empty report (Tryton-gated)."""
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.full,
        "get_cashbox_status",
        {"company": live_company.row_id, "date_from": "2026-11-01", "date_to": "2026-11-30"},
    )
    _assert_no_floats(payload)
    _assert_provenance(payload, company_engine_id=live_company.engine_id)
    assert payload["report"]["lines"] == []
    assert payload["totals"]["total_shortage_irr"] == 0
    assert payload["totals"]["total_overage_irr"] == 0
    assert payload["boxes"] == []
    assert payload["sessions"] == []


def test_empty_cash_forecast_shape_live_company(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
    live_company: LiveCompany,
) -> None:
    """Forecast shape holds even with engine-side invoices (Tryton-gated).

    The live Tryton company may hold posted invoices from other
    contract tests, so this asserts structure — never emptiness:
    items sorted oldest first, totals equal to the item sums, all six
    horizon buckets present, all eight source counts present.
    """
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.full,
        "get_cash_forecast",
        {"company": live_company.row_id, "date_from": "2026-11-20", "date_to": "2027-03-20"},
    )
    _assert_no_floats(payload)
    _assert_provenance(payload, company_engine_id=live_company.engine_id)
    items = payload["items"]
    assert [item["date"] for item in items] == sorted(item["date"] for item in items)
    totals = payload["totals"]
    assert totals["item_count"] == len(items)
    assert totals["inflow_confirmed_irr"] == sum(
        i["amount_irr"] for i in items if i["direction"] == "inflow" and i["bucket"] == "confirmed"
    )
    assert totals["outflow_confirmed_irr"] == sum(
        i["amount_irr"] for i in items if i["direction"] == "outflow" and i["bucket"] == "confirmed"
    )
    assert totals["inflow_forecast_irr"] == 0  # no expected-sales source yet, honestly empty
    assert totals["outflow_forecast_irr"] == 0
    assert [b["key"] for b in payload["horizon"]] == [
        "overdue",
        "0_30",
        "31_60",
        "61_90",
        "91_120",
        "beyond_120",
    ]
    assert set(payload["source_counts"]) == {
        "settlement_tranche",
        "installment_leg",
        "cheque_received",
        "cheque_issued",
        "invoice_receivable",
        "invoice_payable",
        "packaging_purchase",
        "expected_sale",
    }
    assert payload["freshness"]["freshness_indicator"] == "live"
    assert payload["freshness"]["source"] == "hesabyar-registry+tryton:cash-forecast"


# ---------------------------------------------------------------------------
# Scope denial + cross-company fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", [name for name, _ in PHASE5_TOOLS])
def test_tool_scope_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
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


@pytest.mark.parametrize("tool", [name for name, _ in PHASE5_TOOLS])
def test_cross_company_fails_closed(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
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
    keys: Phase5Keys,
) -> None:
    """A company id naming no row of the tenant is denied, not trusted."""
    code = _call_tool_denied_code(
        world,
        session_factory,
        settings,
        keys.full,
        "get_settlement_status",
        {"company": "00000000-0000-0000-0000-000000000000"},
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows_for_client(session_factory, "get_settlement_status", world.client_id)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].error_code == "company_not_found"


def test_restricted_key_can_call_own_company(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """The same restricted key calling with company A succeeds."""
    payload = _call_tool(
        world,
        session_factory,
        settings,
        keys.restricted_a,
        "get_cheque_status",
        {"company": world.company_a_id},
    )
    assert payload["company"]["id"] == world.company_a_id
    rows = _audit_rows_for_client(session_factory, "get_cheque_status", world.client_id)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].company_id == world.company_a_id


# ---------------------------------------------------------------------------
# Error shapes
# ---------------------------------------------------------------------------


def test_unknown_channel_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """An unknown channel key fails as an is_error result naming the channel."""
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_settlement_status",
        {"company": world.company_a_id, "channel_key": "no-such-channel"},
    )
    assert "no-such-channel" in message


def test_invalid_tranche_state_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_settlement_status",
        {"company": world.company_a_id, "state": "bogus"},
    )
    assert "bogus" in message


def test_invalid_cheque_filters_are_tool_errors(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_cheque_status",
        {"company": world.company_a_id, "kind": "sideways"},
    )
    assert "sideways" in message
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_cheque_status",
        {"company": world.company_a_id, "due_within_days": 400},
    )
    assert "due_within_days" in message


def test_invalid_limit_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_packaging_costs",
        {"company": world.company_a_id, "limit": 0},
    )
    assert "limit" in message


def test_reversed_forecast_range_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """A reversed range fails before any engine read (no Tryton needed)."""
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_cash_forecast",
        {"company": world.company_a_id, "date_from": "2026-12-01", "date_to": "2026-11-01"},
    )
    assert "date_to" in message


def test_reversed_cashbox_range_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """A reversed range fails before any engine read (no Tryton needed)."""
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_cashbox_status",
        {"company": world.company_a_id, "date_from": "2026-12-01", "date_to": "2026-11-01"},
    )
    assert "date_to" in message


def test_missing_required_argument_is_tool_error(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """A missing required argument fails as an is_error result (SDK validation)."""
    message = _call_tool_error(
        world,
        session_factory,
        settings,
        keys.full,
        "get_cash_forecast",
        {"company": world.company_a_id, "date_from": "2026-11-20"},
    )
    assert "date_to" in message


# ---------------------------------------------------------------------------
# MCP-vs-REST equality
# ---------------------------------------------------------------------------


def test_settlement_mcp_equals_rest(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """Same channel via MCP and via REST: same tranches, same figures."""
    seed = _seed_settlement(session_factory, world)
    try:
        payload = _call_tool(
            world,
            session_factory,
            settings,
            keys.full,
            "get_settlement_status",
            {"company": world.company_a_id, "channel_key": seed.channel_key},
        )
        _assert_no_floats(payload)
        _assert_provenance(payload, company_engine_id=int(world.company_a_external_id))
        rest = _rest_client(settings)
        page = _rest_get(
            rest,
            keys.full,
            f"/api/v1/settlements/tranches?channel_key={seed.channel_key}&limit=100",
        )
        rest_tranches = page["items"]
        assert page["next_cursor"] is None
        assert len(rest_tranches) == 4
        _assert_rows_equal(
            payload["tranches"],
            rest_tranches,
            key="id",
            fields=[
                "tranche_index",
                "tranche_count",
                "sales_period_jalali",
                "expected_window_start",
                "expected_date",
                "gross_irr",
                "fee_irr",
                "fee_vat_irr",
                "net_irr",
                "confirmed_net_irr",
                "received_net_irr",
                "outstanding_net_irr",
                "state",
                "variance_irr",
                "variance_reason",
            ],
        )
        # Totals are the sums over the same rows.
        assert payload["totals"]["tranche_count"] == 4
        assert payload["totals"]["expected_net_irr"] == sum(t["net_irr"] for t in rest_tranches)
        assert payload["totals"]["confirmed_net_irr"] == sum(t["confirmed_net_irr"] for t in rest_tranches)
        assert payload["totals"]["received_net_irr"] == sum(t["received_net_irr"] for t in rest_tranches)
        assert payload["totals"]["outstanding_net_irr"] == sum(t["outstanding_net_irr"] for t in rest_tranches)
        assert payload["totals"]["variance_irr"] == -50_000
        assert payload["totals"]["by_state"] == {
            "confirmed": 1,
            "received": 1,
            "expected": 2,
        }
        assert payload["returned_tranche_count"] == 4
    finally:
        _clean_settlement(session_factory, seed)


def _jalali_to_gregorian(jalali: str) -> datetime.date:
    """Parse a 'YYYY-MM-DD' Jalali date string to Gregorian."""
    year, month, day = (int(part) for part in jalali.split("-"))
    return jdatetime.date(year, month, day).togregorian()


def test_cheques_mcp_equals_rest(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """Same company via MCP and via REST: same registry, collateral, aging."""
    seed = _seed_cheques(session_factory, world)
    try:
        payload = _call_tool(
            world,
            session_factory,
            settings,
            keys.full,
            "get_cheque_status",
            {"company": world.company_a_id, "due_within_days": 30},
        )
        _assert_no_floats(payload)
        _assert_provenance(payload, company_engine_id=int(world.company_a_external_id))
        rest = _rest_client(settings)
        company = world.company_a_id
        rest_registry = _rest_get_list(
            rest,
            keys.full,
            f"/api/v1/cheques?company={company}&purpose=payment&limit=100",
        )
        # Registry: commercial cheques only — the collateral cheque is absent.
        assert seed.collateral_id not in {c["id"] for c in payload["registry"]}
        assert len(rest_registry) == 3
        _assert_rows_equal(
            payload["registry"],
            rest_registry,
            key="id",
            fields=["state", "kind", "purpose", "amount_irr", "sayad_id", "due_date"],
        )
        assert payload["totals"]["registry_count"] == 3
        assert payload["totals"]["registry_total_irr"] == 22_000_000
        assert payload["totals"]["by_kind"]["received"]["count"] == 2
        assert payload["totals"]["by_kind"]["issued"]["count"] == 1
        # Due-soon cross-check: the REST registry rows whose Gregorian
        # due date falls in [today, today+30] are exactly the MCP due-soon ids.
        today = datetime.date.today()
        expected_due_soon = {
            row["id"]
            for row in rest_registry
            if today <= _jalali_to_gregorian(row["due_date"]) <= today + datetime.timedelta(days=30)
        }
        assert {c["id"] for c in payload["due_soon"]} == expected_due_soon
        assert expected_due_soon == set(seed.due_soon_ids)
        assert payload["totals"]["due_soon_total_irr"] == 10_000_000
        # Collateral (report 6, memorandum-only).
        collateral = _rest_get(rest, keys.full, f"/api/v1/cheques/collateral-summary?company={company}")
        assert payload["collateral"]["received_total_irr"] == collateral["received_total_irr"] == 20_000_000
        assert payload["collateral"]["issued_total_irr"] == collateral["issued_total_irr"] == 0
        assert (
            {c["id"] for c in payload["collateral"]["cheques"]}
            == {c["id"] for c in collateral["cheques"]}
            == {seed.collateral_id}
        )
        # In-clearing aging (report 4).
        aging = _rest_get_list(
            rest,
            keys.full,
            f"/api/v1/cheques/reports/in-clearing-aging?company={company}",
        )
        assert len(aging) == 1
        _assert_rows_equal(
            payload["in_clearing_aging"],
            aging,
            key="cheque_id",
            fields=["sayad_id", "days_in_clearing", "stale", "window_days", "window_unset"],
        )
        assert payload["in_clearing_aging"][0]["days_in_clearing"] == 5
        assert payload["totals"]["in_clearing_count"] == 1
    finally:
        _clean_cheques(session_factory, seed)


def test_packaging_mcp_equals_rest(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
) -> None:
    """Same order via MCP and via REST: same cost, shipments, consumptions."""
    seed = _seed_packaging(session_factory, world)
    try:
        payload = _call_tool(
            world,
            session_factory,
            settings,
            keys.full,
            "get_packaging_costs",
            {"company": world.company_a_id, "order_ref": seed.order_ref},
        )
        _assert_no_floats(payload)
        _assert_provenance(payload, company_engine_id=int(world.company_a_external_id))
        rest = _rest_client(settings)
        company = world.company_a_id
        order_cost = _rest_get(
            rest,
            keys.full,
            f"/api/v1/packaging/orders/{seed.order_ref}/cost?company_id={company}",
        )
        _assert_json_equal(payload["order_cost"], order_cost)
        # The figures: no cost centre (goods/revenue 0), packaging from
        # the two moved consumptions only (2×50k + 3×50k), shipment
        # invoiced 200k / actual 230k / absorbed 30k.
        assert payload["order_cost"]["goods_cost_irr"] == 0
        assert payload["order_cost"]["packaging_cost_irr"] == 250_000
        assert payload["order_cost"]["shipment_invoiced_irr"] == 200_000
        assert payload["order_cost"]["shipment_actual_irr"] == 230_000
        assert payload["order_cost"]["shipment_absorbed_irr"] == 30_000
        assert payload["order_cost"]["consumption_count"] == 2
        shipments = _rest_get(
            rest,
            keys.full,
            f"/api/v1/packaging/shipment-costs?company_id={company}&order_ref={seed.order_ref}",
        )["items"]
        assert len(shipments) == 2  # the other order's shipment is excluded
        _assert_rows_equal(
            payload["shipment_costs"],
            shipments,
            key="id",
            fields=["order_ref", "method", "invoiced_irr", "actual_irr", "absorbed_irr"],
        )
        assert payload["totals"]["shipment_invoiced_irr"] == 200_000
        assert payload["totals"]["shipment_actual_irr"] == 230_000
        assert payload["totals"]["shipment_absorbed_irr"] == 30_000
        consumptions = _rest_get(
            rest,
            keys.full,
            f"/api/v1/packaging/consumptions?company_id={company}&order_ref={seed.order_ref}",
        )["items"]
        assert len(consumptions) == 3  # including the move-less one
        _assert_rows_equal(
            payload["consumptions"],
            consumptions,
            key="id",
            fields=["order_ref", "material_id", "quantity", "unit_cost_irr", "total_cost_irr"],
        )
        assert payload["totals"]["consumption_total_cost_irr"] == 300_000
    finally:
        _clean_packaging(session_factory, seed)


def test_cashbox_mcp_equals_rest(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
    live_company: LiveCompany,
) -> None:
    """Same company + period via MCP and via REST: same report (Tryton-gated)."""
    seed = _seed_cashbox(session_factory, world, live_company)
    try:
        args = {
            "company": live_company.row_id,
            "date_from": "2026-11-01",
            "date_to": "2026-11-30",
        }
        payload = _call_tool(world, session_factory, settings, keys.full, "get_cashbox_status", args)
        _assert_no_floats(payload)
        _assert_provenance(payload, company_engine_id=live_company.engine_id)
        rest = _rest_client(settings)
        report = _rest_get(
            rest,
            keys.full,
            f"/api/v1/cash-boxes/report?company={live_company.row_id}&date_from=2026-11-01&date_to=2026-11-30",
        )
        assert len(report["lines"]) == 1
        _assert_rows_equal(
            payload["report"]["lines"],
            report["lines"],
            key="cash_box_id",
            fields=[
                "cash_box_name_fa",
                "cash_account_code",
                "opening_book_balance_irr",
                "closing_book_balance_irr",
                "receipts_irr",
                "payments_irr",
                "session_count",
                "shortage_irr",
                "overage_irr",
                "net_variance_irr",
                "recorded_sales_irr",
                "possible_unrecorded_sales_irr",
            ],
        )
        for field in (
            "total_shortage_irr",
            "total_overage_irr",
            "total_possible_unrecorded_sales_irr",
        ):
            assert payload["report"][field] == report[field]
            assert payload["totals"][field] == report[field]
        line = payload["report"]["lines"][0]
        assert line["session_count"] == 2
        assert line["shortage_irr"] == 100_000
        assert line["overage_irr"] == 50_000
        assert line["net_variance_irr"] == -50_000
        assert line["possible_unrecorded_sales_irr"] == 0
        boxes = _rest_get(rest, keys.full, f"/api/v1/cash-boxes?company={live_company.row_id}")["items"]
        assert len(boxes) == 1
        _assert_rows_equal(
            payload["boxes"],
            boxes,
            key="id",
            fields=["name_fa", "cash_account_code", "book_balance_irr"],
        )
        assert {s["variance_irr"] for s in payload["sessions"]} == {-100_000, 50_000}
    finally:
        _clean_cashbox(session_factory, seed)


def test_cash_forecast_mcp_equals_rest(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    keys: Phase5Keys,
    live_company: LiveCompany,
) -> None:
    """Same company + range via MCP and via REST: same forecast (Tryton-gated).

    The range is relative to today (the tranche dues derive from a
    sale ten days ago), so the test holds whenever it runs. The
    seeded tranches stay EXPECTED — a received tranche is already
    cash and correctly leaves the forecast.
    """
    today = datetime.date.today()
    date_from = today.isoformat()
    date_to = (today + datetime.timedelta(days=150)).isoformat()
    settlement_seed = _seed_settlement(
        session_factory,
        world,
        sale_date=today - datetime.timedelta(days=10),
        mark_progress=False,
    )
    session = session_factory()
    cheque_id = f"ch-603-f-{uuid.uuid4().hex[:8]}"
    try:
        repository = SqlAlchemyChequeRepository(session)
        cheque = _cheque(
            world,
            cheque_id=cheque_id,
            kind=ChequeKind.RECEIVED,
            purpose=ChequePurpose.PAYMENT,
            state=ChequeState.HELD,
            amount_irr=10_000_000,
            due=today + datetime.timedelta(days=10),
        )
        repository.create(cheque)
        session.commit()
        sayad_id = cheque.sayad_id

        args = {"company": live_company.row_id, "date_from": date_from, "date_to": date_to}
        payload = _call_tool(world, session_factory, settings, keys.full, "get_cash_forecast", args)
        _assert_no_floats(payload)
        _assert_provenance(payload, company_engine_id=live_company.engine_id)
        rest = _rest_client(settings)
        forecast = _rest_get(
            rest,
            keys.full,
            f"/api/v1/reports/cash-forecast?company={live_company.row_id}&date_from={date_from}&date_to={date_to}",
        )
        # Same figures through both interfaces (timestamps excluded:
        # each call stamps its own generation time).
        for field in (
            "company_id",
            "date_from",
            "date_to",
            "as_of",
            "items",
            "months",
            "horizon",
            "totals",
            "source_counts",
            "skipped_undated",
        ):
            _assert_json_equal(payload[field], forecast[field], path=f"$.{field}")
        assert payload["freshness"]["source"] == forecast["freshness"]["source"]
        assert payload["freshness"]["calculation_version"] == forecast["freshness"]["calculation_version"]
        # The seeded records are in the forecast with the right amounts.
        by_ref = {item["ref"]: item for item in payload["items"]}
        for tranche_id in settlement_seed.tranche_ids:
            item = by_ref.get(f"tranche:{tranche_id}")
            assert item is not None, f"missing forecast item for tranche {tranche_id}"
            assert item["source"] == "settlement_tranche"
            assert item["direction"] == "inflow"
            assert item["bucket"] == "confirmed"
            assert item["amount_irr"] == 22_250_000
        cheque_item = by_ref.get(f"cheque:{sayad_id}")
        assert cheque_item is not None
        assert cheque_item["source"] == "cheque_received"
        assert cheque_item["amount_irr"] == 10_000_000
        assert payload["source_counts"]["settlement_tranche"] == 4
        assert payload["source_counts"]["cheque_received"] == 1
    finally:
        session.close()
        _clean_cheques(
            session_factory,
            ChequeSeed(
                cheque_ids=[cheque_id],
                due_soon_ids=[],
                collateral_id="",
                in_clearing_id="",
            ),
        )
        _clean_settlement(session_factory, settlement_seed)
