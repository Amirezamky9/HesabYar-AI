"""Installment command service (US-540 + US-542 + US-543, Phase 5-5 ۵ج).

Plan creation, receipt recording (with the linkage guard), and
sale-contract creation. US-540 creates NO journal and makes NO
engine call: the plan is a schedule, and a receipt records a
payment whose accounting document the caller already posted (or
drafted) — the receipt carries the move id, it does not create the
move. Late-fee journals arrive with US-541 (draft by default, per
ADR 0003 decision 1 and the plan's ``auto_post`` opt-in).

US-542 adds cheque receipts: a receipt with
``payment_method=cheque`` names its cheque (``cheque_id``), the
cheque must be a received commercial cheque in ``held`` or
``in_clearing`` (the receipt's ``journal_move_id`` is its سند ۱),
the receipt allocates to at most ONE leg, and the leg enters
``pending_clearance`` until the cheque clears. The service links
the cheque to the leg (``installment_leg_id`` +
``applied_amount_irr`` + ``installment_leg_status``) and records
no journal of its own — cheque intake still creates NO journal
until ``held`` (US-533 rule, unchanged).

US-543 (BNPL): a BNPL receipt creates the receivable FROM THE
GATEWAY through the US-521 settlement engine — one
``gateway_payment`` per receipt (gross = the receipt amount), its
tranches derived from the gateway's active contract version
(Snapp Pay 4×25% over 4 months, DigiPay lump-sum, …; fee % from
the contract, never hard-coded). The settlement engine is REUSED,
never duplicated: this service calls
``SettlementCommandService.record_payment`` / ``cancel_payment``
and stores only the link (``gateway_payment_id``) on the receipt.
A gateway rejection/cancellation (``reject_bnpl_receipt``)
cancels the gateway payment, marks the receipt dead so its legs
return to ``expected``, and files a review-queue item.

Ordering: validation first (fail-closed, nothing stored on a bad
payload), then the guard, then the settlement call (BNPL only),
then one repository write. The service never commits — the caller
(router / worker) owns the transaction.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence

from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.commands.settlement import SettlementCommandService
from hesabyar.domain.cheques.errors import ChequeNotFoundError
from hesabyar.domain.cheques.models import (
    Cheque,
    ChequeKind,
    ChequePurpose,
    ChequeState,
    InstallmentLegStatus,
)
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.errors import (
    InstallmentBnplAlreadyRejectedError,
    InstallmentBnplGatewayMismatchError,
    InstallmentBnplPlanMethodError,
    InstallmentBnplPlanRequiredError,
    InstallmentBnplUnavailableError,
    InstallmentChequeAlreadyUsedError,
    InstallmentChequeAmountError,
    InstallmentChequeLinkError,
    InstallmentChequeNotReceivableError,
    InstallmentContractNotFoundError,
    InstallmentInvoiceMismatchError,
    InstallmentNotBnplReceiptError,
    InstallmentPlanExistsError,
    InstallmentPlanNotFoundError,
    InstallmentReceiptNotFoundError,
)
from hesabyar.domain.installments.finance import select_effective_contract
from hesabyar.domain.installments.models import (
    BnplReceiptStatus,
    InstallmentLeg,
    InstallmentPlan,
    InstallmentReceipt,
    InstallmentSaleContract,
    LegAllocation,
    PaymentMethod,
    PlanKind,
    PlanSource,
    new_contract_id,
    new_leg_id,
    new_plan_id,
    new_receipt_id,
)
from hesabyar.domain.installments.ports import InstallmentRepositoryPort
from hesabyar.domain.installments.state_machine import (
    LegView,
    allocate_receipt,
    split_allocations,
)
from hesabyar.domain.installments.validation import (
    ValidatedContract,
    ValidatedPlan,
    ValidatedReceipt,
    validate_contract_payload,
    validate_plan_payload,
    validate_receipt_payload,
)
from hesabyar.domain.review_queue.models import ReviewItemType, ReviewSeverity
from hesabyar.domain.settlement.commands import CancelGatewayPayment, CreateGatewayPayment
from hesabyar.domain.settlement.errors import SettlementPaymentInvalidError
from hesabyar.domain.settlement.models import GatewayPaymentStatus, PaymentSource

logger = logging.getLogger(__name__)

#: Cheque states a receipt may name (US-542): the leaf has been
#: accepted (سند ۱ exists, so the receipt's ``journal_move_id``
#: honestly references it) and the cheque can still clear. A
#: ``registered`` cheque has no journal yet; a cleared / bounced /
#: returned / endorsed / voided / expired / rejected cheque can no
#: longer be the instrument of a new receipt.
RECEIVABLE_CHEQUE_STATES: frozenset[ChequeState] = frozenset(
    {ChequeState.HELD, ChequeState.IN_CLEARING}
)


def resolve_cheque_states(
    receipts: Sequence[InstallmentReceipt],
    cheques: ChequeRepositoryPort | None,
    *,
    tenant_id: str,
    installation_id: str,
) -> dict[str, ChequeState]:
    """Resolve the current state of every cheque backing a receipt.

    The input to :func:`split_allocations`: one lookup per cheque
    receipt on the plan (plans carry few receipts). A cheque that
    cannot be resolved — no cheque repository wired, or the row is
    missing — is simply ABSENT from the map, and the split treats
    an unknown cheque as pending (fail-closed: never counted as
    received, visibly pending instead of silently dropped).
    """
    states: dict[str, ChequeState] = {}
    if cheques is None:
        return states
    for receipt in receipts:
        if receipt.payment_method is not PaymentMethod.CHEQUE:
            continue
        if receipt.cheque_id is None or receipt.cheque_id in states:
            continue
        cheque = cheques.get(
            receipt.cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque is not None:
            states[receipt.cheque_id] = cheque.state
    return states


class InstallmentCommandService:
    """Creates plans, contracts and receipts (guard enforced).

    The optional collaborators exist only so the US-540 unit tests
    keep constructing the service with a bare repository:
    ``settlement`` (the US-521 engine) and ``review`` (the unified
    review queue) for BNPL (US-543) — BNPL receipts refuse
    fail-closed without a settlement service
    (``InstallmentBnplUnavailableError``), and a rejection without
    a review service still cancels the payment and marks the
    receipt dead (the queue filing is skipped — the rejection
    itself is always recorded); ``cheques`` (US-542) is REQUIRED
    for cheque receipts — recording one without a cheque
    repository is a configuration error, raised loudly (see
    ``record_receipt``). Without it, existing cheque receipts'
    allocations are treated as pending (never received) by the
    split. Production wiring (REST dependencies) always supplies
    all three.
    """

    def __init__(
        self,
        repository: InstallmentRepositoryPort,
        cheques: ChequeRepositoryPort | None = None,
        *,
        settlement: SettlementCommandService | None = None,
        review: ReviewQueueCommandService | None = None,
    ) -> None:
        # ``cheques`` stays positional-or-keyword: US-542's callers
        # (dependencies + tests) pass it positionally.
        self._repository = repository
        self._settlement = settlement
        self._review = review
        self._cheques = cheques

    # -- plans -----------------------------------------------------------

    def create_plan(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company_engine_id: int,
        payload: dict[str, object],
        today: datetime.date | None = None,
    ) -> InstallmentPlan:
        """Create a plan + its legs from a validated payload.

        One plan per invoice: a second plan for the same invoice is
        a 409 (the DB unique index on
        ``(installation_id, invoice_id)`` is the backstop). When the
        payload names a contract, the contract must exist in this
        installation — a dangling contract ref is a 404, never a
        silent null. The contract may be named by id (one version)
        or by CODE (US-541): with a code, the version in force on
        ``today`` is selected (``select_effective_contract``) and
        the plan links THAT version — a plan's terms are fixed at
        creation, and a newer version never re-prices it.
        """
        validated: ValidatedPlan = validate_plan_payload(payload)
        existing = self._repository.get_plan_by_invoice(
            validated.invoice_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if existing is not None:
            raise InstallmentPlanExistsError(
                validated.invoice_id, existing_plan_id=existing.id
            )
        contract_id = validated.contract_id
        if contract_id is not None:
            contract = self._repository.get_contract(
                contract_id, tenant_id=tenant_id, installation_id=installation_id
            )
            if contract is None:
                raise InstallmentContractNotFoundError(contract_id)
        elif validated.contract_code is not None:
            day = today or datetime.date.today()
            versions = self._repository.list_contracts(
                tenant_id=tenant_id,
                installation_id=installation_id,
                code=validated.contract_code,
                limit=100,
            )
            selected = select_effective_contract(versions, day)
            if selected is None:
                raise InstallmentContractNotFoundError(validated.contract_code)
            contract_id = selected.id
        plan = InstallmentPlan(
            id=new_plan_id(),
            tenant_id=tenant_id,
            installation_id=installation_id,
            company_engine_id=company_engine_id,
            invoice_id=validated.invoice_id,
            total_principal=validated.total_principal,
            leg_count=len(validated.legs),
            contract_id=contract_id,
            customer_ref=validated.customer_ref,
            interval_days=validated.interval_days,
            first_due_date=validated.first_due_date,
            auto_post=validated.auto_post,
            source=PlanSource.MANUAL,
            kind=validated.kind,
            gateway_channel_key=validated.gateway_channel_key,
        )
        legs = [
            InstallmentLeg(
                id=new_leg_id(),
                plan_id=plan.id,
                sequence=leg.sequence,
                due_date=leg.due_date,
                principal=leg.principal,
            )
            for leg in validated.legs
        ]
        return self._repository.create_plan(plan, legs)

    # -- receipts (the linkage guard lives here) --------------------------

    def record_receipt(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        plan_id: str,
        payload: dict[str, object],
        today: datetime.date | None = None,
    ) -> InstallmentReceipt:
        """Record a receipt against a plan, allocating it to legs.

        Guard order (each step fail-closed, nothing stored on failure):
        1. the plan must exist in this installation (404 otherwise);
        2. the receipt must carry BOTH ``invoice_id`` and
           ``journal_move_id`` (``InstallmentLinkageError``, typed
           422 — enforced by ``validate_receipt_payload`` before any
           other field is even looked at);
        3. the receipt's invoice must be the plan's invoice
           (``InstallmentInvoiceMismatchError``, typed 422);
        4. the plan-kind guard (US-543): a BNPL receipt needs a BNPL
           plan, a non-BNPL receipt needs an internal plan, and a
           BNPL receipt's gateway must be the plan's pinned gateway
           — BNPL and internal money never mix on one plan;
        5. for a cheque receipt: the cheque must exist in this
           installation (404), be a received commercial cheque in
           ``held`` or ``in_clearing`` (typed 422 otherwise), not
           already back another receipt (typed 422), cover the
           receipt amount (typed 422), and not already be linked
           to a different leg (typed 422).

        The allocation (oldest overdue first, then earliest due;
        remainder as advance) is computed against the legs'
        CURRENT received + pending totals — counting only receipts
        that are still live (a rejected BNPL receipt's allocations
        no longer count, so its legs can be paid again) — so two
        receipts in a row allocate consistently, and a
        ``pending_clearance`` leg takes no further allocation (it
        is already fully covered by a cheque in flight). A cheque
        receipt allocates to at most ONE leg (the cheque ↔ leg
        link is single-leg); its remainder is advance. ``today``
        pins the wall-clock date for the status computation
        (tests); it defaults to the real today.

        BNPL receipts (US-543) create the gateway receivable BEFORE
        the receipt is stored: the settlement engine records one
        ``gateway_payment`` (gross = the receipt amount, confirmed
        at the receipt's received date) and derives its tranches
        from the gateway's active contract. A duplicate gateway
        reference (the same transaction recorded twice) is a 409
        from the engine, and an unknown channel / missing contract
        fails closed there too — in every case no receipt is stored.

        Cash / bank-transfer / other receipts are collected on
        receipt (``collected_at == received_date``). A cheque
        receipt is collected only when its cheque clears — the
        collection hook sets ``collected_at`` then (see
        :class:`InstallmentCollectionHook`).
        """
        plan = self._repository.get_plan(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None:
            raise InstallmentPlanNotFoundError(plan_id)
        validated: ValidatedReceipt = validate_receipt_payload(payload)
        if validated.invoice_id != plan.invoice_id:
            raise InstallmentInvoiceMismatchError(
                receipt_invoice_id=validated.invoice_id,
                plan_invoice_id=plan.invoice_id,
            )
        # -- US-543 (BNPL): the plan-kind guard ----------------------
        is_bnpl = validated.payment_method is PaymentMethod.BNPL
        if is_bnpl and plan.kind is not PlanKind.BNPL:
            raise InstallmentBnplPlanRequiredError(plan_id=plan.id)
        if not is_bnpl and plan.kind is PlanKind.BNPL:
            raise InstallmentBnplPlanMethodError(
                plan_id=plan.id, method=validated.payment_method.value
            )
        if is_bnpl:
            assert validated.gateway_channel_key is not None  # validation enforced it
            assert validated.gateway_reference is not None
            if validated.gateway_channel_key != plan.gateway_channel_key:
                raise InstallmentBnplGatewayMismatchError(
                    plan_id=plan.id,
                    plan_gateway=plan.gateway_channel_key or "",
                    receipt_gateway=validated.gateway_channel_key,
                )
        # -- end US-543 (BNPL) ---------------------------------------
        # -- US-542 (cheque): the cheque must be receivable ----------
        cheque: Cheque | None = None
        if validated.payment_method is PaymentMethod.CHEQUE:
            cheque = self._require_receivable_cheque(validated, tenant_id, installation_id)
        day = today or datetime.date.today()
        legs = self._repository.list_legs(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        receipts = self._repository.list_receipts(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        views = self._leg_views(
            legs, receipts, tenant_id=tenant_id, installation_id=installation_id
        )
        allocation = allocate_receipt(
            validated.amount,
            views,
            day,
            single_leg=validated.payment_method is PaymentMethod.CHEQUE,
        )
        if cheque is not None:
            self._check_cheque_leg_link(cheque, allocation.allocations)
        gateway_payment_id: str | None = None
        bnpl_status: BnplReceiptStatus | None = None
        if is_bnpl:
            gateway_payment_id = self._record_gateway_payment(
                installation_id=installation_id,
                validated=validated,
            )
            bnpl_status = BnplReceiptStatus.ACTIVE
        receipt = InstallmentReceipt(
            id=new_receipt_id(),
            plan_id=plan.id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            invoice_id=validated.invoice_id,
            journal_move_id=validated.journal_move_id,
            amount=validated.amount,
            received_date=validated.received_date,
            payment_method=validated.payment_method,
            allocations=allocation.allocations,
            advance=allocation.advance,
            reference=validated.reference,
            notes=validated.notes,
            cheque_id=validated.cheque_id,
            collected_at=(
                None
                if validated.payment_method is PaymentMethod.CHEQUE
                else validated.received_date
            ),
            gateway_channel_key=validated.gateway_channel_key,
            gateway_reference=validated.gateway_reference,
            gateway_payment_id=gateway_payment_id,
            bnpl_status=bnpl_status,
        )
        created = self._repository.create_receipt(receipt)
        if cheque is not None and allocation.allocations:
            self._link_cheque_to_leg(cheque, allocation.allocations[0])
        return created

    def _leg_views(
        self,
        legs: list[InstallmentLeg],
        receipts: list[InstallmentReceipt],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> list[LegView]:
        """Build the state-machine views for a plan's legs.

        Received vs pending-clearance per leg comes from
        :func:`split_allocations` (cheque receipts follow their
        cheque's state; everything else counts as received).
        """
        cheque_states = resolve_cheque_states(
            receipts, self._cheques, tenant_id=tenant_id, installation_id=installation_id
        )
        received_by_leg, pending_by_leg = split_allocations(receipts, cheque_states)
        return [
            LegView(
                leg_id=leg.id,
                sequence=leg.sequence,
                due_date=leg.due_date,
                principal=leg.principal,
                received=MoneyIRR(received_by_leg.get(leg.id, 0)),
                pending_clearance=MoneyIRR(pending_by_leg.get(leg.id, 0)),
            )
            for leg in legs
        ]

    def _require_receivable_cheque(
        self,
        validated: ValidatedReceipt,
        tenant_id: str,
        installation_id: str,
    ) -> Cheque:
        """Load + validate the cheque a cheque receipt names.

        Fail-closed, in order: the cheque repository must be wired
        (a deployment bug otherwise — raised loudly, never guessed);
        the cheque must exist in this installation (404); it must
        be a received commercial cheque in ``held`` or
        ``in_clearing``; it must not already back a receipt; and
        its amount must cover the receipt amount.
        """
        if self._cheques is None:
            raise RuntimeError(
                "InstallmentCommandService was constructed without a "
                "cheque repository, so it cannot record a cheque receipt. "
                "Wire the cheque repository (dependencies.py does)."
            )
        assert validated.cheque_id is not None  # validated: cheque => cheque_id
        cheque = self._cheques.get(
            validated.cheque_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if cheque is None:
            raise ChequeNotFoundError(validated.cheque_id)
        if (
            cheque.kind is not ChequeKind.RECEIVED
            or cheque.purpose is not ChequePurpose.PAYMENT
            or cheque.state not in RECEIVABLE_CHEQUE_STATES
        ):
            raise InstallmentChequeNotReceivableError(
                cheque.id,
                state=cheque.state.value,
                kind=cheque.kind.value,
                purpose=cheque.purpose.value,
            )
        existing = self._repository.get_receipt_by_cheque(
            cheque.id, tenant_id=tenant_id, installation_id=installation_id
        )
        if existing is not None:
            raise InstallmentChequeAlreadyUsedError(
                cheque.id, existing_receipt_id=existing.id
            )
        if validated.amount.amount > cheque.amount_irr:
            raise InstallmentChequeAmountError(
                receipt_amount_irr=validated.amount.amount,
                cheque_amount_irr=cheque.amount_irr,
            )
        return cheque

    @staticmethod
    def _check_cheque_leg_link(
        cheque: Cheque, allocations: tuple[LegAllocation, ...]
    ) -> None:
        """Refuse a cheque already linked to a different leg.

        The cheque may already carry an ``installment_leg_id``
        (intake-linked before the receipt); it must be None or the
        leg this receipt allocates to. With no allocations (the
        whole receipt is advance) there is no leg to check — the
        cheque stays unlinked.
        """
        if not allocations:
            return
        leg_id = allocations[0].leg_id
        if cheque.installment_leg_id is not None and cheque.installment_leg_id != leg_id:
            raise InstallmentChequeLinkError(
                cheque.id, linked_leg_id=cheque.installment_leg_id, leg_id=leg_id
            )

    def _link_cheque_to_leg(self, cheque: Cheque, allocation: LegAllocation) -> None:
        """Link the cheque to the leg this receipt pays (US-542).

        Sets the cheque ↔ leg link the rest of the story consumes:
        ``installment_leg_id`` (the leg), ``applied_amount_irr``
        (the slice applied to it — the cheque amount may exceed
        the leg's balance), and ``installment_leg_status`` =
        ``pending_clearance`` (the leg awaits clearing; the cheque
        service flips it to paid/unpaid on clear/bounce). No
        journal is created here — the cheque's سند ۱ already
        exists (the cheque is held/in_clearing).
        """
        assert self._cheques is not None  # set whenever a cheque receipt is recorded
        cheque.installment_leg_id = allocation.leg_id
        cheque.applied_amount_irr = allocation.amount.amount
        cheque.installment_leg_status = InstallmentLegStatus.PENDING_CLEARANCE
        cheque.touch()
        self._cheques.save(cheque)

    # -- BNPL (US-543) ----------------------------------------------------

    def _record_gateway_payment(
        self,
        *,
        installation_id: str,
        validated: ValidatedReceipt,
    ) -> str:
        """Create the gateway receivable for a BNPL receipt (US-521 engine, reused).

        One ``gateway_payment`` per receipt, gross = the receipt
        amount (the gateway's transaction covers the whole receipt,
        advance included — the advance is held on the plan, and the
        gateway settles the full amount). The confirmation time is
        the receipt's received date (midnight UTC); the Jalali
        month of that date anchors the tranche schedule. The
        invoice ref is passed as the engine invoice id only when it
        parses as one (a numeric engine id); the journal move id
        always travels as ``tryton_journal_id``. The fee % and VAT
        rate come from the gateway's active contract version —
        never from a default, never hard-coded.
        """
        if self._settlement is None:
            raise InstallmentBnplUnavailableError()
        assert validated.gateway_channel_key is not None
        assert validated.gateway_reference is not None
        confirmed_at = datetime.datetime.combine(
            validated.received_date.gregorian_date,
            datetime.time.min,
            tzinfo=datetime.UTC,
        )
        result = self._settlement.record_payment(
            CreateGatewayPayment(
                installation_id=installation_id,
                channel_key=validated.gateway_channel_key,
                gateway_transaction_id=validated.gateway_reference,
                gross_irr=validated.amount.amount,
                gateway_confirmed_at=confirmed_at,
                source=PaymentSource.MANUAL_ENTRY,
                tryton_invoice_id=(
                    int(validated.invoice_id) if validated.invoice_id.isdigit() else None
                ),
                tryton_journal_id=validated.journal_move_id,
            )
        )
        return result.payment.id

    def reject_bnpl_receipt(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        plan_id: str,
        receipt_id: str,
        reason: str,
        actor: str | None = None,
    ) -> InstallmentReceipt:
        """Record a gateway rejection/cancellation for a BNPL receipt.

        The gateway payment is cancelled through the settlement
        engine (open tranches → cancelled; tranches already settled
        stay settled and their net is recorded as
        ``refunds_deducted_irr`` — the gateway deducts it from
        later settlements), the receipt is marked ``rejected`` so
        its allocations stop counting and its legs return to
        ``expected``, and ONE review-queue item is filed
        (``settlement_variance`` / ``bnpl_gateway_rejection``).

        The settlement cancel is deliberately called WITHOUT a
        tenant id, so the engine does not file its own
        ``cancelled_after_settlement`` item on top: the BNPL item
        already carries the settled-amount context, and one item
        per rejection keeps the queue readable. A payment the
        gateway already cancelled through the settlement API is
        not an error — the receipt still needs marking dead, and
        the item still needs filing.
        """
        plan = self._repository.get_plan(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None:
            raise InstallmentPlanNotFoundError(plan_id)
        receipt = self._repository.get_receipt(
            receipt_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if receipt is None or receipt.plan_id != plan.id:
            raise InstallmentReceiptNotFoundError(receipt_id)
        if receipt.payment_method is not PaymentMethod.BNPL:
            raise InstallmentNotBnplReceiptError(
                receipt_id=receipt.id, method=receipt.payment_method.value
            )
        if receipt.bnpl_status is not BnplReceiptStatus.ACTIVE:
            raise InstallmentBnplAlreadyRejectedError(
                receipt_id=receipt.id,
                status=receipt.bnpl_status.value if receipt.bnpl_status else "unknown",
            )
        if self._settlement is None:
            raise InstallmentBnplUnavailableError()
        refunds_deducted_irr = 0
        already_cancelled = False
        if receipt.gateway_payment_id is not None:
            try:
                result = self._settlement.cancel_payment(
                    installation_id,
                    CancelGatewayPayment(
                        payment_id=receipt.gateway_payment_id,
                        reason=reason,
                        actor=actor,
                    ),
                )
                refunds_deducted_irr = result.refunds_deducted_irr
            except SettlementPaymentInvalidError as exc:
                status = exc.details.get("status")
                if status in (
                    GatewayPaymentStatus.CANCELLED.value,
                    GatewayPaymentStatus.PARTIALLY_CANCELLED.value,
                ):
                    # Already cancelled through the settlement API —
                    # the receipt still needs marking dead.
                    already_cancelled = True
                    if status == GatewayPaymentStatus.PARTIALLY_CANCELLED.value:
                        refunds_deducted_irr = -1  # settled part exists; exact figure unknown here
                else:
                    raise
        updated = self._repository.update_receipt_bnpl_status(
            receipt.id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            bnpl_status=BnplReceiptStatus.REJECTED,
        )
        assert updated is not None  # the receipt existed a moment ago
        if self._review is not None:
            self._file_bnpl_rejection_item(
                tenant_id=tenant_id,
                receipt=receipt,
                reason=reason,
                refunds_deducted_irr=refunds_deducted_irr,
                already_cancelled=already_cancelled,
                actor=actor,
            )
        return updated

    def _file_bnpl_rejection_item(
        self,
        *,
        tenant_id: str,
        receipt: InstallmentReceipt,
        reason: str,
        refunds_deducted_irr: int,
        already_cancelled: bool,
        actor: str | None,
    ) -> None:
        """File the single review item for a BNPL rejection (US-543).

        ``refunds_deducted_irr`` is the settled amount the gateway
        will net from later settlements (``-1`` when the payment
        was already partially cancelled through the settlement API
        and the exact figure is not visible here — the item says
        so, it never invents a number). Severity is CRITICAL when
        money was already settled (it must be netted), WARNING
        otherwise.
        """
        assert self._review is not None
        gateway = receipt.gateway_channel_key or "unknown gateway"
        detail = (
            f"BNPL payment rejected by {gateway} "
            f"(gateway ref {receipt.gateway_reference or '—'}): {reason}. "
            "The receipt's legs return to expected — the customer owes "
            f"{receipt.amount.amount} IRR again."
        )
        if refunds_deducted_irr > 0:
            detail += (
                f" {refunds_deducted_irr} IRR was already settled and will be "
                "deducted by the gateway from later settlements."
            )
        elif refunds_deducted_irr < 0:
            detail += (
                " Part of the payment was already settled before the "
                "cancellation — check the settlement exceptions for the "
                "amount the gateway will deduct."
            )
        if already_cancelled:
            detail += " (The gateway payment was already cancelled in the settlement engine.)"
        self._review.file_item(
            tenant_id=tenant_id,
            item_type=ReviewItemType.SETTLEMENT_VARIANCE,
            severity=(
                ReviewSeverity.CRITICAL
                if refunds_deducted_irr != 0
                else ReviewSeverity.WARNING
            ),
            reason=detail,
            suggested_action=(
                "Contact the customer to re-arrange payment (cash, bank "
                "transfer, or a new BNPL attempt) and confirm the "
                "gateway's deduction from the next settlement."
            ),
            subtype="bnpl_gateway_rejection",
            channel=gateway,
            source_event_ref=f"installment-receipt:{receipt.id}",
            amount_irr=receipt.amount.amount,
            actor=actor or "installments",
        )

    # -- contracts --------------------------------------------------------

    def create_contract(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        company_engine_id: int,
        payload: dict[str, object],
    ) -> InstallmentSaleContract:
        """Create one version of a sale contract.

        The caller supplies the version + effective date + the
        superseded contract id (when this version replaces one);
        US-540 stores the versioning fields and does NOT
        auto-supersede. The effective-version selection is US-541's
        (``select_effective_contract``): it resolves a contract
        CODE at plan-creation time (``create_plan`` with
        ``contract_code``). When ``supersedes_contract_id`` names
        a contract, it must exist in this installation (404
        otherwise).
        """
        validated: ValidatedContract = validate_contract_payload(payload)
        if validated.supersedes_contract_id is not None:
            superseded = self._repository.get_contract(
                validated.supersedes_contract_id,
                tenant_id=tenant_id,
                installation_id=installation_id,
            )
            if superseded is None:
                raise InstallmentContractNotFoundError(validated.supersedes_contract_id)
        contract = InstallmentSaleContract(
            id=new_contract_id(),
            tenant_id=tenant_id,
            installation_id=installation_id,
            company_engine_id=company_engine_id,
            code=validated.code,
            name=validated.name,
            version=validated.version,
            effective_from=validated.effective_from,
            late_fee_basis=validated.late_fee_basis,
            late_fee_rate=validated.late_fee_rate,
            late_fee_grace_days=validated.late_fee_grace_days,
            penalty_account_engine_id=validated.penalty_account_engine_id,
            penalty_account_code=validated.penalty_account_code,
            finance_income_rate=validated.finance_income_rate,
            dunning_offsets=validated.dunning_offsets,
            receivable_account_engine_id=validated.receivable_account_engine_id,
            receivable_account_code=validated.receivable_account_code,
            early_settlement_fee_policy=validated.early_settlement_fee_policy,
            early_settlement_income_policy=validated.early_settlement_income_policy,
            early_settlement_discount_rate=validated.early_settlement_discount_rate,
            supersedes_contract_id=validated.supersedes_contract_id,
        )
        return self._repository.create_contract(contract)


class InstallmentCollectionHook:
    """The leg-side listener for cheque clear/bounce outcomes (US-542).

    Implements :class:`InstallmentLegOutcomeHook` (the cheque
    domain's port): the cheque command service fires it after a
    successful clear/bounce for a received cheque carrying an
    ``installment_leg_id``. This is the ONLY leg-side reaction to
    a cheque outcome — the leg's status itself is computed from
    the cheque's state and needs no write.

    - On CLEAR (outcome ``PAID``): record the COLLECTION — set
      ``collected_at`` on the receipt backing the cheque, to the
      clear date. This is the hook US-541's income engine
      consumes: finance income is recognised on COLLECTION, never
      on receipt / pending_clearance. Idempotent: a receipt that
      already has ``collected_at`` set is left untouched (a
      re-fired hook — e.g. after a retry — never moves the
      collection date).
    - On BOUNCE (outcome ``UNPAID``): write NOTHING. The leg
      returns to unpaid / overdue by computation, the cheque row
      is the bounce record (reason / date / certificate), and
      US-541's late-fee engine operates on the overdue leg from
      its original due date (§9.1 #13). This story computes NO
      late fee and recognises NO income — US-541 owns both.

    Total by design: a missing receipt (the cheque was linked by
    intake without a receipt — no collection to record) or a
    dangling leg link (the leg cannot be resolved via ``get_leg``)
    is a warning in the log, never an exception. The cheque
    transition has already committed; this hook must not fail it.
    The caller (the cheque service) additionally catches any
    unexpected failure and files a review item.
    """

    def __init__(self, repository: InstallmentRepositoryPort) -> None:
        self._repository = repository

    def on_leg_outcome(
        self,
        *,
        cheque: Cheque,
        outcome: InstallmentLegStatus,
        event_date: datetime.date,
    ) -> None:
        """Record a clear as a collection; observe a bounce without writing."""
        leg_id = cheque.installment_leg_id
        if leg_id is None:
            return
        leg = self._repository.get_leg(
            leg_id, tenant_id=cheque.tenant_id, installation_id=cheque.installation_id
        )
        if leg is None:
            logger.warning(
                "Cheque %s (%s) names installment leg %s, which does not "
                "resolve in installation %s — the leg outcome was not recorded.",
                cheque.id,
                cheque.sayad_id,
                leg_id,
                cheque.installation_id,
            )
            return
        if outcome is InstallmentLegStatus.UNPAID:
            logger.info(
                "Cheque %s (%s) bounced; installment leg %s returns to "
                "unpaid (overdue when past its due date) by computation.",
                cheque.id,
                cheque.sayad_id,
                leg_id,
            )
            return
        if outcome is not InstallmentLegStatus.PAID:
            return
        receipt = self._repository.get_receipt_by_cheque(
            cheque.id,
            tenant_id=cheque.tenant_id,
            installation_id=cheque.installation_id,
        )
        if receipt is None:
            logger.warning(
                "Cheque %s (%s) cleared for installment leg %s, but no "
                "receipt backs it — nothing to mark collected.",
                cheque.id,
                cheque.sayad_id,
                leg_id,
            )
            return
        if receipt.collected_at is not None:
            return
        receipt.collected_at = FiscalDate.from_gregorian(event_date)
        self._repository.save_receipt(receipt)
        logger.info(
            "Installment leg %s collected via cheque %s (%s) on %s "
            "(receipt %s).",
            leg_id,
            cheque.id,
            cheque.sayad_id,
            event_date.isoformat(),
            receipt.id,
        )
