"""Gateway reconciliation routes: verify, reconcile, callbacks, refunds.

US-524 (Phase 5-3 Wave B) — thin routes over the gateway
reconciliation service: no gateway HTTP here, no derivation — the
service and the clients own those.

- ``GET /gateways/{channel_key}/capabilities`` — what the channel
  can and cannot do (API vs manual, settlement source, open
  UNVERIFIED questions). No credentials needed, no HTTP call.
- ``POST /gateways/{channel_key}/verify-and-record`` — verify a
  payment with its gateway and record it as a ``gateway_payment``
  (source ``gateway_poll``). 201 when created, 200 on an idempotent
  replay. A gateway answer that disagrees with the expected amount
  records nothing (422 ``GATEWAY_VERIFICATION_MISMATCH``).
- ``POST /gateways/{channel_key}/callbacks`` — record a callback
  the STORE already verified with the gateway (source
  ``store_webhook`` / ``manual_entry`` / ``manual_import``).
  Idempotent: a replay returns the existing payment (200).
- ``POST /gateways/{channel_key}/reconcile`` — inquire one
  payment's status and converge the recorded row (record a paid
  payment, cancel a cancelled/reverted one, leave a pending one).
- ``POST /gateways/{channel_key}/refunds`` — refund a payment
  through its gateway, then cancel the row. FULL refunds only.

Reads need ``settlement:read``; every mutation needs
``settlement:manage``. Channels and their credentials are managed
under ``/integrations``; this router reads them but never writes
them. A disabled channel reconciles nothing (422
``GATEWAY_NOT_CONFIGURED``); a manual channel (Sadad, SalamPay,
custom) has no API to verify against (422
``GATEWAY_OPERATION_UNSUPPORTED``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from hesabyar.application.commands.gateways import GatewayReconciliationService
from hesabyar.domain.settlement.models import PaymentSource
from hesabyar.infrastructure.auth.identity import AuthenticatedClient
from hesabyar.interfaces.rest.dependencies import (
    get_db_session,
    get_gateway_reconciliation_service,
    require_scopes,
)
from hesabyar.interfaces.rest.errors import ApiError, translate_errors
from hesabyar.interfaces.rest.schemas.gateways import (
    GatewayCapabilitiesResponse,
    GatewayInquiryResponse,
    ReconcileRequest,
    ReconcileResponse,
    RecordCallbackRequest,
    RefundPaymentRequest,
    RefundPaymentResponse,
    VerifiedCallbackResponse,
    VerifyAndRecordRequest,
)
from hesabyar.interfaces.rest.schemas.settlement import (
    GatewayPaymentResponse,
    SettlementTrancheResponse,
)

router = APIRouter(tags=["gateways"])

_CALLBACK_SOURCES = {
    PaymentSource.STORE_WEBHOOK.value,
    PaymentSource.MANUAL_ENTRY.value,
    PaymentSource.MANUAL_IMPORT.value,
}


def _parse_callback_source(value: str) -> PaymentSource:
    """Parse a callback source, failing loudly on gateway_poll."""
    if value not in _CALLBACK_SOURCES:
        raise ApiError(
            status_code=400,
            code="INVALID_FILTER",
            detail=(
                f"Unknown callback source {value!r}. Allowed: "
                f"{', '.join(sorted(_CALLBACK_SOURCES))}. "
                "gateway_poll goes through verify-and-record."
            ),
        )
    return PaymentSource(value)


@router.get(
    "/gateways/{channel_key}/capabilities",
    response_model=GatewayCapabilitiesResponse,
    summary="What a channel can and cannot do (API vs manual, open questions)",
)
def get_channel_capabilities(
    channel_key: str,
    client: AuthenticatedClient = Depends(require_scopes("settlement:read")),
    service: GatewayReconciliationService = Depends(get_gateway_reconciliation_service),
) -> GatewayCapabilitiesResponse:
    """Return a channel's capabilities (no credentials, no HTTP call)."""
    with translate_errors():
        capabilities = service.get_capabilities(client.installation_id, channel_key)
    return GatewayCapabilitiesResponse.from_capabilities(capabilities)


@router.post(
    "/gateways/{channel_key}/verify-and-record",
    response_model=VerifiedCallbackResponse,
    summary="Verify a payment with its gateway and record it",
)
def verify_and_record_payment(
    channel_key: str,
    body: VerifyAndRecordRequest,
    response: Response,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: GatewayReconciliationService = Depends(get_gateway_reconciliation_service),
) -> VerifiedCallbackResponse:
    """Verify with the gateway, then record the payment (201, or 200 on replay).

    The gateway is the authority for the amount: a mismatch against
    ``expected_amount_irr`` records nothing. Manual channels have no
    verify API — use the callbacks route for them.
    """
    with translate_errors():
        result = service.verify_and_record(
            client.installation_id,
            channel_key,
            body.payment_ref,
            expected_amount_irr=body.expected_amount_irr,
            store_order_id=body.store_order_id,
            store_payment_id=body.store_payment_id,
        )
    session.commit()
    response.status_code = 201 if result.created else 200
    return VerifiedCallbackResponse(
        payment=GatewayPaymentResponse.from_payment(result.payment),
        tranches=[SettlementTrancheResponse.from_tranche(t) for t in result.tranches],
        created=result.created,
    )


@router.post(
    "/gateways/{channel_key}/callbacks",
    response_model=VerifiedCallbackResponse,
    summary="Record a callback the store already verified with the gateway",
)
def record_gateway_callback(
    channel_key: str,
    body: RecordCallbackRequest,
    response: Response,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: GatewayReconciliationService = Depends(get_gateway_reconciliation_service),
) -> VerifiedCallbackResponse:
    """Record a store-verified callback (201, or 200 on an idempotent replay)."""
    source = _parse_callback_source(body.source)
    with translate_errors():
        result = service.record_gateway_callback(
            client.installation_id,
            channel_key,
            gateway_transaction_id=body.gateway_transaction_id,
            gross_irr=body.gross_irr,
            gateway_confirmed_at=body.gateway_confirmed_at,
            source=source,
            store_order_id=body.store_order_id,
            store_payment_id=body.store_payment_id,
            tracking_ref=body.tracking_ref,
        )
    session.commit()
    response.status_code = 201 if result.created else 200
    return VerifiedCallbackResponse(
        payment=GatewayPaymentResponse.from_payment(result.payment),
        tranches=[SettlementTrancheResponse.from_tranche(t) for t in result.tranches],
        created=result.created,
    )


@router.post(
    "/gateways/{channel_key}/reconcile",
    response_model=ReconcileResponse,
    summary="Inquire one payment's status and converge the recorded row",
)
def reconcile_transaction(
    channel_key: str,
    body: ReconcileRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: GatewayReconciliationService = Depends(get_gateway_reconciliation_service),
) -> ReconcileResponse:
    """Reconcile one transaction: record a paid payment, cancel a cancelled one."""
    with translate_errors():
        result = service.reconcile_transaction(
            client.installation_id,
            channel_key,
            body.gateway_transaction_id,
            payment_ref=body.payment_ref,
            expected_amount_irr=body.expected_amount_irr,
            store_order_id=body.store_order_id,
        )
    session.commit()
    return ReconcileResponse(
        outcome=result.outcome,
        inquiry=GatewayInquiryResponse.from_inquiry(result.inquiry),
        payment=(GatewayPaymentResponse.from_payment(result.payment) if result.payment is not None else None),
        tranches=[SettlementTrancheResponse.from_tranche(t) for t in result.tranches],
    )


@router.post(
    "/gateways/{channel_key}/refunds",
    response_model=RefundPaymentResponse,
    summary="Refund a payment through its gateway, then cancel the row (full refunds only)",
)
def refund_payment(
    channel_key: str,
    body: RefundPaymentRequest,
    client: AuthenticatedClient = Depends(require_scopes("settlement:manage")),
    session: Session = Depends(get_db_session),
    service: GatewayReconciliationService = Depends(get_gateway_reconciliation_service),
) -> RefundPaymentResponse:
    """Refund through the gateway first; cancel the row only on success."""
    with translate_errors():
        result = service.refund_payment(
            client.installation_id,
            channel_key,
            body.payment_id,
            body.amount_irr,
            reason=body.reason,
        )
    session.commit()
    return RefundPaymentResponse(
        refund_id=result.refund.refund_id,
        amount_irr=result.refund.amount_irr,
        status=result.refund.status.value,
        payment=GatewayPaymentResponse.from_payment(result.payment),
        cancelled_tranche_ids=list(result.cancelled_tranche_ids),
    )
