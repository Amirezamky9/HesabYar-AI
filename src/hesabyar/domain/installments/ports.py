"""Installment repository port (US-540 + US-543).

The application services depend only on this protocol; the
SQLAlchemy implementation lives in
``hesabyar.infrastructure.persistence.postgres.installments_repository``.

Tenant/installation scoping: every read is scoped to
(tenant_id, installation_id); a row of another installation answers
as if it did not exist (the service maps that to 404). The
repository never commits: the caller (router / worker) owns the
transaction.

Legs are read through their plan (``list_legs``); receipts carry
their allocations with them (``list_receipts`` returns receipts
with ``allocations`` populated), so the service can compute leg
statuses and plan totals without a second round of queries per leg.

US-541 adds the finance writes: late-fee assessments (the
idempotency record for dunning runs), schedule amendments (the
audit record of a re-amortisation), leg-principal updates and the
plan's applied-advance tracking.

US-543 (BNPL): ``list_plans`` filters by stored plan ``kind``
(a SQL predicate — kind is stored, unlike the derived status);
``get_receipt`` + ``update_receipt_bnpl_status`` serve the
gateway-rejection flow (a rejected BNPL receipt's allocations
stop counting, but the row is kept as the audit record).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.models import (
    BnplReceiptStatus,
    InstallmentLeg,
    InstallmentPlan,
    InstallmentReceipt,
    InstallmentSaleContract,
    LateFeeAssessment,
    PlanKind,
    ScheduleAmendment,
)


class InstallmentRepositoryPort(Protocol):
    """HesabYar-side installment plan persistence."""

    def create_plan(
        self, plan: InstallmentPlan, legs: Sequence[InstallmentLeg]
    ) -> InstallmentPlan:
        """Insert a plan with its legs, atomically (one transaction)."""
        ...

    def get_plan(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentPlan | None:
        """Return one plan, or None when missing / out of scope."""
        ...

    def get_plan_by_invoice(
        self, invoice_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentPlan | None:
        """Return the plan for an invoice, or None (one plan per invoice)."""
        ...

    def list_plans(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        invoice_id: str | None = None,
        kind: PlanKind | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InstallmentPlan]:
        """List the installation's plans, newest first.

        ``kind`` filters by the stored plan kind (US-543: BNPL and
        internal plans are separate report worlds, so the filter is
        a SQL predicate, not a post-read scan).
        """
        ...

    def list_legs(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[InstallmentLeg]:
        """List a plan's legs in sequence order (empty when the plan is out of scope)."""
        ...

    def get_leg(
        self, leg_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentLeg | None:
        """Return one leg, or None when missing / out of scope.

        US-542 (cheque legs) resolves a cheque's ``installment_leg_id``
        through this — the link target the cheque module already carries.
        """
        ...

    def create_receipt(self, receipt: InstallmentReceipt) -> InstallmentReceipt:
        """Insert a receipt with its allocations, atomically."""
        ...

    def list_receipts(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[InstallmentReceipt]:
        """List a plan's receipts, oldest first, allocations populated."""
        ...

    def get_receipt(
        self, receipt_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentReceipt | None:
        """Return one receipt with its allocations, or None when missing / out of scope.

        US-543: the gateway-rejection flow resolves the receipt to
        reject through this.
        """
        ...

    def get_receipt_by_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentReceipt | None:
        """Return the receipt backed by this cheque, or None (US-542).

        One cheque backs at most one receipt, so this is unique.
        The command service uses it to refuse a second receipt on
        the same cheque, and the collection hook uses it to find
        the receipt to mark collected when the cheque clears.
        """
        ...

    def save_receipt(self, receipt: InstallmentReceipt) -> InstallmentReceipt:
        """Persist an in-memory mutated receipt (US-542).

        The only receipt field that changes after creation is
        ``collected_at`` (set by the collection hook when the
        backing cheque clears); allocations never change. The
        implementation updates the mutable fields and returns the
        saved receipt.
        """
        ...

    def update_receipt_bnpl_status(
        self,
        receipt_id: str,
        *,
        tenant_id: str,
        installation_id: str,
        bnpl_status: BnplReceiptStatus,
    ) -> InstallmentReceipt | None:
        """Set a BNPL receipt's gateway-side status; None when missing / out of scope.

        US-543: a rejection/cancellation marks the receipt dead
        (``rejected`` / ``cancelled``) so its allocations stop
        counting — the legs return to ``expected``. The receipt row
        itself is never deleted (audit record).
        """
        ...

    def create_contract(self, contract: InstallmentSaleContract) -> InstallmentSaleContract:
        """Insert a sale contract (one version; no auto-supersede)."""
        ...

    def get_contract(
        self, contract_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentSaleContract | None:
        """Return one contract, or None when missing / out of scope."""
        ...

    def list_contracts(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        code: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InstallmentSaleContract]:
        """List the installation's contracts, newest first (optionally one code's versions)."""
        ...

    # -- finance (US-541) ----------------------------------------------------

    def update_leg_principals(
        self,
        plan_id: str,
        principals: Mapping[str, MoneyIRR],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> None:
        """Rewrite leg principals (re-amortisation; the amendment row is the audit)."""
        ...

    def update_applied_advance(
        self,
        plan_id: str,
        applied_advance: MoneyIRR,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> None:
        """Set the plan's applied-advance total (advance already applied to the schedule)."""
        ...

    def create_late_fee_assessment(self, assessment: LateFeeAssessment) -> LateFeeAssessment:
        """Insert one late-fee assessment (a cumulative snapshot per leg + date)."""
        ...

    def list_late_fee_assessments(
        self, leg_id: str, *, tenant_id: str, installation_id: str
    ) -> list[LateFeeAssessment]:
        """A leg's assessments, newest first (by assessed date, then creation)."""
        ...

    def create_schedule_amendment(self, amendment: ScheduleAmendment) -> ScheduleAmendment:
        """Insert one schedule amendment (the audit record of a re-amortisation)."""
        ...

    def list_schedule_amendments(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[ScheduleAmendment]:
        """A plan's amendments, oldest first."""
        ...
