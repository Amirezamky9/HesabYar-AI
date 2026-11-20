"""SMS provider registry + reminder routes (US-544, Phase 5-5 ۵ج).

Thin routes over the SMS services: no provider logic here — the
registry service owns the provider CRUD + credential encryption,
the reminder service owns sending + the daily job, the query
service owns the reads.

- ``POST /integrations/sms/providers/seed`` — seed the built-in
  providers (Kavenegar + sms.ir born disabled, simulator born
  enabled + default; idempotent; 201).
- ``POST /integrations/sms/providers`` — create a provider (201);
  ``GET`` list / ``{provider_id}`` — reads; ``PATCH`` — update;
  ``DELETE`` — delete (204; the default provider cannot be deleted).
- ``GET /integrations/sms/settings`` / ``PUT`` — the
  installation's reminder policy (offsets + templates).
- ``GET /integrations/sms/contracts/{contract_id}/reminders`` /
  ``PUT`` — a contract's reminder opt-in (automatic sends only).
- ``GET /integrations/sms/reminders`` — the reminder log
  (the DLQ + audit trail), newest first.

Everything needs ``sms:manage`` (the manual trigger and the
per-plan log live on the installments router, where the log read
needs ``installments:read``). Everything is tenant/installation-
scoped: a row of another installation answers 404 as if it did
not exist. Credentials are write-only: no response ever carries
them, only ``has_credentials``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from hesabyar.application.commands.sms import SmsRegistryService
from hesabyar.application.queries.sms import SmsQueryService
from hesabyar.domain.sms.models import SmsLogStatus, SmsReminderTrigger
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_sms_query_service,
    get_sms_registry_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    reject_unknown_params,
)
from hesabyar.interfaces.rest.schemas.sms import (
    ContractReminderSettingsRequest,
    ContractReminderSettingsResponse,
    CreateSmsProviderRequest,
    ReminderLogResponse,
    ReminderSettingsRequest,
    ReminderSettingsResponse,
    SmsProviderResponse,
    UpdateSmsProviderRequest,
)

router = APIRouter(tags=["sms"])

_LIST_REMINDERS_QUERY_PARAMS = frozenset(
    {"plan_id", "leg_id", "provider_key", "status", "trigger", "limit", "offset"}
)


@router.post(
    "/integrations/sms/providers/seed",
    response_model=list[SmsProviderResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Seed the built-in SMS providers (idempotent)",
)
def seed_providers(
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    session: Session = Depends(get_db_session),
    service: SmsRegistryService = Depends(get_sms_registry_service),
) -> list[SmsProviderResponse]:
    """Create the built-in provider rows (Kavenegar + sms.ir disabled, simulator default)."""
    with translate_errors():
        providers = service.seed_builtin_providers(client.installation_id)
    session.commit()
    return [SmsProviderResponse.from_domain(provider) for provider in providers]


@router.post(
    "/integrations/sms/providers",
    response_model=SmsProviderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an SMS provider",
)
def create_provider(
    body: CreateSmsProviderRequest,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    session: Session = Depends(get_db_session),
    service: SmsRegistryService = Depends(get_sms_registry_service),
) -> SmsProviderResponse:
    """Create a provider row (a custom provider, or a re-created built-in)."""
    with translate_errors():
        provider = service.create_provider(
            installation_id=client.installation_id, payload=body.payload_fields()
        )
    session.commit()
    return SmsProviderResponse.from_domain(provider)


@router.get(
    "/integrations/sms/providers",
    response_model=list[SmsProviderResponse],
    summary="List the installation's SMS providers",
)
def list_providers(
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    service: SmsQueryService = Depends(get_sms_query_service),
) -> list[SmsProviderResponse]:
    """Return the installation's providers, built-ins first, then by key."""
    with translate_errors():
        providers = service.list_providers(installation_id=client.installation_id)
    return [SmsProviderResponse.from_domain(provider) for provider in providers]


@router.get(
    "/integrations/sms/providers/{provider_id}",
    response_model=SmsProviderResponse,
    summary="Get one SMS provider",
)
def get_provider(
    provider_id: str,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    service: SmsQueryService = Depends(get_sms_query_service),
) -> SmsProviderResponse:
    """Return one provider (404 when missing / out of scope)."""
    with translate_errors():
        provider = service.get_provider(
            provider_id, installation_id=client.installation_id
        )
    return SmsProviderResponse.from_domain(provider)


@router.patch(
    "/integrations/sms/providers/{provider_id}",
    response_model=SmsProviderResponse,
    summary="Update an SMS provider",
)
def update_provider(
    provider_id: str,
    body: UpdateSmsProviderRequest,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    session: Session = Depends(get_db_session),
    service: SmsRegistryService = Depends(get_sms_registry_service),
) -> SmsProviderResponse:
    """Patch a provider row (only the supplied fields change)."""
    with translate_errors():
        provider = service.update_provider(
            installation_id=client.installation_id,
            provider_id=provider_id,
            payload=body.payload_fields(),
        )
    session.commit()
    return SmsProviderResponse.from_domain(provider)


@router.delete(
    "/integrations/sms/providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an SMS provider",
)
def delete_provider(
    provider_id: str,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    session: Session = Depends(get_db_session),
    service: SmsRegistryService = Depends(get_sms_registry_service),
) -> None:
    """Delete a provider row (the default provider cannot be deleted)."""
    with translate_errors():
        service.delete_provider(
            installation_id=client.installation_id, provider_id=provider_id
        )
    session.commit()


@router.get(
    "/integrations/sms/settings",
    response_model=ReminderSettingsResponse,
    summary="Get the installation's reminder policy",
)
def get_reminder_settings(
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    service: SmsQueryService = Depends(get_sms_query_service),
) -> ReminderSettingsResponse:
    """Return the reminder policy (defaults when never saved)."""
    with translate_errors():
        settings = service.get_reminder_settings(
            tenant_id=client.tenant_id, installation_id=client.installation_id
        )
    return ReminderSettingsResponse.from_domain(settings)


@router.put(
    "/integrations/sms/settings",
    response_model=ReminderSettingsResponse,
    summary="Replace the installation's reminder policy",
)
def put_reminder_settings(
    body: ReminderSettingsRequest,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    session: Session = Depends(get_db_session),
    service: SmsRegistryService = Depends(get_sms_registry_service),
) -> ReminderSettingsResponse:
    """Insert or replace the reminder policy (all fields required)."""
    with translate_errors():
        settings = service.save_reminder_settings(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            payload=body.payload_fields(),
        )
    session.commit()
    return ReminderSettingsResponse.from_domain(settings)


@router.get(
    "/integrations/sms/contracts/{contract_id}/reminders",
    response_model=ContractReminderSettingsResponse,
    summary="Get a contract's reminder opt-in",
)
def get_contract_reminder_settings(
    contract_id: str,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    service: SmsQueryService = Depends(get_sms_query_service),
) -> ContractReminderSettingsResponse:
    """Return a contract's opt-in (disabled when never configured)."""
    with translate_errors():
        settings = service.get_contract_settings(
            installation_id=client.installation_id, contract_id=contract_id
        )
    if settings is None:
        return ContractReminderSettingsResponse.default_for(
            client.installation_id, contract_id
        )
    return ContractReminderSettingsResponse.from_domain(settings)


@router.put(
    "/integrations/sms/contracts/{contract_id}/reminders",
    response_model=ContractReminderSettingsResponse,
    summary="Set a contract's reminder opt-in",
)
def put_contract_reminder_settings(
    contract_id: str,
    body: ContractReminderSettingsRequest,
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    session: Session = Depends(get_db_session),
    service: SmsRegistryService = Depends(get_sms_registry_service),
) -> ContractReminderSettingsResponse:
    """Insert or replace a contract's opt-in (the contract must exist)."""
    with translate_errors():
        settings = service.save_contract_settings(
            tenant_id=client.tenant_id,
            installation_id=client.installation_id,
            contract_id=contract_id,
            payload=body.payload_fields(),
        )
    session.commit()
    return ContractReminderSettingsResponse.from_domain(settings)


@router.get(
    "/integrations/sms/reminders",
    response_model=list[ReminderLogResponse],
    summary="List the reminder log (DLQ + audit trail)",
)
def list_reminders(
    request: Request,
    plan_id: str | None = Query(default=None, description="Filter by installment plan id"),
    leg_id: str | None = Query(default=None, description="Filter by installment leg id"),
    provider_key: str | None = Query(default=None, description="Filter by provider key"),
    status: str | None = Query(
        default=None,
        description="Filter by status: queued | sent | delivered | failed | skipped | dry_run",
    ),
    trigger: str | None = Query(
        default=None, description="Filter by trigger: scheduled | manual"
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (1-100)"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    client: AuthenticatedClient = Depends(require_scopes("sms:manage")),
    service: SmsQueryService = Depends(get_sms_query_service),
) -> list[ReminderLogResponse]:
    """Return reminder-log rows, newest first (all filters optional)."""
    reject_unknown_params(request, _LIST_REMINDERS_QUERY_PARAMS)
    parsed_status: SmsLogStatus | None = None
    if status is not None:
        try:
            parsed_status = SmsLogStatus(status)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="SMS_VALIDATION_FAILED",
                detail=f"Unknown reminder status {status!r}.",
            ) from exc
    parsed_trigger: SmsReminderTrigger | None = None
    if trigger is not None:
        try:
            parsed_trigger = SmsReminderTrigger(trigger)
        except ValueError as exc:
            raise ApiError(
                status_code=422,
                code="SMS_VALIDATION_FAILED",
                detail=f"Unknown reminder trigger {trigger!r}.",
            ) from exc
    with translate_errors():
        entries = service.list_log(
            installation_id=client.installation_id,
            plan_id=plan_id,
            leg_id=leg_id,
            provider_key=provider_key,
            status=parsed_status,
            trigger=parsed_trigger,
            limit=limit,
            offset=offset,
        )
    return [ReminderLogResponse.from_domain(entry) for entry in entries]
