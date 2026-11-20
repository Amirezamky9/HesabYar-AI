"""Site cheque intake handler (US-533, Phase 5-4 ۵ث).

The adapter between the §10.3 event pipeline and the cheque
command service: it translates a normalized ``cheque.received.*``
event (+ its resolved mappings) into a
:class:`ChequeCommandService
<hesabyar.application.commands.cheques.ChequeCommandService>`
call and back into a :class:`ChequeIntakeOutcome
<hesabyar.application.commands.events.ChequeIntakeOutcome>`.

The pipeline owns the envelope, idempotency, mapping resolution
and audit; this handler owns nothing but the translation — the
registry write, the payee check, the dual-entry convergence and
the review-queue filing all live in the command service.
"""

from __future__ import annotations

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.events import (
    ChequeIntakeOutcome,
)
from hesabyar.domain.cheques.errors import ChequeDuplicateError
from hesabyar.domain.events.models import NormalizedEvent, ResolvedMapping

#: Actor recorded on site-driven cheque writes (the store that sent
#: the event is named, so the audit trail shows the origin).
SITE_INTAKE_ACTOR_PREFIX = "system:store-intake"


class SiteChequeIntakeHandler:
    """Translate pipeline cheque events into cheque-service calls."""

    def __init__(self, commands: ChequeCommandService) -> None:
        self._commands = commands

    def register_site_cheque(
        self,
        *,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        tenant_id: str,
        installation_id: str,
    ) -> ChequeIntakeOutcome:
        """Handle ``cheque.received.registered``: intake, no journal.

        An identical re-registration returns the existing cheque as
        a duplicate-conflict outcome (the review item was already
        filed by the service for a contradicting payload); the
        pipeline still marks the event processed — the existing
        record stands and the event is never retried.
        """
        assert mapping.company is not None  # the pipeline checked completeness
        payload = dict(event.converted_payload)
        try:
            cheque = self._commands.register_site(
                tenant_id=tenant_id,
                installation_id=installation_id,
                company_engine_id=mapping.company.engine_id,
                payload=payload,
                actor=f"{SITE_INTAKE_ACTOR_PREFIX}:{event.event.source_system}",
                source_event_id=event.event.source_event_id,
            )
        except ChequeDuplicateError:
            existing = self._commands.get_by_sayad_id(
                str(payload["sayad_id"]),
                tenant_id=tenant_id,
                installation_id=installation_id,
            )
            amount_irr = (
                existing.amount_irr
                if existing is not None
                else int(payload.get("amount_toman", 0)) * 10
            )
            return ChequeIntakeOutcome(
                engine_cheque_id=existing.engine_id if existing else None,
                amount_irr=amount_irr,
                detail=(
                    f"Site re-registration of sayad_id {payload['sayad_id']} "
                    "conflicts with the live cheque: a duplicate_sayad_id "
                    "review item was filed; the existing record stands."
                ),
                duplicate_conflict=True,
            )
        return ChequeIntakeOutcome(
            engine_cheque_id=cheque.engine_id,
            amount_irr=cheque.amount_irr,
            detail=(
                f"Site cheque {cheque.sayad_id} registered as intake "
                f"(state registered, source site, no journal; engine "
                f"cheque {cheque.engine_id})."
                + (
                    " Sayyad registration unconfirmed — do not accept "
                    "an unregistered cheque (ruling 870)."
                    if cheque.sayad_unconfirmed
                    else ""
                )
            ),
        )

    def cancel_site_cheque(
        self,
        *,
        event: NormalizedEvent,
        mapping: ResolvedMapping,
        tenant_id: str,
        installation_id: str,
    ) -> ChequeIntakeOutcome:
        """Handle ``cheque.received.cancelled``: void or review-file."""
        assert mapping.company is not None  # the pipeline checked completeness
        payload = event.converted_payload
        reason = payload.get("reason")
        outcome = self._commands.cancel_site_registration(
            tenant_id=tenant_id,
            installation_id=installation_id,
            sayad_id=str(payload["sayad_id"]),
            actor=f"{SITE_INTAKE_ACTOR_PREFIX}:{event.event.source_system}",
            reason=reason if isinstance(reason, str) else None,
        )
        cheque = outcome.cheque
        if outcome.outcome == "voided":
            detail = (
                f"Site cancellation voided the intake of cheque "
                f"{cheque.sayad_id} (registered -> voided, no journal)."
            )
        elif outcome.outcome == "review_filed":
            detail = (
                f"Site cancellation of cheque {cheque.sayad_id} arrived "
                f"after held ({cheque.state.value}): a "
                "cancellation_after_held review item was filed for a "
                "manual return; nothing was voided automatically."
            )
        else:
            detail = (
                f"Site cancellation of cheque {cheque.sayad_id} replays "
                f"as a no-op: the intake is already {cheque.state.value}."
            )
        return ChequeIntakeOutcome(
            engine_cheque_id=cheque.engine_id,
            amount_irr=cheque.amount_irr,
            detail=detail,
        )
