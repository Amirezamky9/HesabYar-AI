"""Statement file readers: xlsx / csv / pdf bytes → cell grid (US-514, B1).

The readers own the format libraries (openpyxl, pdfplumber) and CSV
encoding detection; they return a plain grid of strings and the domain
parser (``domain.bank.parsing``) does the rest. Nothing here knows
about tenants, companies or the engine.

Format notes (phased design §4 §2.1):

- Excel/CSV are first priority; PDF is second (table extraction is
  best-effort — a PDF whose tables cannot be extracted falls back to
  text lines split on tabs / 2+ spaces, and rows that still do not
  parse land on the preview as invalid, never silently dropped).
- CSV encoding is auto-detected: UTF-8 (with/without BOM) first, then
  Windows-1256 (common in Iranian bank exports), then Windows-1252.
  The first strict decode wins; the detected encoding is reported so
  the operator can see what was assumed.
- The Baam (Bank Melli) export layout is UNVERIFIED until a real
  customer file is seen — no Baam-specific layout is assumed anywhere.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import pdfplumber

from hesabyar.domain.bank.errors import BankImportFileError
from hesabyar.domain.bank.models import StatementFileFormat
from hesabyar.domain.bank.ports import DetectedFile, StatementFileReaderPort

#: Maximum upload size (a 2,000-row statement is far below this; the
#: guard exists so a mistaken multi-hundred-MB upload fails fast).
MAX_FILE_BYTES = 10 * 1024 * 1024

_CSV_ENCODINGS = ("utf-8", "windows-1256", "windows-1252")


def detect_csv_encoding(content: bytes) -> str:
    """Detect a CSV file's encoding: first strict decode wins.

    A UTF-8 BOM means utf-8-sig; otherwise UTF-8 is tried before
    Windows-1256 because a Windows-1256 decode of UTF-8 bytes
    "succeeds" but garbles Persian text, while a UTF-8 decode of
    Windows-1256 bytes fails strictly. Windows-1252 is the last
    resort (it never fails a strict decode).
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for encoding in _CSV_ENCODINGS:
        try:
            content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return encoding
    return "windows-1252"


def _cell_to_str(value: object) -> str | None:
    """Stringify one spreadsheet cell (None stays None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        # A datetime cell at midnight is a date, not a timestamp.
        if value.time() == datetime.min.time():
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def _read_xlsx(content: bytes) -> list[list[str | None]]:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise BankImportFileError(f"The Excel file could not be read: {exc}") from exc
    try:
        for sheet in workbook.worksheets:
            grid: list[list[str | None]] = []
            for row in sheet.iter_rows(values_only=True):
                cells = [_cell_to_str(cell) for cell in row]
                # Trim trailing empty cells so ragged rows share a width.
                while cells and cells[-1] in (None, ""):
                    cells.pop()
                grid.append(cells)
            # Skip leading fully-empty rows; keep the grid otherwise intact.
            while grid and not any(cell for cell in grid[0]):
                grid.pop(0)
            if grid:
                return grid
    finally:
        workbook.close()
    raise BankImportFileError("The Excel file has no data rows.")


def _read_csv(content: bytes) -> tuple[list[list[str | None]], str]:
    encoding = detect_csv_encoding(content)
    text = content.decode(encoding)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    grid: list[list[str | None]] = [[cell for cell in row] for row in reader]
    while grid and not any((cell or "").strip() for cell in grid[0]):
        grid.pop(0)
    if not grid:
        raise BankImportFileError("The CSV file has no data rows.")
    return grid, encoding


def _normalise_header_row(row: list[str | None]) -> tuple[str, ...]:
    return tuple((cell or "").strip() for cell in row)


def _read_pdf(content: bytes) -> tuple[list[list[str | None]], tuple[str, ...]]:
    notes: list[str] = []
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as exc:
        raise BankImportFileError(f"The PDF file could not be read: {exc}") from exc
    grid: list[list[str | None]] = []
    header: tuple[str, ...] | None = None
    tables_found = 0
    with pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                tables_found += 1
                rows = [[cell for cell in row] for row in table]
                # Multi-page tables repeat the header: drop a repeated
                # header row on later tables.
                if header is not None and rows and _normalise_header_row(rows[0]) == header:
                    rows = rows[1:]
                if header is None and rows:
                    header = _normalise_header_row(rows[0])
                grid.extend(rows)
    if tables_found:
        notes.append(f"extracted {tables_found} table(s) from the PDF")
        while grid and not any(cell for cell in grid[0]):
            grid.pop(0)
        if grid:
            return grid, tuple(notes)
    # Fallback: text lines split on tabs / 2+ spaces (best effort).
    notes.append("no tables found in the PDF; falling back to text lines")
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                if not line.strip():
                    continue
                cells = [part for part in re.split(r"\t| {2,}", line.strip()) if part]
                grid.append(cells)
    if not grid:
        raise BankImportFileError("The PDF file has no extractable text or tables.")
    return grid, tuple(notes)


class StatementFileReader(StatementFileReaderPort):
    """Format-sniffing statement file reader (xlsx / csv / pdf)."""

    def read_file(self, content: bytes, filename: str) -> DetectedFile:
        """Read bytes into a cell grid; raises BankImportFileError when unreadable."""
        if len(content) > MAX_FILE_BYTES:
            raise BankImportFileError(
                f"The file is {len(content)} bytes, over the {MAX_FILE_BYTES}-byte limit.",
                details={"size_bytes": len(content), "max_bytes": MAX_FILE_BYTES},
            )
        if not content:
            raise BankImportFileError("The uploaded file is empty.")
        # Content wins over the extension: a misnamed file is read by
        # what it is, not what it claims to be (PDF magic, zip magic
        # for xlsx, otherwise CSV).
        if content.startswith(b"%PDF"):
            grid, notes = _read_pdf(content)
            return DetectedFile(
                file_format=StatementFileFormat.PDF, grid=grid, detection_notes=notes
            )
        if content[:4] == b"PK\x03\x04":
            return DetectedFile(file_format=StatementFileFormat.XLSX, grid=_read_xlsx(content))
        try:
            grid, encoding = _read_csv(content)
        except BankImportFileError:
            raise
        except Exception as exc:
            raise BankImportFileError(
                f"Could not read {filename!r}: expected an .xlsx, .csv or .pdf statement file.",
                details={"filename": filename},
            ) from exc
        return DetectedFile(
            file_format=StatementFileFormat.CSV,
            grid=grid,
            encoding=encoding,
            detection_notes=(f"detected encoding: {encoding}",),
        )
