import { describe, expect, it } from "vitest";
import {
  AGING_BUCKETS,
  agingChartItems,
  cashFlowSectionLabel,
  csvFilterHeader,
  filterNotesRows,
  jalaliMonthLabel,
  monthlyChartItems,
  notesInstrumentLabel,
  statementSectionLabel,
  waterfallSteps,
  withCsvFilterHeader,
} from "./financial-report-data";
import type { CashFlowResponse, NotesBalanceRowResponse } from "@/lib/api-client";

function notesRow(overrides: Partial<NotesBalanceRowResponse> = {}): NotesBalanceRowResponse {
  return {
    party_id: 1,
    party_name: "شرکت الف",
    instrument: "receivable",
    account_code: "1.2.4",
    account_name: "اسناد دریافتنی",
    bucket_not_yet_due: 0,
    bucket_0_30: 0,
    bucket_30_60: 0,
    bucket_60_90: 0,
    bucket_90_120: 0,
    bucket_120_plus: 0,
    total: 0,
    line_count: 0,
    ...overrides,
  };
}

describe("statement section labels", () => {
  it("maps known keys to Persian labels", () => {
    expect(statementSectionLabel("assets", "Assets")).toBe("دارایی‌ها");
    expect(statementSectionLabel("liabilities", "Liabilities")).toBe("بدهی‌ها");
    expect(statementSectionLabel("equity", "Equity / capital")).toBe("حقوق صاحبان سهام");
    expect(statementSectionLabel("revenue", "Revenue")).toBe("درآمدها");
    expect(statementSectionLabel("expenses", "Expenses")).toBe("هزینه‌ها");
  });

  it("falls back to the English title for unknown keys", () => {
    expect(statementSectionLabel("weird_key", "Weird Section")).toBe("Weird Section");
  });

  it("maps cash-flow section keys", () => {
    expect(cashFlowSectionLabel("operating", "Operating")).toBe("فعالیت‌های عملیاتی");
    expect(cashFlowSectionLabel("investing", "Investing")).toBe("فعالیت‌های سرمایه‌گذاری");
    expect(cashFlowSectionLabel("financing", "Financing")).toBe("فعالیت‌های تأمین مالی");
    expect(cashFlowSectionLabel("other", "Other")).toBe("Other");
  });

  it("maps notes instrument filters", () => {
    expect(notesInstrumentLabel("both")).toBe("دریافتنی و پرداختنی");
    expect(notesInstrumentLabel("receivable")).toBe("دریافتنی");
    expect(notesInstrumentLabel("payable")).toBe("پرداختنی");
  });
});

describe("jalaliMonthLabel", () => {
  it("formats a Jalali year-month in Persian", () => {
    expect(jalaliMonthLabel("1405-01")).toBe("فروردین ۱۴۰۵");
    expect(jalaliMonthLabel("1405-08")).toBe("آبان ۱۴۰۵");
    expect(jalaliMonthLabel("1404-12")).toBe("اسفند ۱۴۰۴");
  });

  it("passes unknown shapes through unchanged", () => {
    expect(jalaliMonthLabel("1405-13")).toBe("1405-13");
    expect(jalaliMonthLabel("not-a-month")).toBe("not-a-month");
  });
});

describe("monthlyChartItems", () => {
  it("labels months in Persian and keeps the figures", () => {
    const items = monthlyChartItems([
      { jalali_month: "1405-08", month_start: "2026-10-23", month_end: "2026-11-21", revenue: 600, expense: 300 },
      { jalali_month: "1405-09", month_start: "2026-11-22", month_end: "2026-12-21", revenue: 0, expense: 50 },
    ]);
    expect(items).toEqual([
      { label: "آبان ۱۴۰۵", revenue: 600, expense: 300 },
      { label: "آذر ۱۴۰۵", revenue: 0, expense: 50 },
    ]);
  });
});

describe("waterfallSteps", () => {
  const cashFlow = {
    opening_cash: 1000,
    closing_cash: 1300,
    sections: [
      { key: "operating", title: "Operating", inflow: 800, outflow: 300, net: 500, line_count: 4 },
      { key: "investing", title: "Investing", inflow: 0, outflow: 200, net: -200, line_count: 1 },
      { key: "financing", title: "Financing", inflow: 0, outflow: 0, net: 0, line_count: 0 },
    ],
  } as CashFlowResponse;

  it("builds opening → sections → closing with running totals", () => {
    const steps = waterfallSteps(cashFlow);
    expect(steps.map((s) => s.label)).toEqual([
      "ماندهٔ ابتدای دوره",
      "فعالیت‌های عملیاتی",
      "فعالیت‌های سرمایه‌گذاری",
      "فعالیت‌های تأمین مالی",
      "ماندهٔ پایان دوره",
    ]);
    expect(steps.map((s) => s.cumulative)).toEqual([1000, 1500, 1300, 1300, 1300]);
    expect(steps.map((s) => s.kind)).toEqual(["opening", "section", "section", "section", "closing"]);
    expect(steps[2].value).toBe(-200);
  });
});

describe("agingChartItems", () => {
  it("emits the six buckets in display order", () => {
    const items = agingChartItems(
      notesRow({
        bucket_not_yet_due: 10,
        bucket_0_30: 20,
        bucket_30_60: 30,
        bucket_60_90: 40,
        bucket_90_120: 50,
        bucket_120_plus: 60,
      }),
    );
    expect(items.map((i) => i.label)).toEqual(AGING_BUCKETS.map((b) => b.label));
    expect(items.map((i) => i.value)).toEqual([10, 20, 30, 40, 50, 60]);
  });
});

describe("filterNotesRows", () => {
  const rows = [
    notesRow({ party_id: 1, party_name: "شرکت الف", instrument: "receivable" }),
    notesRow({ party_id: 2, party_name: "شرکت ب", instrument: "payable" }),
    notesRow({ party_id: 3, party_name: "علی محمدی", instrument: "receivable" }),
  ];

  it("filters by party name substring", () => {
    expect(filterNotesRows(rows, { partyText: "شرکت" })).toHaveLength(2);
    expect(filterNotesRows(rows, { partyText: "علی" })).toHaveLength(1);
    expect(filterNotesRows(rows, { partyText: "نبود" })).toHaveLength(0);
  });

  it("filters by instrument", () => {
    expect(filterNotesRows(rows, { instrument: "payable" })).toHaveLength(1);
    expect(filterNotesRows(rows, { instrument: "both" })).toHaveLength(3);
  });

  it("combines both filters", () => {
    expect(filterNotesRows(rows, { partyText: "شرکت", instrument: "receivable" })).toHaveLength(1);
  });

  it("returns all rows with no filters", () => {
    expect(filterNotesRows(rows, {})).toHaveLength(3);
  });
});

describe("CSV filter header", () => {
  it("builds a #-prefixed block with title and filters", () => {
    expect(csvFilterHeader("تراز آزمایشی", [["شرکت", "اکمه"], ["دوره", "2026-11-01 تا 2026-11-30"]])).toBe(
      "# تراز آزمایشی\r\n# شرکت: اکمه\r\n# دوره: 2026-11-01 تا 2026-11-30\r\n",
    );
  });

  it("prepends the header after the BOM", () => {
    expect(withCsvFilterHeader("\uFEFFa,b\r\n1,2\r\n", "گزارش", [["فیلتر", "مقدار"]])).toBe(
      "\uFEFF# گزارش\r\n# فیلتر: مقدار\r\na,b\r\n1,2\r\n",
    );
  });
});
