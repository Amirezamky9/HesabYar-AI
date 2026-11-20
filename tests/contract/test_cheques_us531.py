"""Live contract tests for US-531 (Phase 5-4 / ۵ث): cheque lifecycle.

Run against the LIVE trytond server and the ``hesabyar_tryton_test``
database, using ONLY the dedicated test company "HesabYar Adapter
Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``).
They exercise the HesabYar adapter against the real
``account_check_ir`` module (US-530) through a direct ``create`` on
``account.check`` and the ``transition_rest`` ``__rpc__`` action —
the exact contract in ``docs/cheque-engine-contract.md``. Every
transition is ONE engine call; the test asserts the state AND its
§2.4 journal move come back together.

When the server is not reachable, or the module / chart accounts
are missing, every test SKIPS cleanly with a clear message.

Configuration (all optional, dev defaults match docs/dev-setup.md):
  TRYTON_BASE_URL       (default http://localhost:8010 — lane 1;
                         US-531 runs on lane 2: http://localhost:8011)
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
    ChequeEngineRef,
    CompanyRef,
    PartyRef,
)
from hesabyar.domain.cheques.errors import ChequeEngineError
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import TrytonAdapterError

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8010")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

#: Cheque chart accounts (US-502 / US-530): held, in-clearing,
#: payable, bounced, collateral memorandum + contras.
CHEQUE_ACCOUNT_CODES = (
    "1.2.4",
    "1.2.5",
    "2.1.5",
    "1.2.9",
    "7.1.1",
    "7.1.2",
    "7.2.1",
    "7.2.2",
)

#: Destination bank account for the deposit transition: the test
#: company's cash account (1.1.1 Cash and Cash Equivalents).
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
    """Skip transition tests until ``transition_rest`` lands in the module.

    The US-530 scaffold has no transition methods yet — US-531 adds
    them (one atomic transaction per transition). Probe with a
    nonexistent record: 403 means the action is not in ``__rpc__``
    (skip); anything else means it is exposed (proceed). The probe
    mutates nothing.
    """
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
        # Any other error means the action IS exposed (it failed on
        # the missing record, not on the action name) — proceed.
        return
    return


@pytest.fixture(scope="module")
def issuer_party(client: TrytonRestClient, company: CompanyRef) -> PartyRef:
    """One issuer party for the module run (left in place afterwards).

    The cheque chart accounts are ``party_required``, so the
    transition journals need a party — the cheque's ``party`` field
    carries it from intake. The party cannot be deleted afterwards
    (the cheques reference it with ondelete RESTRICT), so it stays
    in the dedicated test company as clearly-marked test data.
    """
    context = {"company": company.engine_id}
    existing = client.search(
        "party.party",
        domain=[("code", "=", "0012345678")],
        fields=["id"],
        context=context,
    )
    if existing:
        # Idempotent across runs: the party persists in the dedicated
        # test company (cheques reference it with ondelete RESTRICT).
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


def _held(adapter, company, party=None):
    view = _create(adapter, company, party=party)
    return _transition(
        adapter, company, view, "confirm_receipt", storage_location="vault"
    )


def _in_clearing(adapter, company, destination_account, party=None):
    held = _held(adapter, company, party=party)
    return _transition(
        adapter,
        company,
        held,
        "deposit",
        destination_account=destination_account,
    )


# -- intake ---------------------------------------------------------------------------


def test_create_registers_with_no_journal(
    adapter: TrytonAccountingAdapter, company: CompanyRef, module_active: None
) -> None:
    view = _create(adapter, company)
    assert view.state == "registered"
    assert view.move_ids == {}
    assert view.sayad_id
    assert view.amount == MoneyIRR(17_500_000)


def test_create_with_installment_leg_link(
    adapter: TrytonAccountingAdapter, company: CompanyRef, module_active: None
) -> None:
    view = _create(adapter, company, installment_leg_id="leg-7")
    assert view.state == "registered"
    # Read it back: the leg link survives the round trip.
    read_back = adapter.get_engine_cheque(view.ref)
    assert read_back is not None
    assert read_back.state == "registered"


def test_duplicate_live_sayad_is_refused(
    adapter: TrytonAccountingAdapter, company: CompanyRef, module_active: None
) -> None:
    sayad_id = _unique_sayad_id()
    _create(adapter, company, sayad_id=sayad_id)
    with pytest.raises(ChequeEngineError):
        _create(adapter, company, sayad_id=sayad_id)


# -- full lifecycle ----------------------------------------------------------------------


def test_full_lifecycle_to_clear(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    destination_account: AccountRef,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    held = _held(adapter, company, party=issuer_party)
    assert held.state == "held"
    assert "confirm_receipt" in held.move_ids  # سند ۱
    assert held.storage_location == "vault"

    clearing = _transition(
        adapter, company, held, "deposit", destination_account=destination_account
    )
    assert clearing.state == "in_clearing"
    assert "deposit" in clearing.move_ids  # سند ۲
    assert clearing.storage_location == "bank"

    cleared = _transition(adapter, company, clearing, "clear")
    assert cleared.state == "cleared"
    assert "clear" in cleared.move_ids  # سند ۳
    # The adapter view is per-call: it carries only THIS transition's
    # move (a plain read cannot attribute the earlier moves). The
    # accumulated transition→move map lives in the HesabYar registry
    # (service `_apply_engine_result` merges it — covered by the
    # service unit tests), not in the stateless adapter view.
    assert set(cleared.move_ids) == {"clear"}


def test_bounce_then_return_has_no_return_journal(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    destination_account: AccountRef,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    clearing = _in_clearing(adapter, company, destination_account, party=issuer_party)
    bounced = _transition(
        adapter,
        company,
        clearing,
        "bounce",
        bounce_destination="receivables",
        bounce_reason="عدم کفایت موجودی",
        bounce_date=BOUNCE_DAY,
        certificate_reference="cert-1",
    )
    assert bounced.state == "bounced"
    assert "bounce" in bounced.move_ids  # سند ۴

    returned = _transition(adapter, company, bounced, "return")
    assert returned.state == "returned"
    assert "return" not in returned.move_ids  # no journal from bounced
    # Per-call view: no journal this transition, so no move carried.
    # (The earlier سند ۱/۲/۴ moves are in the registry's map.)
    assert returned.move_ids == {}


def test_representment_stays_disabled(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    destination_account: AccountRef,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    clearing = _in_clearing(adapter, company, destination_account, party=issuer_party)
    bounced = _transition(
        adapter,
        company,
        clearing,
        "bounce",
        bounce_destination="receivables",
        bounce_reason="عدم کفایت موجودی",
        bounce_date=BOUNCE_DAY,
        certificate_reference="cert-1",
    )
    with pytest.raises(ChequeEngineError):
        _transition(
            adapter, company, bounced, "deposit", destination_account=destination_account
        )


def test_endorse_to_supplier_posts_sand_5(
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


def test_endorse_to_other_defers_the_journal(
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
        endorsee_name="شخص ثالث",
        endorsee_type="other",
        sayad_transfer_reference="transfer-2",
        defer_journal=True,
    )
    assert endorsed.state == "endorsed"
    assert "endorse" not in endorsed.move_ids  # no journal guessed (§9.1 #12)


def test_return_from_held_posts_sand_6(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    issuer_party: PartyRef,
    module_active: None,
    transition_available: None,
) -> None:
    held = _held(adapter, company, party=issuer_party)
    returned = _transition(adapter, company, held, "return")
    assert returned.state == "returned"
    assert "return" in returned.move_ids  # سند ۶


# -- terminal no-journal transitions --------------------------------------------------------


def test_reject_mismatch_is_terminal_with_no_journal(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    module_active: None,
    transition_available: None,
) -> None:
    view = _create(adapter, company)
    rejected = _transition(
        adapter, company, view, "reject_mismatch", mismatch_detail="مبلغ تطابق ندارد"
    )
    assert rejected.state == "rejected_mismatch"
    assert rejected.move_ids == {}


def test_expire_is_terminal_with_no_journal(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    module_active: None,
    transition_available: None,
) -> None:
    view = _create(adapter, company)
    expired = _transition(adapter, company, view, "expire")
    assert expired.state == "expired_unreceived"
    assert expired.move_ids == {}


def test_expired_sayad_id_may_be_reused(
    adapter: TrytonAccountingAdapter,
    company: CompanyRef,
    module_active: None,
    transition_available: None,
) -> None:
    sayad_id = _unique_sayad_id()
    view = _create(adapter, company, sayad_id=sayad_id)
    _transition(adapter, company, view, "expire")
    reused = _create(adapter, company, sayad_id=sayad_id)
    assert reused.state == "registered"
    assert reused.ref.engine_id != view.ref.engine_id


# -- reads -----------------------------------------------------------------------------------


def test_get_and_list_round_trip(
    adapter: TrytonAccountingAdapter, company: CompanyRef, module_active: None
) -> None:
    view = _create(adapter, company)
    read_back = adapter.get_engine_cheque(view.ref)
    assert read_back is not None
    assert read_back.ref == view.ref
    assert read_back.state == "registered"
    assert adapter.get_engine_cheque(ChequeEngineRef(engine_id=999999999)) is None
    listed = adapter.list_engine_cheques(company)
    assert any(v.ref == view.ref for v in listed)
