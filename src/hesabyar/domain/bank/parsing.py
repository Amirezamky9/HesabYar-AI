"""Pure statement-file parsing (US-514, B1).

No I/O here: the infrastructure file readers turn bytes into a grid
(``Sequence[Sequence[str | None]]``) and this module turns the grid
into parsed rows under a column mapping. Everything is deterministic
and unit-testable without a database or the engine.

Parsing rules (phased design §4 §2.1):

- Persian/Arabic-Indic digits normalise to Latin before any numeric
  parse; thousands separators (``٬`` ``,`` space) are stripped.
- Dates are Jalali by default (``date_kind='jalali'`` on the mapping);
  Gregorian is opt-in per mapping. A date that does not parse is a
  per-row error, never a silent skip.
- Amounts are whole Rial inside the system (spec §19 rule 7). A file
  in Toman converts ×10 at this boundary; a fractional Rial after
  conversion rounds HALF_UP (the event-platform rule) and the row is
  flagged ``was_rounded``. A Rial file with a fractional amount is a
  per-row error — never silently rounded.
- The match key (dedup layer 2) is ``sha256(date|amount|description)``
  over the normalised description.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal

import jdatetime

from hesabyar.domain.bank.errors import BankImportFileError, BankImportTooManyRowsError
from hesabyar.domain.bank.models import (
    ALL_MAPPING_FIELDS,
    MAX_IMPORT_ROWS,
    REQUIRED_MAPPING_FIELDS,
    BankColumnMapping,
    FileCurrencyUnit,
)
from hesabyar.domain.common.dates import FiscalDate

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_TABLE = str.maketrans(_PERSIAN_DIGITS + _ARABIC_DIGITS, "0123456789" * 2)

_JALALI_DATE_RE = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")
_GREGORIAN_DATE_RE = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")

#: Header synonyms per canonical field (normalised before comparing).
#: Longer synonyms are tried first so "شرح تراکنش" wins over "شرح".
_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": ("تاریخ تراکنش", "تاریخ سند", "تاریخ", "date"),
    "bank_ref": ("شماره پیگیری", "شماره مرجع", "شماره ارجاع", "شماره سند", "پیگیری", "مرجع", "reference", "ref"),
    "description": ("شرح تراکنش", "شرح سند", "توضیحات", "شرح", "description"),
    "debit": ("بدهکار", "برداشت", "بدهی", "debit"),
    "credit": ("بستانکار", "بستانکاری", "واریز", "credit"),
    "amount": ("مبلغ تراکنش", "مبلغ", "amount"),
    "balance": ("مانده حساب", "مانده", "موجودی", "balance"),
    "counterparty": ("شماره حساب مقصد", "طرف مقابل", "مقصد", "counterparty"),
}


def normalize_digits(text: str) -> str:
    """Replace Persian/Arabic-Indic digits with Latin digits."""
    return text.translate(_DIGIT_TABLE)


def normalize_persian_text(text: str) -> str:
    """Normalise Persian text for comparison: digits, ي/ك, case, whitespace.

    ZWNJ is preserved (it is meaningful in Persian words); Arabic
    Yeh/Kaf fold to Persian Yeh/Kaf; Latin letters fold to lowercase
    (English headers match case-insensitively); runs of whitespace
    collapse to one space; surrounding whitespace is stripped.
    """
    folded = normalize_digits(text).replace("ي", "ی").replace("ك", "ک")
    return " ".join(folded.lower().split())


def parse_jalali_date(text: str) -> FiscalDate:
    """Parse a Jalali date (``1405/08/26``, ``1405-08-26``, Persian digits).

    Raises ValueError with a human-readable reason when the text is not
    a valid Jalali date — the caller turns it into a per-row error.
    """
    cleaned = normalize_digits(text.strip())
    match = _JALALI_DATE_RE.match(cleaned)
    if not match:
        raise ValueError(f"not a Jalali date (expected YYYY/MM/DD): {text!r}")
    year, month, day = (int(part) for part in match.groups())
    try:
        gregorian = jdatetime.date(year, month, day).togregorian()
    except ValueError as exc:
        raise ValueError(f"invalid Jalali date {text!r}: {exc}") from exc
    return FiscalDate(gregorian)


def parse_gregorian_date(text: str) -> FiscalDate:
    """Parse a Gregorian date (``2026-11-17`` / ``2026/11/17``).

    A trailing time part (``2026-11-17 10:30:00``, as produced when an
    Excel datetime cell is stringified) is ignored — the date part is
    what a statement row carries.
    """
    cleaned = normalize_digits(text.strip()).split(" ")[0].split("T")[0]
    match = _GREGORIAN_DATE_RE.match(cleaned)
    if not match:
        raise ValueError(f"not a Gregorian date (expected YYYY-MM-DD): {text!r}")
    year, month, day = (int(part) for part in match.groups())
    try:
        return FiscalDate(_date(year, month, day))
    except ValueError as exc:
        raise ValueError(f"invalid Gregorian date {text!r}: {exc}") from exc


def parse_statement_date(text: str, date_kind: str) -> FiscalDate:
    """Parse a statement date under the mapping's ``date_kind``."""
    if date_kind == "gregorian":
        return parse_gregorian_date(text)
    if date_kind == "jalali":
        return parse_jalali_date(text)
    raise ValueError(f"unknown date_kind {date_kind!r} (expected 'jalali' or 'gregorian')")


def parse_amount(text: str, currency_unit: FileCurrencyUnit) -> tuple[int, bool]:
    """Parse a statement amount to whole Rial.

    Returns ``(amount_irr, was_rounded)``. Persian digits, thousands
    separators (``٬`` ``,`` space) and a leading/trailing minus (or
    parentheses) are handled. A Toman amount converts ×10 with HALF_UP
    rounding on a fractional Rial; a Rial amount must already be
    whole. Raises ValueError with a human-readable reason otherwise.
    """
    cleaned = normalize_digits(text.strip())
    if not cleaned:
        raise ValueError("empty amount")
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1].strip()
    if cleaned.startswith(("-", "−")):
        negative = True
        cleaned = cleaned[1:].strip()
    elif cleaned.endswith(("-", "−")):
        negative = True
        cleaned = cleaned[:-1].strip()
    # Strip currency words and thousands separators.
    for word in ("ریال", "تومان", "IRR", "irr"):
        cleaned = cleaned.replace(word, "")
    cleaned = cleaned.replace("٬", "").replace(",", "").replace("_", "")
    cleaned = cleaned.replace("\u00a0", "").replace(" ", "").replace("٫", ".")
    if not cleaned or not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise ValueError(f"not a number: {text!r}")
    value = Decimal(cleaned)
    if currency_unit == FileCurrencyUnit.TOMAN:
        rial = value * 10
        rounded = int(rial.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return (-rounded if negative else rounded, rial != rounded)
    if value != int(value):
        raise ValueError(f"fractional Rial amount is not allowed: {text!r}")
    amount = int(value)
    return (-amount if negative else amount, False)


def build_match_key(row_date: FiscalDate, amount_irr: int, description: str) -> str:
    """Dedup layer-2 key: ``sha256(date|amount|normalised description)``.

    Used for files without a bank reference (and for overlapping
    ranges): two rows with the same date, amount and description are
    the same bank movement.
    """
    payload = f"{row_date.gregorian_date.isoformat()}|{amount_irr}|{normalize_persian_text(description)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedRow:
    """One grid row after parsing (before persistence).

    ``errors`` is non-empty exactly when the row is invalid; an
    invalid row keeps whatever parsed (date/amount may be None) so the
    preview can show the operator what failed.
    """

    row_number: int
    row_date: FiscalDate | None
    amount_irr: int | None
    description: str
    bank_ref: str | None = None
    balance_irr: int | None = None
    errors: tuple[str, ...] = ()
    was_rounded: bool = False

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.row_date is not None and self.amount_irr is not None


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    """Suggest a column mapping from header names (auto-detect).

    Each canonical field takes the first still-unused header whose
    normalised text contains one of the field's synonyms (longest
    synonym first). Fields with no matching header are absent from
    the result — the caller reports them as missing.
    """
    normalised = [normalize_persian_text(header) for header in headers]
    used: set[int] = set()
    suggested: dict[str, str] = {}
    for field in ALL_MAPPING_FIELDS:
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


def validate_mapping_columns(columns: dict[str, str]) -> list[str]:
    """Return the list of mapping problems (empty when valid).

    ``date`` + ``description`` are always required; the amount needs
    either a signed ``amount`` column or a ``debit``/``credit`` pair.
    Unknown canonical field names are rejected (a typo must fail
    loudly, not silently drop a column).
    """
    problems: list[str] = []
    for field in columns:
        if field not in ALL_MAPPING_FIELDS:
            problems.append(f"unknown column field: {field!r}")
    for field in REQUIRED_MAPPING_FIELDS:
        if field not in columns:
            problems.append(f"missing required column: {field!r}")
    has_amount = "amount" in columns
    has_pair = "debit" in columns and "credit" in columns
    if not has_amount and not has_pair:
        problems.append("amount needs an 'amount' column or a 'debit'/'credit' pair")
    if has_amount and ("debit" in columns or "credit" in columns):
        problems.append("use either 'amount' or 'debit'/'credit', not both")
    return problems


def _resolve_column(column_ref: str, headers: list[str], field: str) -> int:
    """Resolve one mapping entry to a 0-based grid column index.

    ``#<n>`` is positional; otherwise the value is a header name
    matched after normalisation. Raises BankImportFileError when the
    column cannot be found — the mapping does not fit this file.
    """
    if column_ref.startswith("#"):
        try:
            index = int(column_ref[1:])
        except ValueError as exc:
            raise BankImportFileError(
                f"Column reference {column_ref!r} for {field!r} is not a valid positional reference.",
                details={"field": field, "column_ref": column_ref},
            ) from exc
        if index < 0 or index >= len(headers):
            raise BankImportFileError(
                f"Column reference {column_ref!r} for {field!r} is out of range "
                f"(the file has {len(headers)} columns).",
                details={"field": field, "column_ref": column_ref},
            )
        return index
    wanted = normalize_persian_text(column_ref)
    for index, header in enumerate(headers):
        if normalize_persian_text(header) == wanted:
            return index
    raise BankImportFileError(
        f"Column {column_ref!r} for {field!r} was not found in the file headers.",
        details={"field": field, "column_ref": column_ref},
    )


def rows_from_grid(
    grid: list[list[str | None]],
    mapping: BankColumnMapping,
    currency_unit: FileCurrencyUnit,
    max_rows: int = MAX_IMPORT_ROWS,
) -> list[ParsedRow]:
    """Parse a cell grid into statement rows under a column mapping.

    ``grid[header_row]`` is the header; later rows are data. Fully
    empty rows are skipped. More than ``max_rows`` data rows raises
    :class:`BankImportTooManyRowsError` (the 2,000-row bank-export
    cap) — the operator splits the range and imports each part.
    """
    if mapping.header_row >= len(grid):
        raise BankImportFileError(
            f"Header row {mapping.header_row} is past the end of the file "
            f"({len(grid)} rows).",
            details={"header_row": mapping.header_row, "grid_rows": len(grid)},
        )
    headers = [str(cell or "") for cell in grid[mapping.header_row]]
    resolved = {
        field: _resolve_column(ref, headers, field) for field, ref in mapping.columns.items()
    }
    data_rows = [
        (index, row)
        for index, row in enumerate(grid[mapping.header_row + 1 :], start=mapping.header_row + 2)
        if any(str(cell or "").strip() for cell in row)
    ]
    if len(data_rows) > max_rows:
        raise BankImportTooManyRowsError(
            f"The file has {len(data_rows)} data rows, over the {max_rows}-row "
            "bank-export cap. Split the date range into smaller exports and "
            "import each file separately — overlapping ranges are deduplicated.",
            details={"rows": len(data_rows), "max_rows": max_rows},
        )
    parsed: list[ParsedRow] = []
    for row_number, row in data_rows:
        parsed.append(_parse_row(row_number, row, resolved, mapping, currency_unit))
    return parsed


def _cell(row: list[str | None], index: int) -> str:
    if index >= len(row):
        return ""
    return str(row[index] or "")


def _parse_row(
    row_number: int,
    row: list[str | None],
    resolved: dict[str, int],
    mapping: BankColumnMapping,
    currency_unit: FileCurrencyUnit,
) -> ParsedRow:
    errors: list[str] = []
    row_date: FiscalDate | None = None
    date_text = _cell(row, resolved["date"]).strip()
    if not date_text:
        errors.append("date is empty")
    else:
        try:
            row_date = parse_statement_date(date_text, mapping.date_kind)
        except ValueError as exc:
            errors.append(str(exc))
    description = _cell(row, resolved["description"]).strip()
    amount_irr: int | None = None
    was_rounded = False
    if "amount" in resolved:
        amount_text = _cell(row, resolved["amount"]).strip()
        if not amount_text:
            errors.append("amount is empty")
        else:
            try:
                amount_irr, was_rounded = parse_amount(amount_text, currency_unit)
            except ValueError as exc:
                errors.append(str(exc))
    else:
        debit_irr = 0
        credit_irr = 0
        debit_text = _cell(row, resolved["debit"]).strip()
        credit_text = _cell(row, resolved["credit"]).strip()
        if not debit_text and not credit_text:
            errors.append("both debit and credit are empty")
        elif debit_text and credit_text:
            # A bank row is either a debit or a credit; both filled is
            # a corrupt export — flag it rather than silently netting.
            errors.append("both debit and credit are set")
        else:
            try:
                if debit_text:
                    debit_irr, _ = parse_amount(debit_text, currency_unit)
                if credit_text:
                    credit_irr, _ = parse_amount(credit_text, currency_unit)
                amount_irr = credit_irr - debit_irr
            except ValueError as exc:
                errors.append(str(exc))
    bank_ref: str | None = None
    if "bank_ref" in resolved:
        ref_text = normalize_digits(_cell(row, resolved["bank_ref"]).strip())
        bank_ref = ref_text or None
    balance_irr: int | None = None
    if "balance" in resolved:
        balance_text = _cell(row, resolved["balance"]).strip()
        if balance_text:
            try:
                balance_irr, _ = parse_amount(balance_text, currency_unit)
            except ValueError as exc:
                errors.append(f"balance: {exc}")
    return ParsedRow(
        row_number=row_number,
        row_date=row_date,
        amount_irr=amount_irr,
        description=description,
        bank_ref=bank_ref,
        balance_irr=balance_irr,
        errors=tuple(errors),
        was_rounded=was_rounded,
    )
