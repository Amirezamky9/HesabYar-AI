/**
 * Dashboard cash card (US-553) — SELF-CONTAINED.
 *
 * One import + one JSX block in the dashboard page; the card fetches
 * its own next-30-days forecast and renders nothing but itself, so
 * the US-551 dashboard rebuild can place it anywhere (or drop it)
 * without touching its internals.
 *
 * Shows the قطعی (confirmed) net of the next 30 days only — the
 * پیش‌بینی column has no source yet and never nets in.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowDownLeft, ArrowUpRight, CalendarDays } from "lucide-react";
import { api, type CashForecastResponse } from "@/lib/api-client";
import { addDaysIso, forecastSourceLabel } from "./cash-forecast-data";
import { todayIso } from "@/lib/jalali";
import { formatMoney } from "@/lib/format-money";
import { Card, CardContent, CardHeader, CardTitle, Skeleton } from "@/components/ui";

type LoadState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ok"; forecast: CashForecastResponse };

export function DashboardCashCard({ companyId }: { companyId: string | null }) {
  const [state, setState] = React.useState<LoadState>({ kind: "loading" });

  React.useEffect(() => {
    if (companyId === null) return;
    let cancelled = false;
    const today = todayIso();
    api
      .getCashForecast({
        company: companyId,
        date_from: today,
        date_to: addDaysIso(today, 30),
      })
      .then((forecast) => {
        if (!cancelled) setState({ kind: "ok", forecast });
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [companyId]);

  if (companyId === null || state.kind === "loading") {
    return (
      <Card data-testid="widget-cash-forecast" aria-label="در حال بارگذاری پیش‌بینی نقد">
        <CardHeader>
          <CardTitle className="text-base">پیش‌بینی نقد ۳۰ روز آینده</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-16 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (state.kind === "error") {
    return (
      <Card data-testid="widget-cash-forecast">
        <CardHeader>
          <CardTitle className="text-base">پیش‌بینی نقد ۳۰ روز آینده</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            پیش‌بینی بارگذاری نشد؛ صفحهٔ{" "}
            <Link href="/reports/cash-forecast" className="text-primary underline-offset-4 hover:underline">
              پیش‌بینی جریان نقد
            </Link>{" "}
            را باز کنید.
          </p>
        </CardContent>
      </Card>
    );
  }

  const totals = state.forecast.totals;
  const net = totals.inflow_confirmed_irr - totals.outflow_confirmed_irr;
  const hasItems = totals.item_count > 0;

  return (
    <Card data-testid="widget-cash-forecast">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base">پیش‌بینی نقد ۳۰ روز آینده</CardTitle>
        <CalendarDays className="size-5 text-muted-foreground" aria-hidden />
      </CardHeader>
      <CardContent className="space-y-3">
        {!hasItems ? (
          <p className="text-sm text-muted-foreground">
            در ۳۰ روز آینده هیچ ورودی یا خروجی قطعی ثبت نشده است.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
              <span className="inline-flex items-center gap-1">
                <ArrowDownLeft className="size-4" aria-hidden />
                ورودی قطعی {formatMoney(totals.inflow_confirmed_irr)}
              </span>
              <span className="inline-flex items-center gap-1">
                <ArrowUpRight className="size-4" aria-hidden />
                خروجی قطعی {formatMoney(totals.outflow_confirmed_irr)}
              </span>
            </div>
            <p className={`text-base font-semibold ${net < 0 ? "text-destructive" : ""}`}>
              خالص قطعی: {formatMoney(net)}
            </p>
            <p className="text-xs text-muted-foreground">
              بر اساس{" "}
              {Object.entries(state.forecast.source_counts)
                .map(([source, count]) => `${forecastSourceLabel(source)} (${count})`)
                .join("، ")}
            </p>
          </>
        )}
        <Link
          href="/reports/cash-forecast"
          className="inline-block text-sm text-primary underline-offset-4 hover:underline"
        >
          مشاهدهٔ تقویم پیش‌بینی
        </Link>
      </CardContent>
    </Card>
  );
}
