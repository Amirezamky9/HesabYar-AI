"""Installment finance use cases (US-541, Phase 5-5 ۵ج).

Early settlement (payoff quote + atomic close, partial payment +
re-amortisation), the stepped-dunning sweep (review-queue filing +
late-fee assessment), and the late-fee journal (draft by default,
via the US-202 journal-draft flow).

The money MATH lives in ``hesabyar.domain.installments.finance``
(pure functions); this service loads the plan's state, calls the
math, and persists the results. The service never commits — the
caller (router / worker) owns the transaction — except for the
engine journal, which is created BEFORE the DB writes: if the DB
write then fails, the draft journal survives orphaned in the
engine (findable by its description; a retry journals the same
increment again only after the assessment row exists, so the
orphan is visible, never silently double-posted).

Optional collaborators:

- ``review`` (the review-queue command service) + ``queue`` (the
  review-queue port): filing dunning items and de-duplicating them.
  Without them the sweep still computes stages and fees, but files
  nothing — the router always wires them.
- ``journals`` (the journal-draft service, narrowed to the
  :class:`LateFeeJournalPort` protocol): creating the late-fee
  draft journals. Without it the fees are assessed and recorded
  (journal ``none``) and a later run with the engine journals the
  unjournaled portion — the assessment row is the memory, not the
  journal.
- ``cheques`` (the cheque repository): the leg views split cheque
  receipts by their cheque's state (US-542) — a pending cheque's
  allocations are NOT received, so no finance income is recognised
  on them and no late fee accrues while the cheque is in flight.
  Without it, cheque receipts' allocations are treated as pending
  (never received) — income recognition stays fail-closed.

BNPL plans (US-543) never reach the finance math: they accrue no
finance income, never enter dunning, and never settle early with
the seller — the gateway owns collection. ``settle_early`` refuses
a BNPL plan outright (typed 422); the dunning sweep skips BNPL
plans entirely.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from hesabyar.application.commands.installments import resolve_cheque_states
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.domain.accounting.commands import JournalDraftLine
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CompanyRef,
    JournalDraftRef,
    JournalRef,
    PostedJournalRef,
)
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.bnpl import plan_enters_dunning
from hesabyar.domain.installments.errors import (
    InstallmentAlreadySettledError,
    InstallmentBnplEarlySettlementError,
    InstallmentInvoiceMismatchError,
    InstallmentLateFeeAccountMissingError,
    InstallmentPlanNotFoundError,
    InstallmentSettlementAmountError,
)
from hesabyar.domain.installments.finance import (
    PayoffQuote,
    assess_leg,
    compute_late_fee,
    compute_payoff_quote,
    days_overdue,
    held_advance,
    reamortise_balances,
)
from hesabyar.domain.installments.models import (
    AmendmentKind,
    InstallmentLeg,
    InstallmentPlan,
    InstallmentReceipt,
    InstallmentSaleContract,
    LateFeeAssessment,
    LateFeeJournalState,
    LegAllocation,
    LegPrincipalSnapshot,
    LegStatus,
    PlanKind,
    ScheduleAmendment,
    SettlementType,
    new_amendment_id,
    new_assessment_id,
    new_receipt_id,
)
from hesabyar.domain.installments.ports import InstallmentRepositoryPort
from hesabyar.domain.installments.state_machine import (
    LegView,
    allocate_receipt,
    leg_status_for,
    split_allocations,
)
from hesabyar.domain.installments.validation import (
    ValidatedSettlement,
    to_jalali_string,
    validate_settlement_payload,
)
from hesabyar.domain.review_queue.models import (
    ReviewItemStatus,
    ReviewItemType,
    ReviewSeverity,
)
from hesabyar.domain.review_queue.ports import ReviewItemFilters, ReviewQueuePort

#: Page size for the dunning sweep's plan scan.
_DUNNING_PAGE_SIZE = 100


class LateFeeJournalPort(Protocol):
    """The narrow journal surface the late-fee flow needs.

    :class:`JournalDraftService` satisfies this structurally; test
    fakes implement just these two methods instead of the whole
    engine port.
    """

    def create_draft(
        self,
        company: CompanyRef,
        date: FiscalDate,
        lines: Sequence[JournalDraftLine],
        description: str = "",
        journal: JournalRef | None = None,
    ) -> JournalDraftRef:
        """Create a draft journal (an unposted move) in the engine."""
        ...

    def post_draft(self, draft: JournalDraftRef) -> PostedJournalRef:
        """Post a draft journal, atomically, in the engine."""
        ...


@dataclass(frozen=True, slots=True)
class EarlySettlementResult:
    """The outcome of an early settlement: receipt + quote + side effects."""

    receipt: InstallmentReceipt
    quote: PayoffQuote
    settlement_type: SettlementType
    journal_move_ids: tuple[int, ...]
    amendment: ScheduleAmendment | None


@dataclass(frozen=True, slots=True)
class DunningRunResult:
    """The outcome of one dunning sweep (what it filed / assessed / journaled)."""

    as_of: datetime.date
    plans_scanned: int
    overdue_legs: int
    items_filed: tuple[str, ...]
    fees_assessed_irr: int
    journals_created: tuple[int, ...]
    legs_skipped_no_account: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FeeOutcome:
    """What assessing one leg's fee did (internal)."""

    assessment: LateFeeAssessment | None
    increment_irr: int
    journal_move_id: int | None
    skipped_no_account: bool


class InstallmentFinanceService:
    """Early settlement + dunning + late-fee journals for installment plans."""

    def __init__(
        self,
        repository: InstallmentRepositoryPort,
        *,
        review: ReviewQueueCommandService | None = None,
        queue: ReviewQueuePort | None = None,
        journals: LateFeeJournalPort | None = None,
        cheques: ChequeRepositoryPort | None = None,
    ) -> None:
        self._repository = repository
        self._review = review
        self._queue = queue
        self._journals = journals
        self._cheques = cheques

    # -- early settlement ---------------------------------------------------

    def settle_early(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        plan_id: str,
        payload: dict[str, object],
    ) -> EarlySettlementResult:
        """Settle a plan early — full close, or a partial payment + re-amortisation.

        Guard order (fail-closed, nothing stored on failure):
        1. the plan must exist in this installation (404);
        2. the plan must not be a BNPL plan (typed 422 — the
           customer owes the gateway, not the seller);
        3. THE LINKAGE GUARD — a settlement is a receipt, so
           ``invoice_id`` AND ``journal_move_id`` are both mandatory
           (typed 422);
        4. the settlement's invoice must be the plan's invoice
           (typed 422);
        5. the plan must have open legs (409 when already settled);
        6. a FULL settlement's amount must cover the payoff quote
           (typed 422 otherwise — less than the payoff is a partial
           payment, and must say so).

        The quote is computed as of the settlement date (not the
        wall clock): a settlement dated in the past settles the
        fees as of that date. The receipt, the assessment rows and
        the amendment (partial only) are written in one transaction
        (the caller commits); the late-fee draft journals are
        created in the engine first (see the module docstring).
        """
        plan = self._repository.get_plan(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None:
            raise InstallmentPlanNotFoundError(plan_id)
        if plan.kind is PlanKind.BNPL:
            raise InstallmentBnplEarlySettlementError(plan_id=plan.id)
        validated: ValidatedSettlement = validate_settlement_payload(payload)
        if validated.invoice_id != plan.invoice_id:
            raise InstallmentInvoiceMismatchError(
                receipt_invoice_id=validated.invoice_id,
                plan_invoice_id=plan.invoice_id,
            )
        day = validated.settlement_date.gregorian_date
        legs = self._repository.list_legs(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        receipts = self._repository.list_receipts(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        contract = self._governing_contract(plan, tenant_id, installation_id)
        views = self._leg_views(
            legs, receipts, tenant_id=tenant_id, installation_id=installation_id
        )
        held = held_advance(
            MoneyIRR(sum(r.advance.amount for r in receipts)), plan.applied_advance
        )
        income_paid = MoneyIRR(sum(r.finance_income.amount for r in receipts))
        quote = compute_payoff_quote(
            plan.id,
            views,
            contract=contract,
            advance_held=held,
            income_paid=income_paid,
            today=day,
        )
        if quote.open_leg_count == 0:
            raise InstallmentAlreadySettledError(plan.id)
        if validated.settlement_type is SettlementType.FULL:
            return self._settle_full(
                plan, views, contract, validated, quote, day, tenant_id, installation_id
            )
        return self._settle_partial(
            plan, views, contract, validated, quote, day, tenant_id, installation_id
        )

    def _settle_full(
        self,
        plan: InstallmentPlan,
        views: list[LegView],
        contract: InstallmentSaleContract | None,
        validated: ValidatedSettlement,
        quote: PayoffQuote,
        day: datetime.date,
        tenant_id: str,
        installation_id: str,
    ) -> EarlySettlementResult:
        """Close every open leg atomically for the payoff amount."""
        if validated.amount.amount < quote.payoff_total.amount:
            raise InstallmentSettlementAmountError(
                amount_irr=validated.amount.amount, payoff_irr=quote.payoff_total.amount
            )
        # Journal the unjournaled part of the kept late fees (one
        # draft per overdue leg; a fee already journaled by a
        # dunning run is never journaled twice).
        journal_move_ids: list[int] = []
        if contract is not None and quote.late_fees_due.amount > 0:
            for view in views:
                assessment = assess_leg(view, contract=contract, today=day)
                if assessment.status is not LegStatus.OVERDUE:
                    continue
                if assessment.late_fee_accrued.amount <= 0:
                    continue
                outcome = self._assess_leg_fee(
                    plan, view, contract, day, tenant_id, installation_id, fail_on_missing_account=True
                )
                if outcome.journal_move_id is not None:
                    journal_move_ids.append(outcome.journal_move_id)
        held = quote.advance_held
        gross = (
            quote.remaining_principal.amount + quote.income_due.amount + quote.late_fees_due.amount
        )
        consumed_advance = min(held.amount, gross)
        advance = validated.amount.amount + consumed_advance - gross
        allocations = tuple(
            LegAllocation(leg_id=view.leg_id, amount=view.balance)
            for view in views
            if view.balance.amount > 0
        )
        receipt = InstallmentReceipt(
            id=new_receipt_id(),
            plan_id=plan.id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            invoice_id=validated.invoice_id,
            journal_move_id=validated.journal_move_id,
            amount=validated.amount,
            received_date=validated.settlement_date,
            payment_method=validated.payment_method,
            allocations=allocations,
            advance=MoneyIRR(advance),
            finance_income=quote.income_due,
            late_fee=quote.late_fees_due,
            applied_advance=MoneyIRR(consumed_advance),
            is_early_settlement=True,
            reference=validated.reference,
            notes=validated.notes,
        )
        saved = self._repository.create_receipt(receipt)
        if consumed_advance > 0:
            self._repository.update_applied_advance(
                plan.id,
                plan.applied_advance.add(MoneyIRR(consumed_advance)),
                tenant_id=tenant_id,
                installation_id=installation_id,
            )
        return EarlySettlementResult(
            receipt=saved,
            quote=quote,
            settlement_type=SettlementType.FULL,
            journal_move_ids=tuple(journal_move_ids),
            amendment=None,
        )

    def _settle_partial(
        self,
        plan: InstallmentPlan,
        views: list[LegView],
        contract: InstallmentSaleContract | None,
        validated: ValidatedSettlement,
        quote: PayoffQuote,
        day: datetime.date,
        tenant_id: str,
        installation_id: str,
    ) -> EarlySettlementResult:
        """Pay part of the schedule; the advance re-amortises the rest.

        The payment allocates by the normal rule (oldest overdue
        first, then earliest due — never a not-yet-due leg). The
        advance it creates, PLUS any advance already held, reduces
        the remaining principal, and the still-open legs are
        re-split equally (due dates unchanged). A partial payment
        pays PRINCIPAL only — late fees and finance income are
        settled by the full settlement, never by a partial one — so
        a partial payment that would close the last open leg while
        income/fees are still due is refused (the caller must use a
        full settlement: there would be no leg left to attach the
        income/fees to).
        """
        allocation = allocate_receipt(validated.amount, views, day)
        new_advance = allocation.advance.amount
        # Post-allocation views: the receipt's slices already count
        # as received when the remaining schedule is measured.
        allocated_by_leg: dict[str, int] = {}
        for slice_ in allocation.allocations:
            allocated_by_leg[slice_.leg_id] = (
                allocated_by_leg.get(slice_.leg_id, 0) + slice_.amount.amount
            )
        post_views = [
            LegView(
                leg_id=view.leg_id,
                sequence=view.sequence,
                due_date=view.due_date,
                principal=view.principal,
                received=MoneyIRR(view.received.amount + allocated_by_leg.get(view.leg_id, 0)),
            )
            for view in views
        ]
        open_after = [view for view in post_views if view.balance.amount > 0]
        remaining_after = sum(view.balance.amount for view in open_after)
        if not open_after and (
            quote.income_due.amount > 0 or quote.late_fees_due.amount > 0
        ):
            raise InstallmentSettlementAmountError(
                amount_irr=validated.amount.amount, payoff_irr=quote.payoff_total.amount
            )
        held = quote.advance_held.amount
        available = new_advance + held
        to_apply = min(available, remaining_after)
        consumed_held = min(held, to_apply)
        consumed_new = to_apply - consumed_held
        receipt = InstallmentReceipt(
            id=new_receipt_id(),
            plan_id=plan.id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            invoice_id=validated.invoice_id,
            journal_move_id=validated.journal_move_id,
            amount=validated.amount,
            received_date=validated.settlement_date,
            payment_method=validated.payment_method,
            allocations=allocation.allocations,
            advance=MoneyIRR(new_advance - consumed_new),
            applied_advance=MoneyIRR(consumed_held),
            is_early_settlement=True,
            reference=validated.reference,
            notes=validated.notes,
        )
        saved = self._repository.create_receipt(receipt)
        amendment: ScheduleAmendment | None = None
        if to_apply > 0 and open_after:
            new_principals = reamortise_balances(
                open_after, MoneyIRR(remaining_after - to_apply)
            )
            legs_before = tuple(
                LegPrincipalSnapshot(leg_id=view.leg_id, sequence=view.sequence, principal=view.principal)
                for view in open_after
            )
            legs_after = tuple(
                LegPrincipalSnapshot(
                    leg_id=view.leg_id, sequence=view.sequence, principal=new_principals[view.leg_id]
                )
                for view in open_after
            )
            self._repository.update_leg_principals(
                plan.id, new_principals, tenant_id=tenant_id, installation_id=installation_id
            )
            amendment = ScheduleAmendment(
                id=new_amendment_id(),
                plan_id=plan.id,
                tenant_id=tenant_id,
                installation_id=installation_id,
                kind=AmendmentKind.RE_AMORTISATION,
                applied_advance=MoneyIRR(to_apply),
                legs_before=legs_before,
                legs_after=legs_after,
                receipt_id=saved.id,
            )
            self._repository.create_schedule_amendment(amendment)
        if consumed_held > 0:
            self._repository.update_applied_advance(
                plan.id,
                plan.applied_advance.add(MoneyIRR(consumed_held)),
                tenant_id=tenant_id,
                installation_id=installation_id,
            )
        return EarlySettlementResult(
            receipt=saved,
            quote=quote,
            settlement_type=SettlementType.PARTIAL,
            journal_move_ids=(),
            amendment=amendment,
        )

    # -- dunning sweep ---------------------------------------------------------

    def run_dunning(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        today: datetime.date | None = None,
        actor: str = "installments",
    ) -> DunningRunResult:
        """One dunning sweep over the installation's plans.

        BNPL plans are skipped entirely — they never enter dunning
        (US-543). For every overdue leg of an internal plan: file a
        review-queue item when the leg reaches a NEW dunning stage
        (one item per leg per stage —
        the ``source_event_ref`` ``installment-leg:{leg}:dunning-stage:{n}``
        makes re-runs idempotent), and assess its late fee (recording
        the cumulative assessment and journaling the increment when
        an engine is wired and the contract's accounts exist). A leg
        whose contract lacks the posting accounts is skipped for
        journaling (listed in the result) but still assessed — the
        fee is real, only its journal is waiting on configuration.
        Legs without a contract, and legs before their first
        reminder offset (stage 0), get no review item.
        """
        day = today or datetime.date.today()
        existing_refs: set[str] = set()
        if self._queue is not None:
            items = self._queue.list_items(
                tenant_id,
                ReviewItemFilters(item_type=ReviewItemType.INSTALLMENT_OVERDUE),
            )
            existing_refs = {
                item.source_event_ref
                for item in items
                if item.status
                in (ReviewItemStatus.OPEN, ReviewItemStatus.IN_REVIEW)
                and item.source_event_ref is not None
            }
        plans_scanned = 0
        overdue_legs = 0
        items_filed: list[str] = []
        fees_assessed = 0
        journals_created: list[int] = []
        skipped_no_account: list[str] = []
        offset = 0
        while True:
            plans = self._repository.list_plans(
                tenant_id=tenant_id,
                installation_id=installation_id,
                limit=_DUNNING_PAGE_SIZE,
                offset=offset,
            )
            if not plans:
                break
            offset += len(plans)
            for plan in plans:
                # BNPL plans never enter dunning: their legs are
                # never overdue — the gateway owns collection
                # (US-543). They are not counted as scanned.
                if not plan_enters_dunning(plan):
                    continue
                plans_scanned += 1
                legs = self._repository.list_legs(
                    plan.id, tenant_id=tenant_id, installation_id=installation_id
                )
                receipts = self._repository.list_receipts(
                    plan.id, tenant_id=tenant_id, installation_id=installation_id
                )
                contract = self._governing_contract(plan, tenant_id, installation_id)
                views = self._leg_views(
                    legs, receipts, tenant_id=tenant_id, installation_id=installation_id
                )
                for view in views:
                    assessment = assess_leg(view, contract=contract, today=day)
                    if assessment.status is not LegStatus.OVERDUE:
                        continue
                    overdue_legs += 1
                    if (
                        contract is not None
                        and assessment.dunning_stage >= 1
                        and self._review is not None
                    ):
                        ref = (
                            f"installment-leg:{view.leg_id}:"
                            f"dunning-stage:{assessment.dunning_stage}"
                        )
                        if ref not in existing_refs:
                            severity = (
                                ReviewSeverity.CRITICAL
                                if assessment.dunning_stage >= len(contract.dunning_offsets)
                                else ReviewSeverity.WARNING
                            )
                            item = self._review.file_item(
                                tenant_id=tenant_id,
                                item_type=ReviewItemType.INSTALLMENT_OVERDUE,
                                severity=severity,
                                reason=(
                                    f"Installment leg {view.sequence} of plan {plan.id} "
                                    f"(invoice {plan.invoice_id}) is "
                                    f"{assessment.days_past_due} days overdue "
                                    f"(dunning stage {assessment.dunning_stage} of "
                                    f"{len(contract.dunning_offsets)}). Balance: "
                                    f"{assessment.balance.amount:,} Rial; late fee accrued: "
                                    f"{assessment.late_fee_accrued.amount:,} Rial."
                                ),
                                suggested_action=(
                                    "Send the stage reminder to the customer, or "
                                    "extend the due date / activate the collateral "
                                    "cheque from the review queue."
                                ),
                                company_id=str(plan.company_engine_id),
                                subtype=f"dunning-stage-{assessment.dunning_stage}",
                                source_event_ref=ref,
                                amount_irr=assessment.balance.amount
                                + assessment.late_fee_accrued.amount,
                                actor=actor,
                            )
                            items_filed.append(item.id)
                            existing_refs.add(ref)
                    if contract is not None:
                        outcome = self._assess_leg_fee(
                            plan, view, contract, day, tenant_id, installation_id
                        )
                        fees_assessed += outcome.increment_irr
                        if outcome.journal_move_id is not None:
                            journals_created.append(outcome.journal_move_id)
                        if outcome.skipped_no_account:
                            skipped_no_account.append(view.leg_id)
        return DunningRunResult(
            as_of=day,
            plans_scanned=plans_scanned,
            overdue_legs=overdue_legs,
            items_filed=tuple(items_filed),
            fees_assessed_irr=fees_assessed,
            journals_created=tuple(journals_created),
            legs_skipped_no_account=tuple(skipped_no_account),
        )

    # -- late-fee assessment + journal ------------------------------------------

    def _assess_leg_fee(
        self,
        plan: InstallmentPlan,
        view: LegView,
        contract: InstallmentSaleContract,
        day: datetime.date,
        tenant_id: str,
        installation_id: str,
        *,
        fail_on_missing_account: bool = False,
    ) -> _FeeOutcome:
        """Assess one overdue leg's late fee; journal the new increment.

        The assessment is a cumulative snapshot (total fee accrued
        through ``day``); a run whose computed fee equals the last
        assessment's fee records nothing. The journal covers only
        the increment since the last JOURNALED assessment — an
        assessment recorded without a journal (no engine, or no
        accounts) never blocks a later journal of the same money.
        With ``fail_on_missing_account`` (settlement), a contract
        without the posting accounts refuses the whole operation
        instead of recording an unjournaled fee.
        """
        if leg_status_for(view, day) is not LegStatus.OVERDUE:
            return _FeeOutcome(None, 0, None, False)
        past_due = days_overdue(view.due_date, day)
        accrued = compute_late_fee(
            view.balance,
            basis=contract.late_fee_basis,
            rate=contract.late_fee_rate,
            grace_days=contract.late_fee_grace_days,
            days_past_due=past_due,
        )
        if accrued.amount <= 0:
            return _FeeOutcome(None, 0, None, False)
        assessments = self._repository.list_late_fee_assessments(
            view.leg_id, tenant_id=tenant_id, installation_id=installation_id
        )
        last_total = assessments[0].fee.amount if assessments else 0
        if accrued.amount <= last_total:
            return _FeeOutcome(None, 0, None, False)
        journaled = max(
            (a.fee.amount for a in assessments if a.journal_move_id is not None),
            default=0,
        )
        increment = accrued.amount - journaled
        journal_move_id: int | None = None
        journal_state = LateFeeJournalState.NONE
        skipped_no_account = False
        if increment > 0 and self._journals is not None:
            missing = self._missing_fee_accounts(contract)
            if missing:
                if fail_on_missing_account:
                    raise InstallmentLateFeeAccountMissingError(
                        contract_id=contract.id, missing=tuple(sorted(missing))
                    )
                skipped_no_account = True
            else:
                journal_move_id, journal_state = self._journal_late_fee(
                    plan, view, contract, increment, day
                )
        assessment = LateFeeAssessment(
            id=new_assessment_id(),
            leg_id=view.leg_id,
            plan_id=plan.id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            assessed_through=FiscalDate.from_gregorian(day),
            days_overdue=past_due,
            fee=accrued,
            journal_move_id=journal_move_id,
            journal_state=journal_state,
        )
        saved = self._repository.create_late_fee_assessment(assessment)
        return _FeeOutcome(saved, accrued.amount - last_total, journal_move_id, skipped_no_account)

    def _journal_late_fee(
        self,
        plan: InstallmentPlan,
        view: LegView,
        contract: InstallmentSaleContract,
        increment: int,
        day: datetime.date,
    ) -> tuple[int, LateFeeJournalState]:
        """Create the late-fee draft journal (Dr receivable / Cr penalty).

        Draft by default; posted only when the plan opted into
        auto-post (ADR 0003 decision 1). The caller guarantees both
        accounts are configured.
        """
        assert self._journals is not None
        assert contract.receivable_account_engine_id is not None
        assert contract.penalty_account_engine_id is not None
        description = (
            f"Late fee — installment plan {plan.id}, leg {view.sequence} "
            f"(invoice {plan.invoice_id}), through {to_jalali_string(day)}"
        )
        draft = self._journals.create_draft(
            CompanyRef(plan.company_engine_id),
            FiscalDate.from_gregorian(day),
            (
                JournalDraftLine(
                    account=AccountRef(contract.receivable_account_engine_id),
                    debit=MoneyIRR(increment),
                    description=description,
                ),
                JournalDraftLine(
                    account=AccountRef(contract.penalty_account_engine_id),
                    credit=MoneyIRR(increment),
                    description=description,
                ),
            ),
            description=description,
        )
        if plan.auto_post:
            posted = self._journals.post_draft(draft)
            return posted.engine_id, LateFeeJournalState.POSTED
        return draft.engine_id, LateFeeJournalState.DRAFT

    # -- shared helpers ----------------------------------------------------------

    @staticmethod
    def _missing_fee_accounts(contract: InstallmentSaleContract) -> list[str]:
        missing: list[str] = []
        if contract.penalty_account_engine_id is None:
            missing.append("penalty_account_engine_id")
        if contract.receivable_account_engine_id is None:
            missing.append("receivable_account_engine_id")
        return missing

    def _governing_contract(
        self,
        plan: InstallmentPlan,
        tenant_id: str,
        installation_id: str,
    ) -> InstallmentSaleContract | None:
        """The contract governing a plan: its linked version, or None.

        A plan's terms are fixed at creation by its linked version —
        a newer version never re-prices an existing plan. A plan
        without a contract has no finance terms at all (no income,
        no fees, no dunning).
        """
        if plan.contract_id is None:
            return None
        return self._repository.get_contract(
            plan.contract_id, tenant_id=tenant_id, installation_id=installation_id
        )

    def _leg_views(
        self,
        legs: list[InstallmentLeg],
        receipts: list[InstallmentReceipt],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> list[LegView]:
        """Leg views over the cheque-aware split (US-542).

        A pending cheque's allocations are NOT received: no finance
        income is recognised on them (income is recognised on
        COLLECTION, never on receipt) and no late fee accrues while
        the cheque is in flight. A bounced cheque's allocations
        count as nothing — the leg is overdue again, and the fee
        accrues from the ORIGINAL due date.
        """
        cheque_states = resolve_cheque_states(
            receipts, self._cheques, tenant_id=tenant_id, installation_id=installation_id
        )
        received, pending = split_allocations(receipts, cheque_states)
        return [
            LegView(
                leg_id=leg.id,
                sequence=leg.sequence,
                due_date=leg.due_date,
                principal=leg.principal,
                received=MoneyIRR(received.get(leg.id, 0)),
                pending_clearance=MoneyIRR(pending.get(leg.id, 0)),
            )
            for leg in legs
        ]
