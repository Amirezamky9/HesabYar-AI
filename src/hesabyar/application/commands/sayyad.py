"""Sayyad inquiry service (US-534, phased design §5 §1.3/§1.4).

Runs Fintech (legal tier) and Ehrazino (natural tier) inquiries
through the simulator ports and records every verdict as a
structured :class:`InquiryReceipt` — verdict/code/time/reference
only, never the raw response and never a full national id (§3.5).
When no port is wired the service still accepts MANUAL receipts
through :meth:`ChequeCommandService.record_inquiry_receipt` — the
manual channel is the working path until the ۵ت remainder (B3)
verifies live transport.

Colour verdicts update the cheque's point-in-time colour fields
(``issuer_color_code`` + ``issuer_color_inquired_at``); the
decision logic itself lives in
:func:`hesabyar.domain.cheques.state_machine.evaluate_pre_acceptance`
and only ever reads the NUMERIC code 1–5, never amount thresholds.

The MANDATORY receipt rule (§1.4) is audited here:
:meth:`audit_receipt_rule` files one ``missing_inquiry_receipt``
review item per held cheque that reached ``held`` without an
item/status receipt (idempotent per open item — a re-run files
nothing new while the item is still open).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.cheques.errors import ChequeNotFoundError, ChequeValidationError
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeState,
    InquiryChannel,
    InquiryReceipt,
    InquiryType,
)
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.cheques.sayyad import (
    EhrazinoPort,
    FintechSayyadPort,
    SayyadInquiryError,
    SayyadInquiryResult,
    has_required_receipt,
    inquiry_type_for_scope,
)
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReceiptRuleAudit:
    """Outcome of one mandatory-receipt-rule audit run."""

    checked: int = 0
    missing: int = 0
    items_filed: int = 0
    cheque_ids: tuple[str, ...] = ()


class SayyadInquiryService:
    """Run Sayyad inquiries and record their receipts."""

    def __init__(
        self,
        repository: ChequeRepositoryPort,
        *,
        finnotech: FintechSayyadPort | None = None,
        ehrazino: EhrazinoPort | None = None,
        review: ReviewQueueCommandService | None = None,
        queue: ReviewQueuePort | None = None,
    ) -> None:
        self._repository = repository
        # Ports are injected by the caller (the interfaces layer wires
        # the simulators until B3, US-534). The application layer must
        # never import infrastructure (§6.3), so there is no default
        # here: an inquiry without a wired port fails closed.
        self._finnotech = finnotech
        self._ehrazino = ehrazino
        self._review = review
        self._queue = queue

    def _require_finnotech(self) -> FintechSayyadPort:
        if self._finnotech is None:
            raise SayyadInquiryError("No Fintech Sayyad inquiry port wired.")
        return self._finnotech

    def _require_ehrazino(self) -> EhrazinoPort:
        if self._ehrazino is None:
            raise SayyadInquiryError("No Ehrazino inquiry port wired.")
        return self._ehrazino

    def get_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque:
        """Load one cheque, tenant/installation-scoped (router access checks use this)."""
        return self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )

    # -- Fintech (legal tier) -------------------------------------------------

    def inquire_finnotech(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        scope: str,
        scenario: str | None = None,
    ) -> InquiryReceipt:
        """Run one Fintech Sayyad scope inquiry; store the verdict receipt.

        Unknown scopes fail closed (no inquiry runs). The cheque's
        colour / back-cheque fields update from a colour /
        back-cheques verdict; the Sayyad ``in_transit`` status in a
        status verdict is recorded on the receipt only — it is NOT
        the accounting ``in_clearing`` state (§1.3).
        """
        inquiry_type = inquiry_type_for_scope(scope)
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        result = self._require_finnotech().inquire(
            scope,
            sayad_id=cheque.sayad_id,
            national_id=cheque.issuer_national_id,
            scenario=scenario,
        )
        receipt = self._store_result(
            cheque=cheque,
            result=result,
            actor=actor,
            inquiry_type=inquiry_type,
        )
        self._apply_verdict_to_cheque(cheque, result)
        return receipt

    # -- Ehrazino (natural tier) ----------------------------------------------

    def inquire_ehrazino_color(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        scenario: str | None = None,
    ) -> InquiryReceipt:
        """Ehrazino issuer-colour inquiry (verified shape: nationalCode -> code 1–5).

        Colour is issuer history only — never a statement about this
        leaf. The numeric code updates the cheque's point-in-time
        colour fields; the name is display only.
        """
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        result = self._require_ehrazino().cheque_color(
            cheque.issuer_national_id, scenario=scenario
        )
        receipt = InquiryReceipt(
            cheque_id=cheque.id,
            channel=InquiryChannel.EHRAZINO_API,
            inquiry_type=InquiryType.COLOR,
            result_code=f"color={result.color_code}",
            result_summary=(
                f"Ehrazino issuer colour is {result.color_name} (code {result.color_code} of 5) — "
                "issuer history only, not a statement about this leaf"
                + (" (simulated)." if result.simulated else ".")
            ),
            reference=(
                f"SIM-ehrazino-color:{cheque.sayad_id}" if result.simulated else "ehrazino-color"
            ),
            recorded_by=actor,
            cost_irr=result.cost_irr,
            requested_at=result.requested_at,
            responded_at=result.responded_at,
        )
        stored = self._repository.add_inquiry_receipt(receipt)
        cheque.issuer_color_code = result.color_code
        cheque.issuer_color_inquired_at = datetime.datetime.now(datetime.UTC)
        cheque.touch()
        self._repository.save(cheque)
        return stored

    def inquire_ehrazino_sayad_check(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        cheque_id: str,
        actor: str,
        scenario: str | None = None,
    ) -> InquiryReceipt:
        """Ehrazino leaf-identity inquiry (verified shape: sayadId -> items verdict)."""
        cheque = self._require_cheque(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        result = self._require_ehrazino().sayad_check(cheque.sayad_id, scenario=scenario)
        receipt = InquiryReceipt(
            cheque_id=cheque.id,
            channel=InquiryChannel.EHRAZINO_API,
            inquiry_type=InquiryType.ITEMS_MATCH,
            result_code=f"items_match={str(result.items_matched).lower()}",
            result_summary=(
                "Ehrazino leaf identity "
                + ("matches" if result.items_matched else "DOES NOT match")
                + " the registered Sayyad items"
                + (" (simulated)." if result.simulated else ".")
            ),
            reference=(
                f"SIM-ehrazino-sayad:{cheque.sayad_id}" if result.simulated else "ehrazino-sayad"
            ),
            recorded_by=actor,
            cost_irr=result.cost_irr,
            requested_at=result.requested_at,
            responded_at=result.responded_at,
        )
        return self._repository.add_inquiry_receipt(receipt)

    # -- mandatory receipt rule (§1.4) ----------------------------------------

    def audit_receipt_rule(
        self, *, tenant_id: str, installation_id: str, actor: str
    ) -> ReceiptRuleAudit:
        """File ``missing_inquiry_receipt`` items for held cheques without a receipt.

        Checks every ``held`` cheque: a cheque with no item/status
        receipt (and the ``receipt_missing_warning`` flag from its
        confirm-receipt) is missing its mandatory receipt. One open
        item per cheque — a re-run files nothing new while the item
        is still open. Cheques whose receipts arrived later clear
        the flag on the next confirm path; this audit only files.
        """
        held = list(
            self._repository.list(
                tenant_id=tenant_id,
                installation_id=installation_id,
                state=ChequeState.HELD,
                limit=1000,
            )
        )
        already_open = self._open_missing_receipt_refs(tenant_id)
        checked = 0
        missing: list[str] = []
        filed = 0
        for cheque in held:
            checked += 1
            receipts = self._repository.list_inquiry_receipts(cheque.id)
            if has_required_receipt([receipt.inquiry_type for receipt in receipts]):
                continue
            if not cheque.receipt_missing_warning and receipts:
                continue
            missing.append(cheque.id)
            ref = f"cheque:{cheque.id}"
            if ref in already_open:
                continue
            if self._review is None:
                logger.warning(
                    "No review service wired; missing-receipt item not filed for %s",
                    cheque.id,
                )
                continue
            try:
                self._review.file_item(
                    tenant_id=tenant_id,
                    item_type=ReviewItemType.CHEQUE_EXCEPTION,
                    severity=ReviewSeverity.WARNING,
                    reason=(
                        f"Cheque {cheque.sayad_id} reached held without an "
                        "item/status inquiry receipt (mandatory receipt rule, §1.4)."
                    ),
                    suggested_action=(
                        "Record the Sayyad item/status inquiry receipt for this "
                        "cheque (SMS / CBI website / bank app / Fintech / "
                        "Ehrazino) — the held transition is recorded with a "
                        "warning until then and appears in the mismatch report."
                    ),
                    company_id=str(cheque.company_engine_id),
                    subtype="missing_inquiry_receipt",
                    source_event_ref=ref,
                    amount_irr=cheque.amount_irr,
                    actor=actor,
                )
                filed += 1
            except Exception:  # noqa: BLE001 - one filing failure must not stop the audit
                logger.exception("Failed to file missing-receipt item for %s", cheque.id)
        return ReceiptRuleAudit(
            checked=checked,
            missing=len(missing),
            items_filed=filed,
            cheque_ids=tuple(missing),
        )

    # -- helpers ---------------------------------------------------------------

    def _require_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> Cheque:
        cheque = self._repository.get(
            cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque is None:
            raise ChequeNotFoundError(cheque_id)
        return cheque

    def _store_result(
        self,
        *,
        cheque: Cheque,
        result: SayyadInquiryResult,
        actor: str,
        inquiry_type: InquiryType,
    ) -> InquiryReceipt:
        receipt = InquiryReceipt(
            cheque_id=cheque.id,
            channel=result.channel,
            inquiry_type=inquiry_type,
            result_code=result.result_code,
            result_summary=result.result_summary,
            reference=result.reference,
            recorded_by=actor,
            cost_irr=result.cost_irr,
            requested_at=result.requested_at,
            responded_at=result.responded_at,
        )
        return self._repository.add_inquiry_receipt(receipt)

    def _apply_verdict_to_cheque(self, cheque: Cheque, result: SayyadInquiryResult) -> None:
        """Apply a verdict's structured fields to the cheque (colour / back-cheques)."""
        changed = False
        if result.color_code is not None:
            if not (1 <= result.color_code <= 5):
                raise ChequeValidationError(
                    f"Issuer colour code {result.color_code} is outside the 1–5 scale.",
                    field_errors={"color_code": "Colour code must be 1–5."},
                )
            cheque.issuer_color_code = result.color_code
            cheque.issuer_color_inquired_at = datetime.datetime.now(datetime.UTC)
            changed = True
        if result.back_cheques_count is not None:
            cheque.issuer_back_cheques = result.back_cheques_count
            changed = True
        if changed:
            cheque.touch()
            self._repository.save(cheque)

    def _open_missing_receipt_refs(self, tenant_id: str) -> set[str]:
        """Source refs of still-open missing-receipt items (audit idempotency)."""
        if self._queue is None:
            return set()
        try:
            items = self._queue.list_items(
                tenant_id,
                ReviewItemFilters(
                    item_type=ReviewItemType.CHEQUE_EXCEPTION,
                ),
            )
        except Exception:  # noqa: BLE001 - a queue read failure degrades to filing (dupes possible)
            logger.exception("Receipt-rule audit could not list open review items")
            return set()
        return {
            item.source_event_ref
            for item in items
            if item.subtype == "missing_inquiry_receipt"
            and not item.is_terminal
            and item.source_event_ref is not None
        }
