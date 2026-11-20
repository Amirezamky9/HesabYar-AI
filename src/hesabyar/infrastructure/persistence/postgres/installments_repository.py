"""SQLAlchemy installment repository (US-540 + US-543, Phase 5-5 ۵ج).

Implements :class:`InstallmentRepositoryPort` over the
``installment_plans`` / ``installment_legs`` /
``installment_receipts`` / ``installment_receipt_allocations`` /
``installment_sale_contracts`` tables, plus the US-541 finance
tables (``installment_late_fee_assessments``,
``installment_schedule_amendments``). The repository never talks
to the engine — the finance service creates journals through the
journal-draft port, not here — and it never commits: the caller
(router / worker) owns the transaction.

Legs are scoped through their plan (the legs table carries no
tenant column of its own); receipts carry their allocations with
them on read so the services can compute leg statuses without a
second query per leg. Dunning offsets travel as a comma-separated
string in the contracts table (``"3,7,14,30"``) and a tuple of
ints in the domain. US-543 adds the plan ``kind`` + gateway
channel columns and the receipt's gateway link
(``gateway_payment_id`` is a cross-module reference into the
settlement engine's ``gateway_payments`` — no SQL FK).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.domain.installments.models import (
    AmendmentKind,
    BnplReceiptStatus,
    EarlySettlementFeePolicy,
    EarlySettlementIncomePolicy,
    InstallmentLeg,
    InstallmentPlan,
    InstallmentReceipt,
    InstallmentSaleContract,
    LateFeeAssessment,
    LateFeeBasis,
    LateFeeJournalState,
    LegAllocation,
    LegPrincipalSnapshot,
    PaymentMethod,
    PlanKind,
    PlanSource,
    ScheduleAmendment,
)
from hesabyar.domain.installments.ports import InstallmentRepositoryPort
from hesabyar.infrastructure.persistence.postgres.models import (
    InstallmentLateFeeAssessmentModel,
    InstallmentLegModel,
    InstallmentPlanModel,
    InstallmentReceiptAllocationModel,
    InstallmentReceiptModel,
    InstallmentSaleContractModel,
    InstallmentScheduleAmendmentModel,
)


def _offsets_to_text(offsets: tuple[int, ...]) -> str:
    return ",".join(str(offset) for offset in offsets)


def _offsets_from_text(raw: str) -> tuple[int, ...]:
    cleaned = raw.strip()
    if not cleaned:
        return ()
    return tuple(int(part) for part in cleaned.split(","))


def _snapshots_to_json(
    snapshots: tuple[LegPrincipalSnapshot, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "leg_id": snapshot.leg_id,
            "sequence": snapshot.sequence,
            "principal_irr": snapshot.principal.amount,
        }
        for snapshot in snapshots
    ]


def _snapshots_from_json(rows: list[dict[str, Any]]) -> tuple[LegPrincipalSnapshot, ...]:
    return tuple(
        LegPrincipalSnapshot(
            leg_id=str(row["leg_id"]),
            sequence=int(row["sequence"]),
            principal=MoneyIRR(int(row["principal_irr"])),
        )
        for row in rows
    )


def _plan_from_row(row: InstallmentPlanModel) -> InstallmentPlan:
    return InstallmentPlan(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        company_engine_id=row.company_engine_id,
        invoice_id=row.invoice_id,
        total_principal=MoneyIRR(row.total_principal_irr),
        leg_count=row.leg_count,
        contract_id=row.contract_id,
        customer_ref=row.customer_ref,
        interval_days=row.interval_days,
        first_due_date=(
            FiscalDate.from_gregorian(row.first_due_date) if row.first_due_date else None
        ),
        auto_post=row.auto_post,
        applied_advance=MoneyIRR(row.applied_advance_irr),
        source=PlanSource(row.source),
        source_event_id=row.source_event_id,
        kind=PlanKind(row.kind),
        gateway_channel_key=row.gateway_channel_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _leg_from_row(row: InstallmentLegModel) -> InstallmentLeg:
    return InstallmentLeg(
        id=row.id,
        plan_id=row.plan_id,
        sequence=row.sequence,
        due_date=FiscalDate.from_gregorian(row.due_date),
        principal=MoneyIRR(row.principal_irr),
        created_at=row.created_at,
    )


def _contract_from_row(row: InstallmentSaleContractModel) -> InstallmentSaleContract:
    return InstallmentSaleContract(
        id=row.id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        company_engine_id=row.company_engine_id,
        code=row.code,
        name=row.name,
        version=row.version,
        effective_from=FiscalDate.from_gregorian(row.effective_from),
        late_fee_basis=LateFeeBasis(row.late_fee_basis),
        late_fee_rate=Decimal(row.late_fee_rate),
        late_fee_grace_days=row.late_fee_grace_days,
        penalty_account_engine_id=row.penalty_account_engine_id,
        penalty_account_code=row.penalty_account_code,
        finance_income_rate=Decimal(row.finance_income_rate),
        dunning_offsets=_offsets_from_text(row.dunning_offsets),
        receivable_account_engine_id=row.receivable_account_engine_id,
        receivable_account_code=row.receivable_account_code,
        early_settlement_fee_policy=EarlySettlementFeePolicy(row.early_settlement_fee_policy),
        early_settlement_income_policy=EarlySettlementIncomePolicy(
            row.early_settlement_income_policy
        ),
        early_settlement_discount_rate=Decimal(row.early_settlement_discount_rate),
        supersedes_contract_id=row.supersedes_contract_id,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _assessment_from_row(row: InstallmentLateFeeAssessmentModel) -> LateFeeAssessment:
    return LateFeeAssessment(
        id=row.id,
        leg_id=row.leg_id,
        plan_id=row.plan_id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        assessed_through=FiscalDate.from_gregorian(row.assessed_through),
        days_overdue=row.days_overdue,
        fee=MoneyIRR(row.fee_irr),
        journal_move_id=row.journal_move_id,
        journal_state=LateFeeJournalState(row.journal_state),
        created_at=row.created_at,
    )


def _amendment_from_row(row: InstallmentScheduleAmendmentModel) -> ScheduleAmendment:
    return ScheduleAmendment(
        id=row.id,
        plan_id=row.plan_id,
        tenant_id=row.tenant_id,
        installation_id=row.installation_id,
        kind=AmendmentKind(row.kind),
        applied_advance=MoneyIRR(row.applied_advance_irr),
        legs_before=_snapshots_from_json(row.legs_before),
        legs_after=_snapshots_from_json(row.legs_after),
        receipt_id=row.receipt_id,
        created_at=row.created_at,
    )


class SqlAlchemyInstallmentRepository(InstallmentRepositoryPort):
    """SQLAlchemy implementation of the installment repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- plans -----------------------------------------------------------

    def create_plan(
        self, plan: InstallmentPlan, legs: Sequence[InstallmentLeg]
    ) -> InstallmentPlan:
        self._session.add(
            InstallmentPlanModel(
                id=plan.id,
                tenant_id=plan.tenant_id,
                installation_id=plan.installation_id,
                company_engine_id=plan.company_engine_id,
                invoice_id=plan.invoice_id,
                contract_id=plan.contract_id,
                customer_ref=plan.customer_ref,
                total_principal_irr=plan.total_principal.amount,
                leg_count=plan.leg_count,
                interval_days=plan.interval_days,
                first_due_date=(
                    plan.first_due_date.gregorian_date if plan.first_due_date else None
                ),
                auto_post=plan.auto_post,
                applied_advance_irr=plan.applied_advance.amount,
                source=plan.source.value,
                source_event_id=plan.source_event_id,
                kind=plan.kind.value,
                gateway_channel_key=plan.gateway_channel_key,
                created_at=plan.created_at,
                updated_at=plan.updated_at,
            )
        )
        for leg in legs:
            self._session.add(
                InstallmentLegModel(
                    id=leg.id,
                    plan_id=leg.plan_id,
                    sequence=leg.sequence,
                    due_date=leg.due_date.gregorian_date,
                    principal_irr=leg.principal.amount,
                    created_at=leg.created_at,
                )
            )
        self._session.flush()
        return plan

    def get_plan(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentPlan | None:
        row = self._session.execute(
            select(InstallmentPlanModel).where(
                InstallmentPlanModel.id == plan_id,
                InstallmentPlanModel.tenant_id == tenant_id,
                InstallmentPlanModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        return _plan_from_row(row) if row is not None else None

    def get_plan_by_invoice(
        self, invoice_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentPlan | None:
        row = self._session.execute(
            select(InstallmentPlanModel).where(
                InstallmentPlanModel.invoice_id == invoice_id,
                InstallmentPlanModel.tenant_id == tenant_id,
                InstallmentPlanModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        return _plan_from_row(row) if row is not None else None

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
        stmt = (
            select(InstallmentPlanModel)
            .where(
                InstallmentPlanModel.tenant_id == tenant_id,
                InstallmentPlanModel.installation_id == installation_id,
            )
            .order_by(InstallmentPlanModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if invoice_id is not None:
            stmt = stmt.where(InstallmentPlanModel.invoice_id == invoice_id)
        if kind is not None:
            stmt = stmt.where(InstallmentPlanModel.kind == kind.value)
        rows = self._session.execute(stmt).scalars().all()
        return [_plan_from_row(row) for row in rows]

    # -- legs ------------------------------------------------------------

    def list_legs(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[InstallmentLeg]:
        plan = self.get_plan(plan_id, tenant_id=tenant_id, installation_id=installation_id)
        if plan is None:
            return []
        rows = self._session.execute(
            select(InstallmentLegModel)
            .where(InstallmentLegModel.plan_id == plan_id)
            .order_by(InstallmentLegModel.sequence.asc())
        ).scalars().all()
        return [_leg_from_row(row) for row in rows]

    def get_leg(
        self, leg_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentLeg | None:
        row = self._session.execute(
            select(InstallmentLegModel)
            .join(InstallmentPlanModel, InstallmentLegModel.plan_id == InstallmentPlanModel.id)
            .where(
                InstallmentLegModel.id == leg_id,
                InstallmentPlanModel.tenant_id == tenant_id,
                InstallmentPlanModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        return _leg_from_row(row) if row is not None else None

    # -- receipts ----------------------------------------------------------

    def create_receipt(self, receipt: InstallmentReceipt) -> InstallmentReceipt:
        self._session.add(
            InstallmentReceiptModel(
                id=receipt.id,
                plan_id=receipt.plan_id,
                tenant_id=receipt.tenant_id,
                installation_id=receipt.installation_id,
                invoice_id=receipt.invoice_id,
                journal_move_id=receipt.journal_move_id,
                amount_irr=receipt.amount.amount,
                received_date=receipt.received_date.gregorian_date,
                payment_method=receipt.payment_method.value,
                advance_irr=receipt.advance.amount,
                finance_income_irr=receipt.finance_income.amount,
                late_fee_irr=receipt.late_fee.amount,
                applied_advance_irr=receipt.applied_advance.amount,
                is_early_settlement=receipt.is_early_settlement,
                reference=receipt.reference,
                notes=receipt.notes,
                cheque_id=receipt.cheque_id,
                collected_at=(
                    receipt.collected_at.gregorian_date if receipt.collected_at else None
                ),
                gateway_channel_key=receipt.gateway_channel_key,
                gateway_reference=receipt.gateway_reference,
                gateway_payment_id=receipt.gateway_payment_id,
                bnpl_status=(
                    receipt.bnpl_status.value if receipt.bnpl_status is not None else None
                ),
                created_at=receipt.created_at,
            )
        )
        for allocation in receipt.allocations:
            self._session.add(
                InstallmentReceiptAllocationModel(
                    receipt_id=receipt.id,
                    leg_id=allocation.leg_id,
                    amount_irr=allocation.amount.amount,
                )
            )
        self._session.flush()
        return receipt

    def _receipt_from_row(self, row: InstallmentReceiptModel) -> InstallmentReceipt:
        """Map one receipt row (+ its allocations) to the domain object."""
        allocation_rows = self._session.execute(
            select(InstallmentReceiptAllocationModel).where(
                InstallmentReceiptAllocationModel.receipt_id == row.id
            )
        ).scalars().all()
        return InstallmentReceipt(
            id=row.id,
            plan_id=row.plan_id,
            tenant_id=row.tenant_id,
            installation_id=row.installation_id,
            invoice_id=row.invoice_id,
            journal_move_id=row.journal_move_id,
            amount=MoneyIRR(row.amount_irr),
            received_date=FiscalDate.from_gregorian(row.received_date),
            payment_method=PaymentMethod(row.payment_method),
            allocations=tuple(
                LegAllocation(leg_id=a.leg_id, amount=MoneyIRR(a.amount_irr))
                for a in allocation_rows
            ),
            advance=MoneyIRR(row.advance_irr),
            finance_income=MoneyIRR(row.finance_income_irr),
            late_fee=MoneyIRR(row.late_fee_irr),
            applied_advance=MoneyIRR(row.applied_advance_irr),
            is_early_settlement=row.is_early_settlement,
            reference=row.reference,
            notes=row.notes,
            cheque_id=row.cheque_id,
            collected_at=(
                FiscalDate.from_gregorian(row.collected_at) if row.collected_at else None
            ),
            gateway_channel_key=row.gateway_channel_key,
            gateway_reference=row.gateway_reference,
            gateway_payment_id=row.gateway_payment_id,
            bnpl_status=(
                BnplReceiptStatus(row.bnpl_status) if row.bnpl_status is not None else None
            ),
            created_at=row.created_at,
        )

    def list_receipts(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[InstallmentReceipt]:
        rows = self._session.execute(
            select(InstallmentReceiptModel)
            .where(
                InstallmentReceiptModel.plan_id == plan_id,
                InstallmentReceiptModel.tenant_id == tenant_id,
                InstallmentReceiptModel.installation_id == installation_id,
            )
            .order_by(InstallmentReceiptModel.created_at.asc())
        ).scalars().all()
        return [self._receipt_from_row(row) for row in rows]

    def get_receipt(
        self, receipt_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentReceipt | None:
        row = self._session.execute(
            select(InstallmentReceiptModel).where(
                InstallmentReceiptModel.id == receipt_id,
                InstallmentReceiptModel.tenant_id == tenant_id,
                InstallmentReceiptModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        return self._receipt_from_row(row) if row is not None else None

    def update_receipt_bnpl_status(
        self,
        receipt_id: str,
        *,
        tenant_id: str,
        installation_id: str,
        bnpl_status: BnplReceiptStatus,
    ) -> InstallmentReceipt | None:
        row = self._session.execute(
            select(InstallmentReceiptModel).where(
                InstallmentReceiptModel.id == receipt_id,
                InstallmentReceiptModel.tenant_id == tenant_id,
                InstallmentReceiptModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.bnpl_status = bnpl_status.value
        self._session.flush()
        return self._receipt_from_row(row)

    def get_receipt_by_cheque(
        self, cheque_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentReceipt | None:
        """Return the receipt backed by this cheque, or None (US-542)."""
        row = self._session.execute(
            select(InstallmentReceiptModel).where(
                InstallmentReceiptModel.cheque_id == cheque_id,
                InstallmentReceiptModel.tenant_id == tenant_id,
                InstallmentReceiptModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        return self._receipt_from_row(row) if row is not None else None

    def save_receipt(self, receipt: InstallmentReceipt) -> InstallmentReceipt:
        """Persist a mutated receipt (US-542): only ``collected_at``
        (and ``cheque_id``) change after creation; allocations never
        change, so they are not re-written."""
        row = self._session.execute(
            select(InstallmentReceiptModel).where(
                InstallmentReceiptModel.id == receipt.id
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Installment receipt {receipt.id!r} does not exist.")
        row.cheque_id = receipt.cheque_id
        row.collected_at = (
            receipt.collected_at.gregorian_date if receipt.collected_at else None
        )
        self._session.flush()
        return receipt

    # -- contracts ---------------------------------------------------------

    def create_contract(self, contract: InstallmentSaleContract) -> InstallmentSaleContract:
        self._session.add(
            InstallmentSaleContractModel(
                id=contract.id,
                tenant_id=contract.tenant_id,
                installation_id=contract.installation_id,
                company_engine_id=contract.company_engine_id,
                code=contract.code,
                name=contract.name,
                version=contract.version,
                effective_from=contract.effective_from.gregorian_date,
                late_fee_basis=contract.late_fee_basis.value,
                late_fee_rate=str(contract.late_fee_rate),
                late_fee_grace_days=contract.late_fee_grace_days,
                penalty_account_engine_id=contract.penalty_account_engine_id,
                penalty_account_code=contract.penalty_account_code,
                finance_income_rate=str(contract.finance_income_rate),
                dunning_offsets=_offsets_to_text(contract.dunning_offsets),
                receivable_account_engine_id=contract.receivable_account_engine_id,
                receivable_account_code=contract.receivable_account_code,
                early_settlement_fee_policy=contract.early_settlement_fee_policy.value,
                early_settlement_income_policy=contract.early_settlement_income_policy.value,
                early_settlement_discount_rate=str(contract.early_settlement_discount_rate),
                supersedes_contract_id=contract.supersedes_contract_id,
                is_active=contract.is_active,
                created_at=contract.created_at,
                updated_at=contract.updated_at,
            )
        )
        self._session.flush()
        return contract

    def get_contract(
        self, contract_id: str, *, tenant_id: str, installation_id: str
    ) -> InstallmentSaleContract | None:
        row = self._session.execute(
            select(InstallmentSaleContractModel).where(
                InstallmentSaleContractModel.id == contract_id,
                InstallmentSaleContractModel.tenant_id == tenant_id,
                InstallmentSaleContractModel.installation_id == installation_id,
            )
        ).scalar_one_or_none()
        return _contract_from_row(row) if row is not None else None

    def list_contracts(
        self,
        *,
        tenant_id: str,
        installation_id: str,
        code: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InstallmentSaleContract]:
        stmt = (
            select(InstallmentSaleContractModel)
            .where(
                InstallmentSaleContractModel.tenant_id == tenant_id,
                InstallmentSaleContractModel.installation_id == installation_id,
            )
            .order_by(InstallmentSaleContractModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if code is not None:
            stmt = stmt.where(InstallmentSaleContractModel.code == code)
        rows = self._session.execute(stmt).scalars().all()
        return [_contract_from_row(row) for row in rows]

    # -- finance (US-541) ----------------------------------------------------

    def update_leg_principals(
        self,
        plan_id: str,
        principals: Mapping[str, MoneyIRR],
        *,
        tenant_id: str,
        installation_id: str,
    ) -> None:
        plan = self.get_plan(plan_id, tenant_id=tenant_id, installation_id=installation_id)
        if plan is None or not principals:
            return
        for leg_id, principal in principals.items():
            self._session.execute(
                update(InstallmentLegModel)
                .where(
                    InstallmentLegModel.id == leg_id,
                    InstallmentLegModel.plan_id == plan_id,
                )
                .values(principal_irr=principal.amount)
            )
        self._session.flush()

    def update_applied_advance(
        self,
        plan_id: str,
        applied_advance: MoneyIRR,
        *,
        tenant_id: str,
        installation_id: str,
    ) -> None:
        self._session.execute(
            update(InstallmentPlanModel)
            .where(
                InstallmentPlanModel.id == plan_id,
                InstallmentPlanModel.tenant_id == tenant_id,
                InstallmentPlanModel.installation_id == installation_id,
            )
            .values(applied_advance_irr=applied_advance.amount)
        )
        self._session.flush()

    def create_late_fee_assessment(self, assessment: LateFeeAssessment) -> LateFeeAssessment:
        self._session.add(
            InstallmentLateFeeAssessmentModel(
                id=assessment.id,
                leg_id=assessment.leg_id,
                plan_id=assessment.plan_id,
                tenant_id=assessment.tenant_id,
                installation_id=assessment.installation_id,
                assessed_through=assessment.assessed_through.gregorian_date,
                days_overdue=assessment.days_overdue,
                fee_irr=assessment.fee.amount,
                journal_move_id=assessment.journal_move_id,
                journal_state=assessment.journal_state.value,
                created_at=assessment.created_at,
            )
        )
        self._session.flush()
        return assessment

    def list_late_fee_assessments(
        self, leg_id: str, *, tenant_id: str, installation_id: str
    ) -> list[LateFeeAssessment]:
        rows = self._session.execute(
            select(InstallmentLateFeeAssessmentModel)
            .where(
                InstallmentLateFeeAssessmentModel.leg_id == leg_id,
                InstallmentLateFeeAssessmentModel.tenant_id == tenant_id,
                InstallmentLateFeeAssessmentModel.installation_id == installation_id,
            )
            .order_by(
                InstallmentLateFeeAssessmentModel.assessed_through.desc(),
                InstallmentLateFeeAssessmentModel.created_at.desc(),
            )
        ).scalars().all()
        return [_assessment_from_row(row) for row in rows]

    def create_schedule_amendment(self, amendment: ScheduleAmendment) -> ScheduleAmendment:
        self._session.add(
            InstallmentScheduleAmendmentModel(
                id=amendment.id,
                plan_id=amendment.plan_id,
                tenant_id=amendment.tenant_id,
                installation_id=amendment.installation_id,
                kind=amendment.kind.value,
                applied_advance_irr=amendment.applied_advance.amount,
                legs_before=_snapshots_to_json(amendment.legs_before),
                legs_after=_snapshots_to_json(amendment.legs_after),
                receipt_id=amendment.receipt_id,
                created_at=amendment.created_at,
            )
        )
        self._session.flush()
        return amendment

    def list_schedule_amendments(
        self, plan_id: str, *, tenant_id: str, installation_id: str
    ) -> list[ScheduleAmendment]:
        rows = self._session.execute(
            select(InstallmentScheduleAmendmentModel)
            .where(
                InstallmentScheduleAmendmentModel.plan_id == plan_id,
                InstallmentScheduleAmendmentModel.tenant_id == tenant_id,
                InstallmentScheduleAmendmentModel.installation_id == installation_id,
            )
            .order_by(InstallmentScheduleAmendmentModel.created_at.asc())
        ).scalars().all()
        return [_amendment_from_row(row) for row in rows]
