/**
 * Cash-forecast calendar helpers (US-553) — pure functions over the
 * GET /reports/cash-forecast response. No fetching, no rendering:
 * the section component owns those, these own the math + labels.
 */

import type {
  CashForecastResponse,
  ForecastCalendarDayResponse,
  ForecastCalendarMonthResponse,
  ForecastHorizonBucketResponse,
} from "@/lib/api-client";
import {
  JALALI_MONTHS,
  isoToJalali,
  jalaliMonthLength,
  jalaliToIso,
  jalaliWeekday,
} from "@/lib/jalali";

/** A Jalali month the calendar can show. */
export interface JalaliMonth {
  jy: number;
  jm: number;
}

/**
 * The forecast fetch window: 180 days back (so the «سررسید گذشته»
 * horizon bucket sees overdue items older than the current month)
 * and 120 days forward (the 30/60/90/120-day forecast). One fetch
 * covers the whole window; month navigation is client-side.
 */
export const FORECAST_LOOKBACK_DAYS = 180;
export const FORECAST_AHEAD_DAYS = 120;

/** Shift a Gregorian ISO date by `delta` days (UTC arithmetic, no TZ drift). */
export function addDaysIso(iso: string, delta: number): string {
  const ms = new Date(`${iso}T00:00:00Z`).getTime() + delta * 86_400_000;
  return new Date(ms).toISOString().slice(0, 10);
}

/** The fetch window around `todayIso`: [today − 180d, today + 120d]. */
export function forecastFetchRange(today: string): { fromIso: string; toIso: string } {
  return {
    fromIso: addDaysIso(today, -FORECAST_LOOKBACK_DAYS),
    toIso: addDaysIso(today, FORECAST_AHEAD_DAYS),
  };
}

/** Compare two Jalali months: negative when `a` is before `b`. */
export function compareJalaliMonths(a: JalaliMonth, b: JalaliMonth): number {
  return a.jy * 12 + a.jm - (b.jy * 12 + b.jm);
}

/** The Gregorian ISO span of one Jalali month (for the API range). */
export function jalaliMonthRange(month: JalaliMonth): { fromIso: string; toIso: string } {
  const length = jalaliMonthLength(month.jy, month.jm);
  return {
    fromIso: jalaliToIso(month.jy, month.jm, 1),
    toIso: jalaliToIso(month.jy, month.jm, length),
  };
}

/** Shift a Jalali month by `delta` months (negative = backwards). */
export function shiftJalaliMonth(month: JalaliMonth, delta: number): JalaliMonth {
  const total = (month.jy - 1) * 12 + (month.jm - 1) + delta;
  const jy = Math.floor(total / 12) + 1;
  const jm = ((total % 12) + 12) % 12 + 1;
  return { jy, jm };
}

/** The Jalali month of a Gregorian ISO date. */
export function jalaliMonthOfIso(iso: string): JalaliMonth {
  const { jy, jm } = isoToJalali(iso);
  return { jy, jm };
}

/** «آبان ۱۴۰۵» — the Persian label of a Jalali month key («YYYY-MM»). */
export function jalaliMonthLabel(monthJalali: string): string {
  const match = monthJalali.match(/^(\d{4})-(\d{2})$/);
  if (!match) return monthJalali;
  const jm = Number(match[2]);
  if (jm < 1 || jm > 12) return monthJalali;
  const year = match[1].replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);
  return `${JALALI_MONTHS[jm - 1]} ${year}`;
}

/**
 * The Persian label of a forecast source. Unknown keys pass through
 * unchanged (a future source must never crash the breakdown).
 */
export function forecastSourceLabel(source: string): string {
  switch (source) {
    case "settlement_tranche":
      return "تسویهٔ درگاه";
    case "installment_leg":
      return "قسط فروش";
    case "cheque_received":
      return "چک دریافتی";
    case "cheque_issued":
      return "چک صادره";
    case "invoice_receivable":
      return "فاکتور مشتری";
    case "invoice_payable":
      return "فاکتور تأمین‌کننده";
    case "packaging_purchase":
      return "خرید بسته‌بندی";
    case "expected_sale":
      return "فروش موردانتظار";
    default:
      return source;
  }
}

/** The Persian label of a horizon bucket. */
export function horizonBucketLabel(bucket: ForecastHorizonBucketResponse["key"]): string {
  switch (bucket) {
    case "overdue":
      return "سررسید گذشته";
    case "0_30":
      return "۰ تا ۳۰ روز آینده";
    case "31_60":
      return "۳۱ تا ۶۰ روز آینده";
    case "61_90":
      return "۶۱ تا ۹۰ روز آینده";
    case "91_120":
      return "۹۱ تا ۱۲۰ روز آینده";
    case "beyond_120":
      return "بیش از ۱۲۰ روز";
  }
}

/** The net of one calendar day (قطعی only — the forecast column never nets in). */
export function confirmedNetIrr(day: ForecastCalendarDayResponse): number {
  return day.inflow_confirmed_irr - day.outflow_confirmed_irr;
}

/** One cell of the month grid: a day, or a blank before the 1st. */
export type CalendarCell =
  | { kind: "blank" }
  | { kind: "day"; day: ForecastCalendarDayResponse; jalaliDay: number };

/**
 * Lay a month's days out as Saturday-first weeks for the grid.
 *
 * Leading blanks pad the first week (the 1st rarely falls on a
 * Saturday); the week rows always have 7 cells. `jalaliDay` is the
 * Jalali day-of-month (the grid shows Jalali numbers, never
 * Gregorian ones).
 */
export function calendarWeeks(
  days: ForecastCalendarDayResponse[],
): CalendarCell[][] {
  if (days.length === 0) return [];
  const first = new Date(`${days[0].date}T00:00:00Z`);
  const leading = jalaliWeekday(first);
  const cells: CalendarCell[] = Array.from({ length: leading }, () => ({ kind: "blank" as const }));
  for (const day of days) {
    const { jd } = isoToJalali(day.date);
    cells.push({ kind: "day", day, jalaliDay: jd });
  }
  while (cells.length % 7 !== 0) {
    cells.push({ kind: "blank" });
  }
  const weeks: CalendarCell[][] = [];
  for (let i = 0; i < cells.length; i += 7) {
    weeks.push(cells.slice(i, i + 7));
  }
  return weeks;
}

/** The month of a forecast response that matches a Jalali month key. */
export function monthForKey(
  forecast: CashForecastResponse,
  monthJalali: string,
): ForecastCalendarMonthResponse | undefined {
  return forecast.months.find((month) => month.month_jalali === monthJalali);
}

/** Total expected inflow/outflow of one horizon bucket (both columns). */
export function horizonBucketTotals(bucket: ForecastHorizonBucketResponse): {
  inflow: number;
  outflow: number;
  net: number;
} {
  const inflow = bucket.inflow_confirmed_irr + bucket.inflow_forecast_irr;
  const outflow = bucket.outflow_confirmed_irr + bucket.outflow_forecast_irr;
  return { inflow, outflow, net: inflow - outflow };
}
