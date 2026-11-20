"""Unit tests for the company-scoped cheque queries (US-551).

DB-free: the query service runs against the in-memory fake from
``tests.unit.test_cheque_services``. The company filter is a
BACKEND filter — the dashboard and the cheque center pass the
active company and the repository scopes the rows; ``None`` keeps
the tenant-wide default. The SQL implementation is covered by
``tests/infrastructure/test_cheque_repository.py`` and the REST
surface by ``tests/interfaces/test_rest_cheques.py``.
"""

from __future__ import annotations

from typing import cast

import pytest

from hesabyar.application.commands.cheques import ChequeCommandService
from hesabyar.application.commands.review_queue import ReviewQueueCommandService
from hesabyar.application.queries.cheques import ChequeQueryService
from hesabyar.domain.accounting.ports import AccountingEnginePort
from hesabyar.domain.cheques.models import Cheque, ChequeState
from tests.unit.test_cheque_services import (
    ACTOR,
    COMPANY_ENGINE_ID,
    INSTALLATION_ID,
    TENANT_ID,
    FakeChequeRepository,
    FakeEngine,
    RecordingReview,
    _payload,
)

OTHER_COMPANY_ENGINE_ID = 3
UNKNOWN_COMPANY_ENGINE_ID = 99


@pytest.fixture()
def world() -> tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService]:
    repo = FakeChequeRepository()
    engine = FakeEngine()
    review = RecordingReview()
    commands = ChequeCommandService(
        repo,
        cast(AccountingEnginePort, engine),
        review=cast(ReviewQueueCommandService, review),
    )
    return repo, commands, ChequeQueryService(repo, cast(AccountingEnginePort, engine))


def _register_for(
    commands: ChequeCommandService, company_engine_id: int, **overrides: object
) -> Cheque:
    return commands.register_manual(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=company_engine_id,
        payload=_payload(**overrides),
        actor=ACTOR,
    )


def _held_for(
    commands: ChequeCommandService, company_engine_id: int, **overrides: object
) -> Cheque:
    cheque = _register_for(commands, company_engine_id, **overrides)
    return commands.confirm_receipt(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        items_matched=True,
        sayad_accepted=True,
        color_code=2,
    )


def _in_clearing_for(
    commands: ChequeCommandService, company_engine_id: int, **overrides: object
) -> Cheque:
    cheque = _held_for(commands, company_engine_id, **overrides)
    return commands.deposit(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        cheque_id=cheque.id,
        actor=ACTOR,
        destination_account_id=42,
        destination_bank_name="بانک سامان",
    )


# -- list_cheques ---------------------------------------------------------------


def test_list_without_company_is_tenant_wide(
    world: tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService],
) -> None:
    _, commands, queries = world
    _register_for(commands, COMPANY_ENGINE_ID, sayad_id="1111111111111111")
    _register_for(commands, OTHER_COMPANY_ENGINE_ID, sayad_id="2222222222222222")
    rows = queries.list_cheques(tenant_id=TENANT_ID, installation_id=INSTALLATION_ID)
    assert {c.sayad_id for c in rows} == {"1111111111111111", "2222222222222222"}


def test_list_scoped_to_one_company(
    world: tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService],
) -> None:
    _, commands, queries = world
    _register_for(commands, COMPANY_ENGINE_ID, sayad_id="1111111111111111")
    _register_for(commands, OTHER_COMPANY_ENGINE_ID, sayad_id="2222222222222222")
    rows = queries.list_cheques(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
    )
    assert [c.sayad_id for c in rows] == ["1111111111111111"]
    other = queries.list_cheques(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=OTHER_COMPANY_ENGINE_ID,
    )
    assert [c.sayad_id for c in other] == ["2222222222222222"]


def test_list_for_unknown_company_is_empty(
    world: tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService],
) -> None:
    _, commands, queries = world
    _register_for(commands, COMPANY_ENGINE_ID, sayad_id="1111111111111111")
    rows = queries.list_cheques(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=UNKNOWN_COMPANY_ENGINE_ID,
    )
    assert rows == []


def test_list_company_filter_composes_with_state(
    world: tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService],
) -> None:
    _, commands, queries = world
    _held_for(commands, COMPANY_ENGINE_ID, sayad_id="1111111111111111")
    _register_for(commands, COMPANY_ENGINE_ID, sayad_id="2222222222222222")
    _held_for(commands, OTHER_COMPANY_ENGINE_ID, sayad_id="3333333333333333")
    rows = queries.list_cheques(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        state=ChequeState.HELD,
        company_engine_id=COMPANY_ENGINE_ID,
    )
    assert [c.sayad_id for c in rows] == ["1111111111111111"]


def test_list_commercial_cheques_scoped_and_still_excludes_collateral(
    world: tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService],
) -> None:
    _, commands, queries = world
    _register_for(commands, COMPANY_ENGINE_ID, sayad_id="1111111111111111")
    _register_for(commands, COMPANY_ENGINE_ID, sayad_id="2222222222222222", purpose="collateral")
    _register_for(commands, OTHER_COMPANY_ENGINE_ID, sayad_id="3333333333333333")
    rows = queries.list_commercial_cheques(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
    )
    assert [c.sayad_id for c in rows] == ["1111111111111111"]


# -- widget queries -------------------------------------------------------------


def test_collateral_summary_scoped_to_one_company(
    world: tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService],
) -> None:
    _, commands, queries = world
    _held_for(
        commands,
        COMPANY_ENGINE_ID,
        sayad_id="1111111111111111",
        purpose="collateral",
        amount_toman=1000000,
    )
    _held_for(
        commands,
        OTHER_COMPANY_ENGINE_ID,
        sayad_id="2222222222222222",
        purpose="collateral",
        amount_toman=2000000,
    )
    scoped = queries.collateral_summary(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
    )
    assert scoped.received_total_irr == 10_000_000
    assert [c.sayad_id for c in scoped.cheques] == ["1111111111111111"]
    tenant_wide = queries.collateral_summary(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert tenant_wide.received_total_irr == 30_000_000
    assert len(tenant_wide.cheques) == 2


def test_in_clearing_aging_scoped_to_one_company(
    world: tuple[FakeChequeRepository, ChequeCommandService, ChequeQueryService],
) -> None:
    _, commands, queries = world
    _in_clearing_for(commands, COMPANY_ENGINE_ID, sayad_id="1111111111111111")
    _in_clearing_for(commands, OTHER_COMPANY_ENGINE_ID, sayad_id="2222222222222222")
    scoped = queries.list_in_clearing_aging(
        tenant_id=TENANT_ID,
        installation_id=INSTALLATION_ID,
        company_engine_id=COMPANY_ENGINE_ID,
    )
    assert [row.sayad_id for row in scoped] == ["1111111111111111"]
    tenant_wide = queries.list_in_clearing_aging(
        tenant_id=TENANT_ID, installation_id=INSTALLATION_ID
    )
    assert {row.sayad_id for row in tenant_wide} == {"1111111111111111", "2222222222222222"}
