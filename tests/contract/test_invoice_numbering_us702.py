"""Concurrent invoice numbering (US-702, spec §17 phase 7).

Live-Tryton contract tests: several threads create + validate + post
customer invoices at once, each through its own REST client + adapter.
The engine assigns the invoice number on posting (docs/adapter.md:
``None`` on drafts), from the company's invoice sequence — so the
assertions are the sequence's guarantees:

- every posted invoice gets a number (non-None);
- no two posted invoices share a number.

Gaps are NOT asserted: a Tryton sequence guarantees uniqueness, not
consecutiveness — a number consumed by a rolled-back transaction is
lost, so consecutive postings may number 1042, 1044. That is the
documented behavior (the sequence is the authority; HesabYar never
numbers invoices itself), and the test pins uniqueness only.

Invoice dates are dynamic (today), like the sales contract tests:
Tryton numbers invoices chronologically, and a fixed date eventually
falls behind the sequence when the shared test DB accumulates posted
invoices from other tests.

Posted invoices stay in the test DB (expected residue, like the
posted moves the other contract tests leave behind); the customer
party's teardown is best-effort for the same reason. Skip when
trytond is unreachable or the test company is missing.

Latencies are recorded for ``docs/performance-baselines.md`` and
never asserted.
"""

from __future__ import annotations

import datetime
import os
import random
import time
import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest

from hesabyar.domain.accounting.commands import CreateInvoiceDraft, InvoiceDraftLine
from hesabyar.domain.accounting.refs import (
    CompanyRef,
    PartyAddressRef,
    PartyRef,
)
from hesabyar.domain.accounting.value_objects import InvoiceView
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import TrytonAdapterError
from tests.load_support import LatencyRecorder, run_threads

BASE_URL = os.environ.get("TRYTON_BASE_URL", "http://localhost:8000")
DATABASE = os.environ.get("TRYTON_DATABASE", "hesabyar_tryton_test")
API_KEY = os.environ.get("TRYTON_API_KEY", "hesabyar-dev-rest-key")
TEST_COMPANY_NAME = os.environ.get("TRYTON_TEST_COMPANY", "HesabYar Adapter Test")

RECEIVABLE_ACCOUNT_CODE = "1.2.1"
REVENUE_ACCOUNT_CODE = "4.1.1"
SERVICES_ACCOUNT_CODE = "4.1.2"

CUSTOMER_NAME = "HesabYar US-702 Contract Customer"

# Today, not a fixed date (see the module docstring).
MOVE_DATE = FiscalDate.from_gregorian(datetime.date.today())


@pytest.fixture(scope="module")
def client() -> Generator[TrytonRestClient, None, None]:
    rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY, timeout=120.0)
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
def accounts(adapter: TrytonAccountingAdapter, company: CompanyRef):
    chart = {view.code: view for view in adapter.get_chart_of_accounts(company)}
    wanted = (
        RECEIVABLE_ACCOUNT_CODE,
        REVENUE_ACCOUNT_CODE,
        SERVICES_ACCOUNT_CODE,
    )
    missing = [code for code in wanted if code not in chart]
    if missing:
        pytest.skip(f"Chart accounts missing from test company: {missing}.")
    return {code: chart[code] for code in wanted}


@pytest.fixture(scope="module")
def customer(client: TrytonRestClient):
    """A customer party with one address, created via REST.

    Teardown is best-effort: posted invoices leave move lines
    referencing the party, which blocks deletion (expected residue).
    """
    party = client.create("party.party", {"name": CUSTOMER_NAME}, fields=["id"])
    party_id = int(party["id"])
    address = client.create(
        "party.address", {"party": party_id, "city": "Tehran"}, fields=["id"]
    )
    address_id = int(address["id"])
    yield PartyRef(party_id), PartyAddressRef(address_id)
    for model, rid in (
        ("party.address", address_id),
        ("party.party", party_id),
    ):
        try:
            client.delete(model, rid)
        except Exception:
            pass  # referenced by posted invoice move lines


def _invoice_command(company: CompanyRef, party: PartyRef, accounts) -> CreateInvoiceDraft:
    return CreateInvoiceDraft(
        company=company,
        invoice_type="out",
        party=party,
        date=MOVE_DATE,
        account=accounts[RECEIVABLE_ACCOUNT_CODE].ref,
        lines=(
            InvoiceDraftLine(
                account=accounts[REVENUE_ACCOUNT_CODE].ref,
                unit_price=MoneyIRR(500_000),
                quantity=Decimal(1),
                description="Goods",
            ),
            InvoiceDraftLine(
                account=accounts[SERVICES_ACCOUNT_CODE].ref,
                unit_price=MoneyIRR(125_000),
                quantity=Decimal(2),
                description="Services",
            ),
        ),
        description=f"US-702 numbering test {uuid.uuid4().hex[:8]}",
    )


#: Polling budget for one concurrent post (see _post_one_with_polling).
#:
#: The invoice + move sequences are ir.sequence.strict (see
#: scripts/ci/provision_adapter_test_company.py): a single row locked
#: with SELECT ... FOR UPDATE NOWAIT (trytond
#: backend/postgresql/database.py: lock_records). A contended post
#: therefore fails INSTANTLY with 55P03 (LockNotAvailable) instead of
#: waiting — and 55P03, like 40001/40P01, is a psycopg
#: OperationalError, which trytond's WSGI layer maps to HTTP 503
#: (trytond/wsgi.py: ServiceUnavailable). trytond's own 5 internal
#: retries (20-100ms sleeps) all re-race within milliseconds while
#: the winner holds the lock for the whole post (seconds), so they
#: are useless — and a sparse retry (6 attempts over ~9s, rounds 1-2
#: of the CI fixes) almost never lands in the millisecond gap
#: between one post's commit and the next post's lock acquisition,
#: which is why this test failed three CI runs in a row.
#:
#: The only strategy that works against NOWAIT is to POLL: keep one
#: draft and re-attempt the post every ~0.1-0.3s until the lock is
#: free. The 16 posts serialize at ~1 post duration each, so 180s
#: covers post durations up to ~11s with margin; the run_threads
#: 300s safety net still bounds the whole test.
_POST_POLL_TIMEOUT_SECONDS = 180.0
_POST_POLL_SLEEP_MIN_SECONDS = 0.1
_POST_POLL_SLEEP_MAX_SECONDS = 0.3


def _is_sequence_contention(exc: TrytonAdapterError) -> bool:
    """True only for HTTP 503 — the proven contention signal.

    trytond's WSGI layer returns 503 (ServiceUnavailable) exactly for
    DatabaseOperationalError, which covers the whole contention
    surface: 55P03 lock-not-available (NOWAIT), 40001 serialization
    failure, 40P01 deadlock. Anything else is NOT contention: a 500
    is a genuine engine error (trytond's own ConcurrencyException
    cannot occur on this path — it needs transaction timestamps,
    which the REST post never sets), and a transport error means
    trytond is down or the response was lost — retrying a
    possibly-completed post would hit the already-posted invoice's
    workflow guard. So only 503 polls; everything else fails fast.
    """
    return exc.status_code == 503


def _post_one_with_polling(
    adapter: TrytonAccountingAdapter, company: CompanyRef, party: PartyRef, accounts
) -> InvoiceView:
    """Create -> validate once, then POLL the post on the same draft.

    Create + validate are uncontended (no shared sequence is touched
    — the number is assigned only by post), so they run once with no
    retry: a failure there is genuine. The post is the contended
    step (two strict-sequence rows, one lock holder at a time), so
    it polls: same draft, short jittered sleeps, until the lock is
    free or the 180s budget expires.

    Retrying the post on the SAME draft is safe: a failed post rolls
    its whole transaction back (number assignment, move, state
    change), so the draft is still validated with no number — and
    unlike rounds 1-2 (new draft per attempt) polling orphans no
    drafts. A thread that keeps polling is guaranteed its win
    eventually: every post release goes to a thread that still needs
    one (threads exit after their 2 posts), so after 16 releases
    every thread holds exactly 2 numbers.
    """
    ref = adapter.create_invoice_draft(_invoice_command(company, party, accounts))
    adapter.validate_invoice(ref)
    deadline = time.monotonic() + _POST_POLL_TIMEOUT_SECONDS
    while True:
        try:
            return adapter.post_invoice(ref)
        except TrytonAdapterError as exc:
            if not _is_sequence_contention(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(
                random.uniform(
                    _POST_POLL_SLEEP_MIN_SECONDS, _POST_POLL_SLEEP_MAX_SECONDS
                )
            )


def test_invoice_number_is_none_on_draft_and_set_on_posting(
    adapter: TrytonAccountingAdapter, company: CompanyRef, accounts, customer
) -> None:
    """The documented behavior, sequentially: no number until posting."""
    party, _address = customer
    ref = adapter.create_invoice_draft(_invoice_command(company, party, accounts))
    draft = adapter.get_invoice(ref)
    assert draft is not None
    assert draft.number is None
    assert draft.state == "draft"
    adapter.validate_invoice(ref)
    posted = adapter.post_invoice(ref)
    assert posted.state == "posted"
    assert posted.number is not None and posted.number != ""


def test_concurrent_invoice_posting_assigns_unique_numbers(
    company: CompanyRef, accounts, customer
) -> None:
    """8 threads x 2 invoices posted concurrently: 16 distinct numbers."""
    party, _address = customer
    recorder = LatencyRecorder()
    threads = 8
    per_thread = 2

    def _post_two(_index: int) -> list[str]:
        rest_client = TrytonRestClient(BASE_URL, DATABASE, API_KEY, timeout=120.0)
        thread_adapter = TrytonAccountingAdapter(rest_client)
        try:
            numbers = []
            for _ in range(per_thread):
                started = time.monotonic()
                posted = _post_one_with_polling(thread_adapter, company, party, accounts)
                recorder.record("invoices.create_validate_post", time.monotonic() - started)
                assert posted.state == "posted"
                assert posted.number is not None and posted.number != ""
                numbers.append(posted.number)
            return numbers
        finally:
            rest_client.close()

    results = run_threads(threads, _post_two)
    numbers = [number for batch in results for number in batch]
    assert len(numbers) == threads * per_thread
    assert len(set(numbers)) == len(numbers), f"duplicate invoice numbers: {numbers}"
    print(recorder.report())
    recorder.maybe_write_json()
