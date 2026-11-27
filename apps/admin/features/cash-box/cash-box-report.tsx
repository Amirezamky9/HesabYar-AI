/**
 * Cash-box variance report (US-516) — the «گزارش‌ها ← صندوق» tab.
 *
 * Per cash box: opening/closing book balances (posted moves only),
 * receipts/payments, counting sessions with shortage/overage totals,
 * and the recorded-sales total (posted customer invoices) with the
 * unrecorded-sales indicator (net overage floored at zero — an
 * indicator, not proof). Every figure states its company, period
 * and source (§11.1 provenance); the sales side is Tryton invoices
 * only until the store link lands, and says so honestly.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, type CashBoxReportResponse } from "@/lib/api-client";
import { formatIsoJalali, todayIso } from "@/lib/jalali";
import { formatMoney } from "@/lib/format-money";
import { faNumber } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  JalaliDatePicker,
  Label,
  Skeleton,
} from "@/components/ui";
import { persianApiErrorMessage } from "@/features/shared/api-error";

function firstDayOfMonthIso(): string {
  const today = todayIso();
  return `${today.slice(0, 7)}-01`;
}

export function CashBoxReportSection({
  companyId,
  companyName,
}: {
  companyId: string | null;
  companyName: string;
}) {
  const router = useRouter();
  const [dateFrom, setDateFrom] = React.useState<string | null>(firstDayOfMonthIso());
  const [dateTo, setDateTo] = React.useState<string | null>(todayIso());
  const [report, setReport] = React.useState<CashBoxReportResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [loaded, setLoaded] = React.useState(false);
  const [reloadKey, setReloadKey] = React.useState(0);

  // Fetch inside the effect with promise callbacks (repo pattern):
  // state is set in the callbacks, never synchronously in the effect
  // body. Picking a date re-fetches; the button forces a refresh.
  React.useEffect(() => {
    if (!companyId || !dateFrom || !dateTo) return;
    let cancelled = false;
    api
      .getCashBoxReport({ company: companyId, date_from: dateFrom, date_to: dateTo })
      .then((data) => {
        if (cancelled) return;
        setReport(data);
        setLoaded(true);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(persianApiErrorMessage(err, "دریافت گزارش صندوق ناموفق بود."));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, dateFrom, dateTo, reloadKey, router]);

  const refresh = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  return (
    <div className="space-y-6" data-testid="cash-box-report">
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <Label htmlFor="cash-box-report-from">از تاریخ</Label>
          <JalaliDatePicker id="cash-box-report-from" value={dateFrom} onChange={setDateFrom} />
        </div>
        <div>
          <Label htmlFor="cash-box-report-to">تا تاریخ</Label>
          <JalaliDatePicker id="cash-box-report-to" value={dateTo} onChange={setDateTo} />
        </div>
        <Button type="button" onClick={refresh} disabled={loading || !dateFrom || !dateTo}>
          به‌روزرسانی گزارش
        </Button>
      </div>

      {loading && !loaded ? (
        <div className="space-y-4" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : report ? (
        <>
          <p className="text-sm text-muted-foreground">
            شرکت: {companyName} · دوره: {formatIsoJalali(report.date_from)} تا {formatIsoJalali(report.date_to)} ·
            واحد: تومان (مبالغ به ریال ذخیره می‌شوند) · منبع فروش: صورت‌حساب‌های ثبت‌شدهٔ ترایتن
          </p>
          <div className="grid gap-4 sm:grid-cols-3">
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-medium text-muted-foreground">مجموع کسری</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-xl font-bold">{formatMoney(report.total_shortage_irr)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-medium text-muted-foreground">مجموع اضافه</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-xl font-bold">{formatMoney(report.total_overage_irr)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  فروش ثبت‌نشدهٔ احتمالی
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-xl font-bold">{formatMoney(report.total_possible_unrecorded_sales_irr)}</p>
                <p className="mt-1 text-sm text-muted-foreground">
                  اضافهٔ خالص صندوق‌ها؛ یک نشانه است، نه اثبات.
                </p>
              </CardContent>
            </Card>
          </div>
          <DataTable
            columns={[
              { key: "box", header: "صندوق", render: (line) => line.cash_box_name_fa },
              {
                key: "opening",
                header: "ماندهٔ اول دوره",
                render: (line) => formatMoney(line.opening_book_balance_irr),
              },
              {
                key: "closing",
                header: "ماندهٔ آخر دوره",
                render: (line) => formatMoney(line.closing_book_balance_irr),
              },
              { key: "receipts", header: "دریافت", render: (line) => formatMoney(line.receipts_irr) },
              { key: "payments", header: "پرداخت", render: (line) => formatMoney(line.payments_irr) },
              {
                key: "sessions",
                header: "جلسات شمارش",
                render: (line) => faNumber(line.session_count),
              },
              { key: "shortage", header: "کسری", render: (line) => formatMoney(line.shortage_irr) },
              { key: "overage", header: "اضافه", render: (line) => formatMoney(line.overage_irr) },
              {
                key: "sales",
                header: "فروش ثبت‌شده",
                render: (line) => formatMoney(line.recorded_sales_irr),
              },
              {
                key: "unrecorded",
                header: "فروش ثبت‌نشدهٔ احتمالی",
                render: (line) => formatMoney(line.possible_unrecorded_sales_irr),
              },
            ]}
            rows={report.lines}
            rowKey={(line) => line.cash_box_id}
            emptyText="صندوقی برای این شرکت ثبت نشده است."
          />
        </>
      ) : null}
    </div>
  );
}
