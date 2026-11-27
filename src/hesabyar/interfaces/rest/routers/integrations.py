"""Integrations routes: canonical event intake + registries + policy gate.

US-501 (event platform) + US-503 (registries + policy gate), one router:
the two stories share the ``/integrations`` prefix with no path overlap.

Event platform (US-501) — thin routes over the event
ingest/pipeline/worker/query services: no envelope validation here, no
engine calls — the services own those.

- ``POST /integrations/{connector}/events`` — the canonical events
  inbound API (§10.2): verify + store the raw event in one
  transaction. 201 for a new event, 200 for an idempotent re-send,
  409 when a key is re-used with a different payload hash. The body
  is the 15-field envelope; the canonical event type travels
  alongside it in the ``X-HesabYar-Event-Type`` header (required,
  one of the nine §10.2 types).
- ``GET /integrations/inbox`` / ``{event_id}`` — inbox reads.
- ``GET /integrations/inbox/stale`` — the staleness alert.
- ``POST /integrations/inbox/{event_id}/replay`` — manual DLQ replay
  (same idempotency key, no duplicate effect).
- ``GET /integrations/outbox`` — outbox reads.
- ``GET /integrations/audit-log`` — automation-decision audit log.
- ``GET|PUT /integrations/stores/{system}/{store}/settings`` — the
  per-store "prices are in Toman" setting.
- ``POST /integrations/worker/poll`` — run one worker sweep now.

Integration registry + policy gate (US-503, Phase 5 F3) — thin routes
over the integration services: no registry SQL here, no gate logic —
the services own that.

Gateway channels (US-524, Phase 5-3 Wave B) — the channel's gateway
API credentials live encrypted on the channel row:

- ``PUT /integrations/channels/{channel_key}/credentials`` — replace
  a channel's stored gateway credentials (encrypted at rest; the
  response carries only ``has_credentials``).

Store reconciliation & backfill (US-512, S2) — poll one store's
changes and ingest the missed events through the same idempotency
path as webhooks (same key, no duplicate effect):

- ``POST /integrations/{connector}/sync`` — poll (HTTP changes API
  or read-only store-DB read) + backfill; returns a sync report
  with created/duplicate/unmapped counts and a resume cursor.
- ``POST /integrations/stores/{store_key}/db-credentials`` — store
  a store's read-only database DSN (encrypted, never returned).

Reads need ``integrations:read``; every mutation needs
``integrations:manage``. Everything is tenant/installation-scoped: a
row of another installation answers 404 as if it did not exist.

No credential material ever appears in a response — bank-connection
and store responses carry only ``has_*`` flags. The two exceptions
are the store create/rotate responses, which return the freshly
generated secrets in plaintext EXACTLY ONCE.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from sqlalchemy.orm import Session

from hesabyar.application.commands.events import (
    DlqReplayService,
    EventIngestService,
    EventWorker,
    StalenessService,
    StoreSettingsService,
)
from hesabyar.application.commands.integrations import (
    BankConnectionService,
    ChannelRegistryService,
    GatewayContractService,
    InstallationSettingsService,
    MappingService,
    PolicyGateConfigService,
    PolicyGateService,
    StoreService,
)
from hesabyar.application.commands.sync import StoreSyncService
from hesabyar.application.queries.events import EventQueryService
from hesabyar.application.queries.integrations import IntegrationsQueryService
from hesabyar.domain.events.models import InboxEventStatus, IncomingEvent
from hesabyar.domain.events.sync import SyncCursor
from hesabyar.domain.integrations.models import (
    AutoPostPolicy,
    MappingKind,
    SuggestionMode,
    SuggestionStatus,
)
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.store_connectors.factory import StoreConnectorFactory
from hesabyar.interfaces.rest.dependencies import (
    company_ref_for_row,
    get_bank_connection_service,
    get_channel_registry_service,
    get_db_session,
    get_dlq_replay_service,
    get_event_ingest_service,
    get_event_query_service,
    get_event_worker,
    get_gateway_contract_service,
    get_installation_settings_service,
    get_integrations_query_service,
    get_mapping_service,
    get_policy_gate_config_service,
    get_policy_gate_service,
    get_staleness_service,
    get_store_connector_factory,
    get_store_service,
    get_store_settings_service,
    get_store_sync_service,
    require_company_row_by_id,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    paginate,
    parse_sort_param,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.gateways import RotateChannelCredentialsRequest
from hesabyar.interfaces.rest.schemas.integrations import (
    AddHolidayRequest,
    AutomationDecisionResponse,
    BankConnectionResponse,
    CreateBankConnectionRequest,
    CreateGatewayContractRequest,
    CreateIntegrationMappingRequest,
    CreateMappingSuggestionRequest,
    CreatePaymentChannelRequest,
    CreateStoreRequest,
    GatewayContractResponse,
    HolidayResponse,
    InboxEventResponse,
    IngestEventResponse,
    InstallationSettingsResponse,
    IntegrationMappingResponse,
    MappingSuggestionResponse,
    OutboxEventResponse,
    PaymentChannelResponse,
    PolicyGateConfigResponse,
    PolicyGateDecisionResponse,
    PolicyGateEvaluateRequest,
    ReplayResponse,
    RotateBankCredentialsRequest,
    RotateStoreApiKeyRequest,
    SetStoreDbCredentialsRequest,
    StaleInboxResponse,
    StoreResponse,
    StoreSecretsResponse,
    StoreSettingsRequest,
    StoreSettingsResponse,
    SyncStoreRequest,
    SyncStoreResponse,
    UpdateBankConnectionRequest,
    UpdatePaymentChannelRequest,
    UpdateStoreRequest,
    UpsertInstallationSettingsRequest,
    UpsertPolicyGateConfigRequest,
    WorkerPollResponse,
)
from hesabyar.interfaces.rest.schemas.pagination import PaginatedResponse

router = APIRouter(tags=["integrations"])

_MAPPING_KINDS = {kind.value for kind in MappingKind}
_SUGGESTION_MODES = {mode.value for mode in SuggestionMode}
_AUTO_POST_POLICIES = {policy.value for policy in AutoPostPolicy}


def _parse_mapping_kind(value: str | None) -> MappingKind | None:
    """Parse a mapping_kind filter/value, failing loudly on unknown labels."""
    if value is None:
        return None
    if value not in _MAPPING_KINDS:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown mapping_kind {value!r}. Allowed: {', '.join(sorted(_MAPPING_KINDS))}."
            ),
        )
    return MappingKind(value)


def _parse_suggestion_mode(value: str) -> SuggestionMode:
    """Parse a suggestion_mode value, failing loudly on unknown labels."""
    if value not in _SUGGESTION_MODES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown suggestion_mode {value!r}. "
                f"Allowed: {', '.join(sorted(_SUGGESTION_MODES))}."
            ),
        )
    return SuggestionMode(value)


def _parse_auto_post_policy(value: str) -> AutoPostPolicy:
    """Parse an auto_post_policy value, failing loudly on unknown labels."""
    if value not in _AUTO_POST_POLICIES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown auto_post_policy {value!r}. "
                f"Allowed: {', '.join(sorted(_AUTO_POST_POLICIES))}."
            ),
        )
    return AutoPostPolicy(value)


# -- payment channels -----------------------------------------------------


_CHANNELS_SORT_KEYS: dict[str, Callable[[PaymentChannelResponse], str]] = {
    "channel_key": lambda item: item.channel_key,
    "display_name_fa": lambda item: item.display_name_fa,
}


def _channels_tie_key(item: PaymentChannelResponse) -> str:
    """Tiebreaker for channel pagination: channel key (total order)."""
    return item.channel_key


_CHANNELS_QUERY_PARAMS = {"enabled", "channel_type", "is_builtin", "sort", "limit", "cursor"}


@router.get(
    "/integrations/channels",
    response_model=PaginatedResponse[PaymentChannelResponse],
    summary="List payment channels in the installation's registry",
)
def list_payment_channels(
    request: Request,
    enabled: bool | None = Query(default=None, description="Filter to enabled/disabled channels"),
    channel_type: str | None = Query(default=None, description="Filter by channel kind"),
    is_builtin: bool | None = Query(default=None, description="Filter to built-in/custom channels"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'channel_key' (default) or 'display_name_fa'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaginatedResponse[PaymentChannelResponse]:
    """Return the installation's channels, cursor-paginated."""
    reject_unknown_params(request, _CHANNELS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _CHANNELS_SORT_KEYS, default="channel_key")
    with translate_errors():
        channels = service.list_channels(client.installation_id)
    items = [PaymentChannelResponse.from_channel(channel) for channel in channels]
    if enabled is not None:
        items = [item for item in items if item.enabled == enabled]
    if channel_type is not None:
        items = [item for item in items if item.channel_type == channel_type]
    if is_builtin is not None:
        items = [item for item in items if item.is_builtin == is_builtin]
    page, next_cursor = paginate(
        items,
        key_fn=_CHANNELS_SORT_KEYS[field],
        tie_fn=_channels_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/integrations/channels/seed",
    response_model=PaginatedResponse[PaymentChannelResponse],
    status_code=201,
    summary="Seed the built-in channels for this installation (idempotent)",
)
def seed_builtin_channels(
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ChannelRegistryService = Depends(get_channel_registry_service),
) -> PaginatedResponse[PaymentChannelResponse]:
    """Create the eight built-in channel rows (disabled, setup status).

    Idempotent: re-running returns the same rows. Ilmaz is NOT among
    them (ADR 0003: it is not a payment channel). ``torob_marketplace``
    is RESERVED (US-524): born disabled, never enableable.
    """
    with translate_errors():
        channels = service.seed_builtin_channels(client.installation_id)
    session.commit()
    items = [PaymentChannelResponse.from_channel(channel) for channel in channels]
    return PaginatedResponse(items=items, next_cursor=None)


@router.post(
    "/integrations/channels",
    response_model=PaymentChannelResponse,
    status_code=201,
    summary="Create a custom payment channel",
)
def create_payment_channel(
    body: CreatePaymentChannelRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ChannelRegistryService = Depends(get_channel_registry_service),
) -> PaymentChannelResponse:
    """Create a custom channel (born disabled, status 'setup').

    Duplicate channel key → 409 ``INTEGRATION_CHANNEL_KEY_CONFLICT``.
    """
    with translate_errors():
        channel = service.create_channel(
            client.installation_id,
            body.channel_key,
            body.display_name_fa,
            body.channel_type,
            merchant_ref=body.merchant_ref,
            settlement_iban=body.settlement_iban,
            environment=body.environment,
            notes=body.notes,
        )
    session.commit()
    return PaymentChannelResponse.from_channel(channel)


@router.get(
    "/integrations/channels/{channel_key}",
    response_model=PaymentChannelResponse,
    summary="Get one payment channel",
)
def get_payment_channel(
    channel_key: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaymentChannelResponse:
    """Return one channel. Unknown key → 404 ``INTEGRATION_CHANNEL_NOT_FOUND``."""
    with translate_errors():
        channel = service.get_channel(client.installation_id, channel_key)
    return PaymentChannelResponse.from_channel(channel)


@router.patch(
    "/integrations/channels/{channel_key}",
    response_model=PaymentChannelResponse,
    summary="Update a payment channel (enable/disable, details)",
)
def update_payment_channel(
    channel_key: str,
    body: UpdatePaymentChannelRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ChannelRegistryService = Depends(get_channel_registry_service),
) -> PaymentChannelResponse:
    """Update a channel. Enabling without an active contract leaves it in 'setup'."""
    with translate_errors():
        channel = service.update_channel(
            client.installation_id,
            channel_key,
            display_name_fa=body.display_name_fa,
            channel_type=body.channel_type,
            enabled=body.enabled,
            merchant_ref=body.merchant_ref,
            settlement_iban=body.settlement_iban,
            environment=body.environment,
            notes=body.notes,
        )
    session.commit()
    return PaymentChannelResponse.from_channel(channel)


@router.delete(
    "/integrations/channels/{channel_key}",
    status_code=204,
    summary="Delete a payment channel (refused while contracts/mappings reference it)",
)
def delete_payment_channel(
    channel_key: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ChannelRegistryService = Depends(get_channel_registry_service),
) -> Response:
    """Delete a channel. In use → 409 ``INTEGRATION_CHANNEL_IN_USE`` (disable instead)."""
    with translate_errors():
        service.delete_channel(client.installation_id, channel_key)
    session.commit()
    return Response(status_code=204)


@router.put(
    "/integrations/channels/{channel_key}/credentials",
    response_model=PaymentChannelResponse,
    summary="Replace a channel's stored gateway credentials (encrypted at rest)",
)
def rotate_channel_credentials(
    channel_key: str,
    body: RotateChannelCredentialsRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: ChannelRegistryService = Depends(get_channel_registry_service),
) -> PaymentChannelResponse:
    """Rotate credentials. The response carries only ``has_credentials`` — never the payload.

    Gateway OAuth passwords are legitimate API credentials here (unlike
    internet-banking passwords, which are never accepted for bank
    connections). Unknown channel → 404 ``INTEGRATION_CHANNEL_NOT_FOUND``.
    """
    with translate_errors():
        channel = service.rotate_channel_credentials(
            client.installation_id, channel_key, body.credentials
        )
    session.commit()
    return PaymentChannelResponse.from_channel(channel)


# -- gateway contracts ----------------------------------------------------


_CONTRACTS_SORT_KEYS: dict[str, Callable[[GatewayContractResponse], str]] = {
    "version": lambda item: f"{item.version:010d}",
    "valid_from": lambda item: item.valid_from.isoformat(),
}


def _contracts_tie_key(item: GatewayContractResponse) -> str:
    """Tiebreaker for contract pagination: version (total order)."""
    return f"{item.version:010d}"


_CONTRACTS_QUERY_PARAMS = {"sort", "limit", "cursor"}


@router.get(
    "/integrations/channels/{channel_key}/contracts",
    response_model=PaginatedResponse[GatewayContractResponse],
    summary="List a channel's contract versions (default sort: version ascending)",
)
def list_gateway_contracts(
    channel_key: str,
    request: Request,
    sort: str | None = Query(
        default=None,
        description="Sort field: 'version' (default) or 'valid_from'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaginatedResponse[GatewayContractResponse]:
    """Return a channel's contract versions, cursor-paginated."""
    reject_unknown_params(request, _CONTRACTS_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _CONTRACTS_SORT_KEYS, default="version")
    with translate_errors():
        contracts = service.list_contracts(client.installation_id, channel_key)
    items = [GatewayContractResponse.from_contract(contract) for contract in contracts]
    page, next_cursor = paginate(
        items,
        key_fn=_CONTRACTS_SORT_KEYS[field],
        tie_fn=_contracts_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/integrations/channels/{channel_key}/contracts",
    response_model=GatewayContractResponse,
    status_code=201,
    summary="Create a NEW contract version (a rate change is always a new version)",
)
def create_gateway_contract(
    channel_key: str,
    body: CreateGatewayContractRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: GatewayContractService = Depends(get_gateway_contract_service),
) -> GatewayContractResponse:
    """Create version N+1; the previous active version is superseded (never rewritten).

    Invalid legs/rates/dates → 422 ``INTEGRATION_CONTRACT_INVALID``.
    """
    with translate_errors():
        contract = service.create_version(
            client.installation_id,
            channel_key,
            valid_from=body.valid_from,
            fee_percent=body.fee_percent,
            fee_vat_rate=body.fee_vat_rate,
            fee_vat_claimable=body.fee_vat_claimable,
            fee_timing=body.fee_timing,
            fee_accrual_policy=body.fee_accrual_policy,
            settlement_model=body.settlement_model,
            tranche_legs=tuple(leg.to_domain() for leg in body.tranche_legs),
            payout_window_start_day=body.payout_window_start_day,
            payout_window_end_day=body.payout_window_end_day,
            payout_calendar=body.payout_calendar,
            working_day_adjustment=body.working_day_adjustment,
            day_lag=body.day_lag,
            grace_days_after_due=body.grace_days_after_due,
            match_tolerance_irr=body.match_tolerance_irr,
            tieout_tolerance_irr=body.tieout_tolerance_irr,
            auto_post_policy=_parse_auto_post_policy(body.auto_post_policy),
            auto_post_max_amount_irr=body.auto_post_max_amount_irr,
            source_note=body.source_note,
        )
    session.commit()
    return GatewayContractResponse.from_contract(contract)


@router.get(
    "/integrations/channels/{channel_key}/contracts/active",
    response_model=GatewayContractResponse,
    summary="Get a channel's active contract version",
)
def get_active_gateway_contract(
    channel_key: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> GatewayContractResponse:
    """Return the active contract. None exists → 404 ``INTEGRATION_CONTRACT_NOT_FOUND``."""
    with translate_errors():
        contract = service.get_active_contract(client.installation_id, channel_key)
    return GatewayContractResponse.from_contract(contract)


@router.get(
    "/integrations/channels/{channel_key}/contracts/{version}",
    response_model=GatewayContractResponse,
    summary="Get one contract version",
)
def get_gateway_contract(
    channel_key: str,
    version: int,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> GatewayContractResponse:
    """Return one version. Unknown version → 404 ``INTEGRATION_CONTRACT_NOT_FOUND``."""
    with translate_errors():
        contract = service.get_contract(client.installation_id, channel_key, version)
    return GatewayContractResponse.from_contract(contract)


# -- mapping center -------------------------------------------------------


_MAPPINGS_SORT_KEYS: dict[str, Callable[[IntegrationMappingResponse], str]] = {
    "mapping_kind": lambda item: item.mapping_kind,
    "source_key": lambda item: item.source_key,
    "version": lambda item: f"{item.version:010d}",
}


def _mappings_tie_key(item: IntegrationMappingResponse) -> str:
    """Tiebreaker for mapping pagination: kind + channel + source + version."""
    return f"{item.mapping_kind}/{item.channel_key}/{item.source_key}/{item.version:010d}"


_MAPPINGS_QUERY_PARAMS = {
    "mapping_kind",
    "channel_key",
    "source_key",
    "status",
    "sort",
    "limit",
    "cursor",
}


@router.get(
    "/integrations/mappings",
    response_model=PaginatedResponse[IntegrationMappingResponse],
    summary="List mappings in the mapping center",
)
def list_integration_mappings(
    request: Request,
    mapping_kind: str | None = Query(default=None, description="Filter by spec §10.5 kind"),
    channel_key: str | None = Query(default=None, description="Filter by owning channel key"),
    source_key: str | None = Query(default=None, description="Filter by external source key"),
    status: str | None = Query(default=None, description="Filter by lifecycle: 'active' | 'superseded'"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'mapping_kind' (default), 'source_key' or 'version'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaginatedResponse[IntegrationMappingResponse]:
    """Return mappings, cursor-paginated."""
    reject_unknown_params(request, _MAPPINGS_QUERY_PARAMS)
    kind = _parse_mapping_kind(mapping_kind)
    field, reverse = parse_sort_param(sort, _MAPPINGS_SORT_KEYS, default="mapping_kind")
    with translate_errors():
        mappings = service.list_mappings(
            client.installation_id,
            mapping_kind=kind,
            channel_key=channel_key,
            source_key=source_key,
            status=status,
        )
    items = [IntegrationMappingResponse.from_mapping(mapping) for mapping in mappings]
    page, next_cursor = paginate(
        items,
        key_fn=_MAPPINGS_SORT_KEYS[field],
        tie_fn=_mappings_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/integrations/mappings",
    response_model=IntegrationMappingResponse,
    status_code=201,
    summary="Create a NEW mapping version (supersedes the active one, if any)",
)
def create_integration_mapping(
    body: CreateIntegrationMappingRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: MappingService = Depends(get_mapping_service),
) -> IntegrationMappingResponse:
    """Create a mapping version. Unknown kind/channel → 400/404 (fail-closed)."""
    kind = _parse_mapping_kind(body.mapping_kind)
    assert kind is not None  # the body field is required
    mode = _parse_suggestion_mode(body.suggestion_mode)
    with translate_errors():
        mapping = service.create_mapping(
            client.installation_id,
            mapping_kind=kind,
            channel_key=body.channel_key,
            source_key=body.source_key,
            target_type=body.target_type,
            target_ref=body.target_ref,
            suggestion_mode=mode,
            source=body.source,
            confidence=body.confidence,
            notes=body.notes,
        )
    session.commit()
    return IntegrationMappingResponse.from_mapping(mapping)


@router.get(
    "/integrations/mappings/resolve",
    response_model=IntegrationMappingResponse,
    summary="Resolve the ACTIVE mapping for a (kind, channel, source) — fail-closed",
)
def resolve_integration_mapping(
    mapping_kind: str = Query(description="Mapping kind: one of the nine spec §10.5 kinds"),
    channel_key: str = Query(default="", description="Owning channel key, or '' for installation-wide"),
    source_key: str = Query(description="External source key to resolve"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: MappingService = Depends(get_mapping_service),
) -> IntegrationMappingResponse:
    """Resolve a mapping. No active mapping → 404 ``INTEGRATION_MAPPING_NOT_FOUND``.

    The pipeline treats the 404 as "unknown mapping" and routes the
    event to the review queue — never to a default account (§10.5).
    """
    kind = _parse_mapping_kind(mapping_kind)
    assert kind is not None  # the query param is required
    with translate_errors():
        mapping = service.resolve_mapping(
            client.installation_id, kind, channel_key, source_key
        )
    return IntegrationMappingResponse.from_mapping(mapping)


@router.get(
    "/integrations/mappings/{mapping_id}",
    response_model=IntegrationMappingResponse,
    summary="Get one mapping version",
)
def get_integration_mapping(
    mapping_id: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> IntegrationMappingResponse:
    """Return one mapping. Unknown id → 404 ``INTEGRATION_MAPPING_NOT_FOUND``."""
    with translate_errors():
        mapping = service.get_mapping(client.installation_id, mapping_id)
    return IntegrationMappingResponse.from_mapping(mapping)


@router.post(
    "/integrations/mappings/{mapping_id}/promote",
    response_model=IntegrationMappingResponse,
    summary="Promote an active Suggest mapping to Auto (US-552, §7.2)",
)
def promote_integration_mapping(
    mapping_id: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: MappingService = Depends(get_mapping_service),
) -> IntegrationMappingResponse:
    """Promote a mapping Suggest → Auto (a human decision).

    Creates a NEW version in ``auto`` mode (the Suggest version is
    superseded, never rewritten) with the confirmation count carried
    unchanged. Unknown id → 404 ``INTEGRATION_MAPPING_NOT_FOUND``;
    already-auto, superseded, or under-confirmed (fewer than 3
    confirmations) → 409 ``INTEGRATION_MAPPING_NOT_PROMOTABLE``.
    """
    with translate_errors():
        mapping = service.promote_mapping(
            client.installation_id, mapping_id, promoted_by=client.client_name
        )
    session.commit()
    return IntegrationMappingResponse.from_mapping(mapping)


# -- mapping suggestions --------------------------------------------------


_SUGGESTIONS_SORT_KEYS: dict[str, Callable[[MappingSuggestionResponse], str]] = {
    "source_key": lambda item: item.source_key,
    "confidence": lambda item: f"{item.confidence:08.4f}",
}


def _suggestions_tie_key(item: MappingSuggestionResponse) -> str:
    """Tiebreaker for suggestion pagination: id (total order)."""
    return item.id


_SUGGESTIONS_QUERY_PARAMS = {"status", "sort", "limit", "cursor"}


@router.get(
    "/integrations/mapping-suggestions",
    response_model=PaginatedResponse[MappingSuggestionResponse],
    summary="List mapping suggestions",
)
def list_mapping_suggestions(
    request: Request,
    status: str | None = Query(default=None, description="Filter by status: 'pending' | 'accepted' | 'rejected'"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'source_key' (default) or 'confidence'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaginatedResponse[MappingSuggestionResponse]:
    """Return suggestions, cursor-paginated."""
    reject_unknown_params(request, _SUGGESTIONS_QUERY_PARAMS)
    parsed_status = None
    if status is not None:
        try:
            parsed_status = SuggestionStatus(status)
        except ValueError:
            raise ApiError(
                status_code=400,
                code="INVALID_FILTER",
                detail=f"Unknown status {status!r}. Allowed: 'pending' | 'accepted' | 'rejected'.",
            ) from None
    field, reverse = parse_sort_param(sort, _SUGGESTIONS_SORT_KEYS, default="source_key")
    with translate_errors():
        suggestions = service.list_suggestions(client.installation_id, status=parsed_status)
    items = [MappingSuggestionResponse.from_suggestion(s) for s in suggestions]
    page, next_cursor = paginate(
        items,
        key_fn=_SUGGESTIONS_SORT_KEYS[field],
        tie_fn=_suggestions_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/integrations/mapping-suggestions",
    response_model=MappingSuggestionResponse,
    status_code=201,
    summary="Record a mapping suggestion (idempotent per pending suggestion)",
)
def create_mapping_suggestion(
    body: CreateMappingSuggestionRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: MappingService = Depends(get_mapping_service),
) -> MappingSuggestionResponse:
    """Record a proposed mapping for the operator to accept or reject."""
    kind = _parse_mapping_kind(body.mapping_kind)
    assert kind is not None  # the body field is required
    with translate_errors():
        suggestion = service.create_suggestion(
            client.installation_id,
            mapping_kind=kind,
            channel_key=body.channel_key,
            source_key=body.source_key,
            suggested_target_type=body.suggested_target_type,
            suggested_target_ref=body.suggested_target_ref,
            confidence=body.confidence,
            basis=body.basis,
        )
    session.commit()
    return MappingSuggestionResponse.from_suggestion(suggestion)


@router.get(
    "/integrations/mapping-suggestions/{suggestion_id}",
    response_model=MappingSuggestionResponse,
    summary="Get one mapping suggestion",
)
def get_mapping_suggestion(
    suggestion_id: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> MappingSuggestionResponse:
    """Return one suggestion. Unknown id → 404 ``INTEGRATION_MAPPING_SUGGESTION_NOT_FOUND``."""
    with translate_errors():
        suggestion = service.get_suggestion(client.installation_id, suggestion_id)
    return MappingSuggestionResponse.from_suggestion(suggestion)


@router.post(
    "/integrations/mapping-suggestions/{suggestion_id}/accept",
    response_model=IntegrationMappingResponse,
    summary="Accept a suggestion: create the mapping (Suggest mode first)",
)
def accept_mapping_suggestion(
    suggestion_id: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: MappingService = Depends(get_mapping_service),
) -> IntegrationMappingResponse:
    """Accept a pending suggestion. Already resolved → 409 ``INTEGRATION_MAPPING_SUGGESTION_RESOLVED``."""
    with translate_errors():
        mapping = service.accept_suggestion(client.installation_id, suggestion_id)
    session.commit()
    return IntegrationMappingResponse.from_mapping(mapping)


@router.post(
    "/integrations/mapping-suggestions/{suggestion_id}/reject",
    response_model=MappingSuggestionResponse,
    summary="Reject a suggestion (recorded, so it is not proposed again silently)",
)
def reject_mapping_suggestion(
    suggestion_id: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: MappingService = Depends(get_mapping_service),
) -> MappingSuggestionResponse:
    """Reject a pending suggestion. Already resolved → 409 ``INTEGRATION_MAPPING_SUGGESTION_RESOLVED``."""
    with translate_errors():
        suggestion = service.reject_suggestion(client.installation_id, suggestion_id)
    session.commit()
    return MappingSuggestionResponse.from_suggestion(suggestion)


# -- policy gate ----------------------------------------------------------


_POLICY_SORT_KEYS: dict[str, Callable[[PolicyGateConfigResponse], str]] = {
    "event_type": lambda item: item.event_type,
}


def _policy_tie_key(item: PolicyGateConfigResponse) -> str:
    """Tiebreaker for policy pagination: event type (total order)."""
    return item.event_type


_POLICY_QUERY_PARAMS = {"sort", "limit", "cursor"}


@router.get(
    "/integrations/policy-gate",
    response_model=PaginatedResponse[PolicyGateConfigResponse],
    summary="List the installation's auto-post policy rows",
)
def list_policy_gate_configs(
    request: Request,
    sort: str | None = Query(
        default=None,
        description="Sort field: 'event_type' (default); prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaginatedResponse[PolicyGateConfigResponse]:
    """Return the installation's policy rows, cursor-paginated."""
    reject_unknown_params(request, _POLICY_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _POLICY_SORT_KEYS, default="event_type")
    with translate_errors():
        configs = service.list_policy_configs(client.installation_id)
    items = [PolicyGateConfigResponse.from_config(config) for config in configs]
    page, next_cursor = paginate(
        items,
        key_fn=_POLICY_SORT_KEYS[field],
        tie_fn=_policy_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/integrations/policy-gate/{event_type}",
    response_model=PolicyGateConfigResponse,
    summary="Get one event type's auto-post policy row",
)
def get_policy_gate_config(
    event_type: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PolicyGateConfigResponse:
    """Return one policy row. No policy for the type → 404 ``POLICY_GATE_CONFIG_NOT_FOUND``."""
    with translate_errors():
        config = service.get_policy_config(client.installation_id, event_type)
    if config is None:
        raise ApiError(
            status_code=404,
            code="POLICY_GATE_CONFIG_NOT_FOUND",
            detail=f"No auto-post policy is configured for event type {event_type!r}.",
        )
    return PolicyGateConfigResponse.from_config(config)


@router.put(
    "/integrations/policy-gate/{event_type}",
    response_model=PolicyGateConfigResponse,
    summary="Create or replace one event type's auto-post policy row",
)
def upsert_policy_gate_config(
    event_type: str,
    body: UpsertPolicyGateConfigRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: PolicyGateConfigService = Depends(get_policy_gate_config_service),
) -> PolicyGateConfigResponse:
    """Upsert a policy row. Auto-post stays off until the installation opt-in is on too."""
    with translate_errors():
        config = service.upsert_config(
            client.installation_id,
            event_type,
            allowed=body.allowed,
            max_amount_irr=body.max_amount_irr,
            require_tieout=body.require_tieout,
        )
    session.commit()
    return PolicyGateConfigResponse.from_config(config)


@router.post(
    "/integrations/policy-gate/evaluate",
    response_model=PolicyGateDecisionResponse,
    summary="Dry-run the three-layer policy gate for one event (no posting happens)",
)
def evaluate_policy_gate(
    body: PolicyGateEvaluateRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    session: Session = Depends(get_db_session),
    service: PolicyGateService = Depends(get_policy_gate_service),
) -> PolicyGateDecisionResponse:
    """Evaluate the gate: event type + amount cap + fiscal-period lock (+ tie-out).

    The fiscal-period layer reads the engine through the port; an
    unreachable engine fails closed (deny). Unknown company → 404
    ``COMPANY_NOT_FOUND``; a key restricted to another company → 403.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    company = company_ref_for_row(row)
    with translate_errors():
        decision = service.evaluate(
            client.installation_id,
            event_type=body.event_type,
            company=company,
            event_date=body.event_date,
            amount_irr=body.amount_irr,
            channel_key=body.channel_key,
            tieout_ok=body.tieout_ok,
        )
    return PolicyGateDecisionResponse.from_decision(decision)


# -- bank connections -----------------------------------------------------


_BANK_SORT_KEYS: dict[str, Callable[[BankConnectionResponse], str]] = {
    "bank_key": lambda item: item.bank_key,
    "display_name": lambda item: item.display_name,
}


def _bank_tie_key(item: BankConnectionResponse) -> str:
    """Tiebreaker for bank-connection pagination: id (total order)."""
    return item.id


_BANK_QUERY_PARAMS = {"company_id", "sort", "limit", "cursor"}


@router.get(
    "/integrations/bank-connections",
    response_model=PaginatedResponse[BankConnectionResponse],
    summary="List bank connections (no credential material — only flags)",
)
def list_bank_connections(
    request: Request,
    company_id: str | None = Query(default=None, description="Filter by API company id"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'bank_key' (default) or 'display_name'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    session: Session = Depends(get_db_session),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaginatedResponse[BankConnectionResponse]:
    """Return the installation's bank connections, cursor-paginated."""
    reject_unknown_params(request, _BANK_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _BANK_SORT_KEYS, default="bank_key")
    company_row_id: str | None = None
    if company_id is not None:
        company_row_id = require_company_row_by_id(session, client, company_id).id
    with translate_errors():
        connections = service.list_bank_connections(
            client.installation_id, company_id=company_row_id
        )
    items = [BankConnectionResponse.from_connection(c) for c in connections]
    page, next_cursor = paginate(
        items,
        key_fn=_BANK_SORT_KEYS[field],
        tie_fn=_bank_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/integrations/bank-connections",
    response_model=BankConnectionResponse,
    status_code=201,
    summary="Create a bank connection (credentials are encrypted at rest)",
)
def create_bank_connection(
    body: CreateBankConnectionRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: BankConnectionService = Depends(get_bank_connection_service),
) -> BankConnectionResponse:
    """Create a connection for a company. A password field → 422
    ``INTEGRATION_CREDENTIALS_FORBIDDEN`` (never stored); no encryption
    key configured → 503 ``INTEGRATION_CREDENTIAL_VAULT_UNAVAILABLE``.
    """
    row = require_company_row_by_id(session, client, body.company_id)
    with translate_errors():
        connection = service.create_connection(
            client.installation_id,
            company_id=row.id,
            bank_key=body.bank_key,
            display_name=body.display_name,
            connection_kind=body.connection_kind,
            credentials=body.credentials,
            notes=body.notes,
        )
    session.commit()
    return BankConnectionResponse.from_connection(connection)


@router.get(
    "/integrations/bank-connections/{connection_id}",
    response_model=BankConnectionResponse,
    summary="Get one bank connection",
)
def get_bank_connection(
    connection_id: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> BankConnectionResponse:
    """Return one connection. Unknown id → 404 ``INTEGRATION_BANK_CONNECTION_NOT_FOUND``."""
    with translate_errors():
        connection = service.get_bank_connection(client.installation_id, connection_id)
    return BankConnectionResponse.from_connection(connection)


@router.patch(
    "/integrations/bank-connections/{connection_id}",
    response_model=BankConnectionResponse,
    summary="Update a bank connection (never its credentials)",
)
def update_bank_connection(
    connection_id: str,
    body: UpdateBankConnectionRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: BankConnectionService = Depends(get_bank_connection_service),
) -> BankConnectionResponse:
    """Update a connection's mutable fields."""
    with translate_errors():
        connection = service.update_connection(
            client.installation_id,
            connection_id,
            display_name=body.display_name,
            connection_kind=body.connection_kind,
            status=body.status,
            notes=body.notes,
        )
    session.commit()
    return BankConnectionResponse.from_connection(connection)


@router.post(
    "/integrations/bank-connections/{connection_id}/credentials",
    response_model=BankConnectionResponse,
    summary="Replace a connection's stored credentials (encrypted at rest)",
)
def rotate_bank_connection_credentials(
    connection_id: str,
    body: RotateBankCredentialsRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: BankConnectionService = Depends(get_bank_connection_service),
) -> BankConnectionResponse:
    """Rotate credentials. A password field → 422 (never stored)."""
    with translate_errors():
        connection = service.rotate_credentials(
            client.installation_id, connection_id, body.credentials
        )
    session.commit()
    return BankConnectionResponse.from_connection(connection)


@router.delete(
    "/integrations/bank-connections/{connection_id}",
    status_code=204,
    summary="Delete a bank connection (its stored ciphertext goes with it)",
)
def delete_bank_connection(
    connection_id: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: BankConnectionService = Depends(get_bank_connection_service),
) -> Response:
    """Delete a connection. Unknown id → 404 ``INTEGRATION_BANK_CONNECTION_NOT_FOUND``."""
    with translate_errors():
        service.delete_connection(client.installation_id, connection_id)
    session.commit()
    return Response(status_code=204)


# -- stores ---------------------------------------------------------------


_STORES_SORT_KEYS: dict[str, Callable[[StoreResponse], str]] = {
    "store_key": lambda item: item.store_key,
    "display_name": lambda item: item.display_name,
}


def _stores_tie_key(item: StoreResponse) -> str:
    """Tiebreaker for store pagination: store key (total order)."""
    return item.store_key


_STORES_QUERY_PARAMS = {"sort", "limit", "cursor"}


@router.get(
    "/integrations/stores",
    response_model=PaginatedResponse[StoreResponse],
    summary="List stores (no secret material — only flags)",
)
def list_stores(
    request: Request,
    sort: str | None = Query(
        default=None,
        description="Sort field: 'store_key' (default) or 'display_name'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> PaginatedResponse[StoreResponse]:
    """Return the installation's stores, cursor-paginated."""
    reject_unknown_params(request, _STORES_QUERY_PARAMS)
    field, reverse = parse_sort_param(sort, _STORES_SORT_KEYS, default="store_key")
    with translate_errors():
        stores = service.list_stores(client.installation_id)
    items = [StoreResponse.from_store(store) for store in stores]
    page, next_cursor = paginate(
        items,
        key_fn=_STORES_SORT_KEYS[field],
        tie_fn=_stores_tie_key,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.post(
    "/integrations/stores",
    response_model=StoreSecretsResponse,
    status_code=201,
    summary="Create a store (webhook secret + read-only API key returned ONCE)",
)
def create_store(
    body: CreateStoreRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: StoreService = Depends(get_store_service),
) -> StoreSecretsResponse:
    """Create a store. Copy the returned secrets now — no later read returns them."""
    with translate_errors():
        created = service.create_store(
            client.installation_id,
            store_key=body.store_key,
            display_name=body.display_name,
            store_type=body.store_type,
            prices_in_toman=body.prices_in_toman,
            api_key_scopes=body.api_key_scopes,
            api_base_url=body.api_base_url,
            notes=body.notes,
        )
    session.commit()
    return StoreSecretsResponse(
        store=StoreResponse.from_store(created.store),
        webhook_secret=created.webhook_secret,
        api_key=created.api_key,
    )


@router.get(
    "/integrations/stores/{store_key}",
    response_model=StoreResponse,
    summary="Get one store",
)
def get_store(
    store_key: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> StoreResponse:
    """Return one store. Unknown key → 404 ``INTEGRATION_STORE_NOT_FOUND``."""
    with translate_errors():
        store = service.get_store(client.installation_id, store_key)
    return StoreResponse.from_store(store)


@router.patch(
    "/integrations/stores/{store_key}",
    response_model=StoreResponse,
    summary="Update a store (never its secrets)",
)
def update_store(
    store_key: str,
    body: UpdateStoreRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: StoreService = Depends(get_store_service),
) -> StoreResponse:
    """Update a store's mutable fields."""
    with translate_errors():
        store = service.update_store(
            client.installation_id,
            store_key,
            display_name=body.display_name,
            store_type=body.store_type,
            prices_in_toman=body.prices_in_toman,
            api_key_scopes=body.api_key_scopes,
            api_base_url=body.api_base_url,
            status=body.status,
            notes=body.notes,
        )
    session.commit()
    return StoreResponse.from_store(store)


@router.post(
    "/integrations/stores/{store_key}/rotate-webhook-secret",
    response_model=StoreSecretsResponse,
    summary="Generate a new webhook secret (returned ONCE)",
)
def rotate_store_webhook_secret(
    store_key: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: StoreService = Depends(get_store_service),
) -> StoreSecretsResponse:
    """Rotate the webhook secret. The API key does not change (empty in the response)."""
    with translate_errors():
        created = service.rotate_webhook_secret(client.installation_id, store_key)
    session.commit()
    return StoreSecretsResponse(
        store=StoreResponse.from_store(created.store),
        webhook_secret=created.webhook_secret,
        api_key=created.api_key,
    )


@router.post(
    "/integrations/stores/{store_key}/rotate-api-key",
    response_model=StoreSecretsResponse,
    summary="Generate a new read-only API key (returned ONCE)",
)
def rotate_store_api_key(
    store_key: str,
    body: RotateStoreApiKeyRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: StoreService = Depends(get_store_service),
) -> StoreSecretsResponse:
    """Rotate the API key. The webhook secret does not change (empty in the response)."""
    with translate_errors():
        created = service.rotate_api_key(
            client.installation_id, store_key, api_key_scopes=body.api_key_scopes
        )
    session.commit()
    return StoreSecretsResponse(
        store=StoreResponse.from_store(created.store),
        webhook_secret=created.webhook_secret,
        api_key=created.api_key,
    )


@router.post(
    "/integrations/stores/{store_key}/db-credentials",
    response_model=StoreResponse,
    summary="Store a store's read-only database DSN (encrypted, never returned)",
)
def set_store_db_credentials(
    store_key: str,
    body: SetStoreDbCredentialsRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: StoreService = Depends(get_store_service),
) -> StoreResponse:
    """Store the read-only DSN for DB backfill (ADR 0003 decision 3).

    The DSN authenticates as the store team's read-only DB user and
    is used ONLY for reconciliation/backfill reads (zero writes).
    The response carries only the ``has_store_db_dsn`` flag — the
    DSN is never returned.
    """
    with translate_errors():
        store = service.set_store_db_dsn(client.installation_id, store_key, dsn=body.dsn)
    session.commit()
    return StoreResponse.from_store(store)


@router.delete(
    "/integrations/stores/{store_key}",
    status_code=204,
    summary="Delete a store (its stored secrets go with it)",
)
def delete_store(
    store_key: str,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: StoreService = Depends(get_store_service),
) -> Response:
    """Delete a store. Unknown key → 404 ``INTEGRATION_STORE_NOT_FOUND``."""
    with translate_errors():
        service.delete_store(client.installation_id, store_key)
    session.commit()
    return Response(status_code=204)


# -- installation settings + holidays -------------------------------------


@router.get(
    "/integrations/settings",
    response_model=InstallationSettingsResponse,
    summary="Get this installation's integration settings (defaults when unset)",
)
def get_installation_settings(
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> InstallationSettingsResponse:
    """Return the settings row, or the fail-closed defaults when unset."""
    with translate_errors():
        settings = service.get_settings(client.installation_id)
    return InstallationSettingsResponse.from_settings(settings)


@router.put(
    "/integrations/settings",
    response_model=InstallationSettingsResponse,
    summary="Create or replace this installation's settings row",
)
def upsert_installation_settings(
    body: UpsertInstallationSettingsRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: InstallationSettingsService = Depends(get_installation_settings_service),
) -> InstallationSettingsResponse:
    """Upsert the settings row. Bad weekday/window → 422 ``INTEGRATION_SETTINGS_INVALID``."""
    with translate_errors():
        settings = service.upsert_settings(
            client.installation_id,
            weekly_holiday=body.weekly_holiday,
            matching_window_days=body.matching_window_days,
            auto_post_enabled=body.auto_post_enabled,
        )
    session.commit()
    return InstallationSettingsResponse.from_settings(settings)


@router.get(
    "/integrations/holidays",
    response_model=list[HolidayResponse],
    summary="List this installation's holidays",
)
def list_holidays(
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: IntegrationsQueryService = Depends(get_integrations_query_service),
) -> list[HolidayResponse]:
    """Return the installation's holidays, ordered by date."""
    with translate_errors():
        holidays = service.list_holidays(client.installation_id)
    return [HolidayResponse.from_holiday(holiday) for holiday in holidays]


@router.post(
    "/integrations/holidays",
    response_model=HolidayResponse,
    status_code=201,
    summary="Add a holiday date (idempotent)",
)
def add_holiday(
    body: AddHolidayRequest,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: InstallationSettingsService = Depends(get_installation_settings_service),
) -> HolidayResponse:
    """Add a holiday date. Re-adding the same date returns the same row."""
    with translate_errors():
        holiday = service.add_holiday(
            client.installation_id, body.holiday_date, body.title
        )
    session.commit()
    return HolidayResponse.from_holiday(holiday)


@router.delete(
    "/integrations/holidays/{holiday_date}",
    status_code=204,
    summary="Remove a holiday date",
)
def remove_holiday(
    holiday_date: date,
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    service: InstallationSettingsService = Depends(get_installation_settings_service),
) -> Response:
    """Remove a holiday date. Not listed → 404 ``INTEGRATION_HOLIDAY_NOT_FOUND``."""
    with translate_errors():
        service.remove_holiday(client.installation_id, holiday_date)
    session.commit()
    return Response(status_code=204)

# ---------------------------------------------------------------------------
# Event platform (US-501) — canonical intake + inbox/outbox/audit/settings
# ---------------------------------------------------------------------------


#: Cap on rows fetched for one in-memory cursor page (the Moadian
#: pattern fetches the filtered list, then paginates in memory).
_LIST_FETCH_CAP = 1000

_INBOX_STATUSES = {status.value for status in InboxEventStatus}
_OUTBOX_STATUSES = {"pending", "published", "failed"}
_DECISIONS = {"draft", "auto_post", "needs_review", "dead_letter", "replayed"}

_INBOX_QUERY_PARAMS = {"status", "source_system", "event_type", "sort", "limit", "cursor"}
_OUTBOX_QUERY_PARAMS = {"status", "event_type", "sort", "limit", "cursor"}
_AUDIT_QUERY_PARAMS = {"inbox_event_id", "decision", "event_type", "sort", "limit", "cursor"}
_STALE_QUERY_PARAMS = {"hours", "limit"}

_INBOX_SORT_KEYS: dict[str, Callable[[InboxEventResponse], str]] = {
    "received_at": lambda item: item.received_at.isoformat(),
    "event_type": lambda item: item.event_type,
    "status": lambda item: item.status,
    "source_system": lambda item: item.source_system,
}

_OUTBOX_SORT_KEYS: dict[str, Callable[[OutboxEventResponse], str]] = {
    "created_at": lambda item: item.created_at.isoformat(),
    "event_type": lambda item: item.event_type,
    "status": lambda item: item.status,
}

_AUDIT_SORT_KEYS: dict[str, Callable[[AutomationDecisionResponse], str]] = {
    "decided_at": lambda item: item.decided_at.isoformat(),
    "decision": lambda item: item.decision,
    "event_type": lambda item: item.event_type,
}


def _parse_inbox_status(status: str | None) -> InboxEventStatus | None:
    if status is None:
        return None
    if status not in _INBOX_STATUSES:
        raise ApiError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"Unknown inbox status {status!r}. Known: {', '.join(sorted(_INBOX_STATUSES))}.",
        )
    return InboxEventStatus(status)


def _parse_outbox_status(status: str | None) -> str | None:
    if status is None:
        return None
    if status not in _OUTBOX_STATUSES:
        raise ApiError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"Unknown outbox status {status!r}. Known: {', '.join(sorted(_OUTBOX_STATUSES))}.",
        )
    return status


def _parse_decision(decision: str | None) -> str | None:
    if decision is None:
        return None
    if decision not in _DECISIONS:
        raise ApiError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"Unknown decision {decision!r}. Known: {', '.join(sorted(_DECISIONS))}.",
        )
    return decision


@router.post(
    "/integrations/{connector}/events",
    response_model=IngestEventResponse,
    summary="Ingest a canonical event",
)
async def ingest_event(
    request: Request,
    response: Response,
    connector: str = Path(..., description="Registered connector name, e.g. 'hayat'"),
    signature: str | None = Header(
        default=None,
        alias="X-HesabYar-Signature",
        description="HMAC-SHA256 signature of the raw body (hex or base64), when the store has a secret configured",
    ),
    event_type: str | None = Header(
        default=None,
        alias="X-HesabYar-Event-Type",
        description=(
            "Canonical event type, e.g. 'sales.order.completed' — one of the nine §10.2 types — "
            "or a store webhook topic, e.g. 'order.completed' (US-511, mapped to canonical types); "
            "travels alongside the envelope, never inside it"
        ),
    ),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    service: EventIngestService = Depends(get_event_ingest_service),
    session: Session = Depends(get_db_session),
) -> IngestEventResponse:
    """Verify + store one event (201 created, 200 duplicate, 409 conflict).

    The ``X-HesabYar-Event-Type`` header carries a canonical type or a
    store webhook topic (``order.completed``, ``refund.created`` ...);
    topics are mapped to canonical types before ingest, and
    ``refund.created`` fans out to two canonical events (the derived
    inbox ids are returned in the response).
    """
    raw_body = await request.body()
    incoming = IncomingEvent(
        connector=connector, raw_body=raw_body, signature=signature, event_type=event_type
    )
    with translate_errors():
        result = service.ingest(incoming, tenant_id=client.tenant_id, installation_id=client.installation_id)
    session.commit()
    response.status_code = 201 if result.status == "created" else 200
    return IngestEventResponse.from_result(result)


@router.post(
    "/integrations/{connector}/sync",
    response_model=SyncStoreResponse,
    summary="Poll one store's changes and ingest the missed events (reconciliation/backfill)",
)
def sync_store(
    body: SyncStoreRequest,
    connector: str = Path(..., description="Registered connector name, e.g. 'hayat'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    session: Session = Depends(get_db_session),
    query_service: IntegrationsQueryService = Depends(get_integrations_query_service),
    store_service: StoreService = Depends(get_store_service),
    sync_service: StoreSyncService = Depends(get_store_sync_service),
    factory: StoreConnectorFactory = Depends(get_store_connector_factory),
) -> SyncStoreResponse:
    """Poll a store and ingest what the webhooks missed (same idempotency keys, no duplicates).

    ``source=api`` polls the store's HTTP changes API;
    ``source=db`` reads the store database directly (read-only).
    A run that fails after fetching pages returns ``status=partial``
    with a resume cursor instead of losing the fetched pages.
    """
    with translate_errors():
        store = query_service.get_store(client.installation_id, connector)
        updated_since = body.updated_since
        if updated_since is None and body.backfill_days is not None:
            updated_since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
                days=body.backfill_days
            )
        try:
            connector_obj = factory.build(
                store, source_store_id=body.source_store_id, source=body.source
            )
            report = sync_service.sync(
                connector_obj,
                source_system=connector,
                source_store_id=body.source_store_id,
                connector_kind=body.source,
                tenant_id=client.tenant_id,
                installation_id=client.installation_id,
                updated_since=updated_since,
                cursor=SyncCursor(value=body.cursor) if body.cursor else None,
                page_size=body.page_size,
                max_pages=body.max_pages,
            )
        finally:
            factory.close()
        store_service.record_successful_poll(client.installation_id, connector)
    session.commit()
    return SyncStoreResponse.from_report(report)


@router.get(
    "/integrations/inbox",
    response_model=PaginatedResponse[InboxEventResponse],
    summary="List inbox events",
)
def list_inbox_events(
    request: Request,
    status: str | None = Query(default=None, description="Filter by processing status"),
    source_system: str | None = Query(default=None, description="Filter by source system"),
    event_type: str | None = Query(default=None, description="Filter by canonical event type"),
    sort: str | None = Query(
        default=None,
        description=(
            "Sort field: 'received_at' (default), 'event_type', 'status' or 'source_system'; "
            "prefix '-' for descending"
        ),
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: EventQueryService = Depends(get_event_query_service),
) -> PaginatedResponse[InboxEventResponse]:
    """Return the tenant's inbox events, cursor-paginated, newest first."""
    reject_unknown_params(request, _INBOX_QUERY_PARAMS)
    parsed_status = _parse_inbox_status(status)
    field, reverse = parse_sort_param(sort, _INBOX_SORT_KEYS, default="received_at")
    if sort is None:
        reverse = True  # newest first by default
    with translate_errors():
        events = service.list_inbox_events(
            client.tenant_id,
            status=parsed_status,
            source_system=source_system,
            event_type=event_type,
            limit=_LIST_FETCH_CAP,
        )
    items = [InboxEventResponse.from_event(event) for event in events]
    page, next_cursor = paginate(
        items,
        key_fn=_INBOX_SORT_KEYS[field],
        tie_fn=lambda item: item.inbox_event_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/integrations/inbox/stale",
    response_model=StaleInboxResponse,
    summary="List stale inbox events",
)
def list_stale_inbox_events(
    request: Request,
    hours: int = Query(default=24, ge=1, description="Staleness threshold in hours"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Max events to return"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: StalenessService = Depends(get_staleness_service),
) -> StaleInboxResponse:
    """Return inbox events stuck in received/processing longer than N hours, oldest first."""
    reject_unknown_params(request, _STALE_QUERY_PARAMS)
    with translate_errors():
        stale = service.list_stale(older_than=datetime.timedelta(hours=hours))
    # Staleness is platform-wide (a stuck event is an ops signal, not
    # tenant data), but the response only shows the caller's tenant:
    # rows of other tenants are filtered out here.
    visible = [event for event in stale if event.tenant_id is None or event.tenant_id == client.tenant_id]
    return StaleInboxResponse(
        stale_after_hours=hours,
        stale_count=len(visible),
        events=[InboxEventResponse.from_event(event) for event in visible[:limit]],
    )


@router.get(
    "/integrations/inbox/{event_id}",
    response_model=InboxEventResponse,
    summary="Get one inbox event",
)
def get_inbox_event(
    event_id: str = Path(..., description="Inbox event id"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: EventQueryService = Depends(get_event_query_service),
) -> InboxEventResponse:
    """Return one inbox event of the tenant (404 when it does not exist)."""
    with translate_errors():
        event = service.get_inbox_event(event_id, client.tenant_id)
    return InboxEventResponse.from_event(event)


@router.post(
    "/integrations/inbox/{event_id}/replay",
    response_model=ReplayResponse,
    summary="Replay a dead-lettered event",
)
def replay_inbox_event(
    event_id: str = Path(..., description="Inbox event id"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    service: DlqReplayService = Depends(get_dlq_replay_service),
    session: Session = Depends(get_db_session),
) -> ReplayResponse:
    """Reset a dead-lettered event to received (same idempotency key, no duplicate effect)."""
    with translate_errors():
        replayed = service.replay(event_id, actor=client.client_id, tenant_id=client.tenant_id)
    session.commit()
    return ReplayResponse.from_event(replayed)


@router.get(
    "/integrations/outbox",
    response_model=PaginatedResponse[OutboxEventResponse],
    summary="List outbox events",
)
def list_outbox_events(
    request: Request,
    status: str | None = Query(default=None, description="Filter by delivery status"),
    event_type: str | None = Query(default=None, description="Filter by event type"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'created_at' (default), 'event_type' or 'status'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: EventQueryService = Depends(get_event_query_service),
) -> PaginatedResponse[OutboxEventResponse]:
    """Return the tenant's outbox events, cursor-paginated, newest first."""
    reject_unknown_params(request, _OUTBOX_QUERY_PARAMS)
    parsed_status = _parse_outbox_status(status)
    field, reverse = parse_sort_param(sort, _OUTBOX_SORT_KEYS, default="created_at")
    if sort is None:
        reverse = True  # newest first by default
    with translate_errors():
        events = service.list_outbox_events(
            client.tenant_id,
            status=parsed_status,
            event_type=event_type,
            limit=_LIST_FETCH_CAP,
        )
    items = [OutboxEventResponse.from_event(event) for event in events]
    page, next_cursor = paginate(
        items,
        key_fn=_OUTBOX_SORT_KEYS[field],
        tie_fn=lambda item: item.outbox_event_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/integrations/audit-log",
    response_model=PaginatedResponse[AutomationDecisionResponse],
    summary="List automation decisions",
)
def list_audit_log(
    request: Request,
    inbox_event_id: str | None = Query(default=None, description="Filter by inbox event id"),
    decision: str | None = Query(default=None, description="Filter by decision"),
    event_type: str | None = Query(default=None, description="Filter by canonical event type"),
    sort: str | None = Query(
        default=None,
        description="Sort field: 'decided_at' (default), 'decision' or 'event_type'; prefix '-' for descending",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page's 'next_cursor'"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: EventQueryService = Depends(get_event_query_service),
) -> PaginatedResponse[AutomationDecisionResponse]:
    """Return the tenant's automation decisions, cursor-paginated, newest first."""
    reject_unknown_params(request, _AUDIT_QUERY_PARAMS)
    parsed_decision = _parse_decision(decision)
    field, reverse = parse_sort_param(sort, _AUDIT_SORT_KEYS, default="decided_at")
    if sort is None:
        reverse = True  # newest first by default
    with translate_errors():
        decisions = service.list_decisions(
            client.tenant_id,
            inbox_event_id=inbox_event_id,
            decision=parsed_decision,
            event_type=event_type,
            limit=_LIST_FETCH_CAP,
        )
    items = [AutomationDecisionResponse.from_decision(item) for item in decisions]
    page, next_cursor = paginate(
        items,
        key_fn=_AUDIT_SORT_KEYS[field],
        tie_fn=lambda item: item.decision_id,
        limit=limit,
        cursor=cursor,
        reverse=reverse,
    )
    return PaginatedResponse(items=page, next_cursor=next_cursor)


@router.get(
    "/integrations/stores/{source_system}/{source_store_id}/settings",
    response_model=StoreSettingsResponse,
    summary="Get a store's settings",
)
def get_store_settings(
    source_system: str = Path(..., description="Source system key, e.g. 'hayat'"),
    source_store_id: str = Path(..., description="Source store id within the source system"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:read")),
    service: StoreSettingsService = Depends(get_store_settings_service),
) -> StoreSettingsResponse:
    """Return a store's settings (Iranian defaults when none are stored)."""
    with translate_errors():
        settings = service.get_settings(source_system, source_store_id)
    return StoreSettingsResponse.from_settings(settings)


@router.put(
    "/integrations/stores/{source_system}/{source_store_id}/settings",
    response_model=StoreSettingsResponse,
    summary="Update a store's settings",
)
def update_store_settings(
    payload: StoreSettingsRequest,
    source_system: str = Path(..., description="Source system key, e.g. 'hayat'"),
    source_store_id: str = Path(..., description="Source store id within the source system"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    service: StoreSettingsService = Depends(get_store_settings_service),
    session: Session = Depends(get_db_session),
) -> StoreSettingsResponse:
    """Store a store's settings (the 'prices are in Toman' switch)."""
    with translate_errors():
        settings = service.update_settings(
            source_system,
            source_store_id,
            prices_in_toman=payload.prices_in_toman,
            currency=payload.currency,
        )
    session.commit()
    return StoreSettingsResponse.from_settings(settings)


@router.post(
    "/integrations/worker/poll",
    response_model=WorkerPollResponse,
    summary="Run one worker sweep now",
)
def poll_worker(
    limit: int = Query(default=50, ge=1, le=500, description="Max events to claim in this sweep"),
    client: AuthenticatedClient = Depends(require_scopes("integrations:manage")),
    worker: EventWorker = Depends(get_event_worker),
    session: Session = Depends(get_db_session),
) -> WorkerPollResponse:
    """Run one worker sweep over due inbox events + pending outbox events."""
    with translate_errors():
        report = worker.poll_once(limit=limit)
    session.commit()
    return WorkerPollResponse.from_report(report)
