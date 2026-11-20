/**
 * Pure helpers for the US-550 financial reports: Persian labels for
 * section keys, Jalali month labels, waterfall steps, chart items and
 * the filter-header block every export carries (AC 7). No fetching
 * here — the sections own that.
 */

import { JALALI_MONTHS, jalaliToIso, toJalali } from "@/lib/jalali";
import { faNumber } from "@/lib/utils";
import type {
  CashFlowResponse,
  MonthlyRevenueExpenseResponse,
  NotesBalanceRowResponse,
} from "@/lib/api-client";

/** Persian labels for statement section keys (balance sheet / P&L). */
export const STATEMENT_SECTION_LABELS: Record<string, string> = {
  assets: "دارایی‌ها",
  liabilities: "بدهی‌ها",
  equity: "حقوق صاحبان سهام",
  revenue: "درآمدها",
  expenses: "هزینه‌ها",
};

export function statementSectionLabel(key: string, fallback: string): string {
  return STATEMENT_SECTION_LABELS[key] ?? fallback;
}

/** Persian labels for cash-flow section keys. */
export const CASH_FLOW_SECTION_LABELS: Record<string, string> = {
  operating: "فعالیت‌های عملیاتی",
  investing: "فعالیت‌های سرمایه‌گذاری",
  financing: "فعالیت‌های تأمین مالی",
};

export function cashFlowSectionLabel(key: string, fallback: string): string {
  return CASH_FLOW_SECTION_LABELS[key] ?? fallback;
}

/** Persian labels for the notes-balance instrument filter. */
export const NOTES_INSTRUMENT_LABELS: Record<string, string> = {
  both: "دریافتنی و پرداختنی",
  receivable: "دریافتنی",
  payable: "پرداختنی",
};

export function notesInstrumentLabel(instrument: string): string {
  return NOTES_INSTRUMENT_LABELS[instrument] ?? instrument;
}

/** Aging buckets in display order: key, Persian label. */
export const AGING_BUCKETS: { key: keyof Pick<NotesBalanceRowResponse, "bucket_not_yet_due" | "bucket_0_30" | "bucket_30_60" | "bucket_60_90" | "bucket_90_120" | "bucket_120_plus">; label: string }[] = [
  { key: "bucket_not_yet_due", label: "سررسید نشده" },
  { key: "bucket_0_30", label: "۰ تا ۳۰ روز" },
  { key: "bucket_30_60", label: "۳۰ تا ۶۰ روز" },
  { key: "bucket_60_90", label: "۶۰ تا ۹۰ روز" },
  { key: "bucket_90_120", label: "۹۰ تا ۱۲۰ روز" },
  { key: "bucket_120_plus", label: "بیش از ۱۲۰ روز" },
];

/** Gregorian ISO of the first day of the current Jalali year (default period start). */
export function currentJalaliYearStartIso(): string {
  const { jy } = toJalali(new Date());
  return jalaliToIso(jy, 1, 1);
}

/** "1405-08" → "آبان ۱۴۰۵". Unknown shapes pass through unchanged. */
export function jalaliMonthLabel(jalaliMonth: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(jalaliMonth);
  if (!match) return jalaliMonth;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return jalaliMonth;
  return `${JALALI_MONTHS[month - 1]} ${faNumber(Number(match[1]))}`;
}

/** Monthly revenue/expense chart items, labelled with Jalali months. */
export function monthlyChartItems(
  monthly: MonthlyRevenueExpenseResponse[],
): { label: string; revenue: number; expense: number }[] {
  return monthly.map((m) => ({
    label: jalaliMonthLabel(m.jalali_month),
    revenue: m.revenue,
    expense: m.expense,
  }));
}

/** One step of the cash-flow waterfall: opening → sections → closing. */
export interface WaterfallStep {
  label: string;
  /** Signed step value (IRR); the opening/closing steps carry the balance. */
  value: number;
  /** Running total after this step (IRR). */
  cumulative: number;
  kind: "opening" | "section" | "closing";
}

export function waterfallSteps(cashFlow: CashFlowResponse): WaterfallStep[] {
  const steps: WaterfallStep[] = [
    { label: "ماندهٔ ابتدای دوره", value: cashFlow.opening_cash, cumulative: cashFlow.opening_cash, kind: "opening" },
  ];
  let running = cashFlow.opening_cash;
  for (const section of cashFlow.sections) {
    running += section.net;
    steps.push({
      label: cashFlowSectionLabel(section.key, section.title),
      value: section.net,
      cumulative: running,
      kind: "section",
    });
  }
  steps.push({ label: "ماندهٔ پایان دوره", value: cashFlow.closing_cash, cumulative: cashFlow.closing_cash, kind: "closing" });
  return steps;
}

/** Aging bar-chart items from the notes totals row. */
export function agingChartItems(totals: NotesBalanceRowResponse): { label: string; value: number }[] {
  return AGING_BUCKETS.map((b) => ({ label: b.label, value: totals[b.key] }));
}

/**
 * Client-side notes-row filter: party name substring (case-insensitive)
 * and instrument. The as-of date and instrument are server filters;
 * the party text filter runs here so typing does not refetch.
 */
export function filterNotesRows(
  rows: NotesBalanceRowResponse[],
  opts: { partyText?: string; instrument?: string },
): NotesBalanceRowResponse[] {
  const text = (opts.partyText ?? "").trim();
  return rows.filter((row) => {
    if (opts.instrument && opts.instrument !== "both" && row.instrument !== opts.instrument) return false;
    if (text && !row.party_name.toLowerCase().includes(text.toLowerCase())) return false;
    return true;
  });
}

/**
 * CSV filter-header block (AC 7): `#`-prefixed lines above the table,
 * so the export carries the report title and every applied filter.
 * Values stay normalized (ISO dates, ASCII ids); labels are Persian.
 */
export function csvFilterHeader(title: string, filters: [string, string][]): string {
  const lines = [`# ${title}`, ...filters.map(([label, value]) => `# ${label}: ${value}`)];
  return `${lines.join("\r\n")}\r\n`;
}

/** Prepend the filter header to CSV text built by buildCsv (keeps the BOM first). */
export function withCsvFilterHeader(csv: string, title: string, filters: [string, string][]): string {
  const header = csvFilterHeader(title, filters);
  if (csv.startsWith("\uFEFF")) return `\uFEFF${header}${csv.slice(1)}`;
  return `${header}${csv}`;
}
