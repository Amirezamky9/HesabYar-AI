/**
 * Bank reconciliation status report (spec §11.2, US-514).
 *
 * Per-journal matched / suggested / ambiguous / unmatched lines in a
 * period, with the §11.1 freshness indicator. Default period is the
 * current Jalali month. Rendered as a tab of the reports page.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  ApiError,
  api,
  type ReconciliationStatusResponse,
} from "@/lib/api-client";
import { formatIsoJalali, jalaliToIso, toJalali, todayIso } from "@/lib/jalali";
import { formatMoney } from "@/lib/format-money";
import { fa } from "@/lib/utils";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  JalaliDatePicker,
  Label,
  Skeleton,
  type DataTableColumn,
} from "@/components/ui";
import {
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";

function currentJalaliMonthStartIso(): string {
  const { jy, jm } = toJalali(new Date());
  return jalaliToIso(jy, jm, 1);
}

const ROW_COLUMNS: DataTableColumn<
  ReconciliationStatusResponse["rows"][number]
>[] = [
  { key: "journal", header: "دفتر", render: (row) => row.journal_name },
  {
    key: "shaba",
    header: "شبا",
    render: (row) =>
      row.destination_shaba ? (
        <span dir="ltr" className="tabular-nums">
          {row.destination_shaba}
        </span>
      ) : (
        "—"
      ),
  },
  { key: "statements", header: "صورت‌حساب", render: (row) => fa(row.statements_count) },
  { key: "lines", header: "ردیف", render: (row) => fa(row.lines_total) },
  { key: "matched", header: "تطبیق‌یافته", render: (row) => fa(row.matched_lines) },
  { key: "suggested", header: "پیشنهاد شده", render: (row) => fa(row.suggested_lines) },
  { key: "ambiguous", header: "مبهم", render: (row) => fa(row.ambiguous_lines) },
  { key: "unmatched", header: "تطبیق‌نیافته", render: (row) => fa(row.unmatched_lines) },
  {
    key: "rate",
    header: "نرخ تطبیق",
    render: (row) =>
      row.match_rate === null ? "—" : `${fa(Math.round(row.match_rate * 100))}٪`,
  },
  {
    key: "balance",
    header: "ماندهٔ تطبیق‌نیافته",
    render: (row) => (
      <span className="tabular-nums">
        {row.unmatched_balance_irr < 0 ? "−" : ""}
        {formatMoney(Math.abs(row.unmatched_balance_irr))}
      </span>
    ),
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "oldest",
    header: "قدیمی‌ترین ردیف (روز)",
    render: (row) =>
      row.oldest_unmatched_age_days === null ? "—" : fa(row.oldest_unmatched_age_days),
  },
];

export function ReconciliationStatusReport({ companyId }: { companyId: string | null }) {
  const router = useRouter();
  const [dateFrom, setDateFrom] = React.useState<string | null>(() =>
    currentJalaliMonthStartIso(),
  );
  const [dateTo, setDateTo] = React.useState<string | null>(() => todayIso());
  const [status, setStatus] = React.useState<ReconciliationStatusResponse | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    if (!companyId || !dateFrom || !dateTo) return;
    let cancelled = false;
    api
      .getReconciliationStatus({ company: companyId, date_from: dateFrom, date_to: dateTo })
      .then((data) => {
        if (!cancelled) setStatus(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.replace("/login");
          return;
        }
        setError(err);
      });
    return () => {
      cancelled = true;
    };
  }, [companyId, dateFrom, dateTo, reloadKey, router]);

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(error, "دریافت گزارش تطبیق ناموفق بود.")}
      />
    );
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت گزارش تطبیق ناموفق بود.")}
        onRetry={() => {
          setError(null);
          setStatus(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-1.5">
          <Label htmlFor="recon-from">از تاریخ</Label>
          <JalaliDatePicker
            id="recon-from"
            value={dateFrom}
            onChange={(iso) => {
              setDateFrom(iso);
              setStatus(null);
            }}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="recon-to">تا تاریخ</Label>
          <JalaliDatePicker
            id="recon-to"
            value={dateTo}
            onChange={(iso) => {
              setDateTo(iso);
              setStatus(null);
            }}
          />
        </div>
      </div>

      {status === null ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>وضعیت تطبیق بانکی</CardTitle>
            <CardDescription>
              دوره: {formatIsoJalali(status.date_from)} تا {formatIsoJalali(status.date_to)}
              {" · "}
              {status.freshness === "live" ? "دادهٔ زنده" : "بدون داده"}
              {" · "}
              منبع: {status.source}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <dl className="grid gap-4 sm:grid-cols-4">
              <div>
                <dt className="text-sm text-muted-foreground">صورت‌حساب‌ها</dt>
                <dd className="mt-1 font-medium tabular-nums">{fa(status.statements_count)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">ردیف‌ها</dt>
                <dd className="mt-1 font-medium tabular-nums">{fa(status.lines_total)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">تطبیق‌یافته</dt>
                <dd className="mt-1 font-medium tabular-nums">{fa(status.matched_lines)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">تطبیق‌نیافته</dt>
                <dd className="mt-1 font-medium tabular-nums">{fa(status.unmatched_lines)}</dd>
              </div>
            </dl>
            <DataTable
              columns={ROW_COLUMNS}
              rows={status.rows}
              rowKey={(row) => row.journal_id}
              emptyText="در این دوره صورت‌حسابی وجود ندارد."
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
