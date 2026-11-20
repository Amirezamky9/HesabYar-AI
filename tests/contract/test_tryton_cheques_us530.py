"""Live contract tests for US-530 (Phase 5-4 / ۵ث): account_check_ir.

Run against the LIVE trytond server and the ``hesabyar_tryton_test``
database, using ONLY the dedicated test company "HesabYar Adapter
Test" (provisioned by
``~/workspace/projects/hesabyar-reference/setup_adapter_test_company.py``;
see docs/adapter.md). They never touch another company's records.

The module must be installed first (US-530 procedure:
``trytond-admin -u account_check_ir hesabyar_setup`` + ``update_chart``
on the test company — see docs/tryton-modules.md). When the server is
not reachable, or the module / chart accounts are missing, every test
SKIPS cleanly with a clear message.

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
from decimal import Decimal

import pytest

from hesabyar.domain.accounting.refs import CompanyRef
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


@pytest.fixture()
def bank(client: TrytonRestClient, company: CompanyRef):
    """A throwaway bank (+ party) for checkbook tests; deleted after."""
    context = {"company": company.engine_id}
    party = client.create(
        "party.party", {"name": "US-530 Test Bank Party"}, context=context
    )
    bank = client.create(
        "bank", {"party": party["id"]}, context=context
    )
    yield bank
    client.delete("bank", bank["id"], context=context)
    client.delete("party.party", party["id"], context=context)


@pytest.fixture()
def checkbook(client: TrytonRestClient, company: CompanyRef, bank):
    """A checkbook with leaves 100–109; deleted after the test."""
    context = {"company": company.engine_id}
    book = client.create(
        "account.checkbook",
        {
            "company": company.engine_id,
            "name": "US-530 Test Checkbook",
            "bank": bank["id"],
            "first_number": 100,
            "last_number": 109,
        },
        context=context,
    )
    yield book
    client.delete("account.checkbook", book["id"], context=context)


def _cheque_values(company: CompanyRef, **overrides):
    values = {
        "company": company.engine_id,
        "kind": "received",
        "purpose": "payment",
        "number": "987654",
        "sayad_id": _unique_sayad_id(),
        "amount": Decimal("1500000"),
        "issue_date": datetime.date(2026, 11, 10),
        "due_date": datetime.date(2026, 12, 10),
    }
    values.update(overrides)
    return values


def test_module_activated(client: TrytonRestClient, module_active: None) -> None:
    records = client.search(
        "ir.module",
        domain=[("name", "=", "account_check_ir")],
        fields=["name", "state"],
    )
    assert records and records[0]["state"] == "activated"


def test_cheque_chart_accounts_present(chart) -> None:
    for code in CHEQUE_ACCOUNT_CODES:
        assert code in chart


def test_checkbook_computed_leaves(
    client: TrytonRestClient,
    company: CompanyRef,
    module_active: None,
    checkbook,
) -> None:
    context = {"company": company.engine_id}
    fields = ["total_leaves", "used_leaves", "voided_leaves", "remaining_leaves"]
    book = client.read("account.checkbook", checkbook["id"], fields=fields, context=context)
    assert book is not None
    assert book["total_leaves"] == 10
    assert book["used_leaves"] == 0
    assert book["voided_leaves"] == 0
    assert book["remaining_leaves"] == 10

    issued = client.create(
        "account.check",
        _cheque_values(
            company,
            kind="issued",
            number="104",
            checkbook=checkbook["id"],
            state="issued",
        ),
        context=context,
    )
    try:
        book = client.read(
            "account.checkbook", checkbook["id"], fields=fields, context=context
        )
        assert book is not None
        assert book["used_leaves"] == 1
        assert book["remaining_leaves"] == 9
    finally:
        client.delete("account.check", issued["id"], context=context)


def test_check_defaults_and_full_field_set(
    client: TrytonRestClient,
    company: CompanyRef,
    module_active: None,
    checkbook,
) -> None:
    context = {"company": company.engine_id}
    cheque = client.create(
        "account.check",
        _cheque_values(
            company,
            storage_location="vault",
            branch="Tehran Central",
            issuer_account_number="0101234567890",
            installment_leg="leg-42",
            applied_amount_irr=Decimal("1500000"),
            endorsee_type="supplier",
            secured_obligation="contract-7",
        ),
        context=context,
    )
    try:
        read = client.read(
            "account.check",
            cheque["id"],
            fields=[
                "kind", "purpose", "number", "sayad_id", "sayad_status",
                "amount", "state", "source", "storage_location", "branch",
                "issuer_account_number", "installment_leg",
                "applied_amount_irr", "endorsee_type", "secured_obligation",
            ],
            context=context,
        )
        assert read is not None
        assert read["kind"] == "received"
        assert read["purpose"] == "payment"
        assert read["sayad_status"] == "unregistered"
        assert read["state"] == "registered"
        assert read["source"] == "manual"
        assert read["storage_location"] == "vault"
        assert read["branch"] == "Tehran Central"
        assert read["issuer_account_number"] == "0101234567890"
        assert read["installment_leg"] == "leg-42"
        assert read["endorsee_type"] == "supplier"
        assert read["secured_obligation"] == "contract-7"
        assert Decimal(str(read["amount"])) == Decimal("1500000")
    finally:
        client.delete("account.check", cheque["id"], context=context)


def test_sayad_id_must_be_16_digits(
    client: TrytonRestClient, company: CompanyRef, module_active: None
) -> None:
    context = {"company": company.engine_id}
    with pytest.raises(TrytonAdapterError):
        client.create(
            "account.check",
            _cheque_values(company, sayad_id="123"),
            context=context,
        )


def test_sayad_id_unique_among_live_states(
    client: TrytonRestClient, company: CompanyRef, module_active: None
) -> None:
    context = {"company": company.engine_id}
    sayad_id = _unique_sayad_id()
    first = client.create(
        "account.check", _cheque_values(company, sayad_id=sayad_id), context=context
    )
    try:
        with pytest.raises(TrytonAdapterError):
            client.create(
                "account.check",
                _cheque_values(company, sayad_id=sayad_id),
                context=context,
            )
    finally:
        client.delete("account.check", first["id"], context=context)


def test_sayad_id_reusable_after_expired_unreceived(
    client: TrytonRestClient, company: CompanyRef, module_active: None
) -> None:
    """§5 §1.1: the uniqueness constraint exempts expired/rejected states."""
    context = {"company": company.engine_id}
    sayad_id = _unique_sayad_id()
    first = client.create(
        "account.check", _cheque_values(company, sayad_id=sayad_id), context=context
    )
    second = None
    try:
        client.write(
            "account.check",
            first["id"],
            {"state": "expired_unreceived"},
            context=context,
        )
        # Re-registration with the same id must now succeed.
        second = client.create(
            "account.check",
            _cheque_values(company, sayad_id=sayad_id),
            context=context,
        )
        assert second["id"] != first["id"]
    finally:
        if second is not None:
            client.delete("account.check", second["id"], context=context)
        client.delete("account.check", first["id"], context=context)


def test_amount_and_dates_validated(
    client: TrytonRestClient, company: CompanyRef, module_active: None
) -> None:
    context = {"company": company.engine_id}
    with pytest.raises(TrytonAdapterError):
        client.create(
            "account.check",
            _cheque_values(company, amount=Decimal("0")),
            context=context,
        )
    with pytest.raises(TrytonAdapterError):
        client.create(
            "account.check",
            _cheque_values(
                company,
                issue_date=datetime.date(2026, 12, 10),
                due_date=datetime.date(2026, 11, 10),
            ),
            context=context,
        )


def test_issued_leaf_must_be_inside_checkbook_range(
    client: TrytonRestClient,
    company: CompanyRef,
    module_active: None,
    checkbook,
) -> None:
    context = {"company": company.engine_id}
    with pytest.raises(TrytonAdapterError):
        client.create(
            "account.check",
            _cheque_values(
                company, kind="issued", number="999", checkbook=checkbook["id"]
            ),
            context=context,
        )


def test_endorsement_history(
    client: TrytonRestClient, company: CompanyRef, module_active: None
) -> None:
    context = {"company": company.engine_id}
    cheque = client.create(
        "account.check", _cheque_values(company), context=context
    )
    try:
        row = client.create(
            "account.check.endorsement",
            {
                "check": cheque["id"],
                "date": datetime.date(2026, 11, 15),
                "sayad_transfer_receipt": "TR-1",
                "reason": "payment to supplier",
            },
            context=context,
        )
        try:
            read = client.read(
                "account.check",
                cheque["id"],
                fields=["endorsement_history"],
                context=context,
            )
            assert read is not None
            assert row["id"] in (read["endorsement_history"] or [])
        finally:
            client.delete("account.check.endorsement", row["id"], context=context)
    finally:
        client.delete("account.check", cheque["id"], context=context)


def test_custody_log_is_append_only(
    client: TrytonRestClient, company: CompanyRef, module_active: None
) -> None:
    context = {"company": company.engine_id}
    cheque = client.create(
        "account.check", _cheque_values(company), context=context
    )
    try:
        row = client.create(
            "account.check.custody_log",
            {
                "check": cheque["id"],
                "origin": "cashbox",
                "destination": "vault",
                "moved_at": datetime.datetime(2026, 11, 19, 10, 30),
                "reason": "end of day count",
            },
            context=context,
        )
        read = client.read(
            "account.check.custody_log",
            row["id"],
            fields=["origin", "destination", "reason"],
            context=context,
        )
        assert read is not None
        assert read["origin"] == "cashbox"
        assert read["destination"] == "vault"
        with pytest.raises(TrytonAdapterError):
            client.write(
                "account.check.custody_log",
                row["id"],
                {"reason": "rewritten"},
                context=context,
            )
        with pytest.raises(TrytonAdapterError):
            client.delete(
                "account.check.custody_log", row["id"], context=context
            )
    finally:
        # Deleting the cheque cascades to its custody rows
        # (ondelete CASCADE), so no orphan is left behind.
        client.delete("account.check", cheque["id"], context=context)
