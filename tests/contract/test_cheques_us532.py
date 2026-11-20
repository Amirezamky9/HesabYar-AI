"""Live contract tests for US-532 (Phase 5-4 / ۵ث): issued + collateral.

Run against the LIVE trytond server and the ``hesabyar_tryton_test``
database, using ONLY the dedicated test company "HesabYar Adapter
Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``).
They exercise the HesabYar adapter against the real
``account_check_ir`` module through ``create`` + the
``transition_rest`` ``__rpc__`` action — the exact contract in
``docs/cheque-engine-contract.md``. Every transition is ONE engine
call; the test asserts the state AND its §2.4 journal move come
back together, and reads the move LINES back from
``account.move.line`` to assert the account CODES (not just the
roles) — including the US-532 correction: سند ۵/۱۰ debit Trade
Payables 2.1.1, NOT Notes Payable 2.1.5 (US-531 had the wrong code;
the live chart says 2.1.1 is Trade Payables, 2.1.5 is Notes Payable).

When the server is not reachable, or the module / chart accounts
are missing, every test SKIPS cleanly with a clear message.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8010 — lane 1)
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

from hesabyar.domain.accounting.commands import CreateEngineCheque, TransitionEngineCheque
from hesabyar.domain.accounting.refs import (
    AccountRef,
    CheckbookEngineRef,
    CompanyRef,
    PartyRef,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import TrytonAdapterError

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8010")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

#: Cheque chart accounts (US-502 / US-530 + US-532 corrections):
#: 1.2.1 Trade Receivables, 1.2.4 Notes Receivable Held, 1.2.5
#: Notes Receivable in Clearing, 1.2.9 Bounced Cheques, 2.1.1 Trade
#: Payables, 2.1.5 Notes Payable, 7.x collateral memorandum + contras.
CHEQUE_ACCOUNT_CODES = (
    "1.2.1",
    "1.2.4",
    "1.2.5",
    "1.2.9",
    "2.1.1",
    "2.1.5",
    "7.1.1",
    "7.1.2",
    "7.2.1",
    "7.2.2",
)

#: Destination bank account for deposit / issue: the test company's
#: cash account (1.1.1 Cash and Cash Equivalents).
DESTINATION_ACCOUNT_CODE = "1.1.1"

ISSUE = FiscalDate.from_gregorian(datetime.date(2026, 11, 17))
DUE = FiscalDate.from_gregorian(datetime.date(2027, 1, 5))
BOUNCE_DAY = FiscalDate.from_gregorian(datetime.date(2027, 1, 10))


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
def transition_available(client: TrytonRestClient) -> None:
    """Skip transition tests until ``transition_rest`` is exposed."""
    try:
        client.action(
            "account.check",
            "transition_rest",
            record_id=999999999,
            data={"transition": "clear"},
        )
    except TrytonAdapterError as exc:
        if exc.status_code == 403:
            pytest.skip(
                "account.check.transition_rest is not exposed yet "
                "(US-531 adds the transition methods to the module)."
            )
        return
    return


@pytest.fixture(scope="module")
def issuer_party(client: TrytonRestClient, company: CompanyRef) -> PartyRef:
    """One issuer party for the module run (left in place afterwards)."""
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
        {"name": "US-531 Test Issuer", "code": "0012345678"},
        context=context,
    )
    return PartyRef(engine_id=int(party["id"]))


@pytest.fixture(scope="module")
def destination_account(chart) -> AccountRef:
    return chart[DESTINATION_ACCOUNT_CODE].ref


@pytest.fixture()
def checkbook(client: TrytonRestClient, company: CompanyRef):
    """A checkbook with leaves 100–109 (US-530 pattern).

    Left in place afterwards: the issued cheques reference it
    (ondelete RESTRICT) and cannot be deleted once their journals
    exist — the same precedent as the issuer party fixture. Each
    test uses its own fresh checkbook, so leaf counts are exact.
    """
    context = {"company": company.engine_id}
    party = client.create(
        "party.party", {"name": "US-532 Test Bank Party"}, context=context
    )
    bank = client.create("bank", {"party": party["id"]}, context=context)
    book = client.create(
        "account.checkbook",
        {
            "company": company.engine_id,
            "name": "US-532 Test Checkbook",
            "bank": bank["id"],
            "first_number": 100,
            "last_number": 109,
        },
        context=context,
    )
    yield book


def _create(adapter: TrytonAccountingAdapter, company: CompanyRef, party=None, **overrides):
    kwargs = {
        "company": company,
        "kind": "received",
        "purpose": "payment",
        "sayad_id": _unique_sayad_id(),
        "amount": MoneyIRR(17_500_000),
        "issue_date": ISSUE,
        "due_date": DUE,
        "issuer_name": "مشتری نمونه",
        "issuer_national_id": "0012345678",
        "issuer_bank": "056 سامان",
        "payee_name": "کافه ایوان",
        "payee_national_id": "00123456789",
        "order_id": "ord-1",
        "customer_ref": "cust-1",
        "sayad_registered": True,
        "source": "manual",
        "party": party,
    }
    kwargs.update(overrides)
    return adapter.create_engine_cheque(CreateEngineCheque(**kwargs))


def _transition(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    view,
    transition: str,
    **kwargs,
):
    return adapter.transition_engine_cheque(
        TransitionEngineCheque(
            cheque=view.ref,
            company=company,
            transition=transition,
            auto_post=False,
            **kwargs,
        )
    )


def _held(adapter, company, party=None, **overrides):
    view = _create(adapter, company, party=party, **overrides)
    return _transition(
        adapter, company, view, "confirm_receipt", storage_location="vault"
    )


def _issued(adapter, company, checkbook_id, destination_account, leaf="101", **overrides):
    """Create an issued cheque from a checkbook and issue it (سند ۱۰)."""
    view = _create(
        adapter,
        company,
        party=None,  # the adapter resolves the BENEFICIARY party
        kind="issued",
        leaf_number=leaf,
        checkbook_engine_id=int(checkbook_id),
        **overrides,
    )
    return _transition(
        adapter,
        company,
        view,
        "issue",
        destination_account=destination_account,
        kind="issued",
    )


def _move_lines(client: TrytonRestClient, chart, move_id: int):
    """(account code, debit, credit) lines of one journal move."""
    id_to_code = {view.ref.engine_id: code for code, view in chart.items()}
    lines = client.search(
        "account.move.line",
        domain=[("move", "=", move_id)],
        fields=["account", "debit", "credit"],
    )
    out = []
    for line in lines:
        account = line["account"]
        account_id = int(account["id"]) if isinstance(account, dict) else int(account)
        out.append(
            (
                id_to_code.get(account_id, f"?{account_id}"),
                int(line["debit"]),
                int(line["credit"]),
            )
        )
    return sorted(out)


def _assert_move(
    client: TrytonRestClient,
    chart,
    move_id: int,
    debit_code: str,
    credit_code: str,
    amount: int = 17_500_000,
) -> None:
    """Assert a two-line move: Dr ``debit_code`` / Cr ``credit_code``."""
    lines = _move_lines(client, chart, move_id)
    assert set(lines) == {
        (credit_code, 0, amount),
        (debit_code, amount, 0),
    }, f"move {move_id} lines: {lines}"


# -- issued lifecycle (سند ۱۰–۱۳) --------------------------------------------------


def test_issue_posts_sand_10_with_corrected_payables(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    checkbook,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    issued = _issued(adapter, company, checkbook["id"], destination_account)
    assert issued.state == "issued"
    assert "issue" in issued.move_ids  # سند ۱۰
    # US-532 correction: سند ۱۰ debits Trade Payables 2.1.1 (US-531
    # had debited Notes Payable 2.1.5 by mistake).
    _assert_move(
        client, chart, issued.move_ids["issue"], "2.1.1", "2.1.5"
    )


def test_issued_clear_posts_sand_11(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    checkbook,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    issued = _issued(adapter, company, checkbook["id"], destination_account)
    cleared = _transition(adapter, company, issued, "clear", kind="issued")
    assert cleared.state == "cleared"
    assert "clear" in cleared.move_ids  # سند ۱۱
    # The bank account recorded at issue is credited.
    _assert_move(
        client, chart, cleared.move_ids["clear"], "2.1.5", "1.1.1"
    )


def test_issued_bounce_posts_sand_12_without_certificate(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    checkbook,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    issued = _issued(adapter, company, checkbook["id"], destination_account)
    bounced = _transition(
        adapter,
        company,
        issued,
        "bounce",
        bounce_reason="عدم کفایت موجودی",
        bounce_date=BOUNCE_DAY,
        kind="issued",
    )
    assert bounced.state == "bounced"
    assert "bounce" in bounced.move_ids  # سند ۱۲
    _assert_move(
        client, chart, bounced.move_ids["bounce"], "2.1.5", "2.1.1"
    )


def test_issued_void_posts_sand_13_and_counts_the_voided_leaf(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    checkbook,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    issued = _issued(adapter, company, checkbook["id"], destination_account)
    voided = _transition(
        adapter,
        company,
        issued,
        "void",
        void_reason="اشتباه در مبلغ",
        kind="issued",
    )
    assert voided.state == "voided"
    assert "void" in voided.move_ids  # سند ۱۳
    _assert_move(
        client, chart, voided.move_ids["void"], "2.1.5", "2.1.1"
    )
    # The leaf is voided in its checkbook (report 8).
    book = client.read(
        "account.checkbook",
        checkbook["id"],
        fields=["total_leaves", "used_leaves", "voided_leaves", "remaining_leaves"],
        context={"company": company.engine_id},
    )
    assert book is not None
    assert book["total_leaves"] == 10
    assert book["used_leaves"] == 0
    assert book["voided_leaves"] == 1
    assert book["remaining_leaves"] == 9


# -- collateral (سند ۷/۸/۹) -----------------------------------------------------------


def test_received_collateral_held_and_returned_are_memorandum_only(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    held = _held(adapter, company, party=issuer_party, purpose="collateral")
    assert held.state == "held"
    assert "confirm_receipt" in held.move_ids  # سند ۷
    # Segregation: the memorandum move touches ONLY 7.x accounts.
    _assert_move(
        client, chart, held.move_ids["confirm_receipt"], "7.1.1", "7.2.1"
    )
    returned = _transition(
        adapter, company, held, "return", kind="received"
    )
    assert returned.state == "returned"
    assert "return" in returned.move_ids  # سند ۸
    _assert_move(
        client, chart, returned.move_ids["return"], "7.2.1", "7.1.1"
    )


def test_call_in_posts_two_moves_and_commercialises(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    issuer_party: PartyRef,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    held = _held(adapter, company, party=issuer_party, purpose="collateral")
    called = _transition(
        adapter,
        company,
        held,
        "call_in",
        accountant_approval="حسابدار ارشد",
        kind="received",
    )
    assert called.state == "called_in"
    # TWO simultaneous journals in ONE transaction.
    assert "call_in_reversal" in called.move_ids  # سند ۹الف
    assert "call_in" in called.move_ids  # سند ۹ب
    _assert_move(
        client, chart, called.move_ids["call_in_reversal"], "7.2.1", "7.1.1"
    )
    _assert_move(
        client, chart, called.move_ids["call_in"], "1.2.4", "1.2.1"
    )
    # The cheque is commercial now: it follows §2.1 (deposit سند ۲).
    read_back = adapter.get_engine_cheque(called.ref)
    assert read_back is not None
    assert read_back.purpose == "payment"
    clearing = _transition(
        adapter,
        company,
        called,
        "deposit",
        destination_account=destination_account,
        kind="received",
    )
    assert clearing.state == "in_clearing"
    assert "deposit" in clearing.move_ids


def test_call_in_without_approval_is_refused(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    held = _held(adapter, company, party=issuer_party, purpose="collateral")
    # The command itself refuses a call-in without the accountant's
    # approval (client-side validation, before anything reaches
    # the engine).
    from hesabyar.domain.cheques.errors import ChequeValidationError

    with pytest.raises(ChequeValidationError):
        _transition(adapter, company, held, "call_in", kind="received")


def test_issued_collateral_memorandum_and_unexpected_call_in(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    checkbook,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    issued = _issued(
        adapter,
        company,
        checkbook["id"],
        destination_account,
        purpose="collateral",
        secured_obligation="قرارداد اجاره ۱۴۰۵/۰۳",
    )
    assert issued.state == "issued"
    assert issued.purpose == "collateral"
    assert "issue" in issued.move_ids  # memorandum سند ۷ (issued direction)
    _assert_move(
        client, chart, issued.move_ids["issue"], "7.1.2", "7.2.2"
    )
    # The counterparty presented it without notice: the memorandum
    # reverses and the cheque becomes commercial — but NO commercial
    # journal is posted here (accountant-gated, §9.1 #4).
    called = _transition(
        adapter,
        company,
        issued,
        "unexpected_call_in",
        statement_reference="stmt-1",
        kind="issued",
    )
    assert called.state == "called_in"
    assert set(called.move_ids) == {"unexpected_call_in"}
    _assert_move(
        client, chart, called.move_ids["unexpected_call_in"], "7.2.2", "7.1.2"
    )
    read_back = adapter.get_engine_cheque(called.ref)
    assert read_back is not None
    assert read_back.purpose == "payment"
    # Once the accountant has posted the commercial journal, the
    # cheque clears from called_in (سند ۱۱).
    cleared = _transition(adapter, company, called, "clear", kind="issued")
    assert cleared.state == "cleared"
    assert "clear" in cleared.move_ids


def test_issued_collateral_return_reverses_the_memorandum(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    checkbook,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    issued = _issued(
        adapter,
        company,
        checkbook["id"],
        destination_account,
        purpose="collateral",
    )
    returned = _transition(
        adapter, company, issued, "return", kind="issued"
    )
    assert returned.state == "returned"
    assert "return" in returned.move_ids  # memorandum سند ۸ (issued direction)
    _assert_move(
        client, chart, returned.move_ids["return"], "7.2.2", "7.1.2"
    )


# -- endorsed bounce (§9.1 #3) + checkbook reads -------------------------------------


def test_endorsed_bounce_posts_no_journal(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    held = _held(adapter, company, party=issuer_party)
    endorsed = _transition(
        adapter,
        company,
        held,
        "endorse",
        endorsee_name="تامین‌کننده الف",
        endorsee_type="supplier",
        sayad_transfer_reference="transfer-1",
    )
    assert endorsed.state == "endorsed"
    assert "endorse" in endorsed.move_ids  # سند ۵
    bounced = _transition(
        adapter,
        company,
        endorsed,
        "endorsed_bounce",
        bounce_reason="عدم کفایت موجودی",
        bounce_date=BOUNCE_DAY,
        certificate_reference="cert-9",
        kind="received",
    )
    assert bounced.state == "bounced"
    # §9.1 #3: the reverse-of-endorsement journal is accountant-gated
    # — no journal is guessed.
    assert bounced.move_ids == {}


def test_endorse_to_supplier_posts_sand_5_on_trade_payables(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    client: TrytonRestClient,
    chart,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    # The same 2.1.1 correction as سند ۱۰, on the US-531 path.
    held = _held(adapter, company, party=issuer_party)
    endorsed = _transition(
        adapter,
        company,
        held,
        "endorse",
        endorsee_name="تامین‌کننده الف",
        endorsee_type="supplier",
        sayad_transfer_reference="transfer-1",
    )
    assert endorsed.state == "endorsed"
    _assert_move(
        client, chart, endorsed.move_ids["endorse"], "2.1.1", "1.2.4"
    )


def test_checkbook_reads_via_adapter(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    checkbook,
    destination_account: AccountRef,
    module_active: None,
    transition_available: None,
) -> None:
    books = adapter.list_engine_checkbooks(company)
    assert any(b.ref.engine_id == int(checkbook["id"]) for b in books)
    view = adapter.get_engine_checkbook(
        company, CheckbookEngineRef(engine_id=int(checkbook["id"]))
    )
    assert view is not None
    assert view.total_leaves == 10
    assert view.used_leaves == 0
    assert view.voided_leaves == 0
    assert view.remaining_leaves == 10
    issued = _issued(adapter, company, checkbook["id"], destination_account)
    view = adapter.get_engine_checkbook(
        company, CheckbookEngineRef(engine_id=int(checkbook["id"]))
    )
    assert view is not None
    assert view.used_leaves == 1
    assert view.remaining_leaves == 9
    _transition(
        adapter, company, issued, "void", void_reason="اشتباه", kind="issued"
    )
    view = adapter.get_engine_checkbook(
        company, CheckbookEngineRef(engine_id=int(checkbook["id"]))
    )
    assert view is not None
    assert view.used_leaves == 0
    assert view.voided_leaves == 1
    assert view.remaining_leaves == 9
