/**
 * Live financial-statement sections (US-550): trial balance, balance
 * sheet, income statement and cash flow, each wired to its
 * /reports/* backend. Every section states its source and §7.5 chart
 * type, carries the §11.1 freshness line, and exports CSV with the
 * applied filters in the header (AC 7). All figures are read live
 * from Tryton through the backend — never recomputed here.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import { ApiError, api, type ReportFreshnessResponse } from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
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
import { csvFilename, downloadCsv } from "@/features/shared/csv";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import {
  cashFlowSectionLabel,
  currentJalaliYearStartIso,
  monthlyChartItems,
  statementSectionLabel,
  waterfallSteps,
} from "./financial-report-data";
import { GroupedBarChart, WaterfallChart } from "./report-charts";
import {
  balanceSheetToCsv,
  cashFlowToCsv,
  incomeStatementToCsv,
  trialBalanceToCsv,
  type CsvFilters,
} from "./report-csv";

export interface DrillDownTarget {
  accountId: number;
  accountName: string;
  dateFrom: string;
  dateTo: string;
}

interface SectionProps {
  companyId: string | null;
  companyName: string;
}

/** §11.1 freshness line: source, generated-at, versions, live indicator. */
export function ReportFreshnessLine({
  freshness,
  sourceLabel,
}: {
  freshness: ReportFreshnessResponse;
  sourceLabel: string;
}) {
  return (
    <p className="text-xs text-muted-foreground">
      منبع: {sourceLabel} · زمان تولید: {formatIsoDateTime(freshness.generated_at)} · نسخهٔ گزارش:{" "}
      {fa(freshness.report_version)} · نسخهٔ محاسبه: {fa(freshness.calculation_version)} ·{" "}
      {freshness.freshness_indicator === "live" ? "دادهٔ زنده" : fa(freshness.freshness_indicator)}
    </p>
  );
}

function SectionError({ error, onRetry, message }: { error: unknown; onRetry: () => void; message: string }) {
  if (isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, message)} />;
  }
  return <ErrorState message={persianApiErrorMessage(error, message)} onRetry={onRetry} />;
}

function SectionSkeleton() {
  return (
    <div className="space-y-2" aria-label="در حال بارگذاری">
      <Skeleton className="h-8 w-56" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

function PeriodPickers({
  idPrefix,
  dateFrom,
  dateTo,
  onFromChange,
  onToChange,
}: {
  idPrefix: string;
  dateFrom: string | null;
  dateTo: string | null;
  onFromChange: (iso: string | null) => void;
  onToChange: (iso: string | null) => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-4 print:hidden">
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-from`}>از تاریخ</Label>
        <JalaliDatePicker id={`${idPrefix}-from`} value={dateFrom} onChange={onFromChange} />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-to`}>تا تاریخ</Label>
        <JalaliDatePicker id={`${idPrefix}-to`} value={dateTo} onChange={onToChange} />
      </div>
    </div>
  );
}

function ExportButton({ onExport, disabled }: { onExport: () => void; disabled?: boolean }) {
  return (
    <Button variant="outline" size="sm" onClick={onExport} disabled={disabled} className="print:hidden">
      <Download className="size-4" aria-hidden />
      خروجی CSV
    </Button>
  );
}

function csvFilters(companyName: string, period: string, extra: CsvFilters = []): CsvFilters {
  return [
    ["شرکت", companyName],
    ["دوره", period],
    ...extra,
  ];
}

/** Trial balance (US-403 backend, US-550 UI) with drill-down to the ledger. */
export function TrialBalanceSection({
  companyId,
  companyName,
  onDrillDown,
}: SectionProps & { onDrillDown: (target: DrillDownTarget) => void }) {
  const router = useRouter();
  const [dateFrom, setDateFrom] = React.useState<string | null>(() => currentJalaliYearStartIso());
  const [dateTo, setDateTo] = React.useState<string | null>(() => todayIso());
  const [report, setReport] = React.useState<Awaited<ReturnType<typeof api.getTrialBalance>> | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    if (!companyId || !dateFrom || !dateTo) return;
    let cancelled = false;
    api
      .getTrialBalance({ company: companyId, date_from: dateFrom, date_to: dateTo })
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
  }, [companyId, dateFrom, dateTo, reloadKey, router]);

  if (error) {
    return (
      <SectionError
        error={error}
        message="دریافت تراز آزمایشی ناموفق بود."
        onRetry={() => {
          setError(null);
          setReport(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  const period = dateFrom && dateTo ? `${dateFrom} تا ${dateTo}` : "";
  const columns: DataTableColumn<Awaited<ReturnType<typeof api.getTrialBalance>>["lines"][number]>[] = [
    { key: "code", header: "کد حساب", render: (row) => <span className="tabular-nums">{fa(row.code)}</span> },
    { key: "name", header: "نام حساب", render: (row) => row.name },
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
      key: "balance",
      header: "مانده",
      render: (row) => <span className="tabular-nums">{formatMoney(row.balance)}</span>,
      className: "text-end",
    },
  ];

  return (
    <div className="space-y-6" data-testid="report-trial-balance">
      <PeriodPickers
        idPrefix="tb"
        dateFrom={dateFrom}
        dateTo={dateTo}
        onFromChange={(iso) => {
          setDateFrom(iso);
          setReport(null);
        }}
        onToChange={(iso) => {
          setDateTo(iso);
          setReport(null);
        }}
      />
      {report === null ? (
        <SectionSkeleton />
      ) : (
        <>
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <CardTitle>تراز آزمایشی</CardTitle>
                <CardDescription>
                  دوره: {formatIsoJalali(report.date_from)} تا {formatIsoJalali(report.date_to)} · منبع: ترایتون
                  (مرجع) · نمودار: جدول · فقط سندهای ثبت‌شده
                </CardDescription>
              </div>
              <ExportButton
                disabled={report.lines.length === 0}
                onExport={() =>
                  downloadCsv(
                    csvFilename("report-trial-balance", todayIso()),
                    trialBalanceToCsv(report, csvFilters(companyName, period)),
                  )
                }
              />
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                جمع بدهکار: {formatMoney(report.total_debit)} · جمع بستانکار: {formatMoney(report.total_credit)} ·{" "}
                {report.balanced ? "تراز متوازن است" : "تراز نامتوازن است"} · مبتنی بر {fa(report.move_count)} سند
                ثبت‌شده و {fa(report.line_count)} ردیف
              </p>
              <DataTable
                columns={columns}
                rows={report.lines}
                rowKey={(row) => row.account_id}
                emptyText="در این دوره سند ثبت‌شده‌ای نیست."
                onRowClick={(row) =>
                  onDrillDown({
                    accountId: row.account_id,
                    accountName: row.name,
                    dateFrom: report.date_from,
                    dateTo: report.date_to,
                  })
                }
                caption="برای دیدن دفتر کل هر حساب، روی ردیف آن کلیک کنید."
              />
            </CardContent>
          </Card>
          <ReportFreshnessLine freshness={report.freshness} sourceLabel="ترایتون (مرجع)" />
        </>
      )}
    </div>
  );
}

interface StatementRow {
  key: string;
  label: string;
  code: string | null;
  depth: number;
  amount: number;
  amountCmp: number | null;
  isTotal: boolean;
}

function sectionRows(
  section: { key: string; lines: { kind: string; ref_id: number; label: string; code: string | null; depth: number; amount: number; amount_cmp: number | null }[]; total: number; total_cmp: number | null },
): StatementRow[] {
  const rows: StatementRow[] = section.lines.map((line) => ({
    key: `${line.kind}-${line.ref_id}`,
    label: line.label,
    code: line.code,
    depth: line.depth,
    amount: line.amount,
    amountCmp: line.amount_cmp,
    isTotal: false,
  }));
  rows.push({
    key: `${section.key}-total`,
    label: "جمع بخش",
    code: null,
    depth: 0,
    amount: section.total,
    amountCmp: section.total_cmp,
    isTotal: true,
  });
  return rows;
}

function StatementTable({
  sectionKey,
  title,
  rows,
  comparisonLabel,
}: {
  sectionKey: string;
  title: string;
  rows: StatementRow[];
  comparisonLabel: string | null;
}) {
  const columns: DataTableColumn<StatementRow>[] = [
    {
      key: "label",
      header: "ردیف",
      render: (row) => (
        <span
          className={row.isTotal ? "font-medium" : undefined}
          style={{ paddingInlineStart: `${row.depth * 16}px` }}
        >
          {row.code ? <span className="text-muted-foreground tabular-nums">{fa(row.code)} </span> : null}
          {row.label}
        </span>
      ),
    },
    {
      key: "amount",
      header: "مبلغ",
      render: (row) => (
        <span className={`tabular-nums ${row.isTotal ? "font-medium" : ""}`}>{formatMoney(row.amount)}</span>
      ),
      className: "text-end",
    },
  ];
  if (comparisonLabel) {
    columns.push({
      key: "comparison",
      header: comparisonLabel,
      render: (row) => (
        <span className={`tabular-nums ${row.isTotal ? "font-medium" : ""}`}>
          {row.amountCmp === null ? "—" : formatMoney(row.amountCmp)}
        </span>
      ),
      className: "text-end",
    });
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(row) => `${sectionKey}-${row.key}`}
          emptyText="ردیفی نیست."
        />
      </CardContent>
    </Card>
  );
}

/** Balance sheet with prior-period comparison. */
export function BalanceSheetSection({ companyId, companyName }: SectionProps) {
  const router = useRouter();
  const [asOf, setAsOf] = React.useState<string | null>(() => todayIso());
  const [report, setReport] = React.useState<Awaited<ReturnType<typeof api.getBalanceSheet>> | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    if (!companyId || !asOf) return;
    let cancelled = false;
    api
      .getBalanceSheet({ company: companyId, as_of: asOf })
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
  }, [companyId, asOf, reloadKey, router]);

  if (error) {
    return (
      <SectionError
        error={error}
        message="دریافت ترازنامه ناموفق بود."
        onRetry={() => {
          setError(null);
          setReport(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  const comparisonLabel =
    report?.comparison_as_of != null ? `مقایسه (${formatIsoJalali(report.comparison_as_of)})` : null;

  return (
    <div className="space-y-6" data-testid="report-balance-sheet">
      <div className="flex flex-wrap items-end gap-4 print:hidden">
        <div className="space-y-1.5">
          <Label htmlFor="bs-as-of">تا تاریخ</Label>
          <JalaliDatePicker
            id="bs-as-of"
            value={asOf}
            onChange={(iso) => {
              setAsOf(iso);
              setReport(null);
            }}
          />
        </div>
      </div>
      {report === null ? (
        <SectionSkeleton />
      ) : (
        <>
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <CardTitle>ترازنامه</CardTitle>
                <CardDescription>
                  در تاریخ {formatIsoJalali(report.as_of)} · مقایسه با{" "}
                  {report.comparison_as_of ? formatIsoJalali(report.comparison_as_of) : "—"} · منبع: ترایتون
                  (مرجع) · نمودار: جدول
                </CardDescription>
              </div>
              <ExportButton
                onExport={() =>
                  downloadCsv(
                    csvFilename("report-balance-sheet", todayIso()),
                    balanceSheetToCsv(
                      report,
                      statementSectionLabel,
                      csvFilters(companyName, report.as_of),
                    ),
                  )
                }
              />
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                جمع دارایی‌ها: {formatMoney(report.total_assets)} · جمع بدهی‌ها:{" "}
                {formatMoney(report.total_liabilities)} · جمع حقوق صاحبان سهام:{" "}
                {formatMoney(report.total_equity)} · {report.balanced ? "ترازنامه متوازن است" : "ترازنامه نامتوازن است"}
              </p>
              {!report.balanced ? (
                <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="note">
                  جمع دارایی‌ها با جمع بدهی‌ها و حقوق صاحبان سهام برابر نیست؛ سندهای ثبت‌شده را بررسی کنید.
                </p>
              ) : null}
            </CardContent>
          </Card>
          {report.sections.map((section) => (
            <StatementTable
              key={section.key}
              sectionKey={section.key}
              title={statementSectionLabel(section.key, section.title)}
              rows={sectionRows(section)}
              comparisonLabel={comparisonLabel}
            />
          ))}
          <ReportFreshnessLine freshness={report.freshness} sourceLabel="ترایتون (مرجع)" />
        </>
      )}
    </div>
  );
}

/** Income statement (P&L) with prior-period comparison + monthly bar chart. */
export function IncomeStatementSection({ companyId, companyName }: SectionProps) {
  const router = useRouter();
  const [dateFrom, setDateFrom] = React.useState<string | null>(() => currentJalaliYearStartIso());
  const [dateTo, setDateTo] = React.useState<string | null>(() => todayIso());
  const [report, setReport] = React.useState<Awaited<ReturnType<typeof api.getIncomeStatement>> | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    if (!companyId || !dateFrom || !dateTo) return;
    let cancelled = false;
    api
      .getIncomeStatement({ company: companyId, date_from: dateFrom, date_to: dateTo })
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
  }, [companyId, dateFrom, dateTo, reloadKey, router]);

  if (error) {
    return (
      <SectionError
        error={error}
        message="دریافت صورت سود و زیان ناموفق بود."
        onRetry={() => {
          setError(null);
          setReport(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  const period = dateFrom && dateTo ? `${dateFrom} تا ${dateTo}` : "";
  const comparisonLabel =
    report?.comparison_from != null && report.comparison_to != null
      ? `مقایسه (${formatIsoJalali(report.comparison_from)} تا ${formatIsoJalali(report.comparison_to)})`
      : null;

  return (
    <div className="space-y-6" data-testid="report-income-statement">
      <PeriodPickers
        idPrefix="pl"
        dateFrom={dateFrom}
        dateTo={dateTo}
        onFromChange={(iso) => {
          setDateFrom(iso);
          setReport(null);
        }}
        onToChange={(iso) => {
          setDateTo(iso);
          setReport(null);
        }}
      />
      {report === null ? (
        <SectionSkeleton />
      ) : (
        <>
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <CardTitle>صورت سود و زیان</CardTitle>
                <CardDescription>
                  دوره: {formatIsoJalali(report.date_from)} تا {formatIsoJalali(report.date_to)} · منبع: ترایتون
                  (مرجع) · نمودار: میله‌ای ماهانهٔ درآمد و هزینه
                </CardDescription>
              </div>
              <ExportButton
                onExport={() =>
                  downloadCsv(
                    csvFilename("report-income-statement", todayIso()),
                    incomeStatementToCsv(report, statementSectionLabel, csvFilters(companyName, period)),
                  )
                }
              />
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                جمع درآمد: {formatMoney(report.total_revenue)} · جمع هزینه: {formatMoney(report.total_expense)} ·{" "}
                سود خالص: {formatMoney(report.net_income)}
                {report.net_income_cmp !== null ? ` · سود خالص دورهٔ مقایسه: ${formatMoney(report.net_income_cmp)}` : ""}
              </p>
              {!report.monthly_ties_out ? (
                <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="note">
                  جمع ماهانه با جمع صورت هم‌خوانی ندارد؛ این نباید رخ دهد — لطفاً به پشتیبانی اطلاع دهید.
                </p>
              ) : null}
              <GroupedBarChart
                items={monthlyChartItems(report.monthly)}
                revenueLabel="درآمد"
                expenseLabel="هزینه"
              />
              <DataTable
                columns={[
                  { key: "month", header: "ماه", render: (row) => row.label },
                  {
                    key: "revenue",
                    header: "درآمد",
                    render: (row) => <span className="tabular-nums">{formatMoney(row.revenue)}</span>,
                    className: "text-end",
                  },
                  {
                    key: "expense",
                    header: "هزینه",
                    render: (row) => <span className="tabular-nums">{formatMoney(row.expense)}</span>,
                    className: "text-end",
                  },
                ]}
                rows={monthlyChartItems(report.monthly)}
                rowKey={(row) => row.label}
                emptyText="در این دوره درآمد یا هزینه‌ای نیست."
              />
            </CardContent>
          </Card>
          {report.sections.map((section) => (
            <StatementTable
              key={section.key}
              sectionKey={section.key}
              title={statementSectionLabel(section.key, section.title)}
              rows={sectionRows(section)}
              comparisonLabel={comparisonLabel}
            />
          ))}
          <ReportFreshnessLine freshness={report.freshness} sourceLabel="ترایتون (مرجع)" />
        </>
      )}
    </div>
  );
}

/** Cash-flow statement: three sections + waterfall chart. */
export function CashFlowSection({ companyId, companyName }: SectionProps) {
  const router = useRouter();
  const [dateFrom, setDateFrom] = React.useState<string | null>(() => currentJalaliYearStartIso());
  const [dateTo, setDateTo] = React.useState<string | null>(() => todayIso());
  const [report, setReport] = React.useState<Awaited<ReturnType<typeof api.getCashFlow>> | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    if (!companyId || !dateFrom || !dateTo) return;
    let cancelled = false;
    api
      .getCashFlow({ company: companyId, date_from: dateFrom, date_to: dateTo })
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
  }, [companyId, dateFrom, dateTo, reloadKey, router]);

  if (error) {
    return (
      <SectionError
        error={error}
        message="دریافت صورت جریان وجوه نقد ناموفق بود."
        onRetry={() => {
          setError(null);
          setReport(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  const period = dateFrom && dateTo ? `${dateFrom} تا ${dateTo}` : "";

  return (
    <div className="space-y-6" data-testid="report-cash-flow">
      <PeriodPickers
        idPrefix="cf"
        dateFrom={dateFrom}
        dateTo={dateTo}
        onFromChange={(iso) => {
          setDateFrom(iso);
          setReport(null);
        }}
        onToChange={(iso) => {
          setDateTo(iso);
          setReport(null);
        }}
      />
      {report === null ? (
        <SectionSkeleton />
      ) : (
        <>
          <Card>
            <CardHeader className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <CardTitle>صورت جریان وجوه نقد</CardTitle>
                <CardDescription>
                  دوره: {formatIsoJalali(report.date_from)} تا {formatIsoJalali(report.date_to)} · منبع: ترایتون
                  (مرجع) — طبقه‌بندی مستقیم حساب‌یار · نمودار: آبشاری
                </CardDescription>
              </div>
              <ExportButton
                onExport={() =>
                  downloadCsv(
                    csvFilename("report-cash-flow", todayIso()),
                    cashFlowToCsv(report, csvFilters(companyName, period)),
                  )
                }
              />
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                ماندهٔ ابتدای دوره: {formatMoney(report.opening_cash)} · تغییر خالص:{" "}
                {formatMoney(report.net_change)} · ماندهٔ پایان دوره: {formatMoney(report.closing_cash)} ·{" "}
                {report.ties_out ? "صورت با ماندهٔ نقد هم‌خوانی دارد" : "صورت با ماندهٔ نقد هم‌خوانی ندارد"}
              </p>
              {report.unclassified_count > 0 ? (
                <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="note">
                  {fa(report.unclassified_count)} سند نقدی ({formatMoney(report.unclassified_total)}) قابل
                  طبقه‌بندی نبود و در هیچ بخشی نیامده است؛ طرف مقابل آن‌ها را بررسی کنید.
                </p>
              ) : null}
              {report.transfer_count > 0 ? (
                <p className="text-sm text-muted-foreground">
                  {fa(report.transfer_count)} انتقال بین حساب‌های نقدی از بخش‌ها کنار گذاشته شده است (جابه‌جایی
                  داخلی نقد نیست).
                </p>
              ) : null}
              <WaterfallChart steps={waterfallSteps(report)} />
              <DataTable
                columns={[
                  {
                    key: "section",
                    header: "بخش",
                    render: (row) => row.label,
                  },
                  {
                    key: "inflow",
                    header: "ورودی",
                    render: (row) => <span className="tabular-nums">{formatMoney(row.inflow)}</span>,
                    className: "text-end",
                  },
                  {
                    key: "outflow",
                    header: "خروجی",
                    render: (row) => <span className="tabular-nums">{formatMoney(row.outflow)}</span>,
                    className: "text-end",
                  },
                  {
                    key: "net",
                    header: "خالص",
                    render: (row) => <span className="tabular-nums">{formatMoney(row.net)}</span>,
                    className: "text-end",
                  },
                  {
                    key: "count",
                    header: "تعداد سند",
                    render: (row) => fa(row.line_count),
                    className: "text-end",
                  },
                ]}
                rows={report.sections.map((s) => ({
                  key: s.key,
                  label: cashFlowSectionLabel(s.key, s.title),
                  inflow: s.inflow,
                  outflow: s.outflow,
                  net: s.net,
                  line_count: s.line_count,
                }))}
                rowKey={(row) => row.key}
                emptyText="در این دوره حرکت نقدی نیست."
                caption={`حساب‌های نقدی خوانده‌شده: ${report.cash_accounts.map((c) => fa(c)).join("، ") || "—"}`}
              />
            </CardContent>
          </Card>
          <ReportFreshnessLine freshness={report.freshness} sourceLabel="ترایتون (مرجع) — طبقه‌بندی مستقیم حساب‌یار" />
        </>
      )}
    </div>
  );
}
