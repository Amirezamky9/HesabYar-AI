"""MCP resources (US-604, spec §13.6).

Six resource templates, ALL company-scoped: every URI carries a
``{company}`` segment, and every read resolves that segment against
the authenticated grant (:func:`hesabyar.interfaces.mcp.auth.resolve_company_row`)
before any data is touched. There is no unfiltered all-company
resource — the shape of the URI makes one impossible to publish by
accident, and ``test_no_unfiltered_resource`` proves every registered
template carries the company parameter.

Resources read ONLY through :class:`McpServices` (the same application
query services REST uses) — no direct PostgreSQL, no engine calls
except through the services. All service calls run in a thread (the
services are synchronous); the handler itself is async.

Scope enforcement: the middleware enforces resource scopes centrally
(US-606: the registry matches a concrete URI against the registered
templates), and each resource handler enforces the same scopes again
(from the same registry record) in :func:`_serve` before touching any
data — defence in depth, so a handler called outside the middleware
still cannot serve an under-scoped key. The scope record lives in one
place (the registry) and the check cannot be forgotten by a future
resource, because :func:`register_hesabyar_resource` builds the
handler from a pure reader — authors never write the enforcement
themselves.

Every resource payload carries a §13.7 provenance block (company,
period, currency, source engine, generated timestamp, snapshot/source
reference, ruleset version, warnings, and the model-interpretation
notice). Amounts are integer Rial or safe decimal strings — never
binary floating-point.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from hesabyar.application.errors import (
    IntegrationContractNotFoundError,
    ReportValidationRunNotFoundError,
    ValidationRunNotFoundError,
)
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import CompanyView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import HesabYarError
from hesabyar.domain.standards.models import RegistryCoverage as StandardsCoverage
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.models import EngineCompanyModel
from hesabyar.interfaces.mcp.auth import resolve_company_row
from hesabyar.interfaces.mcp.context import McpContext
from hesabyar.interfaces.mcp.errors import (
    CompanyAccessError,
    forbidden,
    internal_error,
)
from hesabyar.interfaces.mcp.guards import authenticated_client, require_mcp_context
from hesabyar.interfaces.mcp.provenance import build_provenance
from hesabyar.interfaces.mcp.registry import ToolRegistry
from hesabyar.interfaces.mcp.services import McpServices

#: MIME type of every HesabYar resource payload (JSON text).
RESOURCE_MIME_TYPE = "application/json"


@dataclass(frozen=True)
class ResourceRequest:
    """One authorized resource read: services + resolved company."""

    services: McpServices
    client: AuthenticatedClient
    row: EngineCompanyModel
    ref: CompanyRef
    #: Template parameters beyond ``company`` (e.g. ``{"run_id": ...}``).
    params: dict[str, str]


#: A pure reader: services + resolved company in, JSON-safe payload out.
#: Readers never see the raw URI and never do auth — :func:`_serve`
#: has already enforced scopes and the company grant.
Reader = Callable[[ResourceRequest], dict[str, Any]]


@dataclass(frozen=True)
class ResourceDefinition:
    """One resource template: URI, name, scopes, description."""

    uri_template: str
    name: str
    scopes: tuple[str, ...]
    description: str
    #: Template parameters beyond ``company`` (drives the handler shape).
    params: tuple[str, ...] = ()


RESOURCE_DEFINITIONS: tuple[ResourceDefinition, ...] = (
    ResourceDefinition(
        uri_template="hesabyar://companies/{company}/chart-of-accounts",
        name="chart-of-accounts",
        scopes=("account:read",),
        description=(
            "Active chart-of-accounts metadata for one company: code, name, "
            "type, parent, postable/closed flags. Active accounts only."
        ),
    ),
    ResourceDefinition(
        uri_template="hesabyar://companies/{company}/fiscal-periods",
        name="fiscal-periods",
        scopes=("company:read", "reports:read"),
        description=(
            "Available fiscal years and periods plus company metadata "
            "(name, currency, national id) for one company."
        ),
    ),
    ResourceDefinition(
        uri_template="hesabyar://companies/{company}/standards",
        name="standards",
        scopes=("standards:read",),
        description=(
            "Versioned accounting-standards summaries with verified source "
            "references and registry coverage. Standards are "
            "installation-wide reference data; the read is gated on the "
            "caller's access to the named company."
        ),
    ),
    ResourceDefinition(
        uri_template="hesabyar://companies/{company}/tax-policy",
        name="tax-policy",
        scopes=("tax:read",),
        description=(
            "Active tax-policy profile for one company: the currently "
            "active policy versions with applicability metadata (rule / "
            "performance / filing year, effective dates, verification "
            "status) and their source references."
        ),
    ),
    ResourceDefinition(
        uri_template="hesabyar://companies/{company}/report-snapshots/{run_id}",
        name="report-snapshot",
        scopes=("reports:read",),
        description=(
            "One approved report snapshot (trial-balance document + "
            "validation outcome) for one company. Bounded access: one "
            "snapshot per read, and only when the snapshot belongs to the "
            "named company. A snapshot is approved when its validation run "
            "is valid; failed runs are still served, with the failure "
            "stated plainly."
        ),
        params=("run_id",),
    ),
    ResourceDefinition(
        uri_template="hesabyar://companies/{company}/integration-compliance-status",
        name="integration-compliance-status",
        scopes=("compliance:read", "integrations:read"),
        description=(
            "Integration and compliance status summary for one company: "
            "payment channels with active contracts, the company's bank "
            "connections, stores, Moadian profile/document status, "
            "standards coverage and active tax-policy versions. No "
            "credential material, no secrets."
        ),
    ),
)


# -- serialization helpers ------------------------------------------------

JsonDict = dict[str, Any]


def _date(value: datetime.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _fiscal_date(value: FiscalDate) -> JsonDict:
    """A fiscal date as Gregorian + Jalali ISO strings."""
    return {
        "gregorian": value.gregorian_date.isoformat(),
        "jalali": value.jalali_date.isoformat(),
    }


def _company_block(request: ResourceRequest, view: CompanyView | None = None) -> JsonDict:
    """Company metadata: row id + engine id + name (+ view fields when known).

    The name comes from the ENGINE view when one is available — REST's
    ``CompanyResponse.name`` is documented as "Company display name
    from the engine", and MCP must agree with REST for the same
    company. The mapping row's ``company_name`` is only a tenant-local
    label (fixtures use "Company 1"), so it is the fallback for
    readers that have no engine view.
    """
    block: JsonDict = {
        "id": request.row.id,
        "engine_id": request.ref.engine_id,
        "name": view.name if view is not None else request.row.company_name,
    }
    if view is not None:
        block["currency_code"] = view.currency_code
        block["national_id"] = view.national_id
    return block


def _provenance(
    request: ResourceRequest,
    *,
    report_type: str,
    period_label: str,
    source_engine: str = "Tryton",
    snapshot_ref: str | None = None,
    source_ref: str | None = None,
    ruleset_version: str | None = None,
    generated_at: datetime.datetime | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
) -> JsonDict:
    """The §13.7 provenance block for one resource payload."""
    return build_provenance(
        company_id=request.ref.engine_id,
        company_name=request.row.company_name,
        period_label=period_label,
        report_type=report_type,
        period_from=period_from,
        period_to=period_to,
        source_engine=source_engine,
        snapshot_ref=snapshot_ref,
        source_ref=source_ref,
        ruleset_version=ruleset_version,
        generated_at=generated_at,
    ).to_dict()


def _coverage_block(coverage: StandardsCoverage) -> JsonDict:
    """Registry coverage counters, reported separately (spec §12.1)."""
    return {
        "corpus_standards": coverage.corpus_standards,
        "standards_with_rules": coverage.standards_with_rules,
        "rules_implemented": coverage.rules_implemented,
        "rules_verified": coverage.rules_verified,
        "rules_executable": coverage.rules_executable,
    }


# -- readers ----------------------------------------------------------------

# Each reader is a pure function of (services, resolved company): it is
# unit-tested with stub services and never touches auth, the session,
# or the URI. All amounts are integer Rial or decimal strings.


def _read_chart_of_accounts(request: ResourceRequest) -> JsonDict:
    """Active chart-of-accounts metadata (engine-backed)."""
    services = request.services
    view = services.companies.get_company(request.ref)
    chart = services.accounts.get_chart_of_accounts(request.ref)
    code_by_id = {account.ref.engine_id: account.code for account in chart}
    accounts = [
        {
            "code": account.code,
            "name": account.name,
            "type": account.type_name,
            "parent_code": (
                code_by_id.get(account.parent.engine_id) if account.parent is not None else None
            ),
            "postable": account.is_postable,
            "closed": account.closed,
            "party_required": account.party_required,
        }
        for account in chart
        if account.active
    ]
    return {
        "company": _company_block(request, view),
        "active_only": True,
        "account_count": len(accounts),
        "accounts": accounts,
        "provenance": _provenance(
            request,
            report_type="chart-of-accounts",
            period_label="current chart of accounts",
        ),
    }


def _read_fiscal_periods(request: ResourceRequest) -> JsonDict:
    """Available fiscal years + periods + company metadata (engine-backed)."""
    services = request.services
    view = services.companies.get_company(request.ref)
    years = services.fiscal.list_fiscal_years(request.ref)
    periods = services.fiscal.list_periods(request.ref)
    return {
        "company": _company_block(request, view),
        "fiscal_years": [
            {
                "id": year.ref.engine_id,
                "name": year.name,
                "start_date": _fiscal_date(year.start_date),
                "end_date": _fiscal_date(year.end_date),
                "state": year.state,
                "is_open": year.is_open,
            }
            for year in years
        ],
        "periods": [
            {
                "id": period.ref.engine_id if period.ref is not None else None,
                "fiscal_year_id": (
                    period.fiscal_year.engine_id if period.fiscal_year is not None else None
                ),
                "name": period.name,
                "start_date": _fiscal_date(period.start_date),
                "end_date": _fiscal_date(period.end_date),
                "state": period.state,
                "is_open": period.is_open,
                "period_type": period.period_type,
            }
            for period in periods
        ],
        "provenance": _provenance(
            request,
            report_type="fiscal-periods",
            period_label="available fiscal periods",
        ),
    }


def _read_standards(request: ResourceRequest) -> JsonDict:
    """Versioned standards summaries + verified source references.

    Standards are installation-wide reference data, not per-company
    secrets — the company segment gates ACCESS (the caller must hold
    the grant for the named company), and the payload says so.
    """
    services = request.services
    sources = services.standards.list_standards()
    coverage = services.standards.coverage()
    standards = []
    for source in sources:
        revisions = [
            {
                "revision_id": revision.revision_id,
                "title": revision.title,
                "published_at": _date(revision.published_at),
                "effective_from": _date(revision.effective_from),
                "effective_to": _date(revision.effective_to),
                "status": revision.status,
                "supersedes_revision_id": revision.supersedes_revision_id,
                "source_authority": revision.source_authority,
                "source_url": revision.source_url,
                "snapshot_uri": revision.snapshot_uri,
                "sha256": revision.sha256,
                "verification_status": str(revision.verification_status),
                "verified_by": revision.verified_by,
                "verified_at": _datetime(revision.verified_at),
                "notes": revision.notes,
            }
            for revision in source.revisions
        ]
        latest = source.latest_revision
        standards.append(
            {
                "code": str(source.standard_code),
                "title": source.title,
                "title_en": source.title_en,
                "ifrs_equivalent": source.ifrs_equivalent,
                "phase": source.phase,
                "slug": source.slug,
                "source_authority": source.source_authority,
                "source_url": source.source_url,
                "rules_count": source.rules_count,
                "latest_revision_id": latest.revision_id if latest is not None else None,
                "latest_verification_status": (
                    str(latest.verification_status) if latest is not None else None
                ),
                "revisions": revisions,
            }
        )
    return {
        "company": _company_block(request),
        "scope_note": (
            "Standards are installation-wide reference data; this read is "
            "gated on the caller's access to the named company."
        ),
        "coverage": _coverage_block(coverage),
        "standards": standards,
        "provenance": _provenance(
            request,
            report_type="standards",
            period_label="current standards registry",
            source_engine="HesabYar",
        ),
    }


def _read_tax_policy(request: ResourceRequest) -> JsonDict:
    """Active tax-policy profile + applicability metadata."""
    services = request.services
    sources = services.tax_policy.list_sources()
    active_versions = [
        version for version in services.tax_policy.list_versions() if version.status == "active"
    ]
    return {
        "company": _company_block(request),
        "as_of": datetime.date.today().isoformat(),
        "sources": [
            {
                "source_key": source.source_key,
                "title": source.title,
                "authority": source.authority,
                "source_url": source.source_url,
                "snapshot_uri": source.snapshot_uri,
                "sha256": source.sha256,
                "notes": source.notes,
                "versions_count": source.versions_count,
            }
            for source in sources
        ],
        "active_versions": [
            {
                "id": version.id,
                "source_key": version.source_key,
                "rule_key": str(version.rule_key),
                "rule_name": version.rule_name,
                "version_id": version.version_id,
                # Decimal as a safe decimal string — never a float.
                "parameter_value": str(version.parameter_value),
                "value_unit": str(version.value_unit),
                "rule_year": version.rule_year,
                "performance_year": version.performance_year,
                "filing_year": version.filing_year,
                "effective_from": _date(version.effective_from),
                "effective_to": _date(version.effective_to),
                "status": version.status,
                "verification_status": str(version.verification_status),
                "verified_by": version.verified_by,
                "verified_at": _datetime(version.verified_at),
                "source_authority": version.source_authority,
                "source_url": version.source_url,
                "snapshot_uri": version.snapshot_uri,
            }
            for version in active_versions
        ],
        "provenance": _provenance(
            request,
            report_type="tax-policy",
            period_label="active tax-policy profile",
            source_engine="HesabYar",
        ),
    }


def _read_report_snapshot(request: ResourceRequest) -> JsonDict:
    """One approved report snapshot, with bounded access.

    The snapshot is served ONLY when it belongs to the named company:
    a snapshot of another company (or with no company at all) fails
    closed with the same message as a missing run, so the read never
    confirms another company's snapshot exists.
    """
    run_id = request.params["run_id"]
    try:
        record = request.services.report_validation.get_report_validation_run(
            run_id, request.client.tenant_id
        )
    except (ReportValidationRunNotFoundError, ValidationRunNotFoundError):
        raise internal_error(
            f"Report validation run {run_id!r} does not exist for this company."
        ) from None
    snapshot = record.snapshot
    if snapshot.company_id != request.row.id:
        raise forbidden(f"Report validation run {run_id!r} does not exist for this company.")
    run = record.run
    period_label = (
        f"{snapshot.period_start.isoformat()} to {snapshot.period_end.isoformat()}"
        if snapshot.period_start is not None and snapshot.period_end is not None
        else f"as of {snapshot.as_of_date.isoformat()}"
    )
    return {
        "company": _company_block(request),
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "validation_run_id": snapshot.validation_run_id,
            "period_start": _date(snapshot.period_start),
            "period_end": _date(snapshot.period_end),
            "as_of_date": _date(snapshot.as_of_date),
            "source_endpoint": snapshot.source_endpoint,
            "ruleset_version": snapshot.ruleset_version,
            "payload_hash": snapshot.payload_hash,
            "balanced": snapshot.balanced,
            "total_debit": snapshot.total_debit,
            "total_credit": snapshot.total_credit,
            "created_at": _datetime(snapshot.created_at),
        },
        "validation": {
            "run_id": run.run_id,
            "overall_outcome": str(run.overall_outcome),
            # "Approved" is the run's own verdict: a snapshot whose run
            # is valid. Failed runs are served too, with the failure
            # stated — hiding them would leave the model guessing.
            "is_valid": run.is_valid,
            "approved": run.is_valid,
            "rules_executed": run.rules_executed,
            "rules_passed": run.rules_passed,
            "rules_failed": run.rules_failed,
            "rules_not_applicable": run.rules_not_applicable,
            "rules_errored": run.rules_errored,
            "coverage": {
                "corpus_standards": run.coverage.corpus_standards,
                "standards_with_rules": run.coverage.standards_with_rules,
                "rules_implemented": run.coverage.rules_implemented,
                "rules_verified": run.coverage.rules_verified,
                "rules_executed": run.coverage.rules_executed,
            },
            "results": [
                {
                    "rule_id": str(result.rule_id),
                    "standard_code": str(result.standard_code),
                    "revision_id": result.revision_id,
                    "outcome": str(result.outcome),
                    "severity": str(result.severity),
                    "message": result.message,
                    "detail": result.detail,
                }
                for result in run.results
            ],
            "not_executed_count": len(run.not_executed),
        },
        # The trial-balance document the run evaluated (integer Rial,
        # JSON-safe by construction — see ReportSnapshot).
        "trial_balance": snapshot.payload,
        "provenance": _provenance(
            request,
            report_type="report-snapshot",
            period_label=period_label,
            period_from=_date(snapshot.period_start),
            period_to=_date(snapshot.period_end),
            source_engine="Tryton",
            snapshot_ref=snapshot.snapshot_id,
            source_ref=snapshot.source_endpoint,
            ruleset_version=snapshot.ruleset_version,
            generated_at=snapshot.created_at,
        ),
    }


def _read_integration_compliance_status(request: ResourceRequest) -> JsonDict:
    """Integration + compliance status summary (no secrets, ever).

    Channels, bank connections and stores come from the installation
    registry (scoped by the caller's installation); bank connections
    are additionally filtered to the named company. Moadian data is a
    status summary only — profile metadata and document counts by
    status, never document payloads. Credential material is never
    carried: bank connections expose only ``has_credentials``, stores
    only ``has_webhook_secret`` / ``has_api_key``.
    """
    services = request.services
    installation_id = request.client.installation_id
    tenant_id = request.client.tenant_id

    channels = []
    for channel in services.integrations.list_channels(installation_id):
        try:
            contract = services.integrations.get_active_contract(
                installation_id, channel.channel_key
            )
        except IntegrationContractNotFoundError:
            contract = None
        channels.append(
            {
                "channel_key": channel.channel_key,
                "display_name_fa": channel.display_name_fa,
                "channel_type": channel.channel_type,
                "is_builtin": channel.is_builtin,
                "enabled": channel.enabled,
                "status": str(channel.status),
                "has_active_contract": contract is not None,
                "active_contract": (
                    {
                        "version": contract.version,
                        "valid_from": _date(contract.valid_from),
                        "valid_to": _date(contract.valid_to),
                        # Decimal as a safe decimal string — never a float.
                        "fee_percent": str(contract.fee_percent),
                        "fee_vat_rate": (
                            str(contract.fee_vat_rate)
                            if contract.fee_vat_rate is not None
                            else None
                        ),
                        "fee_vat_claimable": contract.fee_vat_claimable,
                        "fee_timing": contract.fee_timing,
                        "fee_accrual_policy": contract.fee_accrual_policy,
                        "settlement_model": contract.settlement_model,
                        "payout_calendar": contract.payout_calendar,
                        "auto_post_policy": str(contract.auto_post_policy),
                        "status": str(contract.status),
                    }
                    if contract is not None
                    else None
                ),
            }
        )

    bank_connections = [
        {
            "bank_key": connection.bank_key,
            "display_name": connection.display_name,
            "connection_kind": connection.connection_kind,
            "status": connection.status,
            "has_credentials": connection.has_credentials,
            "last_checked_at": _datetime(connection.last_checked_at),
        }
        for connection in services.integrations.list_bank_connections(
            installation_id, company_id=request.row.id
        )
    ]

    stores = [
        {
            "store_key": store.store_key,
            "display_name": store.display_name,
            "store_type": store.store_type,
            "prices_in_toman": store.prices_in_toman,
            "has_webhook_secret": store.has_webhook_secret,
            "has_api_key": store.has_api_key,
            "has_store_db_dsn": store.has_store_db_dsn,
            "api_key_scopes": list(store.api_key_scopes),
            "status": store.status,
            "last_webhook_at": _datetime(store.last_webhook_at),
            "last_poll_at": _datetime(store.last_poll_at),
        }
        for store in services.integrations.list_stores(installation_id)
    ]

    profiles = [
        {
            "id": profile.id,
            "name": profile.name,
            "slug": profile.slug,
            "environment": str(profile.environment),
            "sender_mode": str(profile.sender_mode),
            "verification_status": str(profile.verification_status),
            "is_active": profile.is_active,
        }
        for profile in services.moadian.list_profiles(tenant_id)
    ]
    documents_by_status: dict[str, int] = {
        "draft": 0,
        "submitted": 0,
        "accepted": 0,
        "rejected": 0,
    }
    for document in services.moadian.list_documents(tenant_id):
        documents_by_status[str(document.status)] = (
            documents_by_status.get(str(document.status), 0) + 1
        )

    coverage = services.standards.coverage()
    active_tax_versions = [
        version for version in services.tax_policy.list_versions() if version.status == "active"
    ]

    return {
        "company": _company_block(request),
        "integrations": {
            "channels": channels,
            "bank_connections": bank_connections,
            "stores": stores,
        },
        "compliance": {
            "moadian": {
                "profiles": profiles,
                "documents_by_status": documents_by_status,
            },
            "standards_coverage": _coverage_block(coverage),
            "active_tax_policy_versions": len(active_tax_versions),
        },
        "provenance": _provenance(
            request,
            report_type="integration-compliance-status",
            period_label="current integration and compliance status",
            source_engine="HesabYar",
        ),
    }


_READERS: dict[str, Reader] = {
    "chart-of-accounts": _read_chart_of_accounts,
    "fiscal-periods": _read_fiscal_periods,
    "standards": _read_standards,
    "tax-policy": _read_tax_policy,
    "report-snapshot": _read_report_snapshot,
    "integration-compliance-status": _read_integration_compliance_status,
}


# -- serving ------------------------------------------------------------------

async def _serve(
    ctx: Context | None,
    *,
    scopes: tuple[str, ...],
    company: str,
    params: dict[str, str],
    reader: Reader,
) -> str:
    """Authorize a resource read, then run ``reader`` in a thread.

    Order matters: identity + scopes first (no DB), then the company
    grant check, then the data read — a denied call never reaches the
    services. Application errors from the reader become a clear
    internal error (the message names what is missing); auth errors
    (``forbidden`` from the snapshot company check) pass through
    unchanged so the middleware audits them as denied.
    """
    mcp_context = require_mcp_context(ctx)
    client = authenticated_client(mcp_context, scopes)

    def _run() -> str:
        session = mcp_context.session_factory()
        try:
            try:
                row = resolve_company_row(session, client, company)
            except CompanyAccessError as exc:
                raise forbidden(exc.message) from None
            try:
                ref = CompanyRef(engine_id=int(row.external_company_id))
            except ValueError:
                raise internal_error(
                    "The company mapping is invalid. Please contact support."
                ) from None
            services = mcp_context.services(session)
            try:
                payload = reader(
                    ResourceRequest(
                        services=services, client=client, row=row, ref=ref, params=params
                    )
                )
            except HesabYarError as exc:
                raise internal_error(exc.message) from None
            finally:
                services.close()
            return json.dumps(payload, ensure_ascii=False)
        finally:
            session.close()

    return await asyncio.to_thread(_run)


def _company_handler(scopes: tuple[str, ...], reader: Reader) -> Callable[..., Any]:
    """Build the SDK handler for a ``{company}``-only template."""

    async def handler(company: str, ctx: Context | None = None) -> str:
        return await _serve(ctx, scopes=scopes, company=company, params={}, reader=reader)

    return handler


def _company_run_handler(scopes: tuple[str, ...], reader: Reader) -> Callable[..., Any]:
    """Build the SDK handler for a ``{company}`` + ``{run_id}`` template."""

    async def handler(company: str, run_id: str, ctx: Context | None = None) -> str:
        return await _serve(
            ctx, scopes=scopes, company=company, params={"run_id": run_id}, reader=reader
        )

    return handler


# -- registration ---------------------------------------------------------------

def register_hesabyar_resource(
    server: MCPServer[Any],
    registry: ToolRegistry,
    fn: Callable[..., Any],
    *,
    uri_template: str,
    name: str,
    scopes: Iterable[str],
    description: str,
) -> None:
    """Register a resource on ``server`` AND record its scopes in ``registry``.

    The two registrations are one operation on purpose: a resource
    that is readable but has no scope record would bypass the
    handler's scope check. Resource authors must use this helper
    (never ``server.resource`` directly) — and build the handler with
    the ``_company_*_handler`` factories, so scopes + the company
    grant are enforced by construction.
    """
    registry.register_resource(uri_template, scopes)
    server.resource(
        uri_template, name=name, description=description, mime_type=RESOURCE_MIME_TYPE
    )(fn)


def register_resources(server: MCPServer[Any], context: McpContext) -> None:
    """Register all six US-604 resources on ``server``."""
    for definition in RESOURCE_DEFINITIONS:
        reader = _READERS[definition.name]
        if definition.params == ("run_id",):
            handler = _company_run_handler(definition.scopes, reader)
        elif not definition.params:
            handler = _company_handler(definition.scopes, reader)
        else:  # pragma: no cover - guarded by the definitions table
            raise ValueError(f"Unsupported resource params: {definition.params!r}")
        register_hesabyar_resource(
            server,
            context.registry,
            handler,
            uri_template=definition.uri_template,
            name=definition.name,
            scopes=definition.scopes,
            description=definition.description,
        )
