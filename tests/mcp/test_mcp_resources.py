"""MCP resource tests (US-604, spec §13.6).

Two layers:

- **Harness tests** (real SDK client + server over in-memory streams,
  real test Postgres): scope denial per resource, the cross-company
  SECURITY property (a client scoped to company A cannot read company
  B's resource — it fails closed), the other-tenant property, list +
  audit shape, and end-to-end reads of the session-backed resources
  (standards, tax-policy, integration-compliance-status, and a seeded
  report snapshot). The engine-backed resources (chart-of-accounts,
  fiscal-periods) cannot read end-to-end here — the world's companies
  do not exist in Tryton — so their payload logic is covered by the
  unit tests below, and live-Tryton reads belong to the US-606
  conformance suite.
- **Unit tests** (stub services, no DB, no engine): every reader's
  payload shape, active-only filtering, Decimal-as-string amounts,
  snapshot company checks, and the no-secrets property of the
  integration summary.

Tests are sync functions driving the async harness with
``asyncio.run()`` (no pytest-asyncio in this repo).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import select
from sqlalchemy.orm import Session

from hesabyar.application.errors import (
    IntegrationContractNotFoundError,
    ReportValidationRunNotFoundError,
)
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, FiscalPeriodRef, FiscalYearRef
from hesabyar.domain.accounting.value_objects import (
    AccountView,
    CompanyView,
    FiscalPeriodView,
    FiscalYearView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.integrations.models import (
    BankConnection,
    ChannelStatus,
    GatewayContract,
    PaymentChannel,
    Store,
)
from hesabyar.domain.moadian.models import (
    DocumentStatus,
    MoadianDocument,
    MoadianEnvironment,
    MoadianProfile,
    SenderMode,
)
from hesabyar.domain.reports.models import ReportSnapshot, ReportValidationRun
from hesabyar.domain.standards.models import (
    RegistryCoverage,
    RuleId,
    RuleOutcome,
    RuleSeverity,
    RunCoverage,
    StandardCode,
    StandardRevision,
    StandardSource,
    ValidationResultRecord,
    ValidationRunRecord,
    VerificationStatus,
)
from hesabyar.domain.tax_policy.models import (
    RuleKey,
    TaxPolicyVersion,
    TaxSource,
    ValueUnit,
)
from hesabyar.infrastructure.auth import ApiKeyCrypto, ApiKeyService
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.auth.scopes import KNOWN_SCOPES
from hesabyar.infrastructure.config import AppSettings
from hesabyar.infrastructure.persistence.postgres.models import (
    AiAccessAuditModel,
    ApiClientModel,
    EngineCompanyModel,
    ReportSnapshotModel,
    ValidationRunModel,
)
from hesabyar.interfaces.mcp import McpContext, create_mcp_server
from hesabyar.interfaces.mcp.errors import (
    MCP_ERROR_FORBIDDEN,
    MCP_ERROR_INTERNAL,
    MCP_ERROR_UNAUTHENTICATED,
)
from hesabyar.interfaces.mcp.prompts import register_prompts
from hesabyar.interfaces.mcp.resources import (
    RESOURCE_DEFINITIONS,
    ResourceDefinition,
    ResourceRequest,
    _read_chart_of_accounts,
    _read_fiscal_periods,
    _read_integration_compliance_status,
    _read_report_snapshot,
    _read_standards,
    _read_tax_policy,
    register_resources,
)
from hesabyar.interfaces.mcp.services import McpServices
from tests.mcp.conftest import INSTALLATION_ID, World
from tests.mcp.harness import authenticate_secret, connected_client

#: Every scope any US-604 resource or prompt needs (plus the base scope).
ALL_US604_SCOPES: tuple[str, ...] = (
    "mcp:connect",
    "account:read",
    "banking:read",
    "company:read",
    "compliance:read",
    "integrations:read",
    "ledger:read",
    "reports:read",
    "review:read",
    "standards:read",
    "standards:validate",
    "tax:read",
)

_DUMMY_RUN_ID = "00000000-0000-0000-0000-000000000000"


# -- helpers --------------------------------------------------------------------


def _issue_key(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    world: World,
    scopes: list[str],
    *,
    engine_company_id: str | None = None,
) -> tuple[str, str]:
    """Issue a key under the world's client; return (secret, key id).

    The key belongs to the world's client, so the world teardown
    (tenant cascade) removes it; audit rows carry the tenant id, so
    they are removed the same way.
    """
    session = session_factory()
    try:
        service = ApiKeyService(ApiKeyCrypto(settings.auth.api_key_pepper))
        client = session.get(ApiClientModel, world.client_id)
        assert client is not None
        issued = service.issue_key(
            session,
            client=client,
            name="us604 test key",
            scopes=scopes,
            installation_id=INSTALLATION_ID,
            engine_company_id=engine_company_id,
        )
        session.commit()
        return issued.secret, issued.record.id
    finally:
        session.close()


def _make_us604_server(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None = None,
) -> tuple[Any, McpContext]:
    """Build a server (+ context) with US-604 resources + prompts."""
    context = McpContext(
        settings=settings,
        session_factory=session_factory,
        transport="memory",
    )
    if bootstrap_secret is not None:
        context.bootstrap_client = authenticate_secret(
            session_factory, settings, bootstrap_secret
        )
    server = create_mcp_server(context)
    register_resources(server, context)
    register_prompts(server, context)
    return server, context


def _uri(definition: ResourceDefinition, company: str, run_id: str = _DUMMY_RUN_ID) -> str:
    """Fill a resource template's parameters."""
    return definition.uri_template.replace("{company}", company).replace("{run_id}", run_id)


def _audit_rows(
    session_factory: Callable[[], Session], *, key_id: str, tool: str
) -> list[AiAccessAuditModel]:
    """This key's audit rows for ``tool``, oldest first.

    Filtered by key id (unique per world) as well as tool: prompt
    names are fixed strings shared across parallel CI jobs, so the
    tool name alone is not a safe filter.
    """
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


def _read_denied_code(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str | None,
    uri: str,
) -> int:
    """Read a resource; return the MCPError code (the read must fail)."""

    async def _run() -> int:
        server, _ = _make_us604_server(
            session_factory, settings, bootstrap_secret=bootstrap_secret
        )
        async with connected_client(server) as session:
            with pytest.raises(MCPError) as excinfo:
                await session.read_resource(uri)
            return excinfo.value.code

    return asyncio.run(_run())


def _read_text(
    session_factory: Callable[[], Session],
    settings: AppSettings,
    *,
    bootstrap_secret: str,
    uri: str,
) -> str:
    """Read a resource; return its text content."""

    async def _run() -> str:
        server, _ = _make_us604_server(
            session_factory, settings, bootstrap_secret=bootstrap_secret
        )
        async with connected_client(server) as session:
            result: Any = await session.read_resource(uri)
            assert result.contents, "resource read has no content"
            return str(result.contents[0].text)

    return asyncio.run(_run())


# -- registration + the no-unfiltered-resource property --------------------------


def test_every_resource_template_is_company_scoped() -> None:
    """Every registered template carries a {company} parameter.

    This is the structural half of 'no unfiltered all-company
    resource': a template without the company segment cannot be
    registered, because the table is the only source of templates.
    """
    assert len(RESOURCE_DEFINITIONS) == 6
    for definition in RESOURCE_DEFINITIONS:
        assert "{company}" in definition.uri_template, definition.name
        assert definition.scopes, definition.name
        # Every scope a resource needs must exist in the catalog, or no
        # key could ever carry it.
        assert set(definition.scopes) <= KNOWN_SCOPES, definition.name


def test_register_resources_records_scopes() -> None:
    """Registration records every template's scopes in the registry."""

    def _no_session() -> Session:
        raise RuntimeError("no session in a registration-only test")

    context = McpContext(
        settings=AppSettings(),
        session_factory=_no_session,
    )
    server = create_mcp_server(context)
    register_resources(server, context)
    register_prompts(server, context)
    for definition in RESOURCE_DEFINITIONS:
        assert context.registry.scopes_for_resource(definition.uri_template) == frozenset(
            definition.scopes
        )


def test_mcp_services_exposes_integrations() -> None:
    """The container wires the integrations service (US-604 status resource)."""
    from hesabyar.application.queries.integrations import IntegrationsQueryService

    services = McpServices(Session(), AppSettings())
    assert isinstance(services.integrations, IntegrationsQueryService)
    assert services.integrations is services.integrations
    services.close()


# -- harness: list ----------------------------------------------------------------


def test_resource_templates_listed(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """resources/templates/list returns all six templates."""
    secret, _ = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))

    async def _run() -> set[str]:
        server, _ = _make_us604_server(
            session_factory, settings, bootstrap_secret=secret
        )
        async with connected_client(server) as session:
            templates = await session.list_resource_templates()
            return {t.uri_template for t in templates.resource_templates}

    assert asyncio.run(_run()) == {d.uri_template for d in RESOURCE_DEFINITIONS}


# -- harness: scope denial per resource -------------------------------------------


@pytest.mark.parametrize(
    "definition", RESOURCE_DEFINITIONS, ids=[d.name for d in RESOURCE_DEFINITIONS]
)
def test_resource_scope_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    definition: ResourceDefinition,
) -> None:
    """A key missing one required scope cannot read the resource."""
    missing = definition.scopes[0]
    scopes = [scope for scope in ALL_US604_SCOPES if scope != missing]
    secret, key_id = _issue_key(session_factory, settings, world, scopes)
    uri = _uri(definition, world.company_a_id)
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=secret, uri=uri
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].kind == "resource"
    # US-606: resource scopes are enforced centrally by the middleware
    # (template-aware registry matching), so the denial audits as
    # "missing_scope" — the same code as a tool/prompt scope denial.
    # The handler check stays as defence in depth.
    assert rows[0].error_code == "missing_scope"
    # The audit row names the company the denied call touched.
    assert rows[0].company_id == world.company_a_id


def test_resource_without_identity_fails_unauthenticated(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """No bootstrap client and no bearer token: the read is refused."""
    uri = _uri(RESOURCE_DEFINITIONS[0], world.company_a_id)
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=None, uri=uri
    )
    assert code == MCP_ERROR_UNAUTHENTICATED


def test_resource_key_without_mcp_connect_denied(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A key without the mcp:connect base scope cannot read any resource."""
    uri = _uri(RESOURCE_DEFINITIONS[0], world.company_a_id)
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=world.key_no_mcp_secret, uri=uri
    )
    assert code == MCP_ERROR_FORBIDDEN


# -- harness: the cross-company SECURITY property ----------------------------------


@pytest.mark.parametrize(
    "definition", RESOURCE_DEFINITIONS, ids=[d.name for d in RESOURCE_DEFINITIONS]
)
def test_resource_cross_company_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    definition: ResourceDefinition,
) -> None:
    """SECURITY: a client scoped to company A cannot read company B's resource.

    The key carries every scope, so the denial is purely the company
    grant check — and it fails closed before any service is touched.
    """
    secret, key_id = _issue_key(
        session_factory,
        settings,
        world,
        list(ALL_US604_SCOPES),
        engine_company_id=world.company_a_id,
    )
    uri = _uri(definition, world.company_b_id)
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=secret, uri=uri
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].kind == "resource"
    assert rows[0].error_code == "forbidden"
    # US-606: the audit row still names the company the denied call
    # tried to touch (audit extraction is not authorization).
    assert rows[0].company_id == world.company_b_id


def test_resource_other_tenant_company_denied(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A company of another tenant looks exactly like a nonexistent one."""
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[0], world.company_c_id)
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=secret, uri=uri
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"


def test_resource_unknown_company_denied(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """An unknown company id fails closed."""
    secret, _ = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[0], str(uuid.uuid4()))
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=secret, uri=uri
    )
    assert code == MCP_ERROR_FORBIDDEN


# -- harness: session-backed happy paths -------------------------------------------


def test_standards_resource_reads(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The standards resource reads end-to-end (shape, not seeded content).

    Content assertions stay structural: parallel CI jobs may seed the
    shared registry, so the test must not depend on specific standards.
    """
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[2], world.company_a_id)
    payload = json.loads(
        _read_text(session_factory, settings, bootstrap_secret=secret, uri=uri)
    )
    assert payload["company"]["id"] == world.company_a_id
    assert payload["provenance"]["company_id"] == int(world.company_a_external_id)
    assert payload["provenance"]["report_type"] == "standards"
    assert isinstance(payload["standards"], list)
    assert set(payload["coverage"]) == {
        "corpus_standards",
        "standards_with_rules",
        "rules_implemented",
        "rules_verified",
        "rules_executable",
    }
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"
    assert rows[0].kind == "resource"


def test_tax_policy_resource_reads(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The tax-policy resource reads end-to-end; values are decimal strings."""
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[3], world.company_a_id)
    payload = json.loads(
        _read_text(session_factory, settings, bootstrap_secret=secret, uri=uri)
    )
    assert payload["provenance"]["company_id"] == int(world.company_a_external_id)
    assert isinstance(payload["sources"], list)
    assert isinstance(payload["active_versions"], list)
    for version in payload["active_versions"]:
        assert version["status"] == "active"
        assert isinstance(version["parameter_value"], str)
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"


def test_integration_compliance_status_resource_reads(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """The integration+compliance resource reads end-to-end (shape only)."""
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[5], world.company_a_id)
    payload = json.loads(
        _read_text(session_factory, settings, bootstrap_secret=secret, uri=uri)
    )
    assert payload["provenance"]["company_id"] == int(world.company_a_external_id)
    assert isinstance(payload["integrations"]["channels"], list)
    assert isinstance(payload["integrations"]["bank_connections"], list)
    assert isinstance(payload["integrations"]["stores"], list)
    assert set(payload["compliance"]["moadian"]["documents_by_status"]) == {
        "draft",
        "submitted",
        "accepted",
        "rejected",
    }
    assert isinstance(payload["compliance"]["active_tax_policy_versions"], int)
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"


# -- harness: report snapshots (bounded access) -------------------------------------


@dataclass
class SeededSnapshot:
    """A committed validation run + snapshot owned by company B."""

    run_id: str
    snapshot_id: str


@pytest.fixture()
def snapshot_for_b(
    world: World, session_factory: Callable[[], Session]
) -> Generator[SeededSnapshot, None, None]:
    """Seed a run + snapshot for company B; remove them afterwards."""
    run_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    period_start = datetime.date(2025, 8, 23)
    period_end = datetime.date(2025, 9, 22)
    session = session_factory()
    try:
        run = ValidationRunModel(
            id=run_id,
            tenant_id=world.tenant_id,
            company_id=world.company_b_id,
            period_start=period_start,
            period_end=period_end,
            standard_codes=["1"],
            ruleset_version="1.0",
            input_hash="abc",
            overall_outcome="pass",
            is_valid=True,
            rules_executed=2,
            rules_passed=2,
            rules_failed=0,
            rules_not_applicable=0,
            rules_errored=0,
            coverage_corpus_standards=1,
            coverage_standards_with_rules=1,
            coverage_rules_implemented=2,
            coverage_rules_verified=2,
            coverage_rules_executed=2,
        )
        snapshot = ReportSnapshotModel(
            id=snapshot_id,
            tenant_id=world.tenant_id,
            company_id=world.company_b_id,
            validation_run_id=run_id,
            period_start=period_start,
            period_end=period_end,
            as_of_date=period_end,
            source_endpoint="account.move.line (Tryton REST)",
            ruleset_version="1.0",
            payload_hash="abc",
            payload={
                "trial_balance": {"accounts": []},
                "total_debit": 1000,
                "total_credit": 1000,
            },
            balanced=True,
            total_debit=1000,
            total_credit=1000,
        )
        session.add_all([run, snapshot])
        session.commit()
        yield SeededSnapshot(run_id=run_id, snapshot_id=snapshot_id)
    finally:
        session.close()
        cleanup = session_factory()
        try:
            cleanup.query(ReportSnapshotModel).filter(
                ReportSnapshotModel.id == snapshot_id
            ).delete(synchronize_session=False)
            cleanup.query(ValidationRunModel).filter(
                ValidationRunModel.id == run_id
            ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()


def test_report_snapshot_reads_for_owning_company(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    snapshot_for_b: SeededSnapshot,
) -> None:
    """The owning company reads its snapshot end-to-end."""
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[4], world.company_b_id, run_id=snapshot_for_b.run_id)
    payload = json.loads(
        _read_text(session_factory, settings, bootstrap_secret=secret, uri=uri)
    )
    assert payload["snapshot"]["snapshot_id"] == snapshot_for_b.snapshot_id
    assert payload["snapshot"]["total_debit"] == 1000
    assert payload["snapshot"]["total_credit"] == 1000
    assert payload["validation"]["approved"] is True
    assert payload["validation"]["is_valid"] is True
    assert payload["trial_balance"]["total_debit"] == 1000
    assert payload["provenance"]["snapshot_ref"] == snapshot_for_b.snapshot_id
    assert payload["provenance"]["company_id"] == int(world.company_b_external_id)
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "allowed"


def test_report_snapshot_cross_company_denied(
    world: World,
    session_factory: Callable[[], Session],
    settings: AppSettings,
    snapshot_for_b: SeededSnapshot,
) -> None:
    """BOUNDED ACCESS: company A's read of company B's snapshot fails closed."""
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[4], world.company_a_id, run_id=snapshot_for_b.run_id)
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=secret, uri=uri
    )
    assert code == MCP_ERROR_FORBIDDEN
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "denied"


def test_report_snapshot_unknown_run_errors(
    world: World, session_factory: Callable[[], Session], settings: AppSettings
) -> None:
    """A run that does not exist is a clear error, not an empty snapshot."""
    secret, key_id = _issue_key(session_factory, settings, world, list(ALL_US604_SCOPES))
    uri = _uri(RESOURCE_DEFINITIONS[4], world.company_a_id, run_id=str(uuid.uuid4()))
    code = _read_denied_code(
        session_factory, settings, bootstrap_secret=secret, uri=uri
    )
    assert code == MCP_ERROR_INTERNAL
    rows = _audit_rows(session_factory, key_id=key_id, tool=uri)
    assert len(rows) == 1
    assert rows[0].outcome == "error"


# -- unit tests: readers against stub services --------------------------------------

_CLIENT = AuthenticatedClient(
    client_id="client-1",
    tenant_id="tenant-1",
    installation_id="installation-1",
    key_id="key-1",
    key_prefix="hya_live_test",
    client_name="test client",
    scopes=frozenset(ALL_US604_SCOPES),
    engine_company_id=None,
)


def _request(
    services: Any, *, params: dict[str, str] | None = None
) -> ResourceRequest:
    """A ResourceRequest for company 301 ('Test Co') with stub services."""
    row = EngineCompanyModel(
        id="company-row-a",
        tenant_id="tenant-1",
        engine_connection_id="conn-1",
        external_company_id="301",
        company_name="Test Co",
    )
    return ResourceRequest(
        services=cast(McpServices, services),
        client=_CLIENT,
        row=row,
        ref=CompanyRef(engine_id=301),
        params=params or {},
    )


def _services(**overrides: Any) -> Any:
    """Stub McpServices: only the named services exist."""
    namespace = SimpleNamespace()
    for name in (
        "companies",
        "accounts",
        "fiscal",
        "standards",
        "tax_policy",
        "report_validation",
        "integrations",
        "moadian",
    ):
        setattr(namespace, name, overrides.get(name, SimpleNamespace()))
    return namespace


def _company_view() -> CompanyView:
    return CompanyView(
        ref=CompanyRef(engine_id=301),
        name="Test Co",
        currency_code="IRR",
        national_id="12345678901",
    )


def test_reader_chart_of_accounts_active_only() -> None:
    """The chart reader serves active accounts only, with parent codes."""
    chart = [
        AccountView(
            ref=AccountRef(engine_id=1),
            company=CompanyRef(engine_id=301),
            code="1000",
            name="Cash",
            type_name="asset",
            parent=None,
            closed=False,
            party_required=False,
            active=True,
        ),
        AccountView(
            ref=AccountRef(engine_id=2),
            company=CompanyRef(engine_id=301),
            code="1100",
            name="Bank",
            type_name="asset",
            parent=AccountRef(engine_id=1),
            closed=False,
            party_required=False,
            active=True,
        ),
        AccountView(
            ref=AccountRef(engine_id=3),
            company=CompanyRef(engine_id=301),
            code="9999",
            name="Retired",
            type_name="asset",
            parent=None,
            closed=True,
            party_required=False,
            active=False,
        ),
    ]
    services = _services(
        companies=SimpleNamespace(get_company=lambda ref: _company_view()),
        accounts=SimpleNamespace(get_chart_of_accounts=lambda ref: chart),
    )
    payload = _read_chart_of_accounts(_request(services))
    assert payload["company"]["currency_code"] == "IRR"
    assert payload["company"]["national_id"] == "12345678901"
    assert payload["active_only"] is True
    assert payload["account_count"] == 2
    codes = [account["code"] for account in payload["accounts"]]
    assert codes == ["1000", "1100"]
    assert payload["accounts"][1]["parent_code"] == "1000"
    assert payload["accounts"][0]["postable"] is True
    assert payload["provenance"]["company_id"] == 301
    assert payload["provenance"]["company_name"] == "Test Co"
    assert payload["provenance"]["currency"] == "IRR"
    json.dumps(payload)  # JSON-safe


def test_reader_fiscal_periods() -> None:
    """The fiscal reader serves years + periods with Jalali dates."""
    years = [
        FiscalYearView(
            ref=FiscalYearRef(engine_id=10),
            company=CompanyRef(engine_id=301),
            name="1404",
            start_date=FiscalDate(datetime.date(2025, 3, 21)),
            end_date=FiscalDate(datetime.date(2026, 3, 20)),
            state="open",
        )
    ]
    periods = [
        FiscalPeriodView(
            name="1404-01",
            start_date=FiscalDate(datetime.date(2025, 3, 21)),
            end_date=FiscalDate(datetime.date(2025, 4, 20)),
            state="open",
            ref=FiscalPeriodRef(engine_id=100),
            fiscal_year=FiscalYearRef(engine_id=10),
        )
    ]
    services = _services(
        companies=SimpleNamespace(get_company=lambda ref: _company_view()),
        fiscal=SimpleNamespace(
            list_fiscal_years=lambda ref: years,
            list_periods=lambda ref: periods,
        ),
    )
    payload = _read_fiscal_periods(_request(services))
    assert payload["fiscal_years"][0]["name"] == "1404"
    assert payload["fiscal_years"][0]["is_open"] is True
    assert payload["fiscal_years"][0]["start_date"] == {
        "gregorian": "2025-03-21",
        "jalali": "1404-01-01",
    }
    assert payload["periods"][0]["fiscal_year_id"] == 10
    assert payload["periods"][0]["period_type"] == "standard"
    json.dumps(payload)


def test_reader_standards() -> None:
    """The standards reader serves versions + verification labels + coverage."""
    revisions = (
        StandardRevision(
            standard_code=StandardCode("1"),
            title="Standard 1 (2024)",
            revision_id="rev-2",
            effective_from=datetime.date(2024, 1, 1),
            verification_status=VerificationStatus.VERIFIED_TEXT,
            verified_by="auditor",
            source_url="https://example.invalid/std1",
            sha256="deadbeef",
        ),
        StandardRevision(
            standard_code=StandardCode("1"),
            title="Standard 1 (2020)",
            revision_id="rev-1",
            effective_from=datetime.date(2020, 1, 1),
            verification_status=VerificationStatus.UNVERIFIED,
        ),
    )
    sources = [
        StandardSource(
            standard_code=StandardCode("1"),
            title="Standard One",
            title_en="Standard One",
            ifrs_equivalent=None,
            phase=1,
            slug="std-1",
            source_authority="Authority",
            source_url=None,
            revisions=revisions,
            rules_count=3,
        )
    ]
    coverage = RegistryCoverage(
        corpus_standards=1,
        standards_with_rules=1,
        rules_implemented=3,
        rules_verified=1,
        rules_executable=2,
    )
    services = _services(
        standards=SimpleNamespace(
            list_standards=lambda: sources,
            coverage=lambda: coverage,
        )
    )
    payload = _read_standards(_request(services))
    assert payload["scope_note"]
    assert payload["coverage"]["rules_verified"] == 1
    standard = payload["standards"][0]
    assert standard["code"] == "1"
    assert standard["latest_revision_id"] == "rev-2"
    assert standard["latest_verification_status"] == "VERIFIED_TEXT"
    assert standard["revisions"][0]["sha256"] == "deadbeef"
    assert standard["revisions"][1]["verification_status"] == "UNVERIFIED"
    assert payload["provenance"]["source_engine"] == "HesabYar"
    json.dumps(payload)


def test_reader_tax_policy_active_only_decimal_strings() -> None:
    """The tax reader serves active versions only; Decimal values are strings."""
    source = TaxSource(
        id="src-1",
        source_key="vat-law",
        title="VAT Law",
        authority="Parliament",
    )
    versions = [
        TaxPolicyVersion(
            id="v-1",
            source_id="src-1",
            source_key="vat-law",
            rule_key=RuleKey("vat.general"),
            rule_name="General VAT",
            version_id="v1",
            parameter_value=Decimal("10"),
            value_unit=ValueUnit.PERCENT,
            rule_year=1405,
            performance_year=1405,
            filing_year=1405,
            effective_from=datetime.date(2026, 3, 21),
            status="active",
            verification_status=VerificationStatus.VERIFIED_TEXT,
        ),
        TaxPolicyVersion(
            id="v-0",
            source_id="src-1",
            source_key="vat-law",
            rule_key=RuleKey("vat.general"),
            rule_name="General VAT",
            version_id="v0",
            parameter_value=Decimal("9"),
            value_unit=ValueUnit.PERCENT,
            rule_year=1404,
            status="superseded",
            verification_status=VerificationStatus.VERIFIED_TEXT,
        ),
    ]
    services = _services(
        tax_policy=SimpleNamespace(
            list_sources=lambda: [source],
            list_versions=lambda: versions,
        )
    )
    payload = _read_tax_policy(_request(services))
    assert len(payload["active_versions"]) == 1
    version = payload["active_versions"][0]
    assert version["parameter_value"] == "10"
    assert isinstance(version["parameter_value"], str)
    assert version["value_unit"] == "percent"
    assert version["rule_year"] == 1405
    assert payload["sources"][0]["source_key"] == "vat-law"
    json.dumps(payload)


def _validation_run(*, company_id: str | None, is_valid: bool) -> ReportValidationRun:
    """A report validation run + snapshot for the reader unit tests."""
    run = ValidationRunRecord(
        run_id="run-1",
        tenant_id="tenant-1",
        company_id=company_id,
        period_start=datetime.date(2025, 8, 23),
        period_end=datetime.date(2025, 9, 22),
        standard_codes=("1",),
        ruleset_version="1.0",
        input_hash="abc",
        overall_outcome=RuleOutcome.PASS if is_valid else RuleOutcome.FAIL,
        is_valid=is_valid,
        rules_executed=1,
        rules_passed=1 if is_valid else 0,
        rules_failed=0 if is_valid else 1,
        rules_not_applicable=0,
        rules_errored=0,
        coverage=RunCoverage(
            corpus_standards=1,
            standards_with_rules=1,
            rules_implemented=1,
            rules_verified=1,
            rules_executed=1,
        ),
        results=(
            ValidationResultRecord(
                rule_id=RuleId("TB001"),
                standard_code=StandardCode("1"),
                revision_id="rev-2",
                outcome=RuleOutcome.PASS if is_valid else RuleOutcome.FAIL,
                severity=RuleSeverity.ERROR,
                message="balanced" if is_valid else "unbalanced",
            ),
        ),
    )
    snapshot = ReportSnapshot(
        snapshot_id="snap-1",
        tenant_id="tenant-1",
        company_id=company_id,
        validation_run_id="run-1",
        period_start=datetime.date(2025, 8, 23),
        period_end=datetime.date(2025, 9, 22),
        as_of_date=datetime.date(2025, 9, 22),
        source_endpoint="account.move.line (Tryton REST)",
        ruleset_version="1.0",
        payload_hash="abc",
        payload={"trial_balance": {"accounts": []}, "total_debit": 1000, "total_credit": 1000},
        balanced=True,
        total_debit=1000,
        total_credit=1000,
        created_at=datetime.datetime(2025, 9, 22, 12, 0, tzinfo=datetime.UTC),
    )
    return ReportValidationRun(run=run, snapshot=snapshot)


def test_reader_report_snapshot_owning_company() -> None:
    """The snapshot reader serves the payload + validation outcome."""
    record = _validation_run(company_id="company-row-a", is_valid=True)
    services = _services(
        report_validation=SimpleNamespace(
            get_report_validation_run=lambda run_id, tenant_id: record
        )
    )
    payload = _read_report_snapshot(_request(services, params={"run_id": "run-1"}))
    assert payload["snapshot"]["snapshot_id"] == "snap-1"
    assert payload["snapshot"]["total_debit"] == 1000
    assert payload["validation"]["approved"] is True
    assert payload["validation"]["results"][0]["outcome"] == "pass"
    assert payload["trial_balance"]["total_credit"] == 1000
    assert payload["provenance"]["snapshot_ref"] == "snap-1"
    assert payload["provenance"]["ruleset_version"] == "1.0"
    json.dumps(payload)


def test_reader_report_snapshot_failed_run_served_with_failure() -> None:
    """A failed run's snapshot is served WITH its failure stated."""
    record = _validation_run(company_id="company-row-a", is_valid=False)
    services = _services(
        report_validation=SimpleNamespace(
            get_report_validation_run=lambda run_id, tenant_id: record
        )
    )
    payload = _read_report_snapshot(_request(services, params={"run_id": "run-1"}))
    assert payload["validation"]["approved"] is False
    assert payload["validation"]["overall_outcome"] == "fail"
    assert payload["validation"]["rules_failed"] == 1


def test_reader_report_snapshot_other_company_forbidden() -> None:
    """A snapshot of another company fails closed (forbidden)."""
    record = _validation_run(company_id="company-row-b", is_valid=True)
    services = _services(
        report_validation=SimpleNamespace(
            get_report_validation_run=lambda run_id, tenant_id: record
        )
    )
    with pytest.raises(MCPError) as excinfo:
        _read_report_snapshot(_request(services, params={"run_id": "run-1"}))
    assert excinfo.value.code == MCP_ERROR_FORBIDDEN


def test_reader_report_snapshot_unattributed_forbidden() -> None:
    """A snapshot with no company cannot be served under any company."""
    record = _validation_run(company_id=None, is_valid=True)
    services = _services(
        report_validation=SimpleNamespace(
            get_report_validation_run=lambda run_id, tenant_id: record
        )
    )
    with pytest.raises(MCPError) as excinfo:
        _read_report_snapshot(_request(services, params={"run_id": "run-1"}))
    assert excinfo.value.code == MCP_ERROR_FORBIDDEN


def test_reader_report_snapshot_unknown_run_internal_error() -> None:
    """A missing run is a clear internal error (the SDK surfaces it)."""

    def _missing(run_id: str, tenant_id: str) -> Any:
        raise ReportValidationRunNotFoundError(
            f"Report validation run {run_id!r} does not exist.",
            details={"validation_run_id": run_id},
        )

    services = _services(report_validation=SimpleNamespace(get_report_validation_run=_missing))
    with pytest.raises(MCPError) as excinfo:
        _read_report_snapshot(_request(services, params={"run_id": "nope"}))
    assert excinfo.value.code == MCP_ERROR_INTERNAL


def test_reader_integration_compliance_status() -> None:
    """The status reader summarizes channels/contracts/banks/stores/Moadian.

    No credential material appears anywhere in the payload: bank
    connections carry only ``has_credentials``, stores only the
    ``has_*`` flags.
    """
    channels = [
        PaymentChannel(
            id="ch-1",
            installation_id="installation-1",
            channel_key="zarinpal",
            display_name_fa="زرین‌پال",
            channel_type="gateway",
            is_builtin=True,
            enabled=True,
            status=ChannelStatus.ACTIVE,
        ),
        PaymentChannel(
            id="ch-2",
            installation_id="installation-1",
            channel_key="custom",
            display_name_fa="سفارشی",
            channel_type="custom",
            is_builtin=False,
            enabled=False,
            status=ChannelStatus.SETUP,
        ),
    ]
    contract = GatewayContract(
        id="ct-1",
        channel_id="ch-1",
        channel_key="zarinpal",
        version=2,
        valid_from=datetime.date(2026, 1, 1),
        fee_percent=Decimal("5.5"),
    )

    def _active_contract(installation_id: str, channel_key: str) -> GatewayContract:
        if channel_key == "zarinpal":
            return contract
        raise IntegrationContractNotFoundError(
            f"Channel {channel_key!r} has no active contract.",
            details={"channel_key": channel_key},
        )

    connections = [
        BankConnection(
            id="bc-1",
            installation_id="installation-1",
            company_id="company-row-a",
            bank_key="melli",
            display_name="Melli",
            connection_kind="fintech",
            has_credentials=True,
            credentials_key_id="key-should-not-appear",
            status="active",
        )
    ]
    stores = [
        Store(
            id="st-1",
            installation_id="installation-1",
            store_key="hayat",
            display_name="Hayat",
            store_type="custom",
            prices_in_toman=True,
            has_webhook_secret=True,
            has_api_key=True,
        )
    ]
    profiles = [
        MoadianProfile(
            id="mp-1",
            tenant_id="tenant-1",
            name="Main",
            slug="main",
            environment=MoadianEnvironment.PRODUCTION,
            sender_mode=SenderMode.SELF_TSP,
            fiscal_id="fiscal-should-not-appear",
            economic_code=None,
            packet_type="normal",
            api_base_url="https://example.invalid",
            protocol_version="1.0",
            source_authority=None,
            source_url=None,
            snapshot_uri=None,
            verification_status=VerificationStatus.VERIFIED_TEXT,
            key_reference="ref-should-not-appear",
            key_fingerprint="fp-should-not-appear",
            is_active=True,
            notes=None,
            created_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
            updated_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        )
    ]
    documents = [
        MoadianDocument(
            id=f"doc-{i}",
            tenant_id="tenant-1",
            profile_id="mp-1",
            invoice_id=None,
            taxid=None,
            serial=None,
            invoice_type=1,
            invoice_pattern=1,
            invoice_subject=1,
            payload={},
            payload_hash="x",
            status=status,
            created_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
            updated_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        )
        for i, status in enumerate(
            [DocumentStatus.DRAFT, DocumentStatus.ACCEPTED, DocumentStatus.ACCEPTED]
        )
    ]
    services = _services(
        integrations=SimpleNamespace(
            list_channels=lambda installation_id: channels,
            get_active_contract=_active_contract,
            list_bank_connections=lambda installation_id, company_id=None: connections,
            list_stores=lambda installation_id: stores,
        ),
        moadian=SimpleNamespace(
            list_profiles=lambda tenant_id: profiles,
            list_documents=lambda tenant_id: documents,
        ),
        standards=SimpleNamespace(
            coverage=lambda: RegistryCoverage(1, 1, 3, 1, 2),
        ),
        tax_policy=SimpleNamespace(
            list_versions=lambda: [
                TaxPolicyVersion(
                    id="v-1",
                    source_id="src-1",
                    source_key="vat-law",
                    rule_key=RuleKey("vat.general"),
                    rule_name="General VAT",
                    version_id="v1",
                    parameter_value=Decimal("10"),
                    value_unit=ValueUnit.PERCENT,
                    rule_year=1405,
                    status="active",
                )
            ],
        ),
    )
    payload = _read_integration_compliance_status(_request(services))
    assert payload["integrations"]["channels"][0]["has_active_contract"] is True
    active = payload["integrations"]["channels"][0]["active_contract"]
    assert active["version"] == 2
    assert active["fee_percent"] == "5.5"
    assert isinstance(active["fee_percent"], str)
    assert payload["integrations"]["channels"][1]["has_active_contract"] is False
    assert payload["integrations"]["channels"][1]["active_contract"] is None
    assert payload["integrations"]["bank_connections"][0]["has_credentials"] is True
    assert payload["integrations"]["stores"][0]["has_webhook_secret"] is True
    assert payload["compliance"]["moadian"]["documents_by_status"] == {
        "draft": 1,
        "submitted": 0,
        "accepted": 2,
        "rejected": 0,
    }
    assert payload["compliance"]["active_tax_policy_versions"] == 1
    text = json.dumps(payload)
    # No credential material, key references, or fiscal identifiers leak.
    for leaked in (
        "key-should-not-appear",
        "ref-should-not-appear",
        "fp-should-not-appear",
        "fiscal-should-not-appear",
        "credentials_key_id",
        "key_reference",
        "key_fingerprint",
    ):
        assert leaked not in text
