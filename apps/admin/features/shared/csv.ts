/**
 * CSV export for the US-304 list pages (spec §17: filters, export and
 * detail panels).
 *
 * Values are NORMALIZED, never localized display strings: integer IRR
 * amounts, ISO Gregorian dates, English state codes, ASCII digits.
 * Headers are Persian (the file is for a Persian-speaking accountant).
 * A UTF-8 BOM is prepended so Excel opens the Persian headers correctly.
 */

export interface CsvColumn<T> {
  /** Persian column header. */
  header: string;
  /** Normalized cell value (ASCII, ISO dates, integer amounts). */
  value: (row: T) => string | number | null | undefined;
}

function escapeCell(raw: string): string {
  // Quote when the cell contains a comma, quote or newline; double quotes.
  if (/[",\n\r]/.test(raw)) return `"${raw.replace(/"/g, '""')}"`;
  return raw;
}

/** Build CSV text (CRLF rows, BOM) from normalized column values. */
export function buildCsv<T>(columns: CsvColumn<T>[], rows: T[]): string {
  const lines: string[] = [columns.map((c) => escapeCell(c.header)).join(",")];
  for (const row of rows) {
    lines.push(
      columns
        .map((c) => {
          const v = c.value(row);
          return escapeCell(v === null || v === undefined ? "" : String(v));
        })
        .join(","),
    );
  }
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}

/** Trigger a browser download of CSV text. */
export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** Export filename: `<slug>-<today-iso>.csv` (Gregorian ISO, normalized). */
export function csvFilename(slug: string, todayIso: string): string {
  return `${slug}-${todayIso}.csv`;
}
