/**
 * General ledger section (US-550): summary per account, or one
 * account's posted lines with running balances when an account is
 * picked (or drilled into from the trial balance). Account/period/
 * company filters; CSV export carries the applied filters (AC 7).
 * All figures are read live from Tryton through the backend.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import { AccountPicker } from "@/components/journal-entries/account-picker";
import { ApiError, api, type AccountResponse } from "@/lib/api-client";
import { formatIsoJalali, todayIso } from "@/lib/jalali";
import { formatMoney } from "@/lib/format-money";
import { fa } from "@/lib/utils";
import {
  Button,
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
import { fetchAllPages } from "@/features/dashboard/dashboard-data";
import { csvFilename, downloadCsv } from "@/features/shared/csv";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { ReportFreshnessLine } from "./financial-sections";
import { generalLedgerDetailToCsv, generalLedgerSummaryToCsv, type CsvFilters } from "./report-csv";

type LedgerResponse = Awaited<ReturnType<typeof api.getGeneralLedger>>;
type LedgerAccount = LedgerResponse["accounts"][number];
type LedgerLine = LedgerAccount["lines"][number];

export function GeneralLedgerSection({
  companyId,
  companyName,
  accountId,
  dateFrom,
  dateTo,
  onAccountIdChange,
  onDateFromChange,
  onDateToChange,
}: {
  companyId: string | null;
  companyName: string;
  /** Controlled filters: the parent owns them so the trial-balance drill-down sets them in an event handler. */
  accountId: number | null;
  dateFrom: string | null;
  dateTo: string | null;
  onAccountIdChange: (accountId: number | null) => void;
  onDateFromChange: (iso: string | null) => void;
  onDateToChange: (iso: string | null) => void;
}) {
  const router = useRouter();
  const [accounts, setAccounts] = React.useState<AccountResponse[] | null>(null);
  const [report, setReport] = React.useState<LedgerResponse | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  const setAccountId = (id: number | null) => {
    onAccountIdChange(id);
    setReport(null);
  };
  const setDateFrom = (iso: string | null) => {
    onDateFromChange(iso);
    setReport(null);
  };
  const setDateTo = (iso: string | null) => {
    onDateToChange(iso);
    setReport(null);
  };

  React.useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    fetchAllPages((cursor) => api.listAccounts({ company: companyId, limit: 100, cursor }))
      .then((result) => {
        if (!cancelled) setAccounts(result.items);
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
  }, [companyId, router]);

  React.useEffect(() => {
    if (!companyId || !dateFrom || !dateTo) return;
    let cancelled = false;
    api
      .getGeneralLedger({
        company: companyId,
        date_from: dateFrom,
        date_to: dateTo,
        ...(accountId !== null ? { account_id: accountId } : {}),
      })
      .then((data) => {
        if (!cancelled) setReport(data);
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
  }, [companyId, accountId, dateFrom, dateTo, reloadKey, router]);

  const selectedAccount = accountId !== null ? (report?.accounts[0] ?? null) : null;
  const selectedName =
    accounts?.find((a) => a.id === accountId)?.name ?? selectedAccount?.name ?? "";

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت دفتر کل ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت دفتر کل ناموفق بود.")}
        onRetry={() => {
          setError(null);
          setReport(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  const period = dateFrom && dateTo ? `${dateFrom} تا ${dateTo}` : "";
  const filters: CsvFilters = [
    ["شرکت", companyName],
    ["دوره", period],
    ...(accountId !== null ? ([["حساب", selectedName]] as CsvFilters) : []),
  ];

  const summaryColumns: DataTableColumn<LedgerAccount>[] = [
    { key: "code", header: "کد حساب", render: (row) => <span className="tabular-nums">{fa(row.code)}</span> },
    { key: "name", header: "نام حساب", render: (row) => row.name },
    {
      key: "opening",
      header: "ماندهٔ ابتدا",
      render: (row) => <span className="tabular-nums">{formatMoney(row.opening_balance)}</span>,
      className: "text-end",
    },
    {
      key: "debit",
      header: "بدهکار",
      render: (row) => <span className="tabular-nums">{formatMoney(row.total_debit)}</span>,
      className: "text-end",
    },
    {
      key: "credit",
      header: "بستانکار",
      render: (row) => <span className="tabular-nums">{formatMoney(row.total_credit)}</span>,
      className: "text-end",
    },
    {
      key: "closing",
      header: "ماندهٔ انتها",
      render: (row) => <span className="tabular-nums">{formatMoney(row.closing_balance)}</span>,
      className: "text-end",
    },
  ];

  const lineColumns: DataTableColumn<LedgerLine>[] = [
    { key: "date", header: "تاریخ", render: (row) => formatIsoJalali(row.date) },
    { key: "move", header: "شمارهٔ سند", render: (row) => <span className="tabular-nums">{fa(row.move_number || "—")}</span> },
    { key: "description", header: "شرح", render: (row) => row.description || "—" },
    {
      key: "debit",
      header: "بدهکار",
      render: (row) => <span className="tabular-nums">{formatMoney(row.debit)}</span>,
      className: "text-end",
    },
    {
      key: "credit",
      header: "بستانکار",
      render: (row) => <span className="tabular-nums">{formatMoney(row.credit)}</span>,
      className: "text-end",
    },
    {
      key: "balance",
      header: "مانده",
      render: (row) => <span className="tabular-nums">{formatMoney(row.balance)}</span>,
      className: "text-end",
    },
  ];

  return (
    <div className="space-y-6" data-testid="report-general-ledger">
      <div className="flex flex-wrap items-end gap-4 print:hidden">
        <div className="space-y-1.5">
          <Label htmlFor="gl-from">از تاریخ</Label>
          <JalaliDatePicker
            id="gl-from"
            value={dateFrom}
            onChange={(iso) => {
              setDateFrom(iso);
            }}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="gl-to">تا تاریخ</Label>
          <JalaliDatePicker
            id="gl-to"
            value={dateTo}
            onChange={(iso) => {
              setDateTo(iso);
            }}
          />
        </div>
        <div className="min-w-64 space-y-1.5">
          <Label htmlFor="gl-account">حساب (خالی = همهٔ حساب‌ها)</Label>
          <AccountPicker
            id="gl-account"
            accounts={accounts ?? []}
            value={accountId}
            onChange={(id) => {
              setAccountId(id);
            }}
            loading={accounts === null}
            ariaLabel="انتخاب حساب برای دفتر کل"
          />
        </div>
        {accountId !== null ? (
          <Button
            variant="outline"
            onClick={() => {
              setAccountId(null);
            }}
          >
            بازگشت به خلاصهٔ همهٔ حساب‌ها
          </Button>
        ) : null}
      </div>

      {report === null ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : (
        <>
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <CardTitle>
                  {selectedAccount ? `دفتر کل — ${selectedAccount.code} ${selectedAccount.name}` : "دفتر کل"}
                </CardTitle>
                <CardDescription>
                  دوره: {formatIsoJalali(report.date_from)} تا {formatIsoJalali(report.date_to)} · منبع: ترایتون
                  (مرجع) · نمودار: جدول · فقط سندهای ثبت‌شده
                </CardDescription>
              </div>
              <Button
                variant="outline"
                size="sm"
                className="print:hidden"
                disabled={report.accounts.length === 0}
                onClick={() => {
                  if (selectedAccount) {
                    downloadCsv(
                      csvFilename(`report-general-ledger-${selectedAccount.code}`, todayIso()),
                      generalLedgerDetailToCsv(selectedAccount, report.date_from, report.date_to, filters),
                    );
                  } else {
                    downloadCsv(
                      csvFilename("report-general-ledger", todayIso()),
                      generalLedgerSummaryToCsv(report, filters),
                    );
                  }
                }}
              >
                <Download className="size-4" aria-hidden />
                خروجی CSV
              </Button>
            </CardHeader>
            <CardContent className="space-y-4">
              {selectedAccount ? (
                <>
                  <p className="text-sm text-muted-foreground">
                    ماندهٔ ابتدای دوره: {formatMoney(selectedAccount.opening_balance)} · بدهکار:{" "}
                    {formatMoney(selectedAccount.total_debit)} · بستانکار:{" "}
                    {formatMoney(selectedAccount.total_credit)} · ماندهٔ انتهای دوره:{" "}
                    {formatMoney(selectedAccount.closing_balance)} · مبتنی بر {fa(report.move_count)} سند و{" "}
                    {fa(report.line_count)} ردیف
                  </p>
                  <DataTable
                    columns={lineColumns}
                    rows={selectedAccount.lines}
                    rowKey={(row) => `${row.move_id}-${row.date}-${row.debit}-${row.credit}`}
                    emptyText="این حساب در این دوره سندی ندارد."
                  />
                </>
              ) : (
                <>
                  <p className="text-sm text-muted-foreground">
                    جمع بدهکار: {formatMoney(report.total_debit)} · جمع بستانکار:{" "}
                    {formatMoney(report.total_credit)} · {fa(report.accounts.length)} حساب دارای فعالیت
                  </p>
                  <DataTable
                    columns={summaryColumns}
                    rows={report.accounts}
                    rowKey={(row) => row.account_id}
                    emptyText="در این دوره سند ثبت‌شده‌ای نیست."
                    onRowClick={(row) => {
                      setAccountId(row.account_id);
                    }}
                    caption="برای دیدن سندهای هر حساب، روی ردیف آن کلیک کنید."
                  />
                </>
              )}
            </CardContent>
          </Card>
          <ReportFreshnessLine freshness={report.freshness} sourceLabel="ترایتون (مرجع)" />
        </>
      )}
    </div>
  );
}
