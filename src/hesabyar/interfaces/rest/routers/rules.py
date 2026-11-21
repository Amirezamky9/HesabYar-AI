"""Rules-engine routes (US-561, Phase 5-7 ۵خ W2).

Thin routes over the rules service. Rules are tenant-scoped (and
installation-scoped when the API key carries an installation);
reads need ``rules:read``, writes need ``rules:manage``.

- ``GET /rules`` — list rules (filters: event_type, enabled).
- ``POST /rules`` — create a rule (validated before it is stored).
- ``GET /rules/{rule_id}`` — one rule.
- ``PUT /rules/{rule_id}`` — update a rule (only given fields change).
- ``DELETE /rules/{rule_id}`` — delete a rule (its executions survive).
- ``POST /rules/{rule_id}/enable`` / ``.../disable`` — enable/disable.
- ``POST /rules/{rule_id}/dry-run`` — test a stored rule against a
  past event (``event_id``) or a sample payload (``event_type`` +
  ``payload``). No side effects; the dry-run is audit-logged.
- ``POST /rules/dry-run-definition`` — test an unsaved definition
  (the UI's test-before-save).
- ``POST /rules/evaluate`` — evaluate the tenant's enabled rules
  against one live event (past event or sample payload). Matched
  rules EXECUTE their actions (posts go through the policy gate);
  every execution is audit-logged.
- ``GET /rules/executions`` — execution history (filters: rule_id,
  event_id); ``GET /rules/{rule_id}/executions`` — one rule's history.
- ``GET /rules/executions/{execution_id}`` — one execution.
- ``GET /rules/sets`` / ``PUT /rules/sets/{event_type}`` — a rule
  set's match mode (``first_match`` / ``all_matches``).

Route order matters: the static paths (``/rules/executions``,
``/rules/sets``, ``/rules/dry-run-definition``, ``/rules/evaluate``)
are registered before ``/rules/{rule_id}`` so FastAPI matches them
first. See ``docs/rules-engine.md``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from hesabyar.application.commands.events import normalize_event
from hesabyar.application.commands.rules import RuleEngineService
from hesabyar.domain.events.envelope import parse_event_type
from hesabyar.domain.events.models import InboxEvent, StoreSettings
from hesabyar.domain.rules.models import RuleExecutionContext
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.infrastructure.persistence.postgres.event_repositories import (
    SqlAlchemyEventInboxRepository,
    SqlAlchemyStoreSettingsRepository,
)
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_event_inbox_repository,
    get_rule_engine_service,
    get_store_settings_repository,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.schemas.rules import (
    DryRunDefinitionRequest,
    DryRunRequest,
    DryRunResponse,
    EvaluateRequest,
    EvaluateResponse,
    RuleCreateRequest,
    RuleExecutionResponse,
    RuleResponse,
    RuleSetConfigRequest,
    RuleSetConfigResponse,
    RuleUpdateRequest,
)

router = APIRouter(tags=["rules"])


def _context_from(
    client: AuthenticatedClient,
    body: DryRunRequest | EvaluateRequest,
    *,
    event_date: Any = None,
) -> RuleExecutionContext:
    """Build the execution context from the request body (+ the event)."""
    return RuleExecutionContext(
        tenant_id=client.tenant_id,
        installation_id=client.installation_id,
        company_engine_id=body.company_engine_id,
        event_date=body.event_date or event_date,
        channel_key=body.channel_key,
        review_item_id=body.review_item_id,
        draft_kind=body.draft_kind,
        draft_engine_id=body.draft_engine_id,
        actor=client.client_id,
    )


def _load_past_event(
    body: DryRunRequest | EvaluateRequest,
    client: AuthenticatedClient,
    inbox: SqlAlchemyEventInboxRepository,
    store_settings: SqlAlchemyStoreSettingsRepository,
) -> tuple[str, dict[str, Any], str, Any]:
    """Load + normalize a past inbox event for dry-run/evaluate.

    Returns ``(event_type, converted_payload, event_id, occurred_date)``.
    The payload is normalized exactly like the pipeline sees it
    (Toman→Rial at the boundary), so a dry-run against a past event
    tests the same amounts the live pipeline used. An event of
    another tenant (or a missing one) is a 404, as if it did not
    exist.
    """
    assert body.event_id is not None
    event: InboxEvent | None = inbox.get(body.event_id, client.tenant_id)
    if event is None:
        raise ApiError(
            status_code=404,
            code="EVENT_NOT_FOUND",
            detail=f"Event {body.event_id!r} does not exist.",
        )
    with translate_errors():
        event_type = parse_event_type(event.event_type)
    settings = store_settings.get(event.source_system, event.source_store_id)
    if settings is None:
        settings = StoreSettings(
            source_system=event.source_system,
            source_store_id=event.source_store_id,
        )
    normalized = normalize_event(
        event.to_canonical_event(),
        settings,
        event_type=event_type,
        installation_id=event.installation_id,
    )
    return (
        event_type.value,
        dict(normalized.converted_payload),
        event.id,
        event.occurred_at.date(),
    )


def _resolve_event_input(
    body: DryRunRequest | EvaluateRequest,
    client: AuthenticatedClient,
    inbox: SqlAlchemyEventInboxRepository,
    store_settings: SqlAlchemyStoreSettingsRepository,
) -> tuple[str, dict[str, Any], str | None, RuleExecutionContext]:
    """Resolve a dry-run/evaluate body to (event_type, payload, event_id, context).

    Either ``event_id`` (a past event) or ``event_type`` + ``payload``
    (a sample payload) must be given; both together is a 400.
    """
    if body.event_id is not None and (body.event_type is not None or body.payload is not None):
        raise ApiError(
            status_code=400,
            code="RULE_EVENT_INPUT_CONFLICT",
            detail="Give either 'event_id' (a past event) or 'event_type' + 'payload' (a sample payload), not both.",
        )
    if body.event_id is not None:
        event_type, payload, event_id, occurred_date = _load_past_event(body, client, inbox, store_settings)
        return event_type, payload, event_id, _context_from(client, body, event_date=occurred_date)
    if body.event_type is None or body.payload is None:
        raise ApiError(
            status_code=400,
            code="RULE_EVENT_INPUT_REQUIRED",
            detail="Give either 'event_id' (a past event) or 'event_type' + 'payload' (a sample payload).",
        )
    return body.event_type, dict(body.payload), None, _context_from(client, body)


# -- static paths first (before /rules/{rule_id}) ------------------------------


@router.get(
    "/rules/executions",
    response_model=list[RuleExecutionResponse],
    summary="List rule executions (the audit log), newest first",
)
def list_executions(
    rule_id: str | None = Query(default=None, description="Filter to one rule"),
    event_id: str | None = Query(default=None, description="Filter to one event"),
    limit: int = Query(default=50, ge=1, le=200, description="Max executions to return"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> list[RuleExecutionResponse]:
    """Return the tenant's executions (dry-runs and live runs)."""
    with translate_errors():
        executions = service.list_executions(client.tenant_id, rule_id=rule_id, event_id=event_id, limit=limit)
    return [RuleExecutionResponse.from_execution(e) for e in executions]


@router.get(
    "/rules/executions/{execution_id}",
    response_model=RuleExecutionResponse,
    summary="Get one rule execution",
)
def get_execution(
    execution_id: str = Path(..., description="Execution id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> RuleExecutionResponse:
    """Return one execution (404 when it is not in this tenant)."""
    with translate_errors():
        execution = service.get_execution(client.tenant_id, execution_id)
    return RuleExecutionResponse.from_execution(execution)


@router.get(
    "/rules/sets",
    response_model=list[RuleSetConfigResponse],
    summary="List configured rule-set match modes",
)
def list_rule_sets(
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> list[RuleSetConfigResponse]:
    """Return the tenant's stored rule-set configs (unconfigured sets use first_match)."""
    with translate_errors():
        configs = service.list_set_configs(client.tenant_id)
    return [RuleSetConfigResponse.from_config(c) for c in configs]


@router.put(
    "/rules/sets/{event_type}",
    response_model=RuleSetConfigResponse,
    summary="Configure a rule set's match mode",
)
def set_rule_set_mode(
    event_type: str,
    body: RuleSetConfigRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> RuleSetConfigResponse:
    """Set a rule set's mode: first_match (Xero) or all_matches."""
    with translate_errors():
        config = service.set_match_mode(client.tenant_id, event_type=event_type, match_mode=body.match_mode)
    session.commit()
    return RuleSetConfigResponse.from_config(config)


@router.post(
    "/rules/dry-run-definition",
    response_model=DryRunResponse,
    summary="Dry-run an unsaved rule definition (test before save)",
)
def dry_run_definition(
    body: DryRunDefinitionRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    store_settings: SqlAlchemyStoreSettingsRepository = Depends(get_store_settings_repository),
) -> DryRunResponse:
    """Test an unsaved definition against a past event or sample payload.

    No side effects; the dry-run is audit-logged with ``rule_id=None``.
    """
    event_type, payload, event_id, context = _resolve_event_input(body, client, inbox, store_settings)
    with translate_errors():
        result = service.dry_run_definition(
            client.tenant_id,
            name=body.name,
            event_type=event_type,
            conditions=body.conditions,
            action={"kind": body.action.kind, "params": dict(body.action.params)},
            payload=payload,
            event_id=event_id,
            context=context,
        )
    session.commit()
    return DryRunResponse.from_result(result)


@router.post(
    "/rules/evaluate",
    response_model=EvaluateResponse,
    summary="Evaluate the enabled rules against one live event",
)
def evaluate_event(
    body: EvaluateRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    store_settings: SqlAlchemyStoreSettingsRepository = Depends(get_store_settings_repository),
) -> EvaluateResponse:
    """Run the tenant's enabled rules against one event (live).

    Matched rules EXECUTE their actions — a post action goes through
    the 3-layer policy gate, exactly like a manual post. Every
    execution is audit-logged.
    """
    event_type, payload, event_id, context = _resolve_event_input(body, client, inbox, store_settings)
    with translate_errors():
        executions = service.evaluate_event(
            client.tenant_id,
            event_type=event_type,
            payload=payload,
            event_id=event_id,
            context=context,
            match_mode=body.match_mode,
        )
    session.commit()
    return EvaluateResponse(
        executions=[RuleExecutionResponse.from_execution(e) for e in executions],
        matched_rules=len(executions),
    )


# -- rule CRUD ------------------------------------------------------------------


@router.get(
    "/rules",
    response_model=list[RuleResponse],
    summary="List rules in priority order",
)
def list_rules(
    event_type: str | None = Query(default=None, description="Filter to one event type"),
    enabled: bool | None = Query(default=None, description="Filter by enabled flag"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> list[RuleResponse]:
    """Return the tenant's rules (its installation's + tenant-wide)."""
    with translate_errors():
        rules = service.list_rules(
            client.tenant_id,
            client.installation_id,
            event_type=event_type,
            enabled=enabled,
        )
    return [RuleResponse.from_rule(r) for r in rules]


@router.post(
    "/rules",
    response_model=RuleResponse,
    status_code=201,
    summary="Create a rule",
)
def create_rule(
    body: RuleCreateRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> RuleResponse:
    """Create a rule (validated before it is stored)."""
    with translate_errors():
        rule = service.create_rule(
            client.tenant_id,
            client.installation_id,
            name=body.name,
            description=body.description,
            event_type=body.event_type,
            conditions=body.conditions,
            action={"kind": body.action.kind, "params": dict(body.action.params)},
            priority=body.priority,
            enabled=body.enabled,
            actor=client.client_id,
        )
    session.commit()
    return RuleResponse.from_rule(rule)


@router.get(
    "/rules/{rule_id}",
    response_model=RuleResponse,
    summary="Get one rule",
)
def get_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> RuleResponse:
    """Return one rule (404 when it is not in this tenant)."""
    with translate_errors():
        rule = service.get_rule(client.tenant_id, rule_id)
    return RuleResponse.from_rule(rule)


@router.put(
    "/rules/{rule_id}",
    response_model=RuleResponse,
    summary="Update a rule",
)
def update_rule(
    rule_id: str,
    body: RuleUpdateRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> RuleResponse:
    """Update a rule (only the given fields change)."""
    with translate_errors():
        rule = service.update_rule(
            client.tenant_id,
            rule_id,
            name=body.name,
            description=body.description,
            event_type=body.event_type,
            conditions=body.conditions,
            action=(
                {"kind": body.action.kind, "params": dict(body.action.params)} if body.action is not None else None
            ),
            priority=body.priority,
            enabled=body.enabled,
        )
    session.commit()
    return RuleResponse.from_rule(rule)


@router.delete(
    "/rules/{rule_id}",
    status_code=204,
    summary="Delete a rule",
)
def delete_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> None:
    """Delete a rule (its executions survive — the audit log is append-only)."""
    with translate_errors():
        service.delete_rule(client.tenant_id, rule_id)
    session.commit()


@router.post(
    "/rules/{rule_id}/enable",
    response_model=RuleResponse,
    summary="Enable a rule",
)
def enable_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> RuleResponse:
    """Enable a rule (404 when it is not in this tenant)."""
    with translate_errors():
        rule = service.set_enabled(client.tenant_id, rule_id, enabled=True)
    session.commit()
    return RuleResponse.from_rule(rule)


@router.post(
    "/rules/{rule_id}/disable",
    response_model=RuleResponse,
    summary="Disable a rule",
)
def disable_rule(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> RuleResponse:
    """Disable a rule (it stops firing live; dry-run still works)."""
    with translate_errors():
        rule = service.set_enabled(client.tenant_id, rule_id, enabled=False)
    session.commit()
    return RuleResponse.from_rule(rule)


@router.post(
    "/rules/{rule_id}/dry-run",
    response_model=DryRunResponse,
    summary="Dry-run a stored rule against a past event or sample payload",
)
def dry_run_rule(
    rule_id: str,
    body: DryRunRequest,
    client: AuthenticatedClient = Depends(require_scopes("rules:manage")),
    session: Session = Depends(get_db_session),
    service: RuleEngineService = Depends(get_rule_engine_service),
    inbox: SqlAlchemyEventInboxRepository = Depends(get_event_inbox_repository),
    store_settings: SqlAlchemyStoreSettingsRepository = Depends(get_store_settings_repository),
) -> DryRunResponse:
    """Test a rule against one event — no side effects, audit-logged.

    A disabled rule can be dry-run: test before activation (§6.4).
    """
    event_type, payload, event_id, context = _resolve_event_input(body, client, inbox, store_settings)
    with translate_errors():
        result = service.dry_run_rule(
            client.tenant_id,
            rule_id,
            event_type=event_type,
            payload=payload,
            event_id=event_id,
            context=context,
        )
    session.commit()
    return DryRunResponse.from_result(result)


@router.get(
    "/rules/{rule_id}/executions",
    response_model=list[RuleExecutionResponse],
    summary="List one rule's executions, newest first",
)
def list_rule_executions(
    rule_id: str = Path(..., description="Rule id (UUID)"),
    limit: int = Query(default=50, ge=1, le=200, description="Max executions to return"),
    client: AuthenticatedClient = Depends(require_scopes("rules:read")),
    service: RuleEngineService = Depends(get_rule_engine_service),
) -> list[RuleExecutionResponse]:
    """Return one rule's execution history (404 when the rule is not in this tenant)."""
    with translate_errors():
        service.get_rule(client.tenant_id, rule_id)
        executions = service.list_executions(client.tenant_id, rule_id=rule_id, limit=limit)
    return [RuleExecutionResponse.from_execution(e) for e in executions]
