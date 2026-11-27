"""Domain models for report validations (US-403).

A report snapshot is the reproducible record of one live trial balance
as read from the engine. It stores the trial-balance document itself
(the ``payload``: per-account debit/credit/balance + totals, exactly
the document the validation run evaluated), its SHA-256, the source
endpoint the figures came from, the ruleset version they were validated
against and the as-of date — so any figure in a run can be traced back
to the engine read that produced it, without re-running anything.

A report validation run joins a standards validation run
(``domain/standards``) to its snapshot: the run carries the per-rule
outcomes and coverage, the snapshot carries the provenance.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from hesabyar.domain.standards.models import ValidationRunRecord

#: The trial-balance source endpoint recorded on every snapshot.
TRIAL_BALANCE_SOURCE_ENDPOINT = "account.move.line (Tryton REST)"


@dataclass(frozen=True)
class ReportSnapshot:
    """A persisted snapshot of one live trial balance (``report_snapshots`` row).

    ``payload`` is the trial-balance document the validation run
    evaluated: ``{"trial_balance": {...}, "total_debit": ..., "total_credit": ...}``
    with per-account balances under ``trial_balance.accounts``. It is
    JSON-serializable (ints, strings, bools, mappings only — never
    floats, never engine records). ``payload_hash`` is the SHA-256 of
    its canonical JSON and equals the validation run's ``input_hash``:
    the run is reproducible from the snapshot alone.

    ``created_at`` is None until the port has stored the snapshot.
    """

    snapshot_id: str
    tenant_id: str
    company_id: str | None
    validation_run_id: str | None
    period_start: datetime.date | None
    period_end: datetime.date | None
    as_of_date: datetime.date
    source_endpoint: str
    ruleset_version: str | None
    payload_hash: str
    payload: dict[str, Any]
    balanced: bool
    total_debit: int
    total_credit: int
    created_at: datetime.datetime | None = None


@dataclass(frozen=True)
class ReportValidationRun:
    """A standards validation run over a live trial balance, with its snapshot.

    ``run`` is the persisted validation run (per-rule outcomes, counts,
    coverage snapshot); ``snapshot`` is the trial-balance snapshot the
    run was built on — its id, source endpoint, ruleset version and
    as-of date are the provenance attached to every figure in the run.
    """

    run: ValidationRunRecord
    snapshot: ReportSnapshot
