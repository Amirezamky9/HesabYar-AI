"""Contract tests: IRR precision + Jalali mapping against live Tryton 8.2 (US-004).

These tests talk to the live trytond server over its REST API with httpx
directly — they deliberately do NOT depend on the HesabYar Tryton adapter
(US-003), so they pin Tryton's own behaviour:

- The IRR currency is configured with zero decimal digits (digits=0,
  rounding=1) and Tryton itself rejects fractional Rial values with a
  ``DigitsValidationError`` (HTTP 400) — the zero-decimal rule is enforced
  at the model layer, not just in the adapter.
- Large integer IRR amounts round-trip through ``account.move`` /
  ``account.move.line`` with zero precision loss. Tryton serialises
  ``Decimal`` values as exact strings (``{"__class__": "Decimal", "decimal":
  "…"}``) over REST/JSON-RPC, never as floats.
- ``FiscalDate`` Jalali → Gregorian mapping round-trips through Tryton's
  ``account.move.date`` field, including Persian/Arabic-Indic digit input.

Live tests SKIP CLEANLY when trytond is not reachable, so the suite stays
green without a server; the pure-domain validation tests at the bottom
always run. Test data lives in a dedicated company ("HesabYar IRR Test")
so it never interferes with other stories' fixtures.

Environment (see docs/dev-setup.md): trytond on http://localhost:8000,
database ``hesabyar_tryton_test``, REST application key
``hesabyar-dev-rest-key``. Override with TRYTON_BASE_URL / TRYTON_DB /
TRYTON_REST_KEY.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest

from hesabyar.domain.accounting.commands import CreateJournalDraft, JournalDraftLine
from hesabyar.domain.accounting.refs import AccountRef, CompanyRef, JournalRef
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.common.errors import InvalidDateError, InvalidMoneyError
from hesabyar.domain.common.money import MoneyIRR
from hesabyar.infrastructure.accounting.tryton.adapter import TrytonAccountingAdapter
from hesabyar.infrastructure.accounting.tryton.client import TrytonRestClient
from hesabyar.infrastructure.accounting.tryton.errors import (
    ENGINE_VALIDATION_FAILED,
    TrytonAdapterError,
)
from hesabyar.infrastructure.accounting.tryton.mapping import money_from_tryton

TRYTON_BASE_URL = os.getenv("TRYTON_BASE_URL", "http://localhost:8000")
TRYTON_DB = os.getenv("TRYTON_DB", "hesabyar_tryton_test")
TRYTON_REST_KEY = os.getenv("TRYTON_REST_KEY", "hesabyar-dev-rest-key")

COMPANY_NAME = "HesabYar IRR Test"
CASH_ACCOUNT_CODE = "1000"
REVENUE_ACCOUNT_CODE = "4000"

# Jalali fiscal years covered by the fixture (full-year periods).
FISCAL_YEARS: tuple[tuple[int, str, str], ...] = (
    (1403, "2024-03-20", "2025-03-20"),  # leap year: Esfand has 30 days
    (1404, "2025-03-21", "2026-03-20"),
    (1405, "2026-03-21", "2027-03-20"),
)

# Large integer IRR amounts: a typical invoice value, a multi-trillion value,
# a 15-digit value near realistic maxima, and a 19-digit stress value.
LARGE_AMOUNTS: tuple[int, ...] = (
    1_750_000,
    9_876_543_210_987,
    999_999_999_999_999,
    9_999_999_999_999_999_999,
)


def _decimal_json(value: str) -> dict[str, str]:
    """Encode a Decimal exactly the way Tryton's JSON protocol does."""
    return {"__class__": "Decimal", "decimal": value}


def _decode_decimal(value: Any) -> Decimal:
    """Decode a Tryton JSON Decimal (or a plain JSON int) to Decimal."""
    if isinstance(value, dict) and value.get("__class__") == "Decimal":
        return Decimal(str(value["decimal"]))
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise AssertionError(f"Expected a Tryton Decimal value, got {value!r}")


def _decode_date(value: Any) -> str:
    """Decode a Tryton JSON date to an ISO 'YYYY-MM-DD' string."""
    if isinstance(value, dict) and value.get("__class__") == "date":
        return f"{value['year']:04d}-{value['month']:02d}-{value['day']:02d}"
    if isinstance(value, str):
        return value
    raise AssertionError(f"Expected a Tryton date value, got {value!r}")


class TrytonRest:
    """Minimal Tryton 8.2 REST client (Bearer application key auth)."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=f"{TRYTON_BASE_URL}/{TRYTON_DB}/rest",
            headers={
                "Authorization": f"Bearer {TRYTON_REST_KEY}",
                "Content-Type": "application/json",
            },
            timeout=30,
            # The VM sets proxy env vars whose no_proxy list httpx cannot
            # parse; local Tryton traffic must never go through a proxy.
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def ping(self) -> str:
        """Return the server version via JSON-RPC; raises on transport error."""
        response = httpx.post(
            f"{TRYTON_BASE_URL}/rpc/",
            json={"id": 1, "method": "common.server.version", "params": []},
            timeout=10,
            trust_env=False,
        )
        response.raise_for_status()
        return str(response.json()["result"])

    def search(self, model: str, domain: list[Any], fields: list[str]) -> list[dict[str, Any]]:
        # NOTE: the base64 domain MUST keep its '=' padding — Tryton's
        # urlsafe_b64decode raises "Incorrect padding" without it.
        encoded = base64.urlsafe_b64encode(json.dumps(domain).encode()).decode()
        response = self._client.get("/model/" + model, params={"d": encoded, "f": fields})
        response.raise_for_status()
        result = response.json()
        assert isinstance(result, list)
        return result

    def get(self, model: str, record_id: int, fields: list[str]) -> dict[str, Any]:
        response = self._client.get(f"/model/{model}/{record_id}", params={"f": fields})
        response.raise_for_status()
        result = response.json()
        assert isinstance(result, dict)
        return result

    def create_raw(self, model: str, data: dict[str, Any]) -> httpx.Response:
        return self._client.post("/model/" + model, json=data)

    def create(self, model: str, data: dict[str, Any]) -> dict[str, Any]:
        response = self.create_raw(model, data)
        assert response.status_code < 400, (
            f"POST {model} failed ({response.status_code}): {response.text[:500]}"
        )
        result = response.json()
        assert isinstance(result, dict)
        return result

    def update(self, model: str, record_id: int, data: dict[str, Any]) -> dict[str, Any]:
        response = self._client.put(f"/model/{model}/{record_id}", json=data)
        assert response.status_code < 400, (
            f"PUT {model}/{record_id} failed ({response.status_code}): {response.text[:500]}"
        )
        result = response.json()
        assert isinstance(result, dict)
        return result


def _get_or_create(
    rest: TrytonRest,
    model: str,
    domain: list[Any],
    fields: list[str],
    values: dict[str, Any],
) -> dict[str, Any]:
    rows = rest.search(model, domain, fields)
    if rows:
        return rows[0]
    return rest.create(model, values)


@dataclass
class Fixture:
    """Ids of the dedicated US-004 fixture records in the Tryton test DB."""

    irr_id: int
    company_id: int
    cash_account_id: int
    revenue_account_id: int
    journal_id: int
    periods: dict[int, int] = field(default_factory=dict)  # Jalali year -> period id


@pytest.fixture(scope="module")
def rest() -> Generator[TrytonRest, None, None]:
    """REST client; skips every live test when trytond is not reachable."""
    client = TrytonRest()
    try:
        version = client.ping()
    except (httpx.TransportError, httpx.HTTPStatusError, KeyError, ValueError):
        client.close()
        pytest.skip(
            "trytond not reachable at "
            f"{TRYTON_BASE_URL} — start it per docs/dev-setup.md §3"
        )
    assert version.startswith("8.2"), f"Expected Tryton 8.2.x, got {version!r}"
    yield client
    client.close()


@pytest.fixture(scope="module")
def fx(rest: TrytonRest) -> Fixture:
    """Idempotently ensure the US-004 fixture records exist (shared per run)."""
    # 1. IRR currency with zero decimal digits (digits=0, rounding=1).
    rows = rest.search(
        "currency.currency", [["code", "=", "IRR"]], ["id", "digits", "rounding"]
    )
    if rows:
        irr = rows[0]
        if irr["digits"] != 0 or _decode_decimal(irr["rounding"]) != Decimal(1):
            # US-004 owns the IRR currency configuration: correct it.
            rest.update(
                "currency.currency",
                int(irr["id"]),
                {"digits": 0, "rounding": 1},
            )
    else:
        irr = rest.create(
            "currency.currency",
            {
                "name": "Iranian Rial",
                "symbol": "﷼",
                "code": "IRR",
                "numeric_code": "364",
                "digits": 0,
                "rounding": 1,
            },
        )
    irr_id = int(irr["id"])

    # Base-currency rate of 1 (official docs best practice for the currency
    # other rates are relative to).
    rates = rest.search("currency.currency.rate", [["currency", "=", irr_id]], ["id"])
    if not rates:
        rest.create(
            "currency.currency.rate",
            {"currency": irr_id, "date": "2026-03-21", "rate": 1},
        )

    # 2. Dedicated test company (party + company with IRR currency).
    party = _get_or_create(
        rest, "party.party", [["name", "=", COMPANY_NAME]], ["id"], {"name": COMPANY_NAME}
    )
    company = _get_or_create(
        rest,
        "company.company",
        [["party", "=", party["id"]]],
        ["id"],
        {"party": party["id"], "currency": irr_id, "timezone": "Asia/Tehran"},
    )
    company_id = int(company["id"])

    # 3. Give the admin REST user access to this company. The account
    #    module's "User in companies" rule requires the company to be in
    #    the user's companies AND company_filter='all' (with filter 'one'
    #    and no current company, get_companies() returns []).
    admins = rest.search("res.user", [["login", "=", "admin"]], ["id", "companies", "company_filter"])
    assert admins, "admin user not found in the Tryton test DB"
    admin = admins[0]
    admin_companies = [int(c) for c in admin.get("companies") or []]
    user_update: dict[str, Any] = {}
    if company_id not in admin_companies:
        user_update["companies"] = [["add", [company_id]]]
    if admin.get("company_filter") != "all":
        user_update["company_filter"] = "all"
    if user_update:
        rest.update("res.user", int(admin["id"]), user_update)

    # 4. Account types + accounts (cash 1000, revenue 4000).
    asset_type = _get_or_create(
        rest,
        "account.account.type",
        [["name", "=", "IRR Test Assets"], ["company", "=", company_id]],
        ["id"],
        {"name": "IRR Test Assets", "company": company_id, "statement": "balance"},
    )
    revenue_type = _get_or_create(
        rest,
        "account.account.type",
        [["name", "=", "IRR Test Revenue"], ["company", "=", company_id]],
        ["id"],
        {"name": "IRR Test Revenue", "company": company_id, "statement": "income"},
    )
    cash = _get_or_create(
        rest,
        "account.account",
        [["code", "=", CASH_ACCOUNT_CODE], ["company", "=", company_id]],
        ["id"],
        {
            "name": "IRR Test Cash",
            "code": CASH_ACCOUNT_CODE,
            "company": company_id,
            "type": asset_type["id"],
        },
    )
    revenue = _get_or_create(
        rest,
        "account.account",
        [["code", "=", REVENUE_ACCOUNT_CODE], ["company", "=", company_id]],
        ["id"],
        {
            "name": "IRR Test Revenue",
            "code": REVENUE_ACCOUNT_CODE,
            "company": company_id,
            "type": revenue_type["id"],
        },
    )

    # 5. Shared "Miscellaneous" journal (journals are not company-scoped).
    journals = rest.search("account.journal", [["name", "=", "Miscellaneous"]], ["id"])
    assert journals, "Miscellaneous journal not found — is the account module activated?"
    journal_id = int(journals[0]["id"])

    # 6. Fiscal years + full-year periods. Each fiscal year needs its OWN
    #    move sequence (uniqueness rule) of the account module's
    #    "Account Move" sequence type, owned by our company (domain rule).
    seq_types = rest.search("ir.sequence.type", [["name", "=", "Account Move"]], ["id"])
    assert seq_types, '"Account Move" sequence type not found'
    seq_type_id = int(seq_types[0]["id"])
    periods: dict[int, int] = {}
    for jalali_year, start, end in FISCAL_YEARS:
        fy_rows = rest.search(
            "account.fiscalyear",
            [["name", "=", f"FY {jalali_year} IRR Test"], ["company", "=", company_id]],
            ["id"],
        )
        if fy_rows:
            fy_id = int(fy_rows[0]["id"])
        else:
            seq = rest.create(
                "ir.sequence.strict",
                {
                    "name": f"HesabYar IRR Test Move Sequence {jalali_year}",
                    "sequence_type": seq_type_id,
                    "company": company_id,
                },
            )
            fy = rest.create(
                "account.fiscalyear",
                {
                    "name": f"FY {jalali_year} IRR Test",
                    "start_date": start,
                    "end_date": end,
                    "company": company_id,
                    "move_sequence": seq["id"],
                },
            )
            fy_id = int(fy["id"])
        period = _get_or_create(
            rest,
            "account.period",
            [["name", "=", f"{jalali_year} IRR Test"], ["fiscalyear", "=", fy_id]],
            ["id"],
            {
                "name": f"{jalali_year} IRR Test",
                "start_date": start,
                "end_date": end,
                "fiscalyear": fy_id,
                "type": "standard",
            },
        )
        periods[jalali_year] = int(period["id"])

    return Fixture(
        irr_id=irr_id,
        company_id=company_id,
        cash_account_id=int(cash["id"]),
        revenue_account_id=int(revenue["id"]),
        journal_id=journal_id,
        periods=periods,
    )


@pytest.fixture()
def adapter(
    rest: TrytonRest, monkeypatch: pytest.MonkeyPatch
) -> Generator[TrytonRestClient, None, None]:
    """The US-003 adapter's real REST client, against the live server.

    Scrubs the VM's unparseable proxy env vars first: httpx 0.28 raises
    ``InvalidURL`` constructing any ``httpx.Client()`` with them present,
    and ``TrytonRestClient`` does not pass ``trust_env=False`` (see the
    ``TestAdapterRejectsFractionalRial`` docstring / dev-setup.md §7).
    """
    for var in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)
    client = TrytonRestClient(TRYTON_BASE_URL, TRYTON_DB, TRYTON_REST_KEY)
    assert client.ping()
    yield client
    client.close()


def _create_balanced_move(
    rest: TrytonRest,
    fx: Fixture,
    amount: int | dict[str, str],
    gregorian_date: str,
    period_id: int,
    description: str,
) -> httpx.Response:
    """POST a balanced draft move (debit cash / credit revenue). Raw response."""
    body: dict[str, Any] = {
        "company": fx.company_id,
        "period": period_id,
        "journal": fx.journal_id,
        "date": gregorian_date,
        "description": f"US-004 {description} [{uuid.uuid4().hex[:8]}]",
        "lines": [
            [
                "create",
                [
                    {"account": fx.cash_account_id, "debit": amount, "credit": 0},
                    {"account": fx.revenue_account_id, "debit": 0, "credit": amount},
                ],
            ]
        ],
    }
    return rest.create_raw("account.move", body)


class TestIrrCurrencyConfig:
    """Criterion 1 (config half): IRR is a zero-decimal currency in Tryton."""

    def test_irr_currency_has_zero_digits(self, rest: TrytonRest, fx: Fixture) -> None:
        rows = rest.search(
            "currency.currency",
            [["code", "=", "IRR"]],
            ["code", "name", "symbol", "numeric_code", "digits", "rounding"],
        )
        assert len(rows) == 1
        irr = rows[0]
        assert irr["id"] == fx.irr_id
        assert irr["code"] == "IRR"
        assert irr["numeric_code"] == "364"  # ISO 4217 numeric code for IRR
        assert irr["digits"] == 0
        assert _decode_decimal(irr["rounding"]) == Decimal(1)

    def test_irr_has_base_rate_of_one(self, rest: TrytonRest, fx: Fixture) -> None:
        rates = rest.search(
            "currency.currency.rate", [["currency", "=", fx.irr_id]], ["date", "rate"]
        )
        assert rates, "IRR has no exchange rate — the base currency needs rate 1"
        assert any(_decode_decimal(r["rate"]) == Decimal(1) for r in rates)


class TestLargeIntegerRoundTrip:
    """Criterion 1 (evidence half): large integer IRR amounts, zero loss."""

    @pytest.mark.parametrize("amount", LARGE_AMOUNTS)
    def test_large_integer_amount_round_trip(
        self, rest: TrytonRest, fx: Fixture, amount: int
    ) -> None:
        money = MoneyIRR(amount)
        response = _create_balanced_move(
            rest, fx, money.amount, "2026-04-04", fx.periods[1405], f"amount {amount:,}"
        )
        assert response.status_code == 200, response.text[:500]
        move_id = int(response.json()["id"])

        move = rest.get("account.move", move_id, ["state", "date"])
        assert move["state"] == "draft"
        assert _decode_date(move["date"]) == "2026-04-04"

        lines = rest.search(
            "account.move.line", [["move", "=", move_id]], ["account", "debit", "credit"]
        )
        assert len(lines) == 2
        by_account = {int(line["account"]): line for line in lines}
        debit = _decode_decimal(by_account[fx.cash_account_id]["debit"])
        credit = _decode_decimal(by_account[fx.revenue_account_id]["credit"])
        # Bit-exact: Decimal equality, no float anywhere in the path.
        assert debit == Decimal(amount)
        assert credit == Decimal(amount)
        assert MoneyIRR(int(debit)) == money


class TestFractionalRejection:
    """Tryton itself rejects fractional Rials for a digits=0 currency.

    ``ModelStorage`` validates Monetary values against the currency digits
    (``(None, 0)`` for IRR): any Decimal with a negative exponent raises
    ``DigitsValidationError``. The adapter (criterion 2) must reject such
    values even earlier, before they reach Tryton — see
    ``TestAdapterRejectsFractionalRial``.
    """

    @pytest.mark.parametrize("fraction", ["100.5", "0.1", "1750000.5"])
    def test_tryton_rejects_fractional_rial_decimal(
        self, rest: TrytonRest, fx: Fixture, fraction: str
    ) -> None:
        response = _create_balanced_move(
            rest,
            fx,
            _decimal_json(fraction),
            "2026-04-04",
            fx.periods[1405],
            f"fractional {fraction}",
        )
        assert response.status_code == 400, (
            f"Expected 400 for fractional amount {fraction}, got "
            f"{response.status_code}: {response.text[:500]}"
        )
        assert "digits" in response.text.lower()

    def test_tryton_rejects_fractional_rial_json_float(
        self, rest: TrytonRest, fx: Fixture
    ) -> None:
        # A JSON float 100.5 is rejected the same way (never silently rounded).
        body: dict[str, Any] = {
            "company": fx.company_id,
            "period": fx.periods[1405],
            "journal": fx.journal_id,
            "date": "2026-04-04",
            "description": f"US-004 float fractional [{uuid.uuid4().hex[:8]}]",
            "lines": [
                [
                    "create",
                    [
                        {"account": fx.cash_account_id, "debit": 100.5, "credit": 0},
                        {"account": fx.revenue_account_id, "debit": 0, "credit": 100.5},
                    ],
                ]
            ],
        }
        response = rest.create_raw("account.move", body)
        assert response.status_code == 400, response.text[:500]
        assert "digits" in response.text.lower()


class TestJalaliRoundTrip:
    """Criterion 3: FiscalDate mapping verified against Tryton date fields."""

    @pytest.mark.parametrize(
        ("jalali_input", "canonical"),
        [
            ("1405/01/01", "1405/01/01"),  # Nowruz, first day of the year
            ("1405/01/15", "1405/01/15"),
            ("1405/06/31", "1405/06/31"),  # last day of a 31-day month
            ("1405/07/01", "1405/07/01"),  # first day of a 30-day month
            ("1405/12/29", "1405/12/29"),  # last day of non-leap 1405
            ("1403/12/30", "1403/12/30"),  # leap day (1403 is a leap year)
            ("1403/12/29", "1403/12/29"),
            ("1404/01/01", "1404/01/01"),
            ("1404/12/29", "1404/12/29"),
            ("1405-02-30", "1405/02/30"),  # dash separator accepted
            ("۱۴۰۵/۰۱/۱۵", "1405/01/15"),  # Persian digits
            ("١٤٠٥/٠١/١٥", "1405/01/15"),  # Arabic-Indic digits
        ],
    )
    def test_jalali_date_round_trip_via_tryton(
        self, rest: TrytonRest, fx: Fixture, jalali_input: str, canonical: str
    ) -> None:
        fiscal = FiscalDate.from_jalali_str(jalali_input)
        assert fiscal.to_jalali_str() == canonical
        gregorian = fiscal.to_gregorian_str()

        response = _create_balanced_move(
            rest,
            fx,
            1_000_000,
            gregorian,
            fx.periods[fiscal.jalali_year],
            f"jalali {canonical}",
        )
        assert response.status_code == 200, response.text[:500]
        move_id = int(response.json()["id"])

        move = rest.get("account.move", move_id, ["date"])
        read_back = FiscalDate.from_iso_str(_decode_date(move["date"]))
        assert read_back == fiscal
        assert read_back.to_jalali_str() == canonical
        assert read_back.to_gregorian_str() == gregorian


class TestAdapterRejectsFractionalRial:
    """Criterion 2: the US-003 adapter rejects fractional Rials.

    Verified at the two boundaries that exist in the tree:

    - Transport (``TrytonRestClient``): a fractional Decimal/float sent to
      Tryton comes back as a 400, which the client translates to the stable
      ``TrytonAdapterError`` (code ``ENGINE_VALIDATION_FAILED``) — never a
      silent round, and no move is created.
    - Command boundary (``CreateJournalDraft`` / ``JournalDraftLine``):
      amounts are ``MoneyIRR``, which is integer-only at construction, so a
      fractional Rial cannot even be expressed as adapter input.

    NOTE (environment quirk, not adapter behaviour): this VM exports proxy
    variables whose ``no_proxy`` list contains bracketed IPv6 entries that
    httpx 0.28 cannot parse, so a plain ``httpx.Client()`` raises
    ``InvalidURL`` at construction. ``TrytonRestClient`` does not pass
    ``trust_env=False``, so these tests scrub the proxy variables before
    constructing it. The adapter itself should be fixed to pass
    ``trust_env=False`` (or otherwise not trust the environment) — see
    docs/dev-setup.md §7.
    """

    @pytest.mark.parametrize("fraction", [Decimal("100.5"), Decimal("0.1"), Decimal("1750000.5")])
    def test_adapter_client_rejects_fractional_decimal(
        self, rest: TrytonRest, fx: Fixture, adapter: TrytonRestClient, fraction: Decimal
    ) -> None:
        description = f"US-004 adapter fractional {fraction} [{uuid.uuid4().hex[:8]}]"
        with pytest.raises(TrytonAdapterError) as exc_info:
            adapter.create(
                "account.move",
                {
                    "company": fx.company_id,
                    "period": fx.periods[1405],
                    "journal": fx.journal_id,
                    "date": "2026-04-04",
                    "description": description,
                    "lines": [
                        [
                            "create",
                            [
                                {"account": fx.cash_account_id, "debit": fraction, "credit": 0},
                                {"account": fx.revenue_account_id, "debit": 0, "credit": fraction},
                            ],
                        ]
                    ],
                },
            )
        assert exc_info.value.code == ENGINE_VALIDATION_FAILED
        assert exc_info.value.status_code == 400
        # Nothing was silently created or rounded: no move with that description.
        assert rest.search("account.move", [["description", "=", description]], ["id"]) == []

    def test_adapter_client_rejects_fractional_float(
        self, rest: TrytonRest, fx: Fixture, adapter: TrytonRestClient
    ) -> None:
        description = f"US-004 adapter float fractional [{uuid.uuid4().hex[:8]}]"
        with pytest.raises(TrytonAdapterError) as exc_info:
            adapter.create(
                "account.move",
                {
                    "company": fx.company_id,
                    "period": fx.periods[1405],
                    "journal": fx.journal_id,
                    "date": "2026-04-04",
                    "description": description,
                    "lines": [
                        [
                            "create",
                            [
                                {"account": fx.cash_account_id, "debit": 100.5, "credit": 0},
                                {"account": fx.revenue_account_id, "debit": 0, "credit": 100.5},
                            ],
                        ]
                    ],
                },
            )
        assert exc_info.value.code == ENGINE_VALIDATION_FAILED
        assert rest.search("account.move", [["description", "=", description]], ["id"]) == []

    def test_adapter_client_round_trips_large_integer(
        self, rest: TrytonRest, fx: Fixture, adapter: TrytonRestClient
    ) -> None:
        # The adapter's wire decoding (Decimal dict -> Decimal) is exact too.
        amount = 9_876_543_210_987
        created = adapter.create(
            "account.move",
            {
                "company": fx.company_id,
                "period": fx.periods[1405],
                "journal": fx.journal_id,
                "date": "2026-04-04",
                "description": f"US-004 adapter large int [{uuid.uuid4().hex[:8]}]",
                "lines": [
                    [
                        "create",
                        [
                            {"account": fx.cash_account_id, "debit": amount, "credit": 0},
                            {"account": fx.revenue_account_id, "debit": 0, "credit": amount},
                        ],
                    ]
                ],
            },
        )
        move_id = int(created["id"])
        lines = adapter.search(
            "account.move.line", [["move", "=", move_id]], ["account", "debit", "credit"]
        )
        by_account = {int(line["account"]): line for line in lines}
        assert by_account[fx.cash_account_id]["debit"] == Decimal(amount)
        assert by_account[fx.revenue_account_id]["credit"] == Decimal(amount)

    def test_journal_command_cannot_carry_fractional_rial(self) -> None:
        # Pure-domain: runs without a server. Refs are opaque ints here —
        # construction is what is under test, not any engine record.
        # The command boundary is structurally integer-only: MoneyIRR
        # rejects every non-int amount at construction, so no fractional
        # Rial can ever reach the adapter as a journal amount.
        with pytest.raises(InvalidMoneyError):
            MoneyIRR(Decimal("100.5"))  # type: ignore[arg-type]  # intentional: must reject
        with pytest.raises(InvalidMoneyError):
            MoneyIRR(100.5)  # type: ignore[arg-type]  # intentional: must reject
        # ...while a well-formed integer command builds fine.
        command = CreateJournalDraft(
            company=CompanyRef(1),
            date=FiscalDate.from_jalali_str("1405/01/15"),
            lines=(
                JournalDraftLine(account=AccountRef(1), debit=MoneyIRR(1_000_000)),
                JournalDraftLine(account=AccountRef(2), credit=MoneyIRR(1_000_000)),
            ),
            description="US-004 command boundary check",
        )
        assert command.total_debit == MoneyIRR(1_000_000)
        assert command.total_credit == MoneyIRR(1_000_000)


class TestAdapterPortEndToEnd:
    """End-to-end through the port-level adapter (US-003's public interface).

    ``TrytonAccountingAdapter.create_journal_draft`` is the path production
    code will use, so the large-integer + Jalali guarantees are re-proven
    through it — not just through raw REST.
    """

    def test_adapter_draft_round_trips_large_integer_and_jalali(
        self, rest: TrytonRest, fx: Fixture, adapter: TrytonRestClient
    ) -> None:
        engine = TrytonAccountingAdapter(adapter)
        amount = 9_876_543_210_987
        fiscal = FiscalDate.from_jalali_str("1405/01/15")
        command = CreateJournalDraft(
            company=CompanyRef(fx.company_id),
            # Journal must be explicit: the DB has two general journals
            # ("Miscellaneous" and "Stock"), so the adapter cannot pick one.
            journal=JournalRef(fx.journal_id),
            date=fiscal,
            lines=(
                JournalDraftLine(
                    account=AccountRef(fx.cash_account_id), debit=MoneyIRR(amount)
                ),
                JournalDraftLine(
                    account=AccountRef(fx.revenue_account_id), credit=MoneyIRR(amount)
                ),
            ),
            description=f"US-004 adapter e2e [{uuid.uuid4().hex[:8]}]",
        )
        ref = engine.create_journal_draft(command)

        view = engine.get_journal_draft(ref)
        assert view is not None
        assert view.state == "draft"
        assert view.date == fiscal
        assert view.date.to_jalali_str() == "1405/01/15"
        assert view.total_debit == MoneyIRR(amount)
        assert view.total_credit == MoneyIRR(amount)
        assert view.is_balanced

        summary = engine.validate_journal_draft(ref)
        assert summary.valid, f"adapter validation failed: {summary.issues}"

    def test_adapter_mapping_rejects_fractional_amount_from_engine(self) -> None:
        # Read-side enforcement (pure-domain, no server): if a fractional
        # Rial ever came back from the engine, the mapping rejects it with
        # InvalidMoneyError instead of rounding it.
        with pytest.raises(InvalidMoneyError):
            money_from_tryton(Decimal("100.5"))
        with pytest.raises(InvalidMoneyError):
            money_from_tryton(100.5)
        with pytest.raises(InvalidMoneyError):
            money_from_tryton(Decimal("NaN"))
        assert money_from_tryton(Decimal("100")) == MoneyIRR(100)
        assert money_from_tryton(Decimal("9876543210987")) == MoneyIRR(9_876_543_210_987)
        assert money_from_tryton(1750000) == MoneyIRR(1_750_000)


class TestFiscalDateValidation:
    """Pure-domain edge cases — these run even without a Tryton server."""

    @pytest.mark.parametrize(
        "bad_input",
        [
            "1405/12/30",  # Esfand 30 in non-leap 1405
            "1405/13/01",  # month 13
            "1405/07/31",  # Mehr has 30 days
            "1405/00/10",  # month 0
            "1405/01/00",  # day 0
            "2026/04/04",  # Gregorian year: ambiguous, out of range 1200-1500
            "15/01/1405",  # wrong order / bad format
            "not-a-date",
            "",
        ],
    )
    def test_invalid_jalali_dates_rejected(self, bad_input: str) -> None:
        with pytest.raises(InvalidDateError):
            FiscalDate.from_jalali_str(bad_input)

    def test_iso_parser_rejects_slash_dates(self) -> None:
        with pytest.raises(InvalidDateError):
            FiscalDate.from_iso_str("2026/04/04")

    def test_fiscal_date_rejects_datetimes(self) -> None:
        with pytest.raises(InvalidDateError):
            FiscalDate(datetime.datetime(2026, 4, 4, 12, 0))

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("1403/12/30", "1404/01/01"),  # leap-day boundary
            ("1405/06/31", "1405/07/01"),  # 31-day to 30-day month boundary
        ],
    )
    def test_jalali_day_after_is_next_day(self, first: str, second: str) -> None:
        assert FiscalDate.from_jalali_str(first) + datetime.timedelta(days=1) == (
            FiscalDate.from_jalali_str(second)
        )


class TestMoneyIrrValidation:
    """Pure-domain money rules — these run even without a Tryton server."""

    @pytest.mark.parametrize(
        "bad_value",
        [1.5, 100.5, Decimal("100.5"), True, "1750000"],
    )
    def test_moneyirr_rejects_non_integer_amounts(self, bad_value: Any) -> None:
        with pytest.raises(InvalidMoneyError):
            MoneyIRR(bad_value)

    def test_moneyirr_accepts_large_integers(self) -> None:
        for amount in LARGE_AMOUNTS:
            assert MoneyIRR(amount).amount == amount
