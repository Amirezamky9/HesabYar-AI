"""Installment query service (US-540 + US-541 + US-542 + US-543, Phase 5-5 ۵ج).

Read-only views over the installment plans: one plan with its legs
(statuses computed from receipts + cheque states + today), its
receipts, and its totals; the filtered plan list; and the sale
contracts. Every lookup is tenant/installation-scoped; a row of
another installation answers as if it did not exist.

Statuses are computed here, at read time, from the same pure
functions the command service allocates with — the query service
and the command service can never disagree about a leg's state.
``today`` is injectable (default: the real today) so tests and
reports can pin the wall-clock date.

US-541 adds the finance reads: the payoff quote (early settlement),
the overdue report (the §7.5 report-1 source: every overdue leg in
the installation with its dunning stage + accrued late fee), and
the effective-contract lookup (which version of a code governs on
a date). The finance reads are internal-plan only: a BNPL plan
never accrues finance income, never enters dunning, and never
settles early with the seller — the gateway owns collection (see
``bnpl.plan_accrues_finance_income`` / ``bnpl.plan_enters_dunning``).

US-542: a leg paid by cheque shows its pending-clearance amount
and the linked cheque (sayad_id, due date, state) in the detail —
the cheque's state is what makes the leg ``pending_clearance``,
``paid`` or unpaid again, so the detail resolves the backing
cheques through the (optional) cheque repository.

US-543 (BNPL): a BNPL plan's leg statuses come from the linked
settlement tranches (``hesabyar.domain.installments.bnpl``), not
from the wall-clock date — a BNPL leg is never ``overdue``. The
detail view also carries the gateway-side totals (what the gateway
still owes vs what has settled), which are gateway money and are
never summed with the customer-side totals. When no settlement
repository is wired (bare constructions in tests), BNPL legs fall
back to the receipt alone (``gateway_confirmed`` when a live BNPL
receipt allocates to the leg, else ``expected``) and the gateway
totals read zero — the fallback is documented, never silent about
what it cannot know.

US-545 adds the plan-amendment read (``list_plan_amendments``):
the audit history of the plan-change flow (partial early
settlements that re-amortised the schedule), oldest first.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from hesabyar.application.commands.installments import resolve_cheque_states
from hesabyar.domain.cheques.models import Cheque, ChequeState
from hesabyar.domain.cheques.ports import ChequeRepositoryPort
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.bnpl import (
    BnplLegStatus,
    BnplTrancheProgress,
    bnpl_leg_status,
    bnpl_plan_status,
    bnpl_tranche_progress,
    plan_enters_dunning,
)
from hesabyar.domain.installments.errors import (
    InstallmentBnplEarlySettlementError,
    InstallmentContractNotFoundError,
    InstallmentPlanNotFoundError,
)
from hesabyar.domain.installments.finance import (
    PayoffQuote,
    assess_leg,
    compute_payoff_quote,
    held_advance,
    select_effective_contract,
)
from hesabyar.domain.installments.models import (
    InstallmentLeg,
    InstallmentPlan,
    InstallmentReceipt,
    InstallmentSaleContract,
    LegStatus,
    PaymentMethod,
    PlanKind,
    PlanStatus,
    ScheduleAmendment,
)
from hesabyar.domain.installments.ports import InstallmentRepositoryPort
from hesabyar.domain.installments.state_machine import (
    PENDING_CHEQUE_STATES,
    LegView,
    leg_status_for,
    plan_status,
    split_allocations,
)
from hesabyar.domain.settlement.models import SettlementTranche
from hesabyar.domain.settlement.ports import SettlementRepositoryPort

__all__ = [
    "InstallmentQueryService",
    "LegDetail",
    "LinkedChequeSummary",
    "OverdueLegRow",
    "OverdueReport",
    "PlanDetail",
    "PlanSummary",
]


@dataclass(frozen=True, slots=True)
class LinkedChequeSummary:
    """The cheque linked to a leg (US-542) — what the plan detail shows.

    ``sayad_id`` + due date + state identify the instrument and its
    clearing progress; the amount is the cheque's full amount (the
    slice applied to the leg is ``applied_amount_irr`` on the
    cheque row, and the leg's ``pending_clearance`` is the pending
    slice).
    """

    cheque_id: str
    sayad_id: str
    due_date: FiscalDate
    state: ChequeState
    amount_irr: int

    @classmethod
    def from_cheque(cls, cheque: Cheque) -> LinkedChequeSummary:
        return cls(
            cheque_id=cheque.id,
            sayad_id=cheque.sayad_id,
            due_date=cheque.due_date,
            state=cheque.state,
            amount_irr=cheque.amount_irr,
        )


@dataclass(frozen=True, slots=True)
class LegDetail:
    """One leg with its computed received total + status.

    ``status`` is a :class:`LegStatus` for internal plans and a
    :class:`BnplLegStatus` for BNPL plans — both render as their
    string value at the REST layer. For a BNPL leg, ``received``
    is the gateway-confirmed gross (what the customer no longer
    owes — the gateway took over the debt); the gateway's own
    settlement progress lives in the plan's gateway totals.
    """

    leg: InstallmentLeg
    received: MoneyIRR
    status: LegStatus | BnplLegStatus
    #: Cheque money in flight for this leg (US-542): allocations of
    #: cheque receipts whose cheque has not cleared yet. Does NOT
    #: reduce the balance — the cheque may bounce. Always zero for
    #: BNPL legs (a BNPL plan never takes cheque receipts).
    pending_clearance: MoneyIRR = MoneyIRR.zero()
    #: The cheque linked to this leg (US-542), when one is linked —
    #: the one backing the leg's pending allocations, else the
    #: newest linked cheque. Always None for BNPL legs.
    linked_cheque: LinkedChequeSummary | None = None

    @property
    def balance(self) -> MoneyIRR:
        return MoneyIRR(max(0, self.leg.principal.amount - self.received.amount))


@dataclass(frozen=True, slots=True)
class PlanDetail:
    """One plan with legs, receipts and derived totals."""

    plan: InstallmentPlan
    legs: tuple[LegDetail, ...]
    receipts: tuple[InstallmentReceipt, ...]
    status: PlanStatus
    #: US-543 (BNPL): what the gateway still owes the seller across
    #: the plan's linked settlement tranches (open tranche nets).
    #: Zero for internal plans — gateway money, never customer money.
    gateway_receivable: MoneyIRR = field(default_factory=MoneyIRR.zero)
    #: US-543 (BNPL): what the gateway has already settled across
    #: the plan's linked tranches (received nets). Zero for internal
    #: plans.
    gateway_settled: MoneyIRR = field(default_factory=MoneyIRR.zero)

    @property
    def total_received(self) -> MoneyIRR:
        """Sum of LIVE receipt amounts (including uncollected cheque receipts).

        A rejected BNPL receipt's amount no longer counts (its legs
        are unpaid again). This is the money RECORDED against the
        plan, not the money that has arrived — ``total_pending_clearance``
        is the part still in flight. (Cash receipts are collected on
        receipt; cheque receipts only on clear.)
        """
        return MoneyIRR(
            sum(r.amount.amount for r in self.receipts if r.counts_toward_legs)
        )

    @property
    def total_advance(self) -> MoneyIRR:
        return MoneyIRR(
            sum(r.advance.amount for r in self.receipts if r.counts_toward_legs)
        )

    @property
    def total_pending_clearance(self) -> MoneyIRR:
        """Cheque money in flight across the plan's legs (US-542)."""
        return MoneyIRR(sum(d.pending_clearance.amount for d in self.legs))

    @property
    def outstanding(self) -> MoneyIRR:
        """Unpaid principal across all legs (advance does not reduce it —
        the advance is held, not applied; see docs/installments.md)."""
        return MoneyIRR(sum(d.balance.amount for d in self.legs))


@dataclass(frozen=True, slots=True)
class PlanSummary:
    """One plan in the list view: the plan + its derived status."""

    plan: InstallmentPlan
    status: PlanStatus
    total_received: MoneyIRR
    outstanding: MoneyIRR
    #: Cheque money in flight across the plan's legs (US-542).
    pending_clearance: MoneyIRR = MoneyIRR.zero()


@dataclass(frozen=True, slots=True)
class OverdueLegRow:
    """One overdue leg in the overdue report (US-541, §7.5 report 1's source).

    The dunning stage + accrued late fee are computed at read time
    from the governing contract — a leg whose contract has no
    dunning schedule reports stage 0, and a leg whose contract has
    no fee rule reports a 0 fee.
    """

    plan_id: str
    invoice_id: str
    customer_ref: str | None
    contract_code: str | None
    leg_id: str
    sequence: int
    due_date: FiscalDate
    days_overdue: int
    principal: MoneyIRR
    received: MoneyIRR
    balance: MoneyIRR
    dunning_stage: int
    next_reminder_date: datetime.date | None
    late_fee_accrued: MoneyIRR


@dataclass(frozen=True, slots=True)
class OverdueReport:
    """The overdue report: one page of overdue legs + installation totals.

    The totals cover ALL overdue legs, not just the page — a report
    total must not change when the caller pages through it.
    """

    rows: tuple[OverdueLegRow, ...]
    total_rows: int
    total_balance: MoneyIRR
    total_late_fees: MoneyIRR


class InstallmentQueryService:
    """Read-only installment views (statuses computed at read time).

    ``cheques`` (US-542) is the cheque read port: without it,
    cheque receipts' allocations are treated as pending (never
    received) and no linked cheque is shown. ``settlement_repository``
    (US-543) is the settlement engine's read port: BNPL leg
    statuses derive from the linked gateway payments' tranches —
    without it, BNPL legs fall back to the receipt alone (see the
    module docstring). Both are optional only so bare constructions
    keep working; production wiring (REST dependencies) always
    supplies both.
    """

    def __init__(
        self,
        repository: InstallmentRepositoryPort,
        cheques: ChequeRepositoryPort | None = None,
        *,
        settlement_repository: SettlementRepositoryPort | None = None,
    ) -> None:
        # ``cheques`` stays positional-or-keyword: US-542's callers
        # (dependencies + tests) pass it positionally.
        self._repository = repository
        self._cheques = cheques
        self._settlement = settlement_repository

    def get_plan_detail(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        installation_id: str,
        today: datetime.date | None = None,
    ) -> PlanDetail:
        """Return one plan with legs + receipts + derived totals (404 when out of scope)."""
        plan = self._repository.get_plan(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None:
            raise InstallmentPlanNotFoundError(plan_id)
        day = today or datetime.date.today()
        legs = self._repository.list_legs(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        receipts = self._repository.list_receipts(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan.kind is PlanKind.BNPL:
            # BNPL legs follow the tranche machine (US-543); cheque
            # fields stay at their defaults (a BNPL plan never takes
            # cheque receipts).
            received_by_leg = self._received_totals(receipts)
            statuses, plan_status_value, progress = self._derive_statuses(
                plan, legs, receipts, day, installation_id, tenant_id
            )
            details = tuple(
                LegDetail(
                    leg=leg,
                    received=MoneyIRR(received_by_leg.get(leg.id, 0)),
                    status=status,
                )
                for leg, status in zip(legs, statuses, strict=True)
            )
        else:
            # Internal legs follow the wall-clock machine, with the
            # cheque-aware split (US-542): pending clearance + the
            # linked cheque per leg.
            received_by_leg, pending_by_leg = self._split(
                receipts, tenant_id=tenant_id, installation_id=installation_id
            )
            views = self._leg_views(legs, received_by_leg, pending_by_leg)
            plan_status_value = plan_status(views, day)
            progress = None
            details = tuple(
                self._leg_detail(
                    leg,
                    view,
                    day,
                    receipts=receipts,
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                )
                for leg, view in zip(legs, views, strict=True)
            )
        return PlanDetail(
            plan=plan,
            legs=details,
            receipts=tuple(receipts),
            status=plan_status_value,
            gateway_receivable=MoneyIRR(progress.open_irr if progress else 0),
            gateway_settled=MoneyIRR(progress.settled_irr if progress else 0),
        )

    def list_plans(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        invoice_id: str | None = None,
        kind: PlanKind | None = None,
        status: PlanStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        today: datetime.date | None = None,
    ) -> list[PlanSummary]:
        """List plans with derived statuses (optionally filtered by kind/status/invoice).

        The kind filter is a SQL predicate (kind is stored); the
        status filter applies AFTER derivation (status is computed,
        never stored, so it cannot be a SQL predicate). The list is
        small by nature (one plan per invoice); a status filter scans
        the page's plans, not the whole table.
        """
        day = today or datetime.date.today()
        plans = self._repository.list_plans(
            tenant_id=tenant_id,
            installation_id=installation_id,
            invoice_id=invoice_id,
            kind=kind,
            limit=limit,
            offset=offset,
        )
        summaries: list[PlanSummary] = []
        for plan in plans:
            legs = self._repository.list_legs(
                plan.id, tenant_id=tenant_id, installation_id=installation_id
            )
            receipts = self._repository.list_receipts(
                plan.id, tenant_id=tenant_id, installation_id=installation_id
            )
            if plan.kind is PlanKind.BNPL:
                received_by_leg = self._received_totals(receipts)
                _, plan_status_value, _ = self._derive_statuses(
                    plan, legs, receipts, day, installation_id, tenant_id
                )
                pending_by_leg: dict[str, int] = {}
            else:
                received_by_leg, pending_by_leg = self._split(
                    receipts, tenant_id=tenant_id, installation_id=installation_id
                )
                views = self._leg_views(legs, received_by_leg, pending_by_leg)
                plan_status_value = plan_status(views, day)
            if status is not None and plan_status_value is not status:
                continue
            outstanding = MoneyIRR(
                sum(
                    max(0, leg.principal.amount - received_by_leg.get(leg.id, 0))
                    for leg in legs
                )
            )
            summaries.append(
                PlanSummary(
                    plan=plan,
                    status=plan_status_value,
                    total_received=MoneyIRR(
                        sum(
                            r.amount.amount
                            for r in receipts
                            if r.counts_toward_legs
                        )
                    ),
                    outstanding=outstanding,
                    pending_clearance=MoneyIRR(
                        sum(pending_by_leg.get(leg.id, 0) for leg in legs)
                    ),
                )
            )
        return summaries

    def get_payoff_quote(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        installation_id: str,
        today: datetime.date | None = None,
    ) -> PayoffQuote:
        """Return the early-settlement payoff quote for one plan (404 when out of scope).

        Remaining principal + income due (per the contract's income
        policy) + late fees due (per the fee policy) − advance held.
        A plan without a contract settles for its remaining
        principal minus its held advance. A BNPL plan has no payoff
        quote (typed 422): the customer owes the gateway, not the
        seller — early settlement is the gateway's business.
        """
        plan = self._repository.get_plan(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None:
            raise InstallmentPlanNotFoundError(plan_id)
        if plan.kind is PlanKind.BNPL:
            raise InstallmentBnplEarlySettlementError(plan_id=plan.id)
        day = today or datetime.date.today()
        legs = self._repository.list_legs(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        receipts = self._repository.list_receipts(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        contract = self._governing_contract(plan, tenant_id, installation_id)
        received_by_leg, pending_by_leg = self._split(
            receipts, tenant_id=tenant_id, installation_id=installation_id
        )
        views = self._leg_views(legs, received_by_leg, pending_by_leg)
        held = held_advance(
            MoneyIRR(sum(r.advance.amount for r in receipts)), plan.applied_advance
        )
        income_paid = MoneyIRR(sum(r.finance_income.amount for r in receipts))
        return compute_payoff_quote(
            plan.id,
            views,
            contract=contract,
            advance_held=held,
            income_paid=income_paid,
            today=day,
        )

    def list_overdue(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        plan_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        today: datetime.date | None = None,
    ) -> OverdueReport:
        """Return the installation's overdue legs, most overdue first.

        The §7.5 overdue report's source: every leg past its due
        date with an unpaid balance, with its dunning stage, next
        reminder date and accrued late fee. ``plan_id`` scopes the
        report to one plan (drill-down); the totals always cover
        the whole (filtered) set, not just the page.
        """
        day = today or datetime.date.today()
        if plan_id is not None:
            plan = self._repository.get_plan(
                plan_id, tenant_id=tenant_id, installation_id=installation_id
            )
            plans = [plan] if plan is not None else []
        else:
            plans = []
            page_offset = 0
            while True:
                page = self._repository.list_plans(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    limit=100,
                    offset=page_offset,
                )
                if not page:
                    break
                plans.extend(page)
                page_offset += len(page)
        rows: list[OverdueLegRow] = []
        for plan in plans:
            # BNPL plans never enter dunning: their legs are never
            # overdue — the gateway owns collection (US-543).
            if not plan_enters_dunning(plan):
                continue
            legs = self._repository.list_legs(
                plan.id, tenant_id=tenant_id, installation_id=installation_id
            )
            receipts = self._repository.list_receipts(
                plan.id, tenant_id=tenant_id, installation_id=installation_id
            )
            contract = self._governing_contract(plan, tenant_id, installation_id)
            received_by_leg, pending_by_leg = self._split(
                receipts, tenant_id=tenant_id, installation_id=installation_id
            )
            views = self._leg_views(legs, received_by_leg, pending_by_leg)
            for view in views:
                assessment = assess_leg(view, contract=contract, today=day)
                if assessment.status is not LegStatus.OVERDUE:
                    continue
                rows.append(
                    OverdueLegRow(
                        plan_id=plan.id,
                        invoice_id=plan.invoice_id,
                        customer_ref=plan.customer_ref,
                        contract_code=contract.code if contract is not None else None,
                        leg_id=view.leg_id,
                        sequence=view.sequence,
                        due_date=view.due_date,
                        days_overdue=assessment.days_past_due,
                        principal=view.principal,
                        received=view.received,
                        balance=view.balance,
                        dunning_stage=assessment.dunning_stage,
                        next_reminder_date=assessment.next_reminder,
                        late_fee_accrued=assessment.late_fee_accrued,
                    )
                )
        rows.sort(key=lambda row: (-row.days_overdue, row.due_date.gregorian_date, row.sequence))
        return OverdueReport(
            rows=tuple(rows[offset : offset + limit]),
            total_rows=len(rows),
            total_balance=MoneyIRR(sum(row.balance.amount for row in rows)),
            total_late_fees=MoneyIRR(sum(row.late_fee_accrued.amount for row in rows)),
        )

    def get_effective_contract(
        self,
        code: str,
        *,
        tenant_id: str,
        installation_id: str,
        on_date: datetime.date | None = None,
    ) -> InstallmentSaleContract:
        """Return the version of a contract code in force on a date (404 when none).

        US-541's effective-version selection: the latest active
        version whose ``effective_from`` is on or before the date.
        Used to resolve a contract CODE at plan-creation time — a
        plan's terms are fixed at creation by its linked version.
        """
        day = on_date or datetime.date.today()
        versions = self._repository.list_contracts(
            tenant_id=tenant_id,
            installation_id=installation_id,
            code=code,
            limit=100,
        )
        selected = select_effective_contract(versions, day)
        if selected is None:
            raise InstallmentContractNotFoundError(code)
        return selected

    def get_contract(
        self,
        contract_id: str,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> InstallmentSaleContract:
        """Return one contract (404 when out of scope)."""
        contract = self._repository.get_contract(
            contract_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if contract is None:
            raise InstallmentContractNotFoundError(contract_id)
        return contract

    def list_contracts(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        code: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InstallmentSaleContract]:
        """List contracts, newest first (optionally one code's versions)."""
        return self._repository.list_contracts(
            tenant_id=tenant_id,
            installation_id=installation_id,
            code=code,
            limit=limit,
            offset=offset,
        )

    def list_plan_amendments(
        self,
        plan_id: str,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> list[ScheduleAmendment]:
        """A plan's schedule amendments, oldest first (US-545).

        The audit history of the plan-change flow: every partial
        early settlement that re-amortised the schedule recorded
        one amendment (before/after leg principals + applied
        advance + triggering receipt). 404 when the plan is out of
        scope — an unknown plan and an empty history are different
        answers, and the UI tells them apart.
        """
        plan = self._repository.get_plan(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if plan is None:
            raise InstallmentPlanNotFoundError(plan_id)
        return self._repository.list_schedule_amendments(
            plan_id, tenant_id=tenant_id, installation_id=installation_id
        )

    def _governing_contract(
        self,
        plan: InstallmentPlan,
        tenant_id: str,
        installation_id: str,
    ) -> InstallmentSaleContract | None:
        """The contract governing a plan: its linked version, or None."""
        if plan.contract_id is None:
            return None
        return self._repository.get_contract(
            plan.contract_id, tenant_id=tenant_id, installation_id=installation_id
        )

    @staticmethod
    def _received_totals(receipts: list[InstallmentReceipt]) -> dict[str, int]:
        """Per-leg received totals from LIVE receipts only.

        A rejected/cancelled BNPL receipt's allocations stop
        counting (its legs return to ``expected``) — see
        ``InstallmentReceipt.counts_toward_legs``.
        """
        totals: dict[str, int] = {}
        for receipt in receipts:
            if not receipt.counts_toward_legs:
                continue
            for allocation in receipt.allocations:
                totals[allocation.leg_id] = (
                    totals.get(allocation.leg_id, 0) + allocation.amount.amount
                )
        return totals

    def _derive_statuses(
        self,
        plan: InstallmentPlan,
        legs: list[InstallmentLeg],
        receipts: list[InstallmentReceipt],
        today: datetime.date,
        installation_id: str,
        tenant_id: str,
    ) -> tuple[
        tuple[LegStatus | BnplLegStatus, ...], PlanStatus, BnplTrancheProgress | None
    ]:
        """Derive one plan's leg statuses + plan status (+ BNPL progress).

        Internal plans use the wall-clock machine
        (:func:`leg_status_for` / :func:`plan_status`) over the
        cheque-aware split (US-542 — a leg with a cheque in flight
        is ``pending_clearance``, not due/overdue); BNPL plans use
        the tranche machine (:func:`bnpl_leg_status` /
        :func:`bnpl_plan_status`) — a BNPL plan is never overdue.
        """
        received_by_leg = self._received_totals(receipts)
        if plan.kind is PlanKind.BNPL:
            tranches_by_leg, all_tranches = self._load_bnpl_tranches(
                installation_id, receipts
            )
            statuses: tuple[LegStatus | BnplLegStatus, ...] = tuple(
                self._bnpl_leg_status(
                    leg.id, received_by_leg.get(leg.id, 0), tranches_by_leg
                )
                for leg in legs
            )
            bnpl_statuses = tuple(
                status for status in statuses if isinstance(status, BnplLegStatus)
            )
            return (
                statuses,
                bnpl_plan_status(bnpl_statuses),
                bnpl_tranche_progress(all_tranches),
            )
        # Internal plan: the cheque-aware split (US-542) — pending
        # clearance is part of the view, so a leg with a cheque in
        # flight reads pending_clearance, never due/overdue.
        received, pending = self._split(
            receipts, tenant_id=tenant_id, installation_id=installation_id
        )
        views = self._leg_views(legs, received, pending)
        return (
            tuple(leg_status_for(view, today) for view in views),
            plan_status(views, today),
            None,
        )

    def _load_bnpl_tranches(
        self, installation_id: str, receipts: list[InstallmentReceipt]
    ) -> tuple[dict[str, list[SettlementTranche]], list[SettlementTranche]]:
        """Map each leg to its live BNPL receipts' settlement tranches.

        Returns (per-leg tranche lists, all tranches flattened). A
        leg follows every payment that allocates to it — a leg paid
        by two BNPL receipts moves with both payments' tranches.
        Empty when no settlement repository is wired (the
        receipt-alone fallback) or when the plan has no live BNPL
        receipts.
        """
        by_leg: dict[str, list[SettlementTranche]] = {}
        all_tranches: list[SettlementTranche] = []
        if self._settlement is None:
            return by_leg, all_tranches
        for receipt in receipts:
            if receipt.payment_method is not PaymentMethod.BNPL:
                continue
            if not receipt.counts_toward_legs or receipt.gateway_payment_id is None:
                continue
            tranches = list(
                self._settlement.list_tranches_for_payment(
                    installation_id, receipt.gateway_payment_id
                )
            )
            all_tranches.extend(tranches)
            for allocation in receipt.allocations:
                by_leg.setdefault(allocation.leg_id, []).extend(tranches)
        return by_leg, all_tranches

    def _bnpl_leg_status(
        self,
        leg_id: str,
        received_irr: int,
        tranches_by_leg: dict[str, list[SettlementTranche]],
    ) -> BnplLegStatus:
        """One BNPL leg's status, with the receipt-alone fallback.

        Without a settlement repository, the receipt is all there
        is: a live allocation means ``gateway_confirmed``. With
        one, the tranches decide — except when a live allocation
        has NO tranches at all (the payment row lost its schedule,
        which the engine never produces): the leg is confirmed,
        not expected, because the gateway did confirm the payment.
        """
        tranches = tranches_by_leg.get(leg_id, [])
        if self._settlement is None:
            return (
                BnplLegStatus.GATEWAY_CONFIRMED
                if received_irr > 0
                else BnplLegStatus.EXPECTED
            )
        if not tranches and received_irr > 0:
            return BnplLegStatus.GATEWAY_CONFIRMED
        return bnpl_leg_status(tranches)

    def _split(
        self,
        receipts: list[InstallmentReceipt],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Split the receipts' allocations into received vs pending, per leg."""
        cheque_states = resolve_cheque_states(
            receipts, self._cheques, tenant_id=tenant_id, installation_id=installation_id
        )
        return split_allocations(receipts, cheque_states)

    @staticmethod
    def _leg_views(
        legs: list[InstallmentLeg],
        received_by_leg: dict[str, int],
        pending_by_leg: dict[str, int],
    ) -> list[LegView]:
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

    def _leg_detail(
        self,
        leg: InstallmentLeg,
        view: LegView,
        today: datetime.date,
        *,
        receipts: list[InstallmentReceipt],
        tenant_id: str,
        installation_id: str,
    ) -> LegDetail:
        return LegDetail(
            leg=leg,
            received=view.received,
            status=leg_status_for(view, today),
            pending_clearance=view.pending_clearance,
            linked_cheque=self._linked_cheque(
                leg.id, receipts, tenant_id=tenant_id, installation_id=installation_id
            ),
        )

    def _linked_cheque(
        self,
        leg_id: str,
        receipts: list[InstallmentReceipt],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> LinkedChequeSummary | None:
        """The cheque to show for a leg (US-542), if one is linked.

        Preference: the cheque backing this leg's pending (or
        cleared) receipt allocations — the instrument the leg's
        status actually depends on. Fallback: the newest linked
        cheque (an intake-linked cheque with no receipt yet still
        shows, so the detail never hides a linked instrument).
        Without a cheque repository wired, nothing is shown.
        """
        if self._cheques is None:
            return None
        linked = self._cheques.list_by_installment_leg_id(
            leg_id, tenant_id=tenant_id, installation_id=installation_id
        )
        if not linked:
            return None
        receipt_cheque_ids = {
            receipt.cheque_id
            for receipt in receipts
            if receipt.cheque_id is not None
            and any(a.leg_id == leg_id for a in receipt.allocations)
        }
        backing = [c for c in linked if c.id in receipt_cheque_ids]
        if backing:
            pending = [c for c in backing if c.state in PENDING_CHEQUE_STATES]
            chosen = pending[0] if pending else backing[0]
            return LinkedChequeSummary.from_cheque(chosen)
        return LinkedChequeSummary.from_cheque(linked[0])
