"""Monitoring routes (US-704, Phase 7).

Thin routes over the monitoring service. The monitoring surface is
tenantless by design (operator surface, like the jobs registry):
``monitoring:read`` guards the snapshot + alert/channel/token
reads, ``monitoring:manage`` guards evaluation, alert lifecycle
and channel/token writes.

- ``GET /monitoring/overview`` — the snapshot the operator panel shows.
- ``GET /monitoring/alerts`` — fired alerts, newest first.
- ``POST /monitoring/alerts/{id}/acknowledge`` / ``.../resolve``.
- ``POST /monitoring/evaluate`` — run the evaluation now (the
  worker runs it every 15 minutes on schedule; this is the manual
  trigger — and the only way the 5xx rule fires, since the 5xx
  counters live in the API process).
- ``GET/POST/PUT/DELETE /monitoring/channels`` — alert channels.
- ``GET/POST/PUT/DELETE /monitoring/tokens`` — configured token records.

See docs/monitoring.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from hesabyar.application.commands.monitoring import MonitoringService
from hesabyar.domain.monitoring.models import AlertRule, AlertStatus
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_monitoring_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import translate_errors
from hesabyar.interfaces.rest.schemas.monitoring import (
    AlertChannelRequest,
    AlertChannelResponse,
    AlertResponse,
    EvaluationResponse,
    MonitoringOverviewResponse,
    TokenRecordRequest,
    TokenRecordResponse,
)

router = APIRouter(tags=["monitoring"])


@router.get(
    "/monitoring/overview",
    response_model=MonitoringOverviewResponse,
    summary="Monitoring snapshot: processes, queues, alerts, tokens, backups",
)
def get_overview(
    client: AuthenticatedClient = Depends(require_scopes("monitoring:read")),  # noqa: ARG001 - scope guard
    service: MonitoringService = Depends(get_monitoring_service),
) -> MonitoringOverviewResponse:
    """Return the monitoring snapshot for the operator panel."""
    with translate_errors():
        snapshot = service.overview()
    return MonitoringOverviewResponse.from_snapshot(snapshot)


@router.get(
    "/monitoring/alerts",
    response_model=list[AlertResponse],
    summary="List fired alerts, newest first",
)
def list_alerts(
    status_filter: AlertStatus | None = Query(default=None, alias="status", description="Filter by lifecycle status"),
    rule: AlertRule | None = Query(default=None, description="Filter by rule"),
    limit: int = Query(default=50, ge=1, le=200, description="Max alerts to return"),
    client: AuthenticatedClient = Depends(require_scopes("monitoring:read")),  # noqa: ARG001 - scope guard
    service: MonitoringService = Depends(get_monitoring_service),
) -> list[AlertResponse]:
    """Return fired alerts, newest first."""
    with translate_errors():
        alerts = service.list_alerts(status=status_filter, rule=rule, limit=limit)
    return [AlertResponse.from_alert(alert) for alert in alerts]


@router.post(
    "/monitoring/alerts/{alert_id}/acknowledge",
    response_model=AlertResponse,
    summary="Acknowledge an alert",
)
def acknowledge_alert(
    alert_id: str = Path(..., description="Alert id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> AlertResponse:
    """Acknowledge an alert (404 when unknown)."""
    with translate_errors():
        alert = service.acknowledge_alert(alert_id, actor=client.client_name)
    session.commit()
    return AlertResponse.from_alert(alert)


@router.post(
    "/monitoring/alerts/{alert_id}/resolve",
    response_model=AlertResponse,
    summary="Resolve an alert",
)
def resolve_alert(
    alert_id: str = Path(..., description="Alert id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> AlertResponse:
    """Resolve an alert (404 when unknown)."""
    with translate_errors():
        alert = service.resolve_alert(alert_id, actor=client.client_name)
    session.commit()
    return AlertResponse.from_alert(alert)


@router.post(
    "/monitoring/evaluate",
    response_model=EvaluationResponse,
    summary="Run the alert evaluation now",
)
def evaluate_now(
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> EvaluationResponse:
    """Evaluate the rules, persist newly fired alerts, dispatch them."""
    with translate_errors():
        report = service.evaluate()
    session.commit()
    return EvaluationResponse.from_report(report)


@router.get(
    "/monitoring/channels",
    response_model=list[AlertChannelResponse],
    summary="List configured alert channels",
)
def list_channels(
    client: AuthenticatedClient = Depends(require_scopes("monitoring:read")),  # noqa: ARG001 - scope guard
    service: MonitoringService = Depends(get_monitoring_service),
) -> list[AlertChannelResponse]:
    """Return every configured alert channel, oldest first."""
    with translate_errors():
        channels = service.list_channels()
    return [AlertChannelResponse.from_channel(channel) for channel in channels]


@router.post(
    "/monitoring/channels",
    response_model=AlertChannelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an alert channel",
)
def create_channel(
    request: AlertChannelRequest,
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> AlertChannelResponse:
    """Create a channel (an SMS channel defaults to the caller's installation)."""
    with translate_errors():
        channel = service.create_channel(request.model_dump(), installation_id=client.installation_id)
    session.commit()
    return AlertChannelResponse.from_channel(channel)


@router.put(
    "/monitoring/channels/{channel_id}",
    response_model=AlertChannelResponse,
    summary="Replace an alert channel",
)
def update_channel(
    request: AlertChannelRequest,
    channel_id: str = Path(..., description="Channel id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> AlertChannelResponse:
    """Replace a channel (404 when unknown; full replace)."""
    with translate_errors():
        channel = service.update_channel(channel_id, request.model_dump(), installation_id=client.installation_id)
    session.commit()
    return AlertChannelResponse.from_channel(channel)


@router.delete(
    "/monitoring/channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an alert channel",
)
def delete_channel(
    channel_id: str = Path(..., description="Channel id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> None:
    """Delete a channel (404 when unknown)."""
    with translate_errors():
        service.delete_channel(channel_id)
    session.commit()


@router.get(
    "/monitoring/tokens",
    response_model=list[TokenRecordResponse],
    summary="List configured token records",
)
def list_tokens(
    client: AuthenticatedClient = Depends(require_scopes("monitoring:read")),  # noqa: ARG001 - scope guard
    service: MonitoringService = Depends(get_monitoring_service),
) -> list[TokenRecordResponse]:
    """Return every configured token record, oldest first."""
    with translate_errors():
        records = service.list_token_records()
    return [TokenRecordResponse.from_record(record) for record in records]


@router.post(
    "/monitoring/tokens",
    response_model=TokenRecordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a token record",
)
def create_token(
    request: TokenRecordRequest,
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> TokenRecordResponse:
    """Create a token record (the Fintech token's expiry, until the live token lands)."""
    with translate_errors():
        record = service.create_token_record(request.model_dump())
    session.commit()
    return TokenRecordResponse.from_record(record)


@router.put(
    "/monitoring/tokens/{record_id}",
    response_model=TokenRecordResponse,
    summary="Replace a token record",
)
def update_token(
    request: TokenRecordRequest,
    record_id: str = Path(..., description="Token record id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> TokenRecordResponse:
    """Replace a token record (404 when unknown; full replace)."""
    with translate_errors():
        record = service.update_token_record(record_id, request.model_dump())
    session.commit()
    return TokenRecordResponse.from_record(record)


@router.delete(
    "/monitoring/tokens/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a token record",
)
def delete_token(
    record_id: str = Path(..., description="Token record id (UUID)"),
    client: AuthenticatedClient = Depends(require_scopes("monitoring:manage")),  # noqa: ARG001 - scope guard
    session: Session = Depends(get_db_session),
    service: MonitoringService = Depends(get_monitoring_service),
) -> None:
    """Delete a token record (404 when unknown)."""
    with translate_errors():
        service.delete_token_record(record_id)
    session.commit()
