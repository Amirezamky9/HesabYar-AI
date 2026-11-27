"""Data-migration reconciliation checks (US-705).

The executable form of ``docs/data-migration.md`` §4: after a
store's data is migrated in (generic connector backfill + manual
import), these checks prove the migrated books reconcile — or say
exactly where they do not. A failed check is a discrepancy for the
review queue, never a silent skip: every check returns PASS, FAIL
or SKIPPED with a detail, and an unexpected error inside a check
FAILS it (the restore drill's ``_run_check`` pattern).

Four of the five checks are the US-703 drill checks, reused — not
duplicated — because a migration is verified the same way a
restore is:

- ``hesabyar.orphaned_documents`` — no imported row references a
  missing row (``restore_drill.check_orphaned_documents``);
- ``hesabyar.settlement_tranches`` — every payment's tranches tie
  out (``restore_drill.check_settlement_tranches``);
- ``tryton.trial_balance`` — every company's trial balance balances
  (``restore_drill.check_trial_balance``);
- ``cross_database.external_references`` — every external-id-map
  row pointing at Tryton resolves (``restore_drill.check_external_references``).

The one new check is the aging tie-out, which the drill does not
run:

- ``migration.notes_aging`` — the notes-receivable/payable aging
  report (cheque report 7, ``AccountingEnginePort.get_notes_balance``)
  ties out: per party-row Σ buckets == row total, and the totals
  row's buckets == its total AND == Σ row totals. The read goes
  through the engine port (Tryton REST), never SQL against Tryton's
  database (spec §19 rule 2). All amounts are MoneyIRR integers
  (digits=0); no floats, no Rial/Toman conversion.

What this module deliberately does NOT do: file review items. The
runner reports; the operator (or the migration runbook's
discrepancy procedure) files the review item for each failure with
the check's detail attached. Filing needs a tenant + the review
service, and a reconciliation report must stay readable without
one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.engine import Engine

from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.accounting.value_objects import NotesBalanceRowView
from hesabyar.domain.backup.models import DrillCheckResult, DrillCheckStatus
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.infrastructure.operations.restore_drill import (
    CHECK_EXTERNAL_REFS,
    CHECK_ORPHANS,
    CHECK_TRANCHES,
    CHECK_TRIAL_BALANCE,
    check_external_references,
    check_orphaned_documents,
    check_settlement_tranches,
    check_trial_balance,
)

logger = logging.getLogger(__name__)

#: Check names (stable strings — the migration doc and the tests use them).
CHECK_NOTES_AGING = "migration.notes_aging"

#: Default cap on cross-database reference checks per run (the same
#: default as the restore drill's MAX_EXTERNAL_REFS_CHECKED).
DEFAULT_MAX_EXTERNAL_REFS_CHECKED = 200


def _pass(name: str, detail: str = "") -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.PASS, detail=detail)


def _fail(name: str, detail: str) -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.FAIL, detail=detail)


def _skip(name: str, detail: str) -> DrillCheckResult:
    return DrillCheckResult(name=name, status=DrillCheckStatus.SKIPPED, detail=detail)


#: The six aging buckets of a notes-balance row, in order.
_AGING_BUCKETS: tuple[str, ...] = (
    "bucket_not_yet_due",
    "bucket_0_30",
    "bucket_30_60",
    "bucket_60_90",
    "bucket_90_120",
    "bucket_120_plus",
)


def _bucket_sum(row: NotesBalanceRowView) -> int:
    """Σ of a row's six aging buckets, in Rial (plain ints, no floats)."""
    return sum(getattr(row, bucket).amount for bucket in _AGING_BUCKETS)


def check_notes_aging(
    engine: AccountingEnginePort,
    *,
    as_of: FiscalDate,
) -> DrillCheckResult:
    """The notes aging report ties out, per company (cheque report 7).

    Per party-row: Σ buckets == row total. Per company: the totals
    row's Σ buckets == its total, and Σ row totals == the totals
    total. A company whose chart lacks the notes accounts
    (1.2.4/1.2.5/2.1.5) reads an honest empty report — it is
    skipped, not failed, and named in the detail. No companies at
    all, or no company with notes accounts, SKIPS the check: there
    is nothing to tie out, and the report says so.
    """
    try:
        companies = engine.list_companies()
    except Exception as exc:
        return _fail(CHECK_NOTES_AGING, f"Could not list companies: {exc}")
    if not companies:
        return _skip(
            CHECK_NOTES_AGING,
            "No companies in the engine — nothing to age.",
        )
    mismatches: list[str] = []
    details: list[str] = []
    skipped_companies: list[str] = []
    for company in companies:
        try:
            view = engine.get_notes_balance(company.ref, as_of)
        except Exception as exc:
            return _fail(
                CHECK_NOTES_AGING,
                f"Could not read the notes aging for {company.name}: {exc}",
            )
        if not view.accounts_read:
            skipped_companies.append(company.name)
            continue
        rows_checked = 0
        for row in view.rows:
            rows_checked += 1
            bucket_sum = _bucket_sum(row)
            if bucket_sum != row.total.amount:
                mismatches.append(
                    f"{company.name} / {row.party_name} ({row.instrument}): "
                    f"Σ buckets {bucket_sum} != total {row.total.amount} IRR"
                )
        if view.totals is not None:
            totals = view.totals
            totals_bucket_sum = _bucket_sum(totals)
            if totals_bucket_sum != totals.total.amount:
                mismatches.append(
                    f"{company.name} / totals: Σ buckets {totals_bucket_sum} "
                    f"!= total {totals.total.amount} IRR"
                )
            rows_total = sum(row.total.amount for row in view.rows)
            if rows_total != totals.total.amount:
                mismatches.append(
                    f"{company.name} / totals: Σ row totals {rows_total} "
                    f"!= total {totals.total.amount} IRR"
                )
        details.append(
            f"{company.name}: {rows_checked} aging row(s) tie out "
            f"(accounts: {', '.join(view.accounts_read)})"
        )
    if mismatches:
        return _fail(
            CHECK_NOTES_AGING,
            f"{len(mismatches)} aging tie-out problem(s): " + "; ".join(mismatches),
        )
    if not details:
        return _skip(
            CHECK_NOTES_AGING,
            "No company has notes accounts (1.2.4/1.2.5/2.1.5): "
            + ", ".join(skipped_companies),
        )
    detail = "; ".join(details)
    if skipped_companies:
        detail += f" (no notes accounts, skipped: {', '.join(skipped_companies)})"
    return _pass(CHECK_NOTES_AGING, detail)


def _run_check(
    name: str, func: Callable[..., DrillCheckResult], *args: Any, **kwargs: Any
) -> DrillCheckResult:
    """Run one check; an unexpected error FAILS it (never aborts the run).

    The restore drill's ``_run_check`` pattern, duplicated here so
    this module does not depend on the drill service class.
    """
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        logger.exception("Migration reconciliation check %s raised.", name)
        return _fail(name, f"Check raised unexpectedly: {exc}")


def run_migration_reconciliation(
    *,
    hesabyar_engine: Engine,
    tryton_engine: AccountingEnginePort,
    as_of: FiscalDate,
    max_external_refs: int = DEFAULT_MAX_EXTERNAL_REFS_CHECKED,
) -> list[DrillCheckResult]:
    """Run every migration reconciliation check, in stable order.

    HesabYar-side checks first (orphans, tranches), then the
    cross-database check, then the Tryton-side checks (trial
    balance, notes aging). A failure anywhere is a discrepancy for
    the review queue (see ``docs/data-migration.md`` §5) — the
    caller files it; this function only reports.
    """
    return [
        _run_check(CHECK_ORPHANS, check_orphaned_documents, hesabyar_engine),
        _run_check(CHECK_TRANCHES, check_settlement_tranches, hesabyar_engine),
        _run_check(
            CHECK_EXTERNAL_REFS,
            check_external_references,
            hesabyar_engine,
            tryton_engine,
            max_refs=max_external_refs,
        ),
        _run_check(CHECK_TRIAL_BALANCE, check_trial_balance, tryton_engine),
        _run_check(CHECK_NOTES_AGING, check_notes_aging, tryton_engine, as_of=as_of),
    ]
