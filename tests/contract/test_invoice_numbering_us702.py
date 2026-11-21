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
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_ERROR,
    TrytonAdapterError,
)
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


#: Bounded retries for one concurrent post (see _post_one_with_retry).
#: 8 threads contend on ONE invoice-sequence row: each round at most
#: one simultaneous attempter wins, so a post needs several
#: desynchronized chances. 6 attempts with jittered backoff (base
#: 0.3s, doubling, capped at ~5s: worst case ~9s per invoice,
#: expected half that, parallel across threads).
_MAX_POST_ATTEMPTS = 6
_BACKOFF_BASE_SECONDS = 0.3
_BACKOFF_CAP_SECONDS = 5.0


def _is_transient_contention(exc: TrytonAdapterError) -> bool:
    """Retryable engine errors plus 500s — the contention surface.

    trytond never returns 502/503/504 (verified in the trytond 8.2.3
    source: no 503 anywhere), so round 1's retryable-only filter
    never engaged: under contention trytond surfaces serialization
    failures as HTTP 500 (ENGINE_ERROR, non-retryable by the
    client's taxonomy) once its own internal retries exhaust
    ([database] retry=5, and only for errors raised inside the
    operation — a failure at COMMIT is never retried by trytond).
    Validation/auth/not-found errors are NOT transient and still
    raise immediately.
    """
    return exc.retryable or exc.code == ENGINE_ERROR


def _post_one_with_retry(
    adapter: TrytonAccountingAdapter, company: CompanyRef, party: PartyRef, accounts
) -> InvoiceView:
    """Create -> validate -> post one invoice, retrying transient engine errors.

    trytond runs every connection at REPEATABLE READ, so 8 threads
    posting against one invoice sequence contend on the sequence
    row: all but one simultaneous attempter lose (serialization
    failure), and trytond surfaces the loss as HTTP 500 once its
    internal retries exhaust. This is expected contention under
    load, not an engine outage — the sequential test in this file
    posts fine. Production retries at the event-pipeline level
    (``is_retryable``); this contract test calls the adapter
    directly, so it retries at the call level instead.

    The backoff is JITTERED on purpose: round 1's fixed 0.2s/0.4s
    sleeps re-synchronized the herd — every loser retried at the
    same instant and collided again (CI run #32 still failed after
    3 attempts). A uniform-random sleep spreads the retries across
    the window, so each attempt has a real chance of finding the
    sequence row free. Retrying the whole sequence is safe: drafts
    carry no number, so an orphaned draft from a failed attempt
    cannot duplicate a number (and the module docstring already
    accepts residue). The final attempt still raises, so a genuine
    failure still fails the test.
    """
    for attempt in range(1, _MAX_POST_ATTEMPTS + 1):
        try:
            ref = adapter.create_invoice_draft(_invoice_command(company, party, accounts))
            adapter.validate_invoice(ref)
            return adapter.post_invoice(ref)
        except TrytonAdapterError as exc:
            if not _is_transient_contention(exc) or attempt == _MAX_POST_ATTEMPTS:
                raise
            cap = min(
                _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_CAP_SECONDS
            )
            time.sleep(random.uniform(0, cap))
    raise AssertionError("unreachable: the loop always returns or raises")


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
                posted = _post_one_with_retry(thread_adapter, company, party, accounts)
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
