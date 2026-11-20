"""Unit tests for the monthly clearing-match history (US-553, report 12).

The clearing service records every run's newly decided lines into
the month's history row; the query service reads the trend FROM the
stored rows. No database: the in-memory cheque fake + the clearing
service fakes from ``test_cheque_clearing_service``.
"""

from __future__ import annotations

import datetime

import pytest

from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.domain.accounting.refs import AccountRef, BankStatementRef
from hesabyar.domain.accounting.value_objects import (
    BankStatementLineView,
    BankStatementView,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from tests.unit.test_cheque_clearing_service import (
    AMOUNT_IRR,
    COMPANY,
    DESTINATION_ACCOUNT,
    World,
    _run,
    _statement,
    _world,
)
from tests.unit.test_cheque_services import (
    INSTALLATION_ID,
    TENANT_ID,
    _in_clearing,
)


def _statement_with_id(world: World, statement_id: int, **line_overrides: object) -> int:
    """A second draft statement (the shared helper hardcodes id 700)."""
    line_base: dict[str, object] = {
        "id": 9001,
        "date": FiscalDate(datetime.date(2026, 11, 20)),
        "amount": MoneyIRR(AMOUNT_IRR),
        "account": AccountRef(DESTINATION_ACCOUNT),
        "description": "وصول چک 123456",
    }
    line_base.update(line_overrides)
    world.engine.statements[statement_id] = BankStatementView(
        ref=BankStatementRef(statement_id),
        company=COMPANY,
        name="صورت‌حساب آبان",
        journal_name="بانک سامان",
        journal_id=700,
        date=FiscalDate(datetime.date(2026, 11, 20)),
        state="draft",
        validation="balance",
        currency_code="IRR",
        start_balance=None,
        end_balance=None,
        balance=None,
        total_amount=None,
        number_of_lines=1,
        lines=(BankStatementLineView(**line_base),),  # type: ignore[arg-type]
    )
    return statement_id


def _trend(world: World, **kwargs: object):
    service = ChequeQueryService(world.repo)
    params: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "installation_id": INSTALLATION_ID,
    }
    params.update(kwargs)
    return service.get_monthly_match_trend(**params)  # type: ignore[arg-type]


def _history(world: World):
    return world.repo.list_match_history(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )


# -- recording -----------------------------------------------------------------------


def test_exact_run_records_history_in_the_run_month() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    statement_id = _statement(world)
    result = _run(world, statement_id)
    assert result.exact_count == 1
    rows = _history(world)
    assert len(rows) == 1
    # TODAY is 2026-11-19 = 1405-08-28: the run belongs to Aban.
    assert rows[0].month_jalali == "1405-08"
    assert rows[0].exact_count == 1
    assert rows[0].probable_count == 0
    assert rows[0].total_lines == 1
    assert str(rows[0].match_rate) == "1.0000"


def test_probable_and_unmatched_runs_record_their_counts() -> None:
    world = _world()
    _in_clearing(world.cheques)
    statement_id = _statement(world, description="واریز وجه")
    result = _run(world, statement_id)
    assert result.probable_count == 1
    rows = _history(world)
    assert rows[0].probable_count == 1

    # A second statement with no matching cheque: unmatched.
    world2 = _world()
    statement_id = _statement(world2)
    result = _run(world2, statement_id)
    assert result.unmatched_count == 1
    rows = _history(world2)
    assert rows[0].unmatched_count == 1
    assert rows[0].total_lines == 1


def test_rerun_does_not_double_count_history() -> None:
    """A re-run's already_matched lines were recorded by the run that
    decided them — counting them again would inflate the month."""
    world = _world()
    cheque = _in_clearing(world.cheques)
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    statement_id = _statement(world)
    _run(world, statement_id)
    second = _run(world, statement_id)
    assert second.exact_count == 1  # the result still reports the line
    assert second.lines[0].action == "already_matched"
    rows = _history(world)
    assert len(rows) == 1
    assert rows[0].exact_count == 1
    assert rows[0].total_lines == 1


def test_two_runs_in_one_month_accumulate() -> None:
    world = _world()
    first = _in_clearing(world.cheques)
    first.leaf_number = "123456"
    world.repo.save(first)
    _run(world, _statement(world))
    second = _in_clearing(world.cheques, sayad_id="9999888877776666", order_id="ord-2")
    second.leaf_number = "654321"
    world.repo.save(second)
    _run(world, _statement_with_id(world, 701, description="وصول چک 654321"))
    rows = _history(world)
    assert len(rows) == 1
    assert rows[0].month_jalali == "1405-08"
    assert rows[0].exact_count == 2
    assert rows[0].total_lines == 2


def test_history_is_scoped_to_the_installation() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    _run(world, _statement(world))
    other = world.repo.list_match_history(
        tenant_id=TENANT_ID, installation_id="00000000-0000-0000-0000-000000000999"
    )
    assert other == []


# -- trend read ------------------------------------------------------------------------


def test_trend_reads_from_stored_history() -> None:
    world = _world()
    cheque = _in_clearing(world.cheques)
    cheque.leaf_number = "123456"
    world.repo.save(cheque)
    _run(world, _statement(world))
    trend = _trend(world)
    assert [m.month_jalali for m in trend.months] == ["1405-08"]
    assert trend.months[0].exact_count == 1
    assert trend.total_exact == 1
    assert trend.total_lines == 1
    assert trend.overall_match_rate == "1.0000"
    assert trend.freshness is not None
    assert trend.freshness.freshness_indicator == "stored"
    assert trend.freshness.source == "hesabyar:cheque_match_history"
    assert trend.freshness.calculation_version == "stored-history/1.0"


def test_trend_empty_history_is_an_honest_empty_trend() -> None:
    world = _world()
    trend = _trend(world)
    assert trend.months == ()
    assert trend.total_lines == 0
    assert trend.overall_match_rate == "0"
    assert trend.freshness is not None


def test_trend_rejects_zero_months() -> None:
    world = _world()
    with pytest.raises(ValueError, match="months must be at least 1"):
        _trend(world, months=0)
