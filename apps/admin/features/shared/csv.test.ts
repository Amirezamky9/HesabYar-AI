import { describe, expect, it } from "vitest";
import { buildCsv, csvFilename, type CsvColumn } from "./csv";

interface Row {
  id: number;
  name: string;
  amountIrr: number;
  date: string;
  state: string;
  note: string | null;
}

const COLUMNS: CsvColumn<Row>[] = [
  { header: "شناسه", value: (r) => r.id },
  { header: "نام", value: (r) => r.name },
  { header: "مبلغ (ریال)", value: (r) => r.amountIrr },
  { header: "تاریخ", value: (r) => r.date },
  { header: "وضعیت", value: (r) => r.state },
  { header: "یادداشت", value: (r) => r.note },
];

describe("buildCsv", () => {
  it("writes normalized values: ASCII digits, ISO dates, integer IRR, English state codes", () => {
    const csv = buildCsv(COLUMNS, [
      { id: 7, name: "مشتری نمونه", amountIrr: 12500000, date: "2026-11-17", state: "draft", note: null },
    ]);
    // Persian digits must never appear in values (headers are Persian by design).
    expect(csv).not.toMatch(/[۰-۹]/);
    expect(csv).toContain("7,مشتری نمونه,12500000,2026-11-17,draft,");
  });

  it("starts with a UTF-8 BOM and uses CRLF rows", () => {
    const csv = buildCsv(COLUMNS, []);
    expect(csv.charCodeAt(0)).toBe(0xfeff);
    expect(csv).toContain("\r\n");
  });

  it("quotes cells containing commas, quotes or newlines", () => {
    const csv = buildCsv(COLUMNS, [
      { id: 1, name: 'شرکت "الف"، با مسئولیت محدود', amountIrr: 0, date: "2026-01-01", state: "paid", note: "خط اول\nخط دوم" },
    ]);
    expect(csv).toContain('"شرکت ""الف""، با مسئولیت محدود"');
    expect(csv).toContain('"خط اول\nخط دوم"');
  });

  it("renders an empty list as a header-only file", () => {
    const csv = buildCsv(COLUMNS, []);
    expect(csv.trim()).toBe("شناسه,نام,مبلغ (ریال),تاریخ,وضعیت,یادداشت");
  });
});

describe("csvFilename", () => {
  it("uses the Gregorian ISO date in the filename", () => {
    expect(csvFilename("invoices", "2026-11-17")).toBe("invoices-2026-11-17.csv");
  });
});
