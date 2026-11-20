"""Pipeline consumer for ``payment.settlement.received`` (US-554).

The US-523 deferred item: the canonical event exists (published by
every approved settlement import) but no consumer processed it.
This handler is the consumer — settlement receipts arriving as
events flow through the SAME tie-out + auto-post rules as file
imports: the handler calls the settlement import service's
``run_tieout_for_batch`` (the shared path), never a second
implementation.

Payload contract (the same shape ``approve_import`` publishes, plus
the gateway's own amount fields — normalization converts
``settlement_amount`` / ``net_amount`` to whole Rial when the
store prices are in Toman; ``net_irr`` is already Rial):

- ``channel_key`` (required): the payment channel slug.
- ``payout_date`` (required): ISO date the gateway paid.
- ``net_irr`` | ``settlement_amount`` | ``net_amount`` (one
  required): the settlement net in whole Rial.
- ``settlement_event_id`` | ``import_batch_id`` (at least one
  expected): the HesabYar settlement event / import batch this
  receipt belongs to. An event naming neither is unattributed: a
  ``settlement_variance`` review item is filed and the event is
  processed (not retried — retrying cannot create the missing
  event; a human imports the file or records the deposit).
- ``deductions_irr`` / ``tranche_ids`` (optional): carried for
  provenance; the tie-out reads the stored rows, never the payload.

Idempotency: the pipeline's §10.4 external-entity pre-check
suppresses a redelivery that carries an external entity reference;
the tie-out itself is idempotent (a settled batch re-run is a
no-op, never a re-post); and the pipeline records the §10.4
mapping (settlement event → HesabYar event id) after handling.
Failures: an unknown channel or an invalid payload is permanent
(fail-closed to the review queue via the pipeline); a transient
engine failure retries with the pipeline's bounded backoff and
dead-letters after MAX_ATTEMPTS — never silent.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

from hesabyar.application.commands.events import SettlementReceivedOutcome
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement_import import (
    IMPORT_AWAITING_REVIEW,
    IMPORT_REJECTED,
    IMPORT_SETTLED,
    SettlementImportService,
)
from hesabyar.domain.events.errors import (
    InvalidEventEnvelopeError,
    MappingUnresolvedError,
)
from hesabyar.domain.events.models import NormalizedEvent, ResolvedMapping
from hesabyar.domain.integrations.ports import IntegrationsRegistryPort
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.settlement.models import SettlementEvent
from hesabyar.domain.settlement.ports import SettlementRepositoryPort

__all__ = [
    "SettlementReceivedHandler",
    "validate_settlement_received_payload",
]


def _require_string(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidEventEnvelopeError(
            f"Field {field_name!r} is required and must be a non-empty string.",
            details={"field": field_name},
        )
    return value.strip()


def _optional_string(payload: Mapping[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidEventEnvelopeError(
            f"Field {field_name!r} must be a non-empty string.",
            details={"field": field_name},
        )
    return value.strip()


def _require_amount(payload: Mapping[str, Any]) -> int:
    """The settlement net in whole Rial, from any accepted field name."""
    for field_name in ("net_irr", "settlement_amount", "net_amount"):
        value = payload.get(field_name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidEventEnvelopeError(
                f"Field {field_name!r} must be a whole-Rial integer.",
                details={"field": field_name},
            )
        return value
    raise InvalidEventEnvelopeError(
        "A settlement receipt needs a net amount ('net_irr', "
        "'settlement_amount' or 'net_amount').",
        details={"field": "net_irr"},
    )


def validate_settlement_received_payload(
    payload: Mapping[str, Any],
) -> tuple[str, datetime.date, int, str | None, str | None]:
    """Validate a settlement-received payload (after money conversion).

    Returns ``(channel_key, payout_date, net_irr,
    settlement_event_id, import_batch_id)``. Raises
    :class:`InvalidEventEnvelopeError` on the first problem — the
    pipeline treats it as a permanent failure and routes the event
    to the review queue, never guessing a settlement.
    """
    channel_key = _require_string(payload, "channel_key")
    payout_raw = _require_string(payload, "payout_date")
    try:
        payout_date = datetime.date.fromisoformat(payout_raw)
    except ValueError as exc:
        raise InvalidEventEnvelopeError(
            f"Field 'payout_date' is not a valid ISO date: {payout_raw!r}.",
            details={"field": "payout_date"},
        ) from exc
    net_irr = _require_amount(payload)
    if net_irr < 0:
        raise InvalidEventEnvelopeError(
            f"A settlement receipt net must not be negative, got {net_irr}.",
            details={"field": "net_irr"},
        )
    settlement_event_id = _optional_string(payload, "settlement_event_id")
    import_batch_id = _optional_string(payload, "import_batch_id")
    tranche_ids = payload.get("tranche_ids")
    if tranche_ids is not None and (
        not isinstance(tranche_ids, list)
        or not all(isinstance(item, str) for item in tranche_ids)
    ):
        raise InvalidEventEnvelopeError(
            "Field 'tranche_ids' must be a list of strings.",
            details={"field": "tranche_ids"},
        )
    return channel_key, payout_date, net_irr, settlement_event_id, import_batch_id


class SettlementReceivedHandler:
    """Handle ``payment.settlement.received`` via the shared tie-out path."""

    def __init__(
        self,
        settlement_import: SettlementImportService,
        registry: IntegrationsRegistryPort,
        settlement: SettlementRepositoryPort,
        review: ReviewQueueCommandService,
    ) -> None:
        self._settlement_import = settlement_import
        self._registry = registry
        self._settlement = settlement
        self._review = review

    def handle_settlement_received(
        self,
        *,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        tenant_id: str,
        installation_id: str,
    ) -> SettlementReceivedOutcome:
        """Process one settlement receipt: tie out + auto-post (or file it).

        The company comes from the resolved mapping; a missing
        company runs the tie-out in draft-hold mode (the gate fails
        closed without a company — the batch stays approved, nothing
        posts).
        """
        payload = event.converted_payload
        channel_key, payout_date, net_irr, settlement_event_id, import_batch_id = (
            validate_settlement_received_payload(payload)
        )
        channel = self._registry.get_channel(installation_id, channel_key)
        if channel is None:
            raise MappingUnresolvedError(
                f"Payment channel {channel_key!r} is not in this installation's registry.",
                details={"channel_key": channel_key},
            )
        settlement_event = self._find_settlement_event(
            installation_id, settlement_event_id, import_batch_id
        )
        if settlement_event is None:
            return self._file_unattributed(
                tenant_id=tenant_id,
                channel_key=channel_key,
                payout_date=payout_date,
                net_irr=net_irr,
                settlement_event_id=settlement_event_id,
                import_batch_id=import_batch_id,
                inbox_event_id=event.event.id,
            )
        batch_id = settlement_event.import_batch_id
        if batch_id is None:
            return self._file_unattributed(
                tenant_id=tenant_id,
                channel_key=channel_key,
                payout_date=payout_date,
                net_irr=net_irr,
                settlement_event_id=settlement_event.id,
                import_batch_id=None,
                inbox_event_id=event.event.id,
                reason=(
                    f"رویداد تسویهٔ {settlement_event.id} بچ ایمپورتی ندارد و "
                    "نمی‌تواند tie-out شود."
                ),
            )
        run = self._settlement_import.run_tieout_for_batch(
            installation_id,
            tenant_id,
            batch_id,
            mapping.company,
            "settlement-received-consumer",
        )
        if run is None:
            return self._handle_untieoutable_batch(
                tenant_id=tenant_id,
                channel_key=channel_key,
                settlement_event=settlement_event,
                batch_id=batch_id,
                inbox_event_id=event.event.id,
            )
        if run.posted:
            return SettlementReceivedOutcome(
                settlement_event_id=settlement_event.id,
                import_batch_id=batch_id,
                tieout_holds=run.tieout.holds,
                posted=True,
                tryton_journal_id=run.tryton_journal_id,
                needs_review=False,
                detail=(
                    f"Settlement receipt tied out (difference "
                    f"{run.tieout.difference_irr:,} IRR) and the bank receipt "
                    f"posted (journal {run.tryton_journal_id})."
                ),
            )
        return SettlementReceivedOutcome(
            settlement_event_id=settlement_event.id,
            import_batch_id=batch_id,
            tieout_holds=run.tieout.holds,
            posted=False,
            tryton_journal_id=None,
            needs_review=run.status == "exception",
            detail=(
                f"Settlement receipt tie-out ran (holds={run.tieout.holds}, "
                f"difference {run.tieout.difference_irr:,} IRR); "
                f"not posted: {run.gate_reason or run.status}."
            ),
        )

    def _find_settlement_event(
        self,
        installation_id: str,
        settlement_event_id: str | None,
        import_batch_id: str | None,
    ) -> SettlementEvent | None:
        """Find the settlement event the receipt belongs to, if any."""
        if settlement_event_id is not None:
            return self._settlement.get_settlement_event(
                installation_id, settlement_event_id
            )
        if import_batch_id is not None:
            events = self._settlement.list_settlement_events(
                installation_id, import_batch_id=import_batch_id
            )
            return events[0] if events else None
        return None

    def _handle_untieoutable_batch(
        self,
        *,
        tenant_id: str,
        channel_key: str,
        settlement_event: SettlementEvent,
        batch_id: str,
        inbox_event_id: str,
    ) -> SettlementReceivedOutcome:
        """Explain a batch the tie-out cannot run for (never silent).

        Settled = already posted (idempotent success, no re-post).
        Awaiting review = the operator has not approved the file yet
        (the approve step ties out; the receipt waits). Rejected =
        a genuine discrepancy (the gateway paid for a rejected
        batch) — a review item is filed.
        """
        file_import = self._settlement.get_file_import_by_batch(
            settlement_event.installation_id, batch_id
        )
        status = file_import.status if file_import is not None else "unknown"
        if status == IMPORT_SETTLED:
            return SettlementReceivedOutcome(
                settlement_event_id=settlement_event.id,
                import_batch_id=batch_id,
                tieout_holds=True,
                posted=False,
                tryton_journal_id=None,
                needs_review=False,
                detail="Batch already settled: tie-out skipped, nothing re-posted.",
            )
        if status == IMPORT_AWAITING_REVIEW:
            return SettlementReceivedOutcome(
                settlement_event_id=settlement_event.id,
                import_batch_id=batch_id,
                tieout_holds=None,
                posted=False,
                tryton_journal_id=None,
                needs_review=False,
                detail=(
                    "Batch is still awaiting review: the receipt is recorded "
                    "and the approve step will tie it out."
                ),
            )
        if status == IMPORT_REJECTED:
            self._review.file_item(
                tenant_id=tenant_id,
                item_type=ReviewItemType.SETTLEMENT_VARIANCE,
                severity=ReviewSeverity.WARNING,
                reason=(
                    f"درگاه برای بچ ردشدهٔ {batch_id[:8]} کانال {channel_key} "
                    f"رسید تسویه فرستاده است ({settlement_event.net_irr:,} ریال، "
                    f"تاریخ {settlement_event.payout_date.isoformat()})؛ بچ رد شده "
                    "ولی پول رسیده است."
                ),
                suggested_action=(
                    "بچ ردشده را بازبینی کنید؛ اگر رسید درست است، بچ را دوباره "
                    "اجرا کنید یا واریز را دستی ثبت کنید."
                ),
                subtype="unknown_reference",
                channel=channel_key,
                source_event_ref=f"settlement-event:{inbox_event_id}",
                amount_irr=settlement_event.net_irr,
                actor="settlement-received-consumer",
            )
            return SettlementReceivedOutcome(
                settlement_event_id=settlement_event.id,
                import_batch_id=batch_id,
                tieout_holds=None,
                posted=False,
                tryton_journal_id=None,
                needs_review=True,
                detail="Batch was rejected but the gateway paid: review item filed.",
            )
        return SettlementReceivedOutcome(
            settlement_event_id=settlement_event.id,
            import_batch_id=batch_id,
            tieout_holds=None,
            posted=False,
            tryton_journal_id=None,
            needs_review=True,
            detail=f"Batch is in state {status!r}; tie-out cannot run.",
        )

    def _file_unattributed(
        self,
        *,
        tenant_id: str,
        channel_key: str,
        payout_date: datetime.date,
        net_irr: int,
        settlement_event_id: str | None,
        import_batch_id: str | None,
        inbox_event_id: str,
        reason: str | None = None,
    ) -> SettlementReceivedOutcome:
        """File a review item for a receipt with no settlement event/batch.

        The money is real but unattributed — a human imports the
        file or records the deposit. The event is processed (not
        retried): retrying cannot create the missing event.
        """
        if reason is None:
            reference = settlement_event_id or import_batch_id or "بدون مرجع"
            reason = (
                f"رسید تسویهٔ {net_irr:,} ریالی کانال {channel_key} "
                f"(تاریخ {payout_date.isoformat()}) به هیچ رویداد تسویه یا بچی "
                f"متصل نیست (مرجع: {reference})؛ فایل تسویه ایمپورت نشده است."
            )
        self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.SETTLEMENT_VARIANCE,
            severity=ReviewSeverity.WARNING,
            reason=reason,
            suggested_action="فایل تسویهٔ درگاه را ایمپورت کنید یا واریز بانکی را دستی ثبت کنید تا رسید تطبیق بخورد.",
            subtype="unknown_reference",
            channel=channel_key,
            source_event_ref=f"settlement-event:{inbox_event_id}",
            amount_irr=net_irr,
            actor="settlement-received-consumer",
        )
        return SettlementReceivedOutcome(
            settlement_event_id=settlement_event_id,
            import_batch_id=import_batch_id,
            tieout_holds=None,
            posted=False,
            tryton_journal_id=None,
            needs_review=True,
            detail="Receipt is unattributed (no settlement event/batch): review item filed.",
        )
