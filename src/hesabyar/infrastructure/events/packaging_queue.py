"""Procrastinate wiring for the packaging supply check (US-515).

One periodic job, ``packaging.supply_check`` (daily, 06:30 by
default — after the Tryton supply wizard's 06:00 ``ir.cron`` run):
it discovers every company with saved packaging settings and runs
the supply check for each — read the supply wizard's results (new
``purchase.request`` rows since the watermark) + live days-of-cover,
and file ``packaging_reorder`` review items (deduplicated against
open items).

The job NEVER runs the wizard itself (the wizard runs inside Tryton
via ``ir.cron`` + ``trytond-cron``) and never posts anything. Each
company commits separately: one company's failure (engine down,
settings broken) is logged and never blocks the others.

US-560: the job is registered (``packaging.supply_check``, daily
10:00 Tehran, Friday skipped by default — after the Tryton supply
wizard's 06:00 ``ir.cron`` run on Saturday, the check reads both
days' results). When a recorder is wired, each run is gated and
recorded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import procrastinate

from hesabyar.application.commands.packaging import PackagingCommandService
from hesabyar.domain.accounting.refs import CompanyRef
from hesabyar.domain.scheduler.schedules import tehran_today
from hesabyar.infrastructure.events.scheduler_queue import (
    JobRunRecorder,
    run_recorded,
)

logger = logging.getLogger(__name__)

#: Queue name for packaging jobs.
PACKAGING_QUEUE = "packaging"

#: Periodic job: run the daily supply check for every configured company.
SUPPLY_CHECK_TASK = "packaging.supply_check"

#: Default cadence (daily 06:30 — after the Tryton supply wizard's
#: 06:00 ir.cron run; overridable via HESABYAR_PACKAGING__SUPPLY_CHECK_CRON).
SUPPLY_CHECK_CRON = "30 6 * * *"

#: Actor recorded on review items filed by the scheduled job.
SUPPLY_CHECK_ACTOR = "packaging-worker"


@dataclass(frozen=True)
class PackagingCompany:
    """One company in scope for the supply check."""

    tenant_id: str
    company_id: str  # HesabYar engine_companies row id
    engine_id: int  # Tryton company.company id


@dataclass
class PackagingWorkerHandle:
    """A packaging command service bound to one session, with commit/close.

    The task function builds one handle per job run: the service
    flushes per company, the handle commits per company, and the
    session is always closed.
    """

    commands: PackagingCommandService
    companies: Callable[[], Sequence[PackagingCompany]]
    commit: Callable[[], None]
    close: Callable[[], None]


#: Factory building a fresh packaging handle per job run (the worker
#: script supplies it; it owns settings, engine and session).
PackagingWorkerFactory = Callable[[], PackagingWorkerHandle]


def register_packaging_tasks(
    app: procrastinate.App,
    packaging_factory: PackagingWorkerFactory,
    *,
    supply_check_cron: str = SUPPLY_CHECK_CRON,
    recorder: JobRunRecorder | None = None,
) -> None:
    """Register the packaging supply-check task on a Procrastinate app."""

    @app.periodic(cron=supply_check_cron)
    @app.task(queue=PACKAGING_QUEUE, name=SUPPLY_CHECK_TASK, retry=False)
    def supply_check(timestamp: int = 0) -> None:  # noqa: ARG001 - periodic passes the schedule timestamp
        handle = packaging_factory()
        day = tehran_today(timestamp)
        try:

            def _do() -> dict[str, Any]:
                checked = 0
                new_requests = 0
                items_filed = 0
                skipped_open = 0
                errors: list[str] = []
                companies = handle.companies()
                for company in companies:
                    try:
                        report = handle.commands.run_supply_check(
                            tenant_id=company.tenant_id,
                            company=CompanyRef(company.engine_id),
                            company_id=company.company_id,
                            actor=SUPPLY_CHECK_ACTOR,
                        )
                        handle.commit()
                        logger.info(
                            "Supply check done for company %s: checked=%d new_requests=%d "
                            "items_filed=%d skipped_open=%d errors=%d",
                            company.company_id,
                            report.materials_checked,
                            report.purchase_requests_new,
                            report.items_filed,
                            report.items_skipped_open,
                            len(report.errors),
                        )
                        checked += report.materials_checked
                        new_requests += report.purchase_requests_new
                        items_filed += report.items_filed
                        skipped_open += report.items_skipped_open
                        errors.extend(report.errors)
                        for error in report.errors:
                            logger.warning(
                                "Supply check error for %s: %s", company.company_id, error
                            )
                    except Exception:
                        # One company's failure never blocks the others.
                        logger.exception(
                            "Supply check failed for company %s", company.company_id
                        )
                        errors.append(f"{company.company_id}: failed")
                return {
                    "materials_checked": checked,
                    "purchase_requests_new": new_requests,
                    "items_filed": items_filed,
                    "items_skipped_open": skipped_open,
                    "errors": errors,
                }

            run_recorded(recorder, SUPPLY_CHECK_TASK, _do, today=day)
            handle.commit()
        finally:
            handle.close()
