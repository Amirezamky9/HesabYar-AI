/**
 * Installment reports (US-545; §7.5 — all 11 reports).
 *
 * Every report wires to its real backend endpoint or derives from
 * live plan data; nothing is invented. All 11 are live — none of
 * the sources is missing. The per-figure gaps are stated on the
 * report itself, never filled with a guess:
 *
 * - report 3: the monthly income trend line has no endpoint (the
 *   stacked realised/unrealised bar + per-plan table are live);
 * - report 8: waived/saved income per settlement is not recorded
 *   (the quote at settlement time is not persisted);
 * - report 10: SMS cost is summed only where the provider reported
 *   a known unit.
 *
 * Sources per report:
 *  1 — GET /installments/overdue
 *  2 — derived: open internal legs by due month (plan details)
 *  3 — derived: live payoff quotes of the internal plans
 *  4 — derived: dunning stages of the overdue rows (report 1)
 *  5 — derived: plans list split by kind
 *  6 — derived: plans list grouped by customer
 *  7 — GET /installments/plans/{id} (+ the plan's contract rate)
 *  8 — derived: early-settlement receipts across the plan details
 *  9 — derived: cheque-linked legs across the plan details
 * 10 — GET /integrations/sms/reminders (needs sms:manage)
 * 11 — derived: all outstanding internal legs, aged by due date
 *
 * Plan details are fetched lazily, only when a derived report is
 * selected, and cover the first 100 plans; the page says so when
 * the installation has more.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import {
  ApiError,
  api,
  type DunningRunResponse,
  type InstallmentContractResponse,
  type InstallmentPlanDetailResponse,
  type InstallmentPlanResponse,
  type OverdueReportResponse,
  type PayoffQuoteResponse,
  type ReminderLogResponse,
} from "@/lib/api-client";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  DataTable,
  Label,
  MoneyDisplay,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  type DataTableColumn,
} from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { formatMoney } from "@/lib/format-money";
import { formatIsoDateTime } from "@/lib/format-date";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { buildCsv, csvFilename, downloadCsv } from "@/features/shared/csv";
import { todayIso } from "@/lib/jalali";
import { formatJalaliShort, todayJalaliString } from "../cheques/cheque-data";
import { BarChart, type ChartItem } from "../cheques/cheque-report-charts";
import {
  AGING_BUCKET_LABELS,
  INSTALLMENT_REPORTS,
  OVERDUE_AGE_BUCKET_LABELS,
  aggregateFinanceIncome,
  ageOutstandingLegs,
  bucketOverdueRows,
  collectionForecast,
  dunningStageLabel,
  extractBouncedChequeLegs,
  extractChequeLegs,
  extractEarlySettlements,
  groupOverdueByStage,
  groupPlansByCustomer,
  groupRemindersByMonth,
  jalaliMonthLabel,
  legFinanceIncomeIrr,
  planKindLabel,
  reminderStatusLabel,
  reminderSuccessRate,
  splitPlansByKind,
  type ChequeLegRow,
  type DunningStageTotal,
  type EarlySettlementRow,
  type InstallmentReportMeta,
  type OverdueBucketTotal,
} from "./installment-data";
import { PlanKindBadge, PlanStatusBadge, legTableColumns } from "./installment-shared";

const PLANS_LIMIT = 100;

function ReportHeader({ meta }: { meta: InstallmentReportMeta }) {
  return (
    <div>
      <h2 className="text-xl font-bold">
        گزارش {faNumber(meta.number)} — {meta.title}
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        منبع: {meta.source} · نمودار: {meta.chart}
        {meta.section75 !== null ? ` · §۷٫۵ گزارش ${faNumber(meta.section75)}` : " · بدون ردیف مستقیم در §۷٫۵"}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">فیلترها: {meta.filters}</p>
      {meta.note ? (
        <p className="mt-2 rounded-lg border px-4 py-3 text-sm text-muted-foreground" role="note">
          {meta.note}
        </p>
      ) : null}
    </div>
  );
}

function ExportButton({ onExport, disabled }: { onExport: () => void; disabled?: boolean }) {
  return (
    <Button variant="outline" size="sm" onClick={onExport} disabled={disabled}>
      <Download className="size-4" aria-hidden />
      خروجی CSV
    </Button>
  );
}

function moneyChartItems(items: { label: string; value: number }[]): ChartItem[] {
  return items.map((item) => ({ label: item.label, value: item.value, display: formatMoney(item.value) }));
}

type LazyState<T> =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "ready"; value: T }
  | { state: "error"; error: unknown };

const DETAIL_REPORTS = new Set(["forecast", "early-settlements", "bounced-cheques", "aging"]);

export function InstallmentReportsPage() {
  const router = useRouter();
  // Deep links (?report=<id>) from the center page — read once in
  // the lazy initial state, not in an effect.
  const [selected, setSelected] = React.useState<string>(() => {
    const id = new URLSearchParams(window.location.search).get("report");
    return id && INSTALLMENT_REPORTS.some((r) => r.id === id) ? id : "overdue";
  });
  const [plans, setPlans] = React.useState<InstallmentPlanResponse[]>([]);
  const [plansTruncated, setPlansTruncated] = React.useState(false);
  const [overdue, setOverdue] = React.useState<OverdueReportResponse | null>(null);
  const [details, setDetails] = React.useState<LazyState<InstallmentPlanDetailResponse[]>>({ state: "idle" });
  const [quotes, setQuotes] = React.useState<LazyState<PayoffQuoteResponse[]>>({ state: "idle" });
  const [smsLog, setSmsLog] = React.useState<LazyState<ReminderLogResponse[]>>({ state: "idle" });
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [dunningResult, setDunningResult] = React.useState<DunningRunResponse | null>(null);
  const [dunningRunning, setDunningRunning] = React.useState(false);
  const [dunningError, setDunningError] = React.useState<unknown>(null);
  const [schedulePlanId, setSchedulePlanId] = React.useState("");
  const [scheduleDetail, setScheduleDetail] = React.useState<InstallmentPlanDetailResponse | null>(null);
  const [scheduleContract, setScheduleContract] = React.useState<InstallmentContractResponse | null>(null);
  const [scheduleLoading, setScheduleLoading] = React.useState(false);
  const todayJalali = React.useMemo(() => todayJalaliString(), []);
  const generatedAt = React.useMemo(() => formatIsoDateTime(new Date().toISOString()), []);

  const meta = INSTALLMENT_REPORTS.find((r) => r.id === selected) ?? INSTALLMENT_REPORTS[0];

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.listInstallmentPlans({ limit: PLANS_LIMIT }),
      api.listOverdueLegs({ limit: 100 }),
    ])
      .then(([planRows, overdueReport]) => {
        if (cancelled) return;
        setPlans(planRows);
        setPlansTruncated(planRows.length === PLANS_LIMIT);
        setOverdue(overdueReport);
        setError(null);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  React.useEffect(() => {
    let cleanup: (() => void) | undefined;
    const timer = setTimeout(() => {
      cleanup = load();
    }, 0);
    return () => {
      clearTimeout(timer);
      cleanup?.();
    };
  }, [load]);

  // Lazy: plan details for the derived reports. The loading state
  // is set from a timer callback, never synchronously in the effect
  // body (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    if (!DETAIL_REPORTS.has(selected)) return;
    if (details.state !== "idle" || loading) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      // No plans at all: resolve empty instead of spinning forever.
      if (plans.length === 0) {
        setDetails({ state: "ready", value: [] });
        return;
      }
      setDetails({ state: "loading" });
      Promise.all(
        plans.map((p) => api.getInstallmentPlan(p.id).catch(() => null)),
      ).then((rows) => {
        if (cancelled) return;
        setDetails({ state: "ready", value: rows.filter((r): r is InstallmentPlanDetailResponse => r !== null) });
      });
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [selected, plans, details.state, loading]);

  // Lazy: payoff quotes for the finance-income report (internal plans only).
  React.useEffect(() => {
    if (selected !== "finance-income") return;
    if (quotes.state !== "idle" || loading) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      const openInternal = plans.filter((p) => p.kind === "internal" && p.status !== "paid");
      if (openInternal.length === 0) {
        setQuotes({ state: "ready", value: [] });
        return;
      }
      setQuotes({ state: "loading" });
      Promise.all(
        openInternal.map((p) => api.getPayoffQuote(p.id).catch(() => null)),
      ).then((rows) => {
        if (cancelled) return;
        setQuotes({ state: "ready", value: rows.filter((r): r is PayoffQuoteResponse => r !== null) });
      });
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [selected, plans, quotes.state, loading]);

  // Lazy: the installation-wide SMS log (needs sms:manage).
  React.useEffect(() => {
    if (selected !== "sms-log") return;
    if (smsLog.state !== "idle") return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setSmsLog({ state: "loading" });
      api
        .listSmsReminders({ limit: 100 })
        .then((rows) => {
          if (!cancelled) setSmsLog({ state: "ready", value: rows });
        })
        .catch((err: unknown) => {
          if (!cancelled) setSmsLog({ state: "error", error: err });
        });
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [selected, smsLog.state]);

  // Plan-schedule report: one plan's detail + contract.
  React.useEffect(() => {
    if (selected !== "plan-schedule" || !schedulePlanId) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setScheduleLoading(true);
      setScheduleDetail(null);
      setScheduleContract(null);
      api
        .getInstallmentPlan(schedulePlanId)
        .then(async (fetched) => {
          if (cancelled) return;
          setScheduleDetail(fetched);
          if (fetched.plan.contract_id) {
            const c = await api.getInstallmentContract(fetched.plan.contract_id).catch(() => null);
            if (!cancelled) setScheduleContract(c);
          }
          if (!cancelled) setScheduleLoading(false);
        })
        .catch(() => {
          if (!cancelled) setScheduleLoading(false);
        });
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [selected, schedulePlanId]);

  const runDunning = async () => {
    setDunningRunning(true);
    setDunningError(null);
    setDunningResult(null);
    try {
      const result = await api.runDunning({});
      setDunningResult(result);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setDunningError(err);
    } finally {
      setDunningRunning(false);
    }
  };

  const overdueBuckets = React.useMemo(
    () => (overdue ? bucketOverdueRows(overdue.rows) : []),
    [overdue],
  );
  const dunningStages = React.useMemo(
    () => (overdue ? groupOverdueByStage(overdue.rows) : []),
    [overdue],
  );
  const kindSplit = React.useMemo(() => splitPlansByKind(plans), [plans]);
  const customerStatements = React.useMemo(() => groupPlansByCustomer(plans), [plans]);
  const forecast = React.useMemo(
    () => (details.state === "ready" ? collectionForecast(details.value, todayJalali) : []),
    [details, todayJalali],
  );
  const aging = React.useMemo(
    () => (details.state === "ready" ? ageOutstandingLegs(details.value, todayJalali) : []),
    [details, todayJalali],
  );
  const settlements = React.useMemo(
    () => (details.state === "ready" ? extractEarlySettlements(details.value) : []),
    [details],
  );
  const chequeLegs = React.useMemo(
    () => (details.state === "ready" ? extractChequeLegs(details.value) : []),
    [details],
  );
  const bouncedLegs = React.useMemo(
    () => (details.state === "ready" ? extractBouncedChequeLegs(details.value) : []),
    [details],
  );
  const financeTotals = React.useMemo(
    () => (quotes.state === "ready" ? aggregateFinanceIncome(quotes.value) : null),
    [quotes],
  );
  const smsMonths = React.useMemo(
    () => (smsLog.state === "ready" ? groupRemindersByMonth(smsLog.value) : []),
    [smsLog],
  );
  const smsRate = React.useMemo(
    () => (smsLog.state === "ready" ? reminderSuccessRate(smsLog.value) : null),
    [smsLog],
  );

  const exportOverdueCsv = () => {
    if (!overdue) return;
    downloadCsv(
      csvFilename("installments-overdue", todayIso()),
      buildCsv(
        [
          { header: "شناسهٔ برنامه", value: (r) => r.plan_id },
          { header: "فاکتور", value: (r) => r.invoice_id },
          { header: "مرجع مشتری", value: (r) => r.customer_ref ?? "" },
          { header: "کد قرارداد", value: (r) => r.contract_code ?? "" },
          { header: "شمارهٔ قسط", value: (r) => r.sequence },
          { header: "سررسید (شمسی)", value: (r) => r.due_date },
          { header: "روزهای تأخیر", value: (r) => r.days_overdue },
          { header: "اصل (ریال)", value: (r) => r.principal_irr },
          { header: "دریافت‌شده (ریال)", value: (r) => r.received_irr },
          { header: "مانده (ریال)", value: (r) => r.balance_irr },
          { header: "مرحلهٔ پیگیری", value: (r) => r.dunning_stage },
          { header: "یادآوری بعدی (شمسی)", value: (r) => r.next_reminder_date ?? "" },
          { header: "جریمهٔ انباشته (ریال)", value: (r) => r.late_fee_accrued_irr },
        ],
        overdue.rows,
      ),
    );
  };

  const exportSmsCsv = () => {
    if (smsLog.state !== "ready") return;
    downloadCsv(
      csvFilename("installments-sms-log", todayIso()),
      buildCsv(
        [
          { header: "شناسهٔ برنامه", value: (r) => r.plan_id },
          { header: "شناسهٔ قسط", value: (r) => r.leg_id },
          { header: "ارائه‌دهنده", value: (r) => r.provider_key },
          { header: "گیرنده", value: (r) => r.recipient_phone },
          { header: "فاصله از سررسید (روز)", value: (r) => r.offset_days },
          { header: "محرک", value: (r) => r.trigger },
          { header: "وضعیت", value: (r) => r.status },
          { header: "هزینه (تومان)", value: (r) => r.cost_toman ?? "" },
          { header: "ایجاد", value: (r) => r.created_at ?? "" },
        ],
        smsLog.value,
      ),
    );
  };

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن گزارش‌های اقساط را ندارد." />;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">گزارش‌های اقساط</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            ۱۱ گزارش §۷٫۵ — هر گزارش منبع و نوع نمودارش را می‌گوید؛ عددی که منبع ندارد ساخته نمی‌شود.
          </p>
        </div>
        <Link
          href="/installments"
          className="inline-flex min-h-11 items-center rounded-md border px-4 text-sm font-medium"
        >
          بازگشت به مرکز اقساط
        </Link>
      </div>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="space-y-2">
            <Label htmlFor="installment-report-select">گزارش</Label>
            <Select
              value={selected}
              onValueChange={(value) => {
                if (value) setSelected(value);
              }}
            >
              <SelectTrigger id="installment-report-select">
                <SelectValue>
                  گزارش {faNumber(meta.number)} — {meta.title}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {INSTALLMENT_REPORTS.map((report) => (
                  <SelectItem key={report.id} value={report.id}>
                    گزارش {faNumber(report.number)} — {report.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-wrap gap-2" role="list" aria-label="فهرست گزارش‌ها">
            {INSTALLMENT_REPORTS.map((report) => (
              <Button
                key={report.id}
                variant={selected === report.id ? "default" : "outline"}
                size="sm"
                onClick={() => setSelected(report.id)}
                aria-current={selected === report.id ? "true" : undefined}
              >
                {faNumber(report.number)}. {report.title}
              </Button>
            ))}
          </div>
        </CardContent>
      </Card>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "دریافت دادهٔ گزارش‌ها ناموفق بود.")}
          onRetry={load}
        />
      ) : loading ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : (
        <Card>
          <CardHeader>
            <ReportHeader meta={meta} />
          </CardHeader>
          <CardContent className="space-y-6">
            <p className="text-xs text-muted-foreground">
              زمان تولید: {generatedAt} · واحد: تومان (مبالغ به ریال ذخیره می‌شوند)
              {plansTruncated ? ` · گزارش‌های تجمیعی روی ${faNumber(PLANS_LIMIT)} برنامهٔ اول محاسبه شده‌اند؛ نصب بیش از ${faNumber(PLANS_LIMIT)} برنامه دارد.` : ""}
            </p>

            {selected === "overdue" && overdue ? <OverdueReportSection
              overdue={overdue}
              buckets={overdueBuckets}
              onExportCsv={exportOverdueCsv}
              onRunDunning={runDunning}
              dunningRunning={dunningRunning}
              dunningResult={dunningResult}
              dunningError={dunningError}
            /> : null}

            {selected === "forecast" ? (
              <LazySection state={details} label="پیش‌بینی وصول">
                <BarChart items={moneyChartItems(forecast.map((b) => ({ label: `${b.label} (${faNumber(b.count)} قسط)`, value: b.amountIrr })))} />
                <DataTable
                  columns={[
                    { key: "bucket", header: "بازه", render: (row) => row.label },
                    { key: "count", header: "تعداد اقساط", render: (row) => faNumber(row.count) },
                    { key: "amount", header: "مبلغ موردانتظار", render: (row) => <MoneyDisplay amountIrr={row.amountIrr} size="sm" /> },
                  ]}
                  rows={forecast}
                  rowKey={(row) => row.key}
                  emptyText="قسط بازی برای پیش‌بینی نیست."
                />
              </LazySection>
            ) : null}

            {selected === "finance-income" ? (
              <LazySection state={quotes} label="سود تأمین مالی">
                {financeTotals ? (
                  <>
                    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                      <Card>
                        <CardContent className="pt-6">
                          <p className="text-sm text-muted-foreground">تحقق‌یافته</p>
                          <p className="mt-1"><MoneyDisplay amountIrr={financeTotals.realisedIrr} size="md" /></p>
                        </CardContent>
                      </Card>
                      <Card>
                        <CardContent className="pt-6">
                          <p className="text-sm text-muted-foreground">تحقق‌یافتهٔ پرداخت‌نشده</p>
                          <p className="mt-1"><MoneyDisplay amountIrr={financeTotals.realisedUnpaidIrr} size="md" /></p>
                        </CardContent>
                      </Card>
                      <Card>
                        <CardContent className="pt-6">
                          <p className="text-sm text-muted-foreground">تحقق‌نیافته</p>
                          <p className="mt-1"><MoneyDisplay amountIrr={financeTotals.unrealisedIrr} size="md" /></p>
                        </CardContent>
                      </Card>
                      <Card>
                        <CardContent className="pt-6">
                          <p className="text-sm text-muted-foreground">برنامه‌های داخلی باز</p>
                          <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(financeTotals.planCount)}</p>
                        </CardContent>
                      </Card>
                    </div>
                    <BarChart
                      items={moneyChartItems([
                        { label: "تحقق‌یافته", value: financeTotals.realisedIrr },
                        { label: "تحقق‌نیافته", value: financeTotals.unrealisedIrr },
                      ])}
                    />
                    <DataTable
                      columns={[
                        {
                          key: "plan",
                          header: "برنامه",
                          render: (row) => (
                            <Link href={`/installments/${row.plan_id}`} className="font-medium text-primary underline-offset-4 hover:underline">
                              <span dir="ltr">{row.invoice_id}</span>
                            </Link>
                          ),
                        },
                        { key: "realised", header: "تحقق‌یافته", render: (row) => <MoneyDisplay amountIrr={row.income_realised_irr} size="sm" /> },
                        { key: "unpaid", header: "تحقق‌یافتهٔ پرداخت‌نشده", render: (row) => <MoneyDisplay amountIrr={row.income_realised_unpaid_irr} size="sm" /> },
                        { key: "unrealised", header: "تحقق‌نیافته", render: (row) => <MoneyDisplay amountIrr={row.income_unrealised_irr} size="sm" /> },
                        { key: "due", header: "قابل پرداخت در تسویه", render: (row) => <MoneyDisplay amountIrr={row.income_due_irr} size="sm" /> },
                      ]}
                      rows={quotes.state === "ready" ? quotes.value.map((q) => ({
                        ...q,
                        invoice_id: plans.find((p) => p.id === q.plan_id)?.invoice_id ?? q.plan_id,
                      })) : []}
                      rowKey={(row) => row.plan_id}
                      emptyText="برنامهٔ داخلی بازی نیست."
                    />
                  </>
                ) : null}
              </LazySection>
            ) : null}

            {selected === "dunning" ? (
              <>
                <BarChart
                  items={dunningStages.map((s) => ({
                    label: `${dunningStageLabel(s.stage)} (${faNumber(s.count)} قسط)`,
                    value: s.balanceIrr,
                    display: formatMoney(s.balanceIrr),
                  }))}
                />
                <DataTable
                  columns={[
                    { key: "stage", header: "مرحله", render: (row: DunningStageTotal) => dunningStageLabel(row.stage) },
                    { key: "count", header: "تعداد اقساط", render: (row: DunningStageTotal) => faNumber(row.count) },
                    { key: "balance", header: "مانده", render: (row: DunningStageTotal) => <MoneyDisplay amountIrr={row.balanceIrr} size="sm" /> },
                    { key: "fees", header: "جریمهٔ انباشته", render: (row: DunningStageTotal) => <MoneyDisplay amountIrr={row.lateFeesIrr} size="sm" /> },
                  ]}
                  rows={dunningStages}
                  rowKey={(row) => row.stage}
                  emptyText="قسط معوقی نیست."
                />
                <p className="text-xs text-muted-foreground">
                  هر مرحله یک آیتم «قسط معوق» در صف بازبینی دارد؛ اجرای پیگیری از گزارش ۱ انجام می‌شود.
                </p>
              </>
            ) : null}

            {selected === "bnpl-split" ? (
              <>
                <BarChart
                  items={moneyChartItems(
                    kindSplit.flatMap((s) => [
                      { label: `${planKindLabel(s.kind)} — اصل`, value: s.principalIrr },
                      { label: `${planKindLabel(s.kind)} — دریافت‌شده`, value: s.receivedIrr },
                      { label: `${planKindLabel(s.kind)} — مانده`, value: s.outstandingIrr },
                    ]),
                  )}
                />
                <DataTable
                  columns={[
                    { key: "kind", header: "نوع", render: (row) => <PlanKindBadge kind={row.kind} /> },
                    { key: "count", header: "تعداد برنامه", render: (row) => faNumber(row.planCount) },
                    { key: "principal", header: "اصل کل", render: (row) => <MoneyDisplay amountIrr={row.principalIrr} size="sm" /> },
                    { key: "received", header: "دریافت‌شده", render: (row) => <MoneyDisplay amountIrr={row.receivedIrr} size="sm" /> },
                    { key: "outstanding", header: "مانده", render: (row) => <MoneyDisplay amountIrr={row.outstandingIrr} size="sm" /> },
                  ]}
                  rows={kindSplit}
                  rowKey={(row) => row.kind}
                  emptyText="برنامه‌ای نیست."
                />
              </>
            ) : null}

            {selected === "customer-statement" ? (
              <DataTable
                columns={[
                  { key: "customer", header: "مشتری", render: (row) => row.customerRef },
                  { key: "plans", header: "برنامه‌ها", render: (row) => faNumber(row.planCount) },
                  { key: "principal", header: "اصل کل", render: (row) => <MoneyDisplay amountIrr={row.principalIrr} size="sm" /> },
                  { key: "received", header: "دریافت‌شده", render: (row) => <MoneyDisplay amountIrr={row.receivedIrr} size="sm" /> },
                  { key: "outstanding", header: "مانده", render: (row) => <MoneyDisplay amountIrr={row.outstandingIrr} size="sm" /> },
                  { key: "overdue", header: "برنامه‌های معوق", render: (row) => faNumber(row.overduePlanCount) },
                ]}
                rows={customerStatements}
                rowKey={(row) => row.customerRef}
                emptyText="برنامه‌ای نیست."
              />
            ) : null}

            {selected === "plan-schedule" ? (
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="schedule-plan-select">برنامه</Label>
                  <Select value={schedulePlanId || undefined} onValueChange={(value) => setSchedulePlanId(value ?? "")}>
                    <SelectTrigger id="schedule-plan-select">
                      <SelectValue placeholder="انتخاب برنامه" />
                    </SelectTrigger>
                    <SelectContent>
                      {plans.map((p) => (
                        <SelectItem key={p.id} value={p.id}>
                          {p.invoice_id} — {p.customer_ref ?? "بدون مرجع مشتری"}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {scheduleLoading ? (
                  <div className="space-y-2" aria-label="در حال بارگذاری">
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-full" />
                  </div>
                ) : scheduleDetail ? (
                  <>
                    <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                      <PlanKindBadge kind={scheduleDetail.plan.kind} />
                      <PlanStatusBadge status={scheduleDetail.status} />
                      <Link href={`/installments/${scheduleDetail.plan.id}`} className="font-medium text-primary hover:underline">
                        جزئیات برنامه
                      </Link>
                    </p>
                    <DataTable
                      columns={legTableColumns({
                        showCheque: scheduleDetail.plan.kind !== "bnpl",
                        incomeIrrOf:
                          scheduleDetail.plan.kind !== "bnpl" && scheduleContract
                            ? (leg) => legFinanceIncomeIrr(leg.principal_irr, Number(scheduleContract.finance_income_rate))
                            : undefined,
                      })}
                      rows={scheduleDetail.legs}
                      rowKey={(row) => row.id}
                      emptyText="این برنامه قسطی ندارد."
                    />
                    <BarChart
                      items={moneyChartItems(
                        scheduleDetail.legs.map((leg) => ({
                          label: `قسط ${faNumber(leg.sequence)} — سررسید ${formatJalaliShort(leg.due_date)}`,
                          value: leg.principal_irr,
                        })),
                      )}
                    />
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">برنامه‌ای را انتخاب کنید تا جدول سررسیدش را ببینید.</p>
                )}
              </div>
            ) : null}

            {selected === "early-settlements" ? (
              <LazySection state={details} label="تسویه‌های زودهنگام">
                <DataTable
                  columns={earlySettlementColumns()}
                  rows={settlements}
                  rowKey={(row) => row.receipt.id}
                  emptyText="هنوز تسویهٔ زودهنگامی ثبت نشده است."
                />
              </LazySection>
            ) : null}

            {selected === "bounced-cheques" ? (
              <LazySection state={details} label="اقساط با چک برگشتی">
                <div className="space-y-4">
                  <div>
                    <h3 className="mb-2 font-medium">چک‌های برگشتی ({faNumber(bouncedLegs.length)} قسط)</h3>
                    <DataTable
                      columns={chequeLegColumns()}
                      rows={bouncedLegs}
                      rowKey={(row) => row.leg.id}
                      emptyText="قسطی با چک برگشتی نیست."
                    />
                  </div>
                  <div>
                    <h3 className="mb-2 font-medium">
                      همهٔ اقساط چک‌دار ({faNumber(chequeLegs.length)} قسط) — شامل در انتظار وصول
                    </h3>
                    <DataTable
                      columns={chequeLegColumns()}
                      rows={chequeLegs}
                      rowKey={(row) => row.leg.id}
                      emptyText="قسط چک‌داری نیست."
                    />
                  </div>
                </div>
              </LazySection>
            ) : null}

            {selected === "sms-log" ? (
              smsLog.state === "error" ? (
                isPermissionDenied(smsLog.error) ? (
                  <PermissionDeniedState message="کلید شما دسترسی خواندن لاگ پیامک (sms:manage) را ندارد؛ لاگ هر برنامه در صفحهٔ جزئیات همان برنامه در دسترس است." />
                ) : (
                  <ErrorState
                    message={persianApiErrorMessage(smsLog.error, "دریافت لاگ پیامک ناموفق بود.")}
                    onRetry={() => setSmsLog({ state: "idle" })}
                  />
                )
              ) : (
                <LazySection state={smsLog} label="لاگ یادآوری پیامکی">
                  {smsRate ? (
                    <div className="flex flex-wrap items-center gap-4">
                      <p className="text-sm">
                        نرخ موفقیت:{" "}
                        {smsRate.rate === null ? (
                          "—"
                        ) : (
                          <span className="font-medium">{faNumber(Math.round(smsRate.rate * 100))}٪</span>
                        )}{" "}
                        ({faNumber(smsRate.sent + smsRate.delivered)} موفق از {faNumber(smsRate.sent + smsRate.delivered + smsRate.failed)} تلاش)
                      </p>
                      <p className="text-sm text-muted-foreground">
                        هزینهٔ شناخته‌شده: {faNumber(smsRate.knownCostToman)} تومان
                        {smsRate.unknownCostCount > 0
                          ? ` — هزینهٔ ${faNumber(smsRate.unknownCostCount)} پیام مشخص نیست و در جمع نیامده است`
                          : ""}
                      </p>
                      <ExportButton onExport={exportSmsCsv} disabled={smsLog.state !== "ready" || smsLog.value.length === 0} />
                    </div>
                  ) : null}
                  <BarChart
                    items={smsMonths.map((m) => ({
                      label: `${jalaliMonthLabel(m.monthKey)} — ارسال‌شده`,
                      value: m.sent,
                    }))}
                  />
                  <DataTable
                    columns={[
                      {
                        key: "created",
                        header: "تاریخ",
                        render: (row) => (row.created_at ? formatIsoDateTime(row.created_at) : "—"),
                      },
                      {
                        key: "plan",
                        header: "برنامه",
                        render: (row) => (
                          <Link href={`/installments/${row.plan_id}`} className="font-medium text-primary underline-offset-4 hover:underline">
                            مشاهدهٔ برنامه
                          </Link>
                        ),
                      },
                      { key: "provider", header: "ارائه‌دهنده", render: (row) => <span dir="ltr">{row.provider_key}</span> },
                      { key: "phone", header: "گیرنده", render: (row) => <span dir="ltr">{row.recipient_phone}</span> },
                      {
                        key: "offset",
                        header: "فاصله از سررسید",
                        render: (row) =>
                          row.offset_days === 0
                            ? "روز سررسید"
                            : row.offset_days < 0
                              ? `${faNumber(-row.offset_days)} روز پیش`
                              : `${faNumber(row.offset_days)} روز پس`,
                      },
                      { key: "status", header: "وضعیت", render: (row) => reminderStatusLabel(row.status) },
                      {
                        key: "cost",
                        header: "هزینه (تومان)",
                        render: (row) => (row.cost_toman === null ? "نامشخص" : faNumber(row.cost_toman)),
                      },
                    ]}
                    rows={smsLog.state === "ready" ? smsLog.value : []}
                    rowKey={(row) => row.id}
                    emptyText="هنوز یادآوری‌ای ثبت نشده است."
                  />
                </LazySection>
              )
            ) : null}

            {selected === "aging" ? (
              <LazySection state={details} label="قدمت اقساط">
                <BarChart
                  items={moneyChartItems(
                    aging.map((b) => ({
                      label: `${AGING_BUCKET_LABELS[b.bucket]} (${faNumber(b.count)} قسط)`,
                      value: b.balanceIrr,
                    })),
                  )}
                />
                <DataTable
                  columns={[
                    { key: "bucket", header: "سطل", render: (row) => AGING_BUCKET_LABELS[row.bucket] },
                    { key: "count", header: "تعداد اقساط", render: (row) => faNumber(row.count) },
                    { key: "balance", header: "مانده", render: (row) => <MoneyDisplay amountIrr={row.balanceIrr} size="sm" /> },
                  ]}
                  rows={aging}
                  rowKey={(row) => row.bucket}
                  emptyText="قسط مانده‌داری نیست."
                />
              </LazySection>
            ) : null}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/** The overdue report (report 1) — a first-class view with CSV export + the dunning sweep. */
function OverdueReportSection({
  overdue,
  buckets,
  onExportCsv,
  onRunDunning,
  dunningRunning,
  dunningResult,
  dunningError,
}: {
  overdue: OverdueReportResponse;
  buckets: OverdueBucketTotal[];
  onExportCsv: () => void;
  onRunDunning: () => void;
  dunningRunning: boolean;
  dunningResult: DunningRunResponse | null;
  dunningError: unknown;
}) {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">اقساط معوق</p>
            <p className="mt-1 text-2xl font-bold tabular-nums">{faNumber(overdue.total_rows)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">ماندهٔ معوق</p>
            <p className="mt-1"><MoneyDisplay amountIrr={overdue.total_balance_irr} size="md" /></p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">جریمهٔ انباشته</p>
            <p className="mt-1"><MoneyDisplay amountIrr={overdue.total_late_fees_irr} size="md" /></p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="space-y-2 pt-6">
            <p className="text-sm text-muted-foreground">پیگیری (دونینگ)</p>
            <Button onClick={onRunDunning} disabled={dunningRunning} size="sm">
              {dunningRunning ? "در حال اجرا…" : "اجرای پیگیری"}
            </Button>
            <p className="text-xs text-muted-foreground">
              جریمه‌ها را محاسبه و آیتم‌های مرحلهٔ جدید را در صف بازبینی ثبت می‌کند؛ اجرای دوباره چیزی را تکرار نمی‌کند.
            </p>
          </CardContent>
        </Card>
      </div>

      {dunningResult ? (
        <p role="status" className="rounded-lg border border-success/40 bg-success/10 px-4 py-3 text-sm">
          پیگیری اجرا شد: {faNumber(dunningResult.plans_scanned)} برنامه بررسی شد، {faNumber(dunningResult.overdue_legs)} قسط
          معوق، {faNumber(dunningResult.items_filed.length)} آیتم جدید در صف بازبینی،{" "}
          <MoneyDisplay amountIrr={dunningResult.fees_assessed_irr} size="sm" /> جریمه محاسبه شد
          {dunningResult.legs_skipped_no_account.length > 0
            ? ` — ${faNumber(dunningResult.legs_skipped_no_account.length)} قسط به‌خاطر نبود حساب جریمه سند نزد (فقط محاسبه شد).`
            : " — بدون قسط جاافتاده."}
        </p>
      ) : null}
      {dunningError ? (
        <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {persianApiErrorMessage(dunningError, "اجرای پیگیری ناموفق بود.")}
        </p>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-4">
        <h3 className="font-medium">قدمت معوق‌ها</h3>
        <ExportButton onExport={onExportCsv} disabled={overdue.rows.length === 0} />
      </div>
      <BarChart
        items={moneyChartItems(
          buckets.map((b) => ({
            label: `${OVERDUE_AGE_BUCKET_LABELS[b.bucket]} (${faNumber(b.count)} قسط)`,
            value: b.balanceIrr,
          })),
        )}
      />
      <DataTable
        columns={[
          {
            key: "plan",
            header: "برنامه",
            render: (row) => (
              <Link href={`/installments/${row.plan_id}`} className="font-medium text-primary underline-offset-4 hover:underline">
                <span dir="ltr">{row.invoice_id}</span>
              </Link>
            ),
          },
          { key: "customer", header: "مشتری", render: (row) => row.customer_ref ?? "—" },
          { key: "leg", header: "قسط", render: (row) => faNumber(row.sequence) },
          { key: "due", header: "سررسید", render: (row) => formatJalaliShort(row.due_date) },
          { key: "days", header: "روزهای تأخیر", render: (row) => faNumber(row.days_overdue) },
          { key: "balance", header: "مانده", render: (row) => <MoneyDisplay amountIrr={row.balance_irr} size="sm" /> },
          { key: "stage", header: "مرحلهٔ پیگیری", render: (row) => dunningStageLabel(row.dunning_stage) },
          {
            key: "next",
            header: "یادآوری بعدی",
            render: (row) => (row.next_reminder_date ? formatJalaliShort(row.next_reminder_date) : "—"),
          },
          { key: "fee", header: "جریمهٔ انباشته", render: (row) => <MoneyDisplay amountIrr={row.late_fee_accrued_irr} size="sm" /> },
        ]}
        rows={overdue.rows}
        rowKey={(row) => row.leg_id}
        emptyText="قسط معوقی نیست."
        caption={`اقساط معوق — ${faNumber(overdue.total_rows)} قسط، ${formatMoney(overdue.total_balance_irr)} مانده`}
      />
      <p className="text-xs text-muted-foreground">
        مجموع‌ها کل فیلتر را پوشش می‌دهند، نه فقط این صفحه را. قسط در انتظار وصول چک اینجا نیست — مشتری پرداخت
        کرده و پول در مسیر وصول است.
      </p>
    </div>
  );
}

/** Loading / error wrapper for the lazily fetched report data. */
function LazySection({
  state,
  label,
  children,
}: {
  state: LazyState<unknown>;
  label: string;
  children: React.ReactNode;
}) {
  if (state.state === "loading" || state.state === "idle") {
    return (
      <div className="space-y-2" aria-label={`در حال بارگذاری ${label}`}>
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }
  if (state.state === "error") {
    return (
      <ErrorState
        message={persianApiErrorMessage(state.error, `دریافت دادهٔ ${label} ناموفق بود.`)}
        onRetry={() => window.location.reload()}
      />
    );
  }
  return <div className="space-y-6">{children}</div>;
}

function earlySettlementColumns(): DataTableColumn<EarlySettlementRow>[] {
  return [
    {
      key: "plan",
      header: "برنامه",
      render: (row) => (
        <Link href={`/installments/${row.planId}`} className="font-medium text-primary underline-offset-4 hover:underline">
          <span dir="ltr">{row.invoiceId}</span>
        </Link>
      ),
    },
    { key: "customer", header: "مشتری", render: (row) => row.customerRef ?? "—" },
    { key: "date", header: "تاریخ", render: (row) => formatJalaliShort(row.receipt.received_date) },
    { key: "amount", header: "مبلغ تسویه", render: (row) => <MoneyDisplay amountIrr={row.receipt.amount_irr} size="sm" /> },
    { key: "income", header: "سود دریافت‌شده", render: (row) => <MoneyDisplay amountIrr={row.receipt.finance_income_irr} size="sm" /> },
    { key: "fee", header: "جریمهٔ دریافت‌شده", render: (row) => <MoneyDisplay amountIrr={row.receipt.late_fee_irr} size="sm" /> },
    { key: "advance", header: "پیش‌پرداخت اعمال‌شده", render: (row) => <MoneyDisplay amountIrr={row.receipt.applied_advance_irr} size="sm" /> },
  ];
}

function chequeLegColumns(): DataTableColumn<ChequeLegRow>[] {
  return [
    {
      key: "plan",
      header: "برنامه",
      render: (row) => (
        <Link href={`/installments/${row.planId}`} className="font-medium text-primary underline-offset-4 hover:underline">
          <span dir="ltr">{row.invoiceId}</span>
        </Link>
      ),
    },
    { key: "customer", header: "مشتری", render: (row) => row.customerRef ?? "—" },
    { key: "leg", header: "قسط", render: (row) => faNumber(row.leg.sequence) },
    { key: "due", header: "سررسید قسط", render: (row) => formatJalaliShort(row.leg.due_date) },
    { key: "balance", header: "مانده", render: (row) => <MoneyDisplay amountIrr={row.leg.balance_irr} size="sm" /> },
    {
      key: "cheque",
      header: "چک",
      render: (row) =>
        row.leg.linked_cheque ? (
          <Link href={`/cheques/${row.leg.linked_cheque.cheque_id}`} className="text-primary underline-offset-4 hover:underline" dir="ltr">
            {row.leg.linked_cheque.sayad_id}
          </Link>
        ) : (
          "—"
        ),
    },
    {
      key: "cheque-state",
      header: "وضعیت چک",
      render: (row) => row.leg.linked_cheque?.state ?? "—",
    },
    {
      key: "cheque-due",
      header: "سررسید چک",
      render: (row) => (row.leg.linked_cheque ? formatJalaliShort(row.leg.linked_cheque.due_date) : "—"),
    },
  ];
}
