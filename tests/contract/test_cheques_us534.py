"""Live contract tests for US-534 (Phase 5-4 / ۵ث): Sayyad clearing.

Run against the LIVE trytond server and the ``hesabyar_tryton_test``
database, using ONLY the dedicated test company "HesabYar Adapter
Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``).
They exercise the real ``account_check_ir`` module through the
adapter:

1. ``clear`` + clearing-leg reconciliation: after deposit (سند ۲)
   and clear (سند ۳), the deposit move's clearing-account debit
   reconciles with the clear move's clearing-account credit via
   ``reconcile_move_lines`` — the exact-match apply path, live.
   This is the risky step: it proves draft (unposted) cheque moves
   carry ``valid`` lines that Tryton's ``Line.reconcile`` accepts.
2. A real draft statement's deposit line (amount + leaf number in
   the Persian description) matches a live in-clearing cheque with
   confidence ``exact`` through the pure matcher — the Layer 3
   keys, read back from the engine.

When the server is not reachable, or the module / chart accounts /
statement journal are missing, every test SKIPS cleanly with a
clear message.

Configuration (all optional; LOCAL lane defaults — US-534 runs on
the local DB, lane 1 is US-532's, lane 2 is US-533's):
  TRYTON_BASE_URL       (default http://localhost:8020)
  TRYTON_DATABASE       (default hesabyar_tryton_test)
  TRYTON_API_KEY        (default hesabyar-dev-rest-key)
  TRYTON_TEST_COMPANY   (default "HesabYar Adapter Test")
"""

from __future__ import annotations

import datetime
import os
import time
from collections.abc import Generator

import pytest

from hesabyar.domain.accounting.commands import (
    CreateBankStatement,
    CreateBankStatementLine,
    CreateEngineCheque,
    ReconcileMoveLines,
    TransitionEngineCheque,
)
from hesabyar.domain.accounting.refs import (
    AccountRef,
    BankStatementRef,
    CompanyRef,
    JournalDraftRef,
    MoveLineRef,
    PartyRef,
    StatementJournalRef,
)
from hesabyar.domain.cheques.clearing import (
    ClearingCandidate,
    ClearingConfidence,
    ClearingLine,
    match_clearing_line,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8020")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

CHEQUE_ACCOUNT_CODES = ("1.2.4", "1.2.5", "2.1.5", "1.2.9")
DESTINATION_ACCOUNT_CODE = "1.1.1"
AMOUNT = MoneyIRR(17_500_000)
ISSUE = FiscalDate.from_gregorian(datetime.date(2026, 11, 17))
DUE = FiscalDate.from_gregorian(datetime.date(2027, 1, 5))
LEAF_NUMBER = "123456"


def _unique_sayad_id() -> str:
    """A fresh 16-digit Sayyad id per call (time-based, never reused)."""
    return f"{time.time_ns() % 10**15:015d}1"


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY)
    if not rest_client.ping():
        rest_client.close()
        pytest.skip(
            f"trytond server not reachable at {BASE_URL} "
            "(start it per docs/dev-setup.md §3 to run the contract tests)"
        )
    yield rest_client
    rest_client.close()


@pytest.fixture(scope="module")
def adapter(client: TrytonRestClient) -> TrytonAccountingAdapter:
    return TrytonAccountingAdapter(client)


@pytest.fixture(scope="module")
def company(adapter: TrytonAccountingAdapter) -> CompanyRef:
    for view in adapter.list_companies():
        if view.name == TEST_COMPANY_NAME:
            return view.ref
    pytest.skip(
        f"Test company {TEST_COMPANY_NAME!r} not found in Tryton DB {DATABASE!r}. "
        "Provision it with setup_adapter_test_company.py (see docs/adapter.md)."
    )


@pytest.fixture(scope="module")
def chart(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    missing = [code for code in CHEQUE_ACCOUNT_CODES if code not in chart]
    if missing:
        pytest.skip(
            "Cheque chart accounts missing from test company: "
            f"{missing}. Run update_chart (US-530 install procedure)."
        )
    if DESTINATION_ACCOUNT_CODE not in chart:
        pytest.skip(
            f"Destination account {DESTINATION_ACCOUNT_CODE} missing from test company."
        )
    return chart


@pytest.fixture(scope="module")
def module_active(client: TrytonRestClient) -> None:
    records = client.search(
        "ir.module",
        domain=[("name", "=", "account_check_ir")],
        fields=["name", "state"],
    )
    if not records or records[0].get("state") != "activated":
        pytest.skip(
            "account_check_ir is not activated in this Tryton DB "
            "(run the US-530 install procedure first)."
        )


@pytest.fixture(scope="module")
def issuer_party(client: TrytonRestClient, company: CompanyRef) -> PartyRef:
    context = {"company": company.engine_id}
    existing = client.search(
        "party.party",
        domain=[("code", "=", "0012345678")],
        fields=["id"],
        context=context,
    )
    if existing:
        return PartyRef(engine_id=int(existing[0]["id"]))
    party = client.create(
        "party.party",
        {"name": "US-534 Test Issuer", "code": "0012345678"},
        context=context,
    )
    return PartyRef(engine_id=int(party["id"]))


@pytest.fixture(scope="module")
def destination_account(chart) -> AccountRef:
    return chart[DESTINATION_ACCOUNT_CODE].ref


def _create(adapter, company, party=None, **overrides):
    kwargs = {
        "company": company,
        "kind": "received",
        "purpose": "payment",
        "sayad_id": _unique_sayad_id(),
        "amount": AMOUNT,
        "issue_date": ISSUE,
        "due_date": DUE,
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": "00123456789",
        "order_id": "ord-534",
        "customer_ref": "cust-534",
        "sayad_registered": True,
        "source": "manual",
        "leaf_number": LEAF_NUMBER,
        "party": party,
    }
    kwargs.update(overrides)
    return adapter.create_engine_cheque(CreateEngineCheque(**kwargs))


def _transition(adapter, company, view, transition, **kwargs):
    return adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=view.ref,
            company=company,
            transition=transition,
            auto_post=False,
            **kwargs,
        )
    )


def _in_clearing(adapter, company, destination_account, party=None):
    view = _create(adapter, company, party=party)
    held = _transition(adapter, company, view, "confirm_receipt", storage_location="vault")
    return _transition(
        adapter, company, held, "deposit", destination_account=destination_account
    )


def _clearing_legs(adapter, company, move_id, amount_irr):
    """The clearing-account legs of one move: (debit leg, credit leg)."""
    lines = adapter.list_move_lines(company, move=JournalDraftRef(move_id))
    debit = next(
        (line for line in lines if line.balance.amount == amount_irr), None
    )
    credit = next(
        (line for line in lines if line.balance.amount == -amount_irr), None
    )
    return debit, credit


def test_clear_and_reconcile_clearing_legs_live(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    module_active: None,
    destination_account: AccountRef,
    issuer_party: PartyRef,
) -> None:
    """سند ۲ + سند ۳ live: the clearing legs reconcile via Line.reconcile."""
    in_clearing = _in_clearing(adapter, company, destination_account, party=issuer_party)
    assert in_clearing.state == "in_clearing"
    deposit_move_id = in_clearing.move_ids["deposit"]
    cleared = _transition(
        adapter, company, in_clearing, "clear", statement_reference="us534-live"
    )
    assert cleared.state == "cleared"
    clear_move_id = cleared.move_ids["clear"]
    deposit_debit, _ = _clearing_legs(adapter, company, deposit_move_id, AMOUNT.amount)
    _, clear_credit = _clearing_legs(adapter, company, clear_move_id, AMOUNT.amount)
    assert deposit_debit is not None, "deposit move has no clearing-account debit leg"
    assert clear_credit is not None, "clear move has no clearing-account credit leg"
    assert deposit_debit.account.engine_id == clear_credit.account.engine_id
    assert deposit_debit.party == clear_credit.party
    assert deposit_debit.state == "valid", "draft-move lines must be 'valid' to reconcile"
    assert clear_credit.state == "valid"
    result = adapter.reconcile_move_lines(
        ReconcileMoveLines(
            company=company,
            lines=(
                MoveLineRef(deposit_debit.ref.engine_id),
                MoveLineRef(clear_credit.ref.engine_id),
            ),
        )
    )
    assert result.ref.engine_id > 0
    # Both legs are reconciled now.
    after = adapter.list_move_lines(company, move=JournalDraftRef(deposit_move_id))
    reconciled = [line for line in after if line.ref.engine_id == deposit_debit.ref.engine_id]
    assert reconciled and reconciled[0].reconciliation is not None


def test_real_statement_line_matches_live_cheque_exact(
    adapter: TrytonAccountingAdapter,
    client: TrytonRestClient,
    company: CompanyRef,
    module_active: None,
    destination_account: AccountRef,
    issuer_party: PartyRef,
    request: pytest.FixtureRequest,
) -> None:
    """A draft statement's deposit line matches the live cheque with confidence exact."""
    journals = adapter.list_statement_journals(company)
    journal = next(
        (j for j in journals if j.account.engine_id == destination_account.engine_id),
        None,
    )
    if journal is None:
        pytest.skip(
            "No statement journal for the destination account in the test "
            "company — cannot create a live statement for it."
        )
    in_clearing = _in_clearing(adapter, company, destination_account, party=issuer_party)
    today = FiscalDate(datetime.date.today())
    statement_ref = adapter.create_bank_statement(
        CreateBankStatement(
            company=company,
            journal=StatementJournalRef(journal.ref.engine_id),
            name="US-534 clearing statement",
            date=today,
            # The test company's cash journal is balance-validated:
            # the statement must carry its start/end balances.
            start_balance=MoneyIRR(0),
            end_balance=AMOUNT,
            lines=(
                CreateBankStatementLine(
                    date=today,
                    amount=AMOUNT,
                    account=destination_account,
                    description=f"وصول چک {LEAF_NUMBER}",
                ),
            ),
        )
    )
    # Delete the draft statement afterwards: it is dated the run day,
    # so a leaked copy outranks every other test's statement in the
    # shared company's newest-first list — it broke
    # test_phase2_full_cycle_e2e's latest-statement assertion in CI
    # run #1 (latest was this statement's id 1, not the phase2
    # statement's id 3).
    def _delete_statement() -> None:
        try:
            client.delete("account.statement", statement_ref.engine_id)
        except Exception:
            pass

    request.addfinalizer(_delete_statement)
    view = adapter.get_bank_statement(BankStatementRef(statement_ref.engine_id))
    assert view is not None
    assert view.state == "draft"
    assert len(view.lines) == 1
    line = view.lines[0]
    match = match_clearing_line(
        ClearingLine(
            line_id=line.id,
            statement_id=view.ref.engine_id,
            date=line.date,
            amount_irr=line.amount.amount,
            description=line.description,
            number=line.number,
            destination_account_id=journal.account.engine_id,
        ),
        [
            ClearingCandidate(
                cheque_id="live-cheque",
                sayad_id=in_clearing.sayad_id,
                leaf_number=LEAF_NUMBER,
                amount_irr=AMOUNT.amount,
                deposit_date=today,
                destination_bank_account_id=destination_account.engine_id,
                direction="in",
                kind="received",
            )
        ],
        clearing_window_days=7,
    )
    assert match.confidence == ClearingConfidence.EXACT
    assert match.leaf_matched is True
