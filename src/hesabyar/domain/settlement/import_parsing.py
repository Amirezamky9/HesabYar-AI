"""Pure settlement-file parsing (US-523, Phase 5-3 Wave B).

No I/O here: the infrastructure file reader turns bytes into a grid
(``Sequence[Sequence[str | None]]``) and this module turns the grid
into parsed settlement rows under a column mapping. Everything is
deterministic and unit-testable without a database or the engine.

Parsing rules (phased design §3.2ب + §3 §1.3 SnappPay):

- Persian/Arabic-Indic digits normalise to Latin before any numeric
  parse; thousands separators (``٬`` ``,`` space) are stripped
  (reused from :mod:`hesabyar.domain.bank.parsing`).
- Amounts are whole Rial inside the system. A file in Toman converts
  ×10 at this boundary; a fractional Rial after conversion rounds
  HALF_UP and the row is flagged ``was_rounded``. The per-import
  currency setting is the operator's declaration — the SnappPay Excel
  currency is UNVERIFIED until the first real customer file.
- Dates are Jalali by default (``date_kind='jalali'`` on the import);
  Gregorian is opt-in per import.
- ``Net Sale`` rows and totals rows are DROPPED at parse time and
  logged (returned as :class:`DroppedRow` entries): the first row of
  the SnappPay monthly detail Excel is ``Net Sale`` = total net sales
  — it is a summary, not a settlement row, and must never be matched
  or summed.
- A row whose own arithmetic does not hold (``net ≠ gross − fee −
  vat`` when all are present) is a per-row error, never silently
  fixed: the file's own columns must agree before any contract
  comparison (a file-vs-contract fee difference is a
  ``fee_mismatch`` exception at matching time, not a parse error).
- Negative amounts (parenthesised or minus-signed) are legitimate:
  refunds and negative adjustments arrive as negative rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from hesabyar.domain.bank.models import MAX_IMPORT_ROWS, FileCurrencyUnit
from hesabyar.domain.bank.parsing import (
    normalize_digits,
    normalize_persian_text,
    parse_amount,
    parse_statement_date,
)
from hesabyar.domain.common.dates import FiscalDate
from hesabyar.domain.settlement.errors import SettlementImportInvalidError

__all__ = [
    "ALL_SETTLEMENT_FIELDS",
    "DroppedRow",
    "ParsedSettlementFile",
    "ParsedSettlementRow",
    "rows_from_settlement_grid",
    "suggest_settlement_mapping",
    "validate_settlement_mapping_columns",
]

#: Canonical settlement-file fields (the mapping vocabulary).
ALL_SETTLEMENT_FIELDS: tuple[str, ...] = (
    "transaction_id",
    "date",
    "gross",
    "fee",
    "vat",
    "net",
    "deductions",
    "tracking_ref",
    "tran_type",
    "description",
)

#: Fields a detail file must map (a transaction row needs its id, its
#: date and at least one amount).
DETAIL_REQUIRED_FIELDS: tuple[str, ...] = ("transaction_id", "date")

#: Fields a payouts/statement file must map (an aggregate payout row
#: needs its date and its net).
PAYOUTS_REQUIRED_FIELDS: tuple[str, ...] = ("date", "net")

#: Header synonyms per canonical field (normalised before comparing).
#: Longer synonyms are tried first. SnappPay's exact Excel headers are
#: UNVERIFIED — these cover the likely Persian/English forms, and an
#: inline column mapping always overrides auto-detection.
_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "transaction_id": (
        "شماره تراکنش",
        "شناسه تراکنش",
        "کد تراکنش",
        "transaction id",
        "transaction",
        "تراکنش",
    ),
    "date": ("تاریخ تراکنش", "تاریخ واریز", "تاریخ تسویه", "تاریخ", "date"),
    "gross": ("مبلغ ناخالص", "مبلغ فروش", "مبلغ تراکنش", "gross", "ناخالص", "مبلغ"),
    "fee": ("کارمزد", "کمیسیون", "commission", "fee"),
    "vat": ("مالیات کارمزد", "مالیات بر ارزش افزوده", "مالیات", "vat", "tax"),
    "net": ("مبلغ خالص", "خالص پرداختی", "مبلغ تسویه", "net", "خالص"),
    "deductions": ("کسورات", "کسر", "بدهی دوره قبل", "deductions", "deduction"),
    "tracking_ref": ("شماره پیگیری", "شماره مرجع", "پیگیری", "tracking", "reference", "ref"),
    "tran_type": ("نوع تراکنش", "نوع ردیف", "نوع", "type"),
    "description": ("شرح تراکنش", "شرح", "توضیحات", "description"),
}

#: Row-type synonyms → canonical tran_type.
_TRAN_TYPE_SYNONYMS: dict[str, str] = {
    "sale": "sale",
    "فروش": "sale",
    "پرداخت": "sale",
    "refund": "refund",
    "برگشت": "refund",
    "عودت": "refund",
    "استرداد": "refund",
    "cancel": "cancel",
    "لغو": "cancel",
    "adjustment": "adjustment",
    "تعدیل": "adjustment",
    "deduction": "deduction",
    "کسورات": "deduction",
    "کسر": "deduction",
    "payout": "payout",
    "واریز": "payout",
    "تسویه": "payout",
}

#: Markers that identify a dropped summary row (Net Sale / totals).
#: Matched against the normalised tran_type / description / first
#: cell — whole-token equality, so "جمعه" (Friday) never matches "جمع".
_DROP_MARKERS: frozenset[str] = frozenset(
    {
        "net sale",
        "فروش خالص",
        "total",
        "totals",
        "جمع",
        "مجموع",
        "جمع کل",
        "sum",
    }
)


def _is_dropped_row(text_values: list[str]) -> str | None:
    """Return the drop reason when a row is a Net Sale / totals row.

    Checks the row's text cells (type, description, first cell) for a
    drop marker as a whole token. Returns the matched marker, or None.
    """
    for text in text_values:
        normalised = normalize_persian_text(text)
        if not normalised:
            continue
        tokens = set(normalised.split())
        for marker in _DROP_MARKERS:
            if marker in tokens or normalised == marker:
                return marker
        if "net sale" in normalised:
            return "net sale"
    return None


def normalize_tran_type(raw: str) -> str:
    """Normalise a raw row-type cell to its canonical tran_type.

    Known synonyms map to ``sale`` / ``refund`` / ``cancel`` /
    ``adjustment`` / ``deduction`` / ``payout``; anything else is
    kept as its normalised raw text so a new type surfaces visibly
    instead of being forced into a bucket.
    """
    normalised = normalize_persian_text(raw)
    return _TRAN_TYPE_SYNONYMS.get(normalised, normalised)


@dataclass(frozen=True)
class ParsedSettlementRow:
    """One grid row after parsing (before persistence).

    ``errors`` is non-empty exactly when the row is invalid; an
    invalid row keeps whatever parsed so the preview can show the
    operator what failed. Amounts are integer IRR (Toman ×10 at this
    boundary when the import declares Toman). ``raw`` keeps the
    canonical fields' original cell text for provenance.
    """

    row_number: int
    transaction_id: str | None
    tran_type: str | None
    row_date: FiscalDate | None
    gross_irr: int | None
    fee_irr: int | None
    vat_irr: int | None
    net_irr: int | None
    deductions_irr: int
    tracking_ref: str | None
    description: str
    raw: dict[str, str]
    errors: tuple[str, ...] = ()
    was_rounded: bool = False

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.row_date is not None and self.net_irr is not None


@dataclass(frozen=True)
class DroppedRow:
    """A summary row dropped at parse time (Net Sale / totals).

    Dropped rows are never matched or summed — but they are logged:
    the import result reports them so the operator can see exactly
    what the parser skipped and why.
    """

    row_number: int
    reason: str
    text: str


@dataclass(frozen=True)
class ParsedSettlementFile:
    """The parse result for one settlement file."""

    rows: tuple[ParsedSettlementRow, ...]
    dropped_rows: tuple[DroppedRow, ...]

    @property
    def valid_rows(self) -> tuple[ParsedSettlementRow, ...]:
        """Rows that parsed cleanly (matchable)."""
        return tuple(row for row in self.rows if row.is_valid)

    @property
    def invalid_rows(self) -> tuple[ParsedSettlementRow, ...]:
        """Rows with parse errors (counted, never matched)."""
        return tuple(row for row in self.rows if not row.is_valid)


def suggest_settlement_mapping(headers: list[str]) -> dict[str, str]:
    """Suggest a column mapping from header names (auto-detect).

    Each canonical field takes the first still-unused header whose
    normalised text contains one of the field's synonyms (longest
    synonym first). Fields with no matching header are absent from
    the result — the caller reports them as missing.
    """
    normalised = [normalize_persian_text(header) for header in headers]
    used: set[int] = set()
    suggested: dict[str, str] = {}
    for field in ALL_SETTLEMENT_FIELDS:
        for synonym in sorted(_HEADER_SYNONYMS[field], key=len, reverse=True):
            hit = next(
                (
                    index
                    for index, header in enumerate(normalised)
                    if index not in used and synonym in header
                ),
                None,
            )
            if hit is not None:
                suggested[field] = headers[hit]
                used.add(hit)
                break
    return suggested


def validate_settlement_mapping_columns(
    columns: dict[str, str], file_type: str
) -> list[str]:
    """Return the list of mapping problems (empty when valid).

    Detail files need ``transaction_id`` + ``date`` + at least one of
    ``net`` / ``gross``; payouts/statement files need ``date`` +
    ``net``. Unknown canonical field names are rejected (a typo must
    fail loudly, not silently drop a column).
    """
    problems: list[str] = []
    for field in columns:
        if field not in ALL_SETTLEMENT_FIELDS:
            problems.append(f"unknown column field: {field!r}")
    if file_type == "detail":
        required = DETAIL_REQUIRED_FIELDS
    elif file_type in ("payouts", "statement"):
        required = PAYOUTS_REQUIRED_FIELDS
    else:
        return [f"unknown file_type {file_type!r} (expected 'detail', 'payouts' or 'statement')"]
    for field in required:
        if field not in columns:
            problems.append(f"missing required column: {field!r}")
    if file_type == "detail" and "net" not in columns and "gross" not in columns:
        problems.append("a detail file needs a 'net' or a 'gross' column")
    return problems


def _resolve_column(column_ref: str, headers: list[str], field: str) -> int:
    """Resolve one mapping entry to a 0-based grid column index.

    ``#<n>`` is positional; otherwise the value is a header name
    matched after normalisation. Raises SettlementImportInvalidError
    when the column cannot be found — the mapping does not fit this file.
    """
    if column_ref.startswith("#"):
        try:
            index = int(column_ref[1:])
        except ValueError as exc:
            raise SettlementImportInvalidError(
                f"Column reference {column_ref!r} for {field!r} is not a valid positional reference.",
                details={"field": field, "column_ref": column_ref},
            ) from exc
        if index < 0 or index >= len(headers):
            raise SettlementImportInvalidError(
                f"Column reference {column_ref!r} for {field!r} is out of range "
                f"(the file has {len(headers)} columns).",
                details={"field": field, "column_ref": column_ref},
            )
        return index
    wanted = normalize_persian_text(column_ref)
    for index, header in enumerate(headers):
        if normalize_persian_text(header) == wanted:
            return index
    raise SettlementImportInvalidError(
        f"Column {column_ref!r} for {field!r} was not found in the file headers.",
        details={"field": field, "column_ref": column_ref},
    )


def _cell(row: list[str | None], index: int) -> str:
    if index >= len(row):
        return ""
    return str(row[index] or "")


def rows_from_settlement_grid(
    grid: list[list[str | None]],
    columns: dict[str, str],
    *,
    currency_unit: str,
    date_kind: str = "jalali",
    header_row: int = 0,
    file_type: str = "detail",
    max_rows: int = MAX_IMPORT_ROWS,
) -> ParsedSettlementFile:
    """Parse a cell grid into settlement rows under a column mapping.

    ``grid[header_row]`` is the header; later rows are data. Fully
    empty rows are skipped. ``Net Sale`` rows and totals rows are
    dropped and returned as :class:`DroppedRow` entries. More than
    ``max_rows`` data rows raises — the operator splits the file.
    """
    try:
        unit = FileCurrencyUnit(currency_unit)
    except ValueError as exc:
        raise SettlementImportInvalidError(
            f"Unknown currency_unit {currency_unit!r} (expected 'rial' or 'toman').",
            details={"currency_unit": currency_unit},
        ) from exc
    if date_kind not in ("jalali", "gregorian"):
        raise SettlementImportInvalidError(
            f"Unknown date_kind {date_kind!r} (expected 'jalali' or 'gregorian').",
            details={"date_kind": date_kind},
        )
    if header_row >= len(grid):
        raise SettlementImportInvalidError(
            f"Header row {header_row} is past the end of the file ({len(grid)} rows).",
            details={"header_row": header_row, "grid_rows": len(grid)},
        )
    headers = [str(cell or "") for cell in grid[header_row]]
    resolved = {
        field: _resolve_column(ref, headers, field) for field, ref in columns.items()
    }
    data_rows = [
        (index, row)
        for index, row in enumerate(grid[header_row + 1 :], start=header_row + 2)
        if any(str(cell or "").strip() for cell in row)
    ]
    if len(data_rows) > max_rows:
        raise SettlementImportInvalidError(
            f"The file has {len(data_rows)} data rows, over the {max_rows}-row cap. "
            "Split the file into smaller exports and import each part separately.",
            details={"rows": len(data_rows), "max_rows": max_rows},
        )
    parsed: list[ParsedSettlementRow] = []
    dropped: list[DroppedRow] = []
    for row_number, row in data_rows:
        drop_reason = _drop_reason_for(row, resolved)
        if drop_reason is not None:
            dropped.append(
                DroppedRow(
                    row_number=row_number,
                    reason=f"summary row ({drop_reason!r}) — dropped at parse, never matched or summed",
                    text=" | ".join(str(cell or "") for cell in row if str(cell or "").strip()),
                )
            )
            continue
        parsed.append(_parse_row(row_number, row, resolved, unit, date_kind))
    return ParsedSettlementFile(rows=tuple(parsed), dropped_rows=tuple(dropped))


def _drop_reason_for(row: list[str | None], resolved: dict[str, int]) -> str | None:
    """Return the drop marker for a summary row, or None for a data row."""
    texts: list[str] = []
    for field in ("tran_type", "description"):
        if field in resolved:
            texts.append(_cell(row, resolved[field]))
    first = next((str(cell or "") for cell in row if str(cell or "").strip()), "")
    texts.append(first)
    return _is_dropped_row(texts)


def _parse_amount_cell(
    row: list[str | None],
    resolved: dict[str, int],
    field: str,
    unit: FileCurrencyUnit,
    errors: list[str],
) -> tuple[int | None, bool]:
    """Parse one optional amount cell (None when the column is unmapped/empty)."""
    if field not in resolved:
        return None, False
    text = _cell(row, resolved[field]).strip()
    if not text:
        return None, False
    try:
        return parse_amount(text, unit)
    except ValueError as exc:
        errors.append(f"{field}: {exc}")
        return None, False


def _parse_row(
    row_number: int,
    row: list[str | None],
    resolved: dict[str, int],
    unit: FileCurrencyUnit,
    date_kind: str,
) -> ParsedSettlementRow:
    errors: list[str] = []
    raw: dict[str, str] = {
        field: _cell(row, index).strip()
        for field, index in resolved.items()
        if _cell(row, index).strip()
    }
    row_date = None
    date_text = _cell(row, resolved["date"]).strip() if "date" in resolved else ""
    if not date_text:
        errors.append("date is empty")
    else:
        try:
            row_date = parse_statement_date(date_text, date_kind)
        except ValueError as exc:
            errors.append(str(exc))
    transaction_id: str | None = None
    if "transaction_id" in resolved:
        tid = normalize_digits(_cell(row, resolved["transaction_id"]).strip())
        transaction_id = tid or None
    tran_type: str | None = None
    if "tran_type" in resolved:
        type_text = _cell(row, resolved["tran_type"]).strip()
        tran_type = normalize_tran_type(type_text) if type_text else None
    tracking_ref: str | None = None
    if "tracking_ref" in resolved:
        ref_text = normalize_digits(_cell(row, resolved["tracking_ref"]).strip())
        tracking_ref = ref_text or None
    description = _cell(row, resolved["description"]).strip() if "description" in resolved else ""
    was_rounded = False
    gross_irr, rounded = _parse_amount_cell(row, resolved, "gross", unit, errors)
    was_rounded = was_rounded or rounded
    fee_irr, rounded = _parse_amount_cell(row, resolved, "fee", unit, errors)
    was_rounded = was_rounded or rounded
    vat_irr, rounded = _parse_amount_cell(row, resolved, "vat", unit, errors)
    was_rounded = was_rounded or rounded
    net_irr, rounded = _parse_amount_cell(row, resolved, "net", unit, errors)
    was_rounded = was_rounded or rounded
    deductions_irr, rounded = _parse_amount_cell(row, resolved, "deductions", unit, errors)
    was_rounded = was_rounded or rounded
    # Derive the missing leg when the other two are present.
    if net_irr is None and gross_irr is not None and fee_irr is not None:
        net_irr = gross_irr - fee_irr - (vat_irr or 0)
    elif gross_irr is None and net_irr is not None and fee_irr is not None:
        gross_irr = net_irr + fee_irr + (vat_irr or 0)
    # The file's own arithmetic must hold when all legs are present.
    if (
        gross_irr is not None
        and fee_irr is not None
        and net_irr is not None
        and net_irr != gross_irr - fee_irr - (vat_irr or 0)
    ):
        errors.append(
            f"net ({net_irr}) does not equal gross ({gross_irr}) - fee ({fee_irr})"
            f" - vat ({vat_irr or 0})"
        )
    if net_irr is None:
        errors.append("net amount is missing (needs 'net' or 'gross' + 'fee')")
    return ParsedSettlementRow(
        row_number=row_number,
        transaction_id=transaction_id,
        tran_type=tran_type,
        row_date=row_date,
        gross_irr=gross_irr,
        fee_irr=fee_irr,
        vat_irr=vat_irr,
        net_irr=net_irr,
        deductions_irr=deductions_irr or 0,
        tracking_ref=tracking_ref,
        description=description,
        raw=raw,
        errors=tuple(errors),
        was_rounded=was_rounded,
    )
