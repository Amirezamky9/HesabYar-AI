"""Response schemas for the trial-balance + report-validation endpoints (US-403).

Responses map from the domain views — no ORM object ever leaks. Every
figure carries its provenance: the source endpoint it came from, the
snapshot id, the ruleset version and the as-of date. Amounts are
integer Rials (no floats anywhere in financial paths).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator

from hesabyar.domain.accounting.value_objects import TrialBalanceView
from hesabyar.domain.reports.models import ReportValidationRun
from hesabyar.interfaces.rest.schemas.standards import ValidationRunResponse


class TrialBalanceLineResponse(BaseModel):
    """One account's totals over posted moves in the trial-balance period."""

    account_id: int = Field(description="Engine account id")
    code: str = Field(description="Account code, e.g. '1.1.1'")
    name: str = Field(description="Account name")
    total_debit: int = Field(description="Total debits over posted moves in the period (IRR)")
    total_credit: int = Field(description="Total credits over posted moves in the period (IRR)")
    balance: int = Field(description="Signed balance: debit minus credit (IRR)")


class TrialBalanceResponse(BaseModel):
    """A trial balance read from POSTED moves only, with source metadata."""

    company_id: str = Field(description="Engine company id the trial balance was read for")
    date_from: date = Field(description="Start of the trial-balance period")
    date_to: date = Field(description="End of the trial-balance period")
    as_of_date: date = Field(description="As-of date of the trial balance (the period end)")
    lines: list[TrialBalanceLineResponse] = Field(description="Per-account totals, ordered by account code")
    total_debit: int = Field(description="Total debits over posted moves in the period (IRR)")
    total_credit: int = Field(description="Total credits over posted moves in the period (IRR)")
    balanced: bool = Field(description="True when total debits equal total credits")
    move_count: int = Field(description="Posted moves contributing lines in the period")
    line_count: int = Field(description="Posted move lines aggregated in the period")
    source_endpoint: str = Field(description="Engine read the figures came from")

    @classmethod
    def from_view(cls, view: TrialBalanceView, company_id: str) -> TrialBalanceResponse:
        """Build a trial-balance response from the domain view."""
        return cls(
            company_id=company_id,
            date_from=view.date_from.gregorian_date,
            date_to=view.date_to.gregorian_date,
            as_of_date=view.as_of_date.gregorian_date,
            lines=[
                TrialBalanceLineResponse(
                    account_id=line.account.engine_id,
                    code=line.code,
                    name=line.name,
                    total_debit=line.total_debit.amount,
                    total_credit=line.total_credit.amount,
                    balance=line.balance.amount,
                )
                for line in view.lines
            ],
            total_debit=view.total_debit.amount,
            total_credit=view.total_credit.amount,
            balanced=view.is_balanced,
            move_count=view.move_count,
            line_count=view.line_count,
            source_endpoint=view.source_endpoint,
        )


class ReportProvenanceResponse(BaseModel):
    """Provenance attached to every figure of a report validation run."""

    snapshot_id: str = Field(description="Report snapshot id the run was built on")
    source_endpoint: str = Field(description="Engine read the figures came from")
    as_of_date: date = Field(description="As-of date of the snapshot (the trial-balance period end)")
    payload_hash: str = Field(description="SHA-256 of the trial-balance document (equals the run's input hash)")
    ruleset_version: str = Field(description="Ruleset version the run executed")
    total_debit: int = Field(description="Trial-balance total debits (IRR)")
    total_credit: int = Field(description="Trial-balance total credits (IRR)")
    balanced: bool = Field(description="True when the trial balance balanced")


class ReportValidationRunResponse(BaseModel):
    """A report validation run: the standards run plus its provenance."""

    run: ValidationRunResponse = Field(description="Validation run with per-rule results and coverage")
    provenance: ReportProvenanceResponse = Field(description="Snapshot provenance of every figure in the run")
    note: str | None = Field(
        default=None,
        description="Persian note when some rules could not be evaluated from a trial balance alone",
    )

    @classmethod
    def from_record(cls, record: ReportValidationRun) -> ReportValidationRunResponse:
        """Build a report-run response from the domain record."""
        snapshot = record.snapshot
        run = record.run
        note: str | None = None
        if run.rules_errored > 0:
            note = (
                "برخی قواعد به ارقام صورت‌های مالی (مانند ترازنامه و صورت سود و زیان) "
                "نیاز دارند که در تراز آزمایشی وجود ندارد؛ این قواعد با وضعیت «خطا» "
                "ثبت شده‌اند و به‌معنای قبولی نیستند."
            )
        return cls(
            run=ValidationRunResponse.from_record(run),
            provenance=ReportProvenanceResponse(
                snapshot_id=snapshot.snapshot_id,
                source_endpoint=snapshot.source_endpoint,
                as_of_date=snapshot.as_of_date,
                payload_hash=snapshot.payload_hash,
                ruleset_version=snapshot.ruleset_version or "unknown",
                total_debit=snapshot.total_debit,
                total_credit=snapshot.total_credit,
                balanced=snapshot.balanced,
            ),
            note=note,
        )


class CreateReportValidationRunRequest(BaseModel):
    """Request to validate a company's live trial balance against the standards."""

    company_id: str = Field(
        description="API company id (HesabYar row id or engine company id)",
    )
    period_start: date = Field(
        description="Start of the validated period (drives effective-date applicability)",
    )
    period_end: date = Field(description="End of the validated period")
    standard_codes: list[str] | None = Field(
        default=None,
        description="Standard codes to validate (e.g. ['1']); null means all standards in the registry",
    )

    @model_validator(mode="after")
    def _check_period_range(self) -> CreateReportValidationRunRequest:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start.")
        return self
