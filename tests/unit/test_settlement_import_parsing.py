"""Unit tests for settlement file parsing (US-523).

Pure: grids in, parsed rows out. Covers header auto-detection via
the Persian/English synonym map, inline column-mapping override,
per-import currency (Toman ×10), Net Sale / totals rows dropped at
parse, the file-internal arithmetic check, negative rows, the row
cap, and mapping validation.
"""

from __future__ import annotations

import pytest

from hesabyar.domain.settlement.errors import SettlementImportInvalidError
from hesabyar.domain.settlement.import_parsing import (
    rows_from_settlement_grid,
    suggest_settlement_mapping,
    validate_settlement_mapping_columns,
)

PERSIAN_HEADERS = [
    "شماره تراکنش",
    "تاریخ تراکنش",
    "مبلغ ناخالص",
    "کارمزد",
    "مالیات",
    "مبلغ خالص",
    "نوع تراکنش",
]

COLUMNS = {
    "transaction_id": "شماره تراکنش",
    "date": "تاریخ تراکنش",
    "gross": "مبلغ ناخالص",
    "fee": "کارمزد",
    "vat": "مالیات",
    "net": "مبلغ خالص",
    "tran_type": "نوع تراکنش",
}


def _grid(*rows: list[str | None]) -> list[list[str | None]]:
    first: list[str | None] = [*PERSIAN_HEADERS]
    return [first, *rows]


def test_header_auto_detect_persian() -> None:
    """Persian headers map to canonical fields without an inline mapping."""
    suggested = suggest_settlement_mapping(PERSIAN_HEADERS)
    assert suggested["transaction_id"] == "شماره تراکنش"
    assert suggested["date"] == "تاریخ تراکنش"
    assert suggested["gross"] == "مبلغ ناخالص"
    assert suggested["fee"] == "کارمزد"
    assert suggested["vat"] == "مالیات"
    assert suggested["net"] == "مبلغ خالص"
    assert suggested["tran_type"] == "نوع تراکنش"


def test_header_auto_detect_english() -> None:
    """English headers map too (the portal may export English columns)."""
    suggested = suggest_settlement_mapping(
        ["Transaction ID", "Date", "Gross", "Fee", "VAT", "Net", "Type"]
    )
    assert suggested["transaction_id"] == "Transaction ID"
    assert suggested["date"] == "Date"
    assert suggested["gross"] == "Gross"
    assert suggested["fee"] == "Fee"
    assert suggested["vat"] == "VAT"
    assert suggested["net"] == "Net"


def test_parse_detail_row_rial() -> None:
    """A clean detail row parses with Jalali dates and Rial amounts."""
    parsed = rows_from_settlement_grid(
        _grid(["TXN-1", "1405/08/15", "1000000", "100000", "10000", "890000", "فروش"]),
        COLUMNS,
        currency_unit="rial",
        date_kind="jalali",
    )
    assert len(parsed.valid_rows) == 1
    row = parsed.valid_rows[0]
    assert row.transaction_id == "TXN-1"
    assert row.tran_type == "sale"
    assert row.gross_irr == 1_000_000
    assert row.fee_irr == 100_000
    assert row.vat_irr == 10_000
    assert row.net_irr == 890_000
    assert row.row_date is not None
    assert row.row_date.gregorian_date.isoformat() == "2026-11-06"


def test_parse_toman_converts_to_rial() -> None:
    """A Toman file converts ×10 at the import boundary."""
    parsed = rows_from_settlement_grid(
        _grid(["TXN-1", "1405/08/15", "100000", "10000", "1000", "89000", "فروش"]),
        COLUMNS,
        currency_unit="toman",
        date_kind="jalali",
    )
    row = parsed.valid_rows[0]
    assert row.gross_irr == 1_000_000
    assert row.fee_irr == 100_000
    assert row.vat_irr == 10_000
    assert row.net_irr == 890_000


def test_net_sale_and_totals_rows_dropped() -> None:
    """Net Sale / totals rows are dropped and logged, never matched or summed."""
    parsed = rows_from_settlement_grid(
        _grid(
            ["TXN-1", "1405/08/15", "1000000", "100000", "10000", "890000", "فروش"],
            ["", "1405/08/15", "1000000", "100000", "10000", "890000", "Net Sale"],
            ["", "1405/08/15", "1000000", "100000", "10000", "890000", "جمع کل"],
        ),
        COLUMNS,
        currency_unit="rial",
        date_kind="jalali",
    )
    assert len(parsed.valid_rows) == 1
    assert len(parsed.dropped_rows) == 2
    assert parsed.dropped_rows[0].row_number == 3
    assert parsed.dropped_rows[1].row_number == 4
    assert "dropped at parse" in parsed.dropped_rows[0].reason


def test_friday_never_matches_totals_marker() -> None:
    """Whole-token matching: 'جمعه' (Friday) must not match 'جمع' (total)."""
    parsed = rows_from_settlement_grid(
        _grid(["TXN-1", "1405/08/15", "1000000", "100000", "10000", "890000", "جمعه"]),
        COLUMNS,
        currency_unit="rial",
        date_kind="jalali",
    )
    assert len(parsed.valid_rows) == 1
    assert len(parsed.dropped_rows) == 0


def test_arithmetic_mismatch_is_invalid() -> None:
    """net != gross − fee − vat makes the row invalid (never silently fixed)."""
    parsed = rows_from_settlement_grid(
        _grid(["TXN-1", "1405/08/15", "1000000", "100000", "10000", "900000", "فروش"]),
        COLUMNS,
        currency_unit="rial",
        date_kind="jalali",
    )
    assert len(parsed.valid_rows) == 0
    assert len(parsed.invalid_rows) == 1
    assert "does not equal" in parsed.invalid_rows[0].errors[0]


def test_missing_net_derived_from_gross_and_fee() -> None:
    """A missing net leg is derived (gross − fee − vat) when the others are present."""
    columns = {k: v for k, v in COLUMNS.items() if k != "net"}
    grid: list[list[str | None]] = [
        ["شماره تراکنش", "تاریخ تراکنش", "مبلغ ناخالص", "کارمزد", "مالیات", "نوع تراکنش"],
        ["TXN-1", "1405/08/15", "1000000", "100000", "10000", "فروش"],
    ]
    parsed = rows_from_settlement_grid(
        grid, columns, currency_unit="rial", date_kind="jalali"
    )
    assert len(parsed.valid_rows) == 1
    assert parsed.valid_rows[0].net_irr == 890_000


def test_negative_refund_row_allowed() -> None:
    """Negative rows (refunds) parse — a refund is not an error."""
    parsed = rows_from_settlement_grid(
        _grid(["TXN-2", "1405/08/16", "-500000", "-50000", "-5000", "-445000", "برگشت"]),
        COLUMNS,
        currency_unit="rial",
        date_kind="jalali",
    )
    assert len(parsed.valid_rows) == 1
    row = parsed.valid_rows[0]
    assert row.tran_type == "refund"
    assert row.net_irr == -445_000


def test_gregorian_dates_accepted() -> None:
    """Gregorian files parse when date_kind is gregorian."""
    parsed = rows_from_settlement_grid(
        _grid(["TXN-1", "2026-11-06", "1000000", "100000", "10000", "890000", "sale"]),
        COLUMNS,
        currency_unit="rial",
        date_kind="gregorian",
    )
    assert len(parsed.valid_rows) == 1
    assert parsed.valid_rows[0].row_date is not None
    assert parsed.valid_rows[0].row_date.gregorian_date.isoformat() == "2026-11-06"


def test_row_cap_enforced() -> None:
    """More than max_rows data rows raises (the operator splits the file)."""
    rows: list[list[str | None]] = [
        ["TXN-1", "1405/08/15", "1000000", "100000", "10000", "890000", "فروش"],
        ["TXN-2", "1405/08/15", "1000000", "100000", "10000", "890000", "فروش"],
        ["TXN-3", "1405/08/15", "1000000", "100000", "10000", "890000", "فروش"],
    ]
    with pytest.raises(SettlementImportInvalidError, match="over the 2-row cap"):
        rows_from_settlement_grid(
            _grid(*rows), COLUMNS, currency_unit="rial", date_kind="jalali", max_rows=2
        )


def test_unknown_currency_rejected() -> None:
    """An unknown currency unit fails closed at the boundary."""
    with pytest.raises(SettlementImportInvalidError, match="Unknown currency_unit"):
        rows_from_settlement_grid(
            _grid(["TXN-1", "1405/08/15", "1000000", "100000", "10000", "890000", "فروش"]),
            COLUMNS,
            currency_unit="usd",
            date_kind="jalali",
        )


def test_unknown_date_kind_rejected() -> None:
    """An unknown date kind fails closed at the boundary."""
    with pytest.raises(SettlementImportInvalidError, match="Unknown date_kind"):
        rows_from_settlement_grid(
            _grid(["TXN-1", "1405/08/15", "1000000", "100000", "10000", "890000", "فروش"]),
            COLUMNS,
            currency_unit="rial",
            date_kind="hijri",
        )


def test_validate_mapping_detail_requires_transaction_and_date() -> None:
    """A detail file without transaction_id/date columns is rejected."""
    problems = validate_settlement_mapping_columns({"gross": "مبلغ"}, "detail")
    assert any("transaction_id" in p for p in problems)
    assert any("date" in p for p in problems)


def test_validate_mapping_payouts_requires_date_and_net() -> None:
    """A payouts file without date/net columns is rejected."""
    problems = validate_settlement_mapping_columns({"gross": "مبلغ"}, "payouts")
    assert any("date" in p for p in problems)
    assert any("net" in p for p in problems)
    assert validate_settlement_mapping_columns(
        {"date": "تاریخ", "net": "خالص"}, "payouts"
    ) == []


def test_validate_mapping_detail_ok() -> None:
    """A complete detail mapping validates cleanly."""
    assert validate_settlement_mapping_columns(COLUMNS, "detail") == []


def test_empty_date_is_invalid() -> None:
    """A row without a date is invalid, not guessed."""
    parsed = rows_from_settlement_grid(
        _grid(["TXN-1", "", "1000000", "100000", "10000", "890000", "فروش"]),
        COLUMNS,
        currency_unit="rial",
        date_kind="jalali",
    )
    assert len(parsed.invalid_rows) == 1
    assert "date is empty" in parsed.invalid_rows[0].errors
