"""Report validation use cases (US-403).

Runs the standards rules over the LIVE trial balance: the service
fetches the company's posted-move trial balance through the engine
port, builds the DSL document from it, delegates rule execution to
the US-401 :class:`StandardsValidationService` (so the Standard 15
BLOCKED_SOURCE_GAP gate, effective-date applicability and coverage
reporting are exactly the same as a plain validation run), and
persists a report snapshot so every figure in the run is traceable to
the engine read that produced it.

The service talks to the engine ONLY through the domain
``AccountingEnginePort``, to the registry only through the validation
service, and to snapshots only through the domain
``ReportSnapshotPort`` — it never imports infrastructure (spec §6.3).

Honest limits (spec §12.1, ARCHITECTURE.md §9.4):

- A trial balance alone does not contain statement-level figures
  (``total_assets``, ``revenue`` ...), so the statement rules that
  need them yield ERROR, never PASS — the DSL enforces this and the
  service preserves it. The run says so per rule; the snapshot's
  payload is the document that was evaluated, so the gap is
  reproducible, not hidden.
- The trial balance must balance (posted moves are balanced by the
  engine). An unbalanced trial balance raises
  :class:`UnbalancedTrialBalanceError` and no run is built on it.
- P&L, balance sheet and cash flow are NOT built here — only the
  trial balance closes the Phase 2 ``get_report`` gap.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from hesabyar.application.commands.standards import StandardsValidationService
from hesabyar.application.errors import (
    ReportValidationRunNotFoundError,
    UnbalancedTrialBalanceError,
    translate_port_errors,
)
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.accounting.value_objects import TrialBalanceView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.reports.models import ReportSnapshot, ReportValidationRun
from hesabyar.domain.reports.ports import ReportSnapshotPort


def build_trial_balance_document(trial_balance: TrialBalanceView) -> dict[str, Any]:
    """Build the DSL document a validation run evaluates over a trial balance.

    The document is JSON-serializable (ints, strings, bools, mappings
    only — never floats, never engine records) and is exactly the
    snapshot payload: the run's ``input_hash`` is the SHA-256 of this
    document's canonical JSON, so the run is reproducible from its
    snapshot alone. Per-account balances live under
    ``trial_balance.accounts`` keyed by account code; totals are also
    top-level for the rules that read flat field names.
    """
    total_debit = trial_balance.total_debit.amount
    total_credit = trial_balance.total_credit.amount
    return {
        "trial_balance": {
            "total_debit": total_debit,
            "total_credit": total_credit,
            "balance": total_debit - total_credit,
            "balanced": trial_balance.is_balanced,
            "accounts_count": len(trial_balance.lines),
            "moves_count": trial_balance.move_count,
            "lines_count": trial_balance.line_count,
            "accounts": {
                line.code: {
                    "name": line.name,
                    "debit": line.total_debit.amount,
                    "credit": line.total_credit.amount,
                    "balance": line.balance.amount,
                }
                for line in trial_balance.lines
            },
        },
        "total_debit": total_debit,
        "total_credit": total_credit,
    }


class ReportValidationService:
    """Create and read report validation runs over a live trial balance."""

    def __init__(
        self,
        engine: AccountingEnginePort,
        validation: StandardsValidationService,
        snapshots: ReportSnapshotPort,
    ) -> None:
        self._engine = engine
        self._validation = validation
        self._snapshots = snapshots

    def create_report_validation_run(
        self,
        *,
        tenant_id: str,
        company: CompanyRef,
        company_id: str | None = None,
        period_start: date,
        period_end: date,
        standard_codes: Sequence[str] | None = None,
    ) -> ReportValidationRun:
        """Validate the company's live trial balance and persist run + snapshot.

        The Standard 15 gate fires inside the delegated validation run
        (a 1405+ period — or a period the gate cannot establish — raises
        :class:`StandardSourceBlockedError` before anything is
        persisted). The snapshot port flushes the snapshot; the caller
        owns the transaction, like every other service that writes.
        """
        if period_end < period_start:
            raise ValueError("period_end must be on or after period_start.")
        with translate_port_errors():
            trial_balance = self._engine.get_trial_balance(
                company,
                FiscalDate(period_start),
                FiscalDate(period_end),
            )
        if not trial_balance.is_balanced:
            raise UnbalancedTrialBalanceError(
                "The trial balance does not balance: total debits "
                f"({trial_balance.total_debit.amount}) do not equal total "
                f"credits ({trial_balance.total_credit.amount}) over posted "
                "moves. No validation run was built on these figures.",
                details={
                    "company_id": company.engine_id,
                    "total_debit": trial_balance.total_debit.amount,
                    "total_credit": trial_balance.total_credit.amount,
                },
            )
        document = build_trial_balance_document(trial_balance)
        run = self._validation.create_run(
            tenant_id=tenant_id,
            company_id=company_id,
            period_start=period_start,
            period_end=period_end,
            standard_codes=standard_codes,
            data=document,
        )
        snapshot = ReportSnapshot(
            snapshot_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            company_id=company_id,
            validation_run_id=run.run_id,
            period_start=period_start,
            period_end=period_end,
            as_of_date=period_end,
            source_endpoint=trial_balance.source_endpoint,
            ruleset_version=run.ruleset_version,
            payload_hash=run.input_hash,
            payload=document,
            balanced=trial_balance.is_balanced,
            total_debit=trial_balance.total_debit.amount,
            total_credit=trial_balance.total_credit.amount,
        )
        saved = self._snapshots.save_snapshot(snapshot)
        return ReportValidationRun(run=run, snapshot=saved)

    def get_report_validation_run(self, run_id: str, tenant_id: str) -> ReportValidationRun:
        """Return one report validation run with its snapshot and results.

        A run with no trial-balance snapshot (e.g. one created by the
        plain standards endpoint) or of another tenant looks exactly
        like a run that does not exist.
        """
        snapshot = self._snapshots.get_snapshot_for_run(run_id, tenant_id)
        if snapshot is None:
            raise ReportValidationRunNotFoundError(
                f"Report validation run {run_id!r} does not exist.",
                details={"validation_run_id": run_id},
            )
        run = self._validation.get_run(run_id, tenant_id)
        return ReportValidationRun(run=run, snapshot=snapshot)
