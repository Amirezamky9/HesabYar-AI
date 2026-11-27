/**
 * Cash-forecast calendar (US-553).
 *
 * Monthly Jalali view of expected inflows and outflows, computed by
 * GET /reports/cash-forecast from registered records only: gateway
 * settlement tranches, sales installments, due cheques (report 14),
 * due invoices and packaging purchase requests. Two columns stay
 * strictly separate everywhere: قطعی (registered contract / cheque /
 * installment) and پیش‌بینی (expected sales — no source yet, so the
 * column is honestly empty until a sales-forecast source lands).
 * Collateral cheques appear in NEITHER column (backend-enforced).
 *
 * One fetch covers [today − 180d, today + 120d]; month navigation is
 * client-side, clamped to that window. The horizon table is the
 * §11.2 30/60/90/120-day forecast.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowDownLeft, ArrowUpRight, CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import { api, type CashForecastResponse, type ForecastItemResponse } from "@/lib/api-client";
import { formatIsoJalali, JALALI_WEEKDAYS, JALALI_WEEKDAYS_SHORT, todayIso } from "@/lib/jalali";
import { formatMoney } from "@/lib/format-money";
import { fa } from "@/lib/utils";
import { Button, Card, CardContent, CardHeader, CardTitle, Skeleton } from "@/components/ui";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { ReportFreshnessLine } from "@/features/reports/financial-sections";
import {
  calendarWeeks,
  compareJalaliMonths,
  forecastFetchRange,
  forecastSourceLabel,
  horizonBucketLabel,
  horizonBucketTotals,
  jalaliMonthLabel,
  jalaliMonthOfIso,
  monthForKey,
  shiftJalaliMonth,
  type JalaliMonth,
} from "./cash-forecast-data";

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; error: unknown }
  | { kind: "ok"; forecast: CashForecastResponse };

function monthKey(month: JalaliMonth): string {
  return `${month.jy}-${String(month.jm).padStart(2, "0")}`;
}

function monthTotals(forecast: CashForecastResponse, key: string) {
  const month = monthForKey(forecast, key);
  const totals = {
    inflowConfirmed: 0,
    outflowConfirmed: 0,
    inflowForecast: 0,
    outflowForecast: 0,
    itemCount: 0,
  };
  if (!month) return totals;
  for (const day of month.days) {
    totals.inflowConfirmed += day.inflow_confirmed_irr;
    totals.outflowConfirmed += day.outflow_confirmed_irr;
    totals.inflowForecast += day.inflow_forecast_irr;
    totals.outflowForecast += day.outflow_forecast_irr;
    totals.itemCount += day.item_count;
  }
  return totals;
}

function SourceBreakdown({ forecast }: { forecast: CashForecastResponse }) {
  const sums = new Map<ForecastItemResponse["source"], { inflow: number; outflow: number; count: number }>();
  for (const item of forecast.items) {
    const entry = sums.get(item.source) ?? { inflow: 0, outflow: 0, count: 0 };
    if (item.direction === "inflow") entry.inflow += item.amount_irr;
    else entry.outflow += item.amount_irr;
    entry.count += 1;
    sums.set(item.source, entry);
  }
  const rows = [...sums.entries()].sort((a, b) => b[1].inflow + b[1].outflow - (a[1].inflow + a[1].outflow));
  if (rows.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        در این بازه هیچ قلم ثبت‌شده‌ای برای پیش‌بینی وجود ندارد؛ تقویم پس از ثبت تسویه، قسط، چک یا
        فاکتور پر می‌شود.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {rows.map(([source, entry]) => (
        <li key={source} className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border pb-2 last:border-0 last:pb-0">
          <span className="text-sm">
            {forecastSourceLabel(source)} <span className="text-muted-foreground">({fa(entry.count)} قلم)</span>
          </span>
          <span className="flex items-center gap-4 text-sm">
            {entry.inflow > 0 && (
              <span className="inline-flex items-center gap-1">
                <ArrowDownLeft className="size-4" aria-hidden />
                {formatMoney(entry.inflow)}
              </span>
            )}
            {entry.outflow > 0 && (
              <span className="inline-flex items-center gap-1">
                <ArrowUpRight className="size-4" aria-hidden />
                {formatMoney(entry.outflow)}
              </span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function DayItems({ items, date }: { items: ForecastItemResponse[]; date: string }) {
  if (items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        برای {formatIsoJalali(date)} هیچ قلمی ثبت نشده است.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {items.map((item) => (
        <li key={`${item.source}-${item.ref}-${item.date}`} className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border pb-2 last:border-0 last:pb-0">
          <span className="text-sm">
            {item.label} <span className="text-muted-foreground">· {forecastSourceLabel(item.source)}</span>
            <span className="text-muted-foreground"> · {item.bucket === "confirmed" ? "قطعی" : "پیش‌بینی"}</span>
          </span>
          <span className="inline-flex items-center gap-1 text-sm">
            {item.direction === "inflow" ? (
              <ArrowDownLeft className="size-4" aria-hidden />
            ) : (
              <ArrowUpRight className="size-4" aria-hidden />
            )}
            {formatMoney(item.amount_irr)}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function CashForecastSection({
  companyId,
  companyName,
}: {
  companyId: string | null;
  companyName?: string;
}) {
  const today = todayIso();
  const range = React.useMemo(() => forecastFetchRange(today), [today]);
  const minMonth = React.useMemo(() => jalaliMonthOfIso(range.fromIso), [range.fromIso]);
  const maxMonth = React.useMemo(() => jalaliMonthOfIso(range.toIso), [range.toIso]);
  const [month, setMonth] = React.useState<JalaliMonth>(() => jalaliMonthOfIso(today));
  const [selectedDay, setSelectedDay] = React.useState<string | null>(null);
  const [state, setState] = React.useState<LoadState>({ kind: "loading" });
  const [nonce, setNonce] = React.useState(0);

  // Loading state is set by load() from a timer callback, never
  // synchronously in the effect body (react-hooks/set-state-in-effect).
  const load = React.useCallback(() => {
    if (companyId === null) return () => undefined;
    let cancelled = false;
    setState({ kind: "loading" });
    api
      .getCashForecast({ company: companyId, date_from: range.fromIso, date_to: range.toIso })
      .then((forecast) => {
        if (!cancelled) setState({ kind: "ok", forecast });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ kind: "error", error });
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, range.fromIso, range.toIso]);

  React.useEffect(() => {
    let cleanup: (() => void) | undefined;
    const timer = setTimeout(() => {
      cleanup = load();
    }, 0);
    return () => {
      clearTimeout(timer);
      cleanup?.();
    };
  }, [load, nonce]);

  const key = monthKey(month);
  const canPrev = compareJalaliMonths(month, minMonth) > 0;
  const canNext = compareJalaliMonths(month, maxMonth) < 0;

  if (companyId === null) {
    return (
      <div className="space-y-4" aria-label="در حال بارگذاری شرکت">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (state.kind === "loading") {
    return (
      <div className="space-y-4" aria-label="در حال بارگذاری پیش‌بینی جریان نقد">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-96 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  if (state.kind === "error") {
    if (isPermissionDenied(state.error)) {
      return (
        <PermissionDeniedState
          message={persianApiErrorMessage(state.error, "دسترسی به پیش‌بینی جریان نقد ندارید.")}
        />
      );
    }
    return (
      <ErrorState
        message={persianApiErrorMessage(state.error, "پیش‌بینی جریان نقد بارگذاری نشد.")}
        onRetry={() => setNonce((n) => n + 1)}
      />
    );
  }

  const forecast = state.forecast;
  const monthData = monthForKey(forecast, key);
  const weeks = monthData ? calendarWeeks(monthData.days) : [];
  const totals = monthTotals(forecast, key);
  const dayItems = selectedDay
    ? forecast.items.filter((item) => item.date === selectedDay)
    : [];
  const isToday = (date: string) => date === today;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">پیش‌بینی جریان نقد</h2>
          <p className="text-sm text-muted-foreground">
            {companyName ? `شرکت ${companyName} · ` : ""}
            تقویم ماهانهٔ ورودی‌ها و خروجی‌های موردانتظار، فقط از روی سوابق ثبت‌شده
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-11"
            disabled={!canPrev}
            onClick={() => setMonth((m) => shiftJalaliMonth(m, -1))}
            aria-label="ماه قبل"
          >
            <ChevronRight className="size-5" aria-hidden />
          </Button>
          <span className="min-w-28 text-center text-base font-semibold" aria-live="polite">
            {jalaliMonthLabel(key)}
          </span>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-11"
            disabled={!canNext}
            onClick={() => setMonth((m) => shiftJalaliMonth(m, 1))}
            aria-label="ماه بعد"
          >
            <ChevronLeft className="size-5" aria-hidden />
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">ورودی قطعی این ماه</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-base font-semibold">{formatMoney(totals.inflowConfirmed)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">خروجی قطعی این ماه</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-base font-semibold">{formatMoney(totals.outflowConfirmed)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">ورودی پیش‌بینی این ماه</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-base font-semibold">{formatMoney(totals.inflowForecast)}</p>
            <p className="text-xs text-muted-foreground">فروش موردانتظار هنوز منبع ندارد</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">خروجی پیش‌بینی این ماه</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-base font-semibold">{formatMoney(totals.outflowForecast)}</p>
            <p className="text-xs text-muted-foreground">فروش موردانتظار هنوز منبع ندارد</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <CalendarDays className="size-5" aria-hidden />
            تقویم {jalaliMonthLabel(key)}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {totals.itemCount === 0 ? (
            <p className="text-sm text-muted-foreground">
              در {jalaliMonthLabel(key)} هیچ ورودی یا خروجی ثبت‌شده‌ای سررسید ندارد.
            </p>
          ) : null}
          <div className="grid grid-cols-7 gap-1" role="grid" aria-label={`تقویم ${jalaliMonthLabel(key)}`}>
            {JALALI_WEEKDAYS_SHORT.map((short, i) => (
              <div
                key={short}
                className="py-2 text-center text-xs font-medium text-muted-foreground"
                aria-label={JALALI_WEEKDAYS[i]}
              >
                {short}
              </div>
            ))}
            {weeks.flat().map((cell, index) => {
              if (cell.kind === "blank") {
                return <div key={`blank-${index}`} className="min-h-11" aria-hidden />;
              }
              const { day, jalaliDay } = cell;
              const hasItems = day.item_count > 0;
              const selected = selectedDay === day.date;
              return (
                <button
                  key={day.date}
                  type="button"
                  onClick={() => setSelectedDay(selected ? null : day.date)}
                  aria-pressed={selected}
                  aria-label={`${formatIsoJalali(day.date)}${hasItems ? `، ${fa(day.item_count)} قلم` : "، بدون قلم"}`}
                  className={`flex min-h-11 flex-col items-center justify-start gap-0.5 rounded-md border px-0.5 py-1.5 text-xs ${
                    selected
                      ? "border-primary bg-primary/10"
                      : "border-border hover:border-primary/50"
                  } ${isToday(day.date) ? "ring-1 ring-primary" : ""}`}
                >
                  <span className="text-sm font-medium">{fa(jalaliDay)}</span>
                  {day.inflow_confirmed_irr > 0 && (
                    <span className="inline-flex items-center gap-0.5">
                      <ArrowDownLeft className="size-3 shrink-0" aria-hidden />
                      {formatMoney(day.inflow_confirmed_irr)}
                    </span>
                  )}
                  {day.outflow_confirmed_irr > 0 && (
                    <span className="inline-flex items-center gap-0.5">
                      <ArrowUpRight className="size-3 shrink-0" aria-hidden />
                      {formatMoney(day.outflow_confirmed_irr)}
                    </span>
                  )}
                  {(day.inflow_forecast_irr > 0 || day.outflow_forecast_irr > 0) && (
                    <span className="text-muted-foreground">پیش‌بینی</span>
                  )}
                </button>
              );
            })}
          </div>
          {selectedDay && (
            <div className="border-t border-border pt-4">
              <h3 className="mb-2 text-sm font-semibold">اقلام {formatIsoJalali(selectedDay)}</h3>
              <DayItems items={dayItems} date={selectedDay} />
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">پیش‌بینی ۳۰ تا ۱۲۰ روزه</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-start text-muted-foreground">
                  <th scope="col" className="py-2 pe-2 text-start font-medium">بازه</th>
                  <th scope="col" className="py-2 pe-2 text-start font-medium">ورودی قطعی</th>
                  <th scope="col" className="py-2 pe-2 text-start font-medium">خروجی قطعی</th>
                  <th scope="col" className="py-2 pe-2 text-start font-medium">ورودی پیش‌بینی</th>
                  <th scope="col" className="py-2 pe-2 text-start font-medium">خروجی پیش‌بینی</th>
                  <th scope="col" className="py-2 text-start font-medium">خالص</th>
                </tr>
              </thead>
              <tbody>
                {forecast.horizon.map((bucket) => {
                  const { net } = horizonBucketTotals(bucket);
                  return (
                    <tr key={bucket.key} className="border-b border-border last:border-0">
                      <th scope="row" className="py-2 pe-2 text-start font-medium">
                        {horizonBucketLabel(bucket.key)}
                        <span className="text-muted-foreground"> ({fa(bucket.item_count)} قلم)</span>
                      </th>
                      <td className="py-2 pe-2">{formatMoney(bucket.inflow_confirmed_irr)}</td>
                      <td className="py-2 pe-2">{formatMoney(bucket.outflow_confirmed_irr)}</td>
                      <td className="py-2 pe-2">{formatMoney(bucket.inflow_forecast_irr)}</td>
                      <td className="py-2 pe-2">{formatMoney(bucket.outflow_forecast_irr)}</td>
                      <td className={`py-2 font-medium ${net < 0 ? "text-destructive" : ""}`}>
                        {formatMoney(net)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            اقلام سررسید گذشته تا ۱۸۰ روز پیش در بازهٔ «سررسید گذشته» آمده‌اند.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">تفکیک بر اساس منبع</CardTitle>
        </CardHeader>
        <CardContent>
          <SourceBreakdown forecast={forecast} />
          {forecast.skipped_undated > 0 && (
            <p className="mt-4 text-xs text-muted-foreground">
              {fa(forecast.skipped_undated)} فاکتور بدون سررسید در پیش‌بینی نیامده‌اند؛ تاریخ
              سررسید آن‌ها در ترایتن ثبت نشده است.
            </p>
          )}
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <ReportFreshnessLine freshness={forecast.freshness} sourceLabel="سوابق ثبت‌شده (تسویه، قسط، چک، فاکتور، خرید)" />
        <Link href="/cheques/reports" className="text-sm text-primary underline-offset-4 hover:underline">
          گزارش‌های چک
        </Link>
      </div>
    </div>
  );
}
