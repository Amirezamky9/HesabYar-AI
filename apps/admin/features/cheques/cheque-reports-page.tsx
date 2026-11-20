/**
 * Cheque reports (US-535; §7.5 — all 17 reports).
 *
 * Every report wires to its real backend endpoint or derives from
 * the registry rows the list endpoint returns; nothing is invented.
 * Report 7 (notes balance) reads the Tryton reference report
 * through GET /reports/notes-balance (US-550) — never recomputed
 * client-side from partial data (§11.1).
 *
 * Sources per report:
 *  1, 2, 3, 9 — GET /cheques (server filters + client filters)
 *  4 — GET /cheques/reports/in-clearing-aging
 *  5 — GET /cheques/endorsement-summary
 *  6 — GET /cheques/collateral-summary
 *  7 — GET /reports/notes-balance (Tryton reference, US-550)
 *  8 — GET /cheques/checkbooks (+ /cheques/checkbooks/{id})
 * 10 — review queue (cheque_exception) + mismatch flags on the rows
 * 11 — GET /cheques/intake + GET /cheques/settings (deadline)
 * 12 — GET /cheques/reports/monthly-match-trend (stored history, US-553) + POST /bank/statements/{id}/cheque-clearing
 * 13 — GET /cheques/reports/inquiry-ledger + receipt-rule audit
 * 14 — derived 12-week forecast over the registry (collateral excluded)
 * 15 — derived per-issuer statement over the registry
 * 16 — derived storage-location grouping over the registry
 * 17 — endorsed rows + per-cheque custody timeline (GET /cheques/{id})
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import {
  ApiError,
  api,
  type CheckbookResponse,
  type ChequeDetailResponse,
  type ChequeResponse,
  type ClearingAgingResponse,
  type CollateralSummaryResponse,
  type EndorsementSummaryResponse,
  type InquiryLedgerResponse,
  type MonthlyMatchTrendResponse,
  type ReviewItemResponse,
} from "@/lib/api-client";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  Input,
  JalaliDatePicker,
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
import { fa, faNumber } from "@/lib/utils";
import { formatMoney } from "@/lib/format-money";
import { formatIsoDate, formatIsoDateTime } from "@/lib/format-date";
import { JALALI_MONTHS, formatIsoJalali, todayIso } from "@/lib/jalali";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import { buildCsv, csvFilename, downloadCsv } from "@/features/shared/csv";
import { agingChartItems, filterNotesRows, notesInstrumentLabel } from "@/features/reports/financial-report-data";
import { ReportFreshnessLine } from "@/features/reports/financial-sections";
import { notesBalanceToCsv } from "@/features/reports/report-csv";
import { useCompany } from "@/features/shared/use-company";
import { AreaChart, BarChart, DonutChart, LineChart, type ChartItem } from "./cheque-report-charts";
import {
  CHEQUE_REPORTS,
  CHEQUE_STATES,
  CLEARING_AGE_BUCKET_LABELS,
  CLEARING_AGE_BUCKETS,
  DUE_BUCKET_LABELS,
  DUE_BUCKETS,
  INTAKE_REMINDER_LABELS,
  cashForecast,
  chequeStateLabel,
  clearingAgeBucket,
  clearingConfidenceLabel,
  dueBucket,
  formatJalaliShort,
  intakeReminderState,
  issuerColorLabel,
  lifecycleSteps,
  storageLocationLabel,
  sumIrr,
  todayJalaliString,
} from "./cheque-data";
import { applyChequeFilters, chequeTableColumns, chequeSourceLabel } from "./cheque-shared";

function monthLabel(jalaliMonth: string): string {
  const match = jalaliMonth.match(/^(\d{4})-(\d{2})$/);
  if (!match) return jalaliMonth;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return jalaliMonth;
  return `${JALALI_MONTHS[month - 1]} ${faNumber(Number(match[1]))}`;
}

function monthKey(jalaliDate: string): string {
  return jalaliDate.slice(0, 7);
}

function groupByMonth(rows: ChequeResponse[], dateOf: (row: ChequeResponse) => string | null): ChartItem[] {
  const counts = new Map<string, number>();
  for (const row of rows) {
    const date = dateOf(row);
    if (!date) continue;
    const key = monthKey(date);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([key, value]) => ({ label: monthLabel(key), value }));
}

function ReportHeader({ number, title, meta }: { number: number; title: string; meta: string }) {
  return (
    <div>
      <h2 className="text-xl font-bold">
        گزارش {faNumber(number)} — {title}
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">{meta}</p>
    </div>
  );
}

function FreshnessLine({ generatedAt }: { generatedAt: string }) {
  return (
    <p className="text-xs text-muted-foreground">
      منبع: حساب‌یار (رجیستری داخلی) · زمان تولید: {generatedAt} · واحد: تومان (مبالغ به ریال ذخیره می‌شوند)
    </p>
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

interface ReportsData {
  cheques: ChequeResponse[];
  intake: ChequeResponse[];
  aging: ClearingAgingResponse[];
  ledger: InquiryLedgerResponse;
  collateral: CollateralSummaryResponse;
  endorsement: EndorsementSummaryResponse;
  trend: MonthlyMatchTrendResponse;
  deadlineDays: number;
  reviewItems: ReviewItemResponse[];
}

export function ChequeReportsPage() {
  const router = useRouter();
  const { companies, companyId } = useCompany();
  const company = companies.find((c) => c.id === companyId) ?? null;
  // Deep links (?report=<id>) from the dashboard widgets / center page —
  // read once in the lazy initial state, not in an effect.
  const [selected, setSelected] = React.useState<string>(() => {
    const id = new URLSearchParams(window.location.search).get("report");
    return id && CHEQUE_REPORTS.some((r) => r.id === id) ? id : "due-received";
  });
  const [data, setData] = React.useState<ReportsData | null>(null);
  const [checkbooks, setCheckbooks] = React.useState<CheckbookResponse[] | null>(null);
  const [checkbooksFailed, setCheckbooksFailed] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const todayJalali = React.useMemo(() => todayJalaliString(), []);
  const generatedAt = React.useMemo(() => formatIsoDateTime(new Date().toISOString()), []);

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.listCheques({ limit: 100 }),
      api.listChequeIntake({ limit: 100 }),
      api.listInClearingAging(),
      api.getInquiryLedger(),
      api.getCollateralSummary(),
      api.getEndorsementSummary(),
      api.getChequeSettings(),
      api.listReviewItems({ type: "cheque_exception", limit: 100 }),
      api.getMonthlyMatchTrend({ months: 12 }),
    ])
      .then(([cheques, intake, aging, ledger, collateral, endorsement, settings, reviewPage, trend]) => {
        if (cancelled) return;
        setData({
          cheques,
          intake,
          aging,
          ledger,
          collateral,
          endorsement,
          trend,
          deadlineDays: settings.leaf_arrival_deadline_days,
          reviewItems: reviewPage.items,
        });
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

  // Loading state is set by load() from a timer callback, never
  // synchronously in the effect body (react-hooks/set-state-in-effect).
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

  React.useEffect(() => {
    if (!company) return;
    let cancelled = false;
    api
      .listCheckbooks(company.engine_id)
      .then((rows) => {
        if (!cancelled) setCheckbooks(rows);
      })
      .catch(() => {
        if (!cancelled) setCheckbooksFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [company]);

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن چک‌ها را ندارد." />;
  }
  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت داده‌های گزارش‌های چک ناموفق بود.")}
        onRetry={load}
      />
    );
  }

  const meta = CHEQUE_REPORTS.find((r) => r.id === selected) ?? CHEQUE_REPORTS[0];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">گزارش‌های چک</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          ۱۷ گزارش §۷٫۵ — «چقدر چک دادیم» (گزارش ۲) و «چقدر چک پشتوانه کردیم» (گزارش ۶) کاملاً جدا از هم‌اند.
          {data ? ` بر اساس ${faNumber(data.cheques.length)} چک رجیستری (حداکثر ۱۰۰ ردیف اول).` : ""}
        </p>
      </div>

      {loading || !data ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
          <nav aria-label="فهرست گزارش‌های چک" className="space-y-1">
            {CHEQUE_REPORTS.map((report) => (
              <button
                key={report.id}
                type="button"
                onClick={() => setSelected(report.id)}
                aria-current={selected === report.id ? "true" : undefined}
                className={`flex min-h-11 w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-start text-sm ${
                  selected === report.id ? "bg-primary/10 font-medium text-primary" : "hover:bg-muted"
                }`}
              >
                <span>
                  {faNumber(report.number)}. {report.title}
                </span>
                {report.backend === "none" ? (
                  <span className="shrink-0 rounded-full border px-2 py-0.5 text-xs text-muted-foreground">به‌زودی</span>
                ) : null}
              </button>
            ))}
          </nav>

          <div className="space-y-6" key={selected}>
            <ReportView
              reportId={selected}
              meta={meta}
              data={data}
              checkbooks={checkbooks}
              checkbooksFailed={checkbooksFailed}
              companyId={companyId}
              companyName={company?.name ?? null}
              todayJalali={todayJalali}
              generatedAt={generatedAt}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function ReportView({
  reportId,
  meta,
  data,
  checkbooks,
  checkbooksFailed,
  companyId,
  companyName,
  todayJalali,
  generatedAt,
}: {
  reportId: string;
  meta: (typeof CHEQUE_REPORTS)[number];
  data: ReportsData;
  checkbooks: CheckbookResponse[] | null;
  checkbooksFailed: boolean;
  companyId: string | null;
  companyName: string | null;
  todayJalali: string;
  generatedAt: string;
}) {
  switch (reportId) {
    case "due-received":
      return <DueReceivedReport meta={meta} cheques={data.cheques} todayJalali={todayJalali} generatedAt={generatedAt} />;
    case "issued":
      return <IssuedReport meta={meta} cheques={data.cheques} generatedAt={generatedAt} />;
    case "bounced":
      return <BouncedReport meta={meta} cheques={data.cheques} generatedAt={generatedAt} />;
    case "in-clearing":
      return <InClearingReport meta={meta} cheques={data.cheques} aging={data.aging} generatedAt={generatedAt} />;
    case "endorsed":
      return <EndorsedReport meta={meta} summary={data.endorsement} generatedAt={generatedAt} />;
    case "collateral":
      return <CollateralReport meta={meta} summary={data.collateral} generatedAt={generatedAt} />;
    case "notes-balance":
      return <NotesBalanceReport meta={meta} companyId={companyId} companyName={companyName} />;
    case "checkbooks":
      return <CheckbooksReport meta={meta} checkbooks={checkbooks} failed={checkbooksFailed} companyName={companyName} generatedAt={generatedAt} />;
    case "registry":
      return <RegistryReport meta={meta} cheques={data.cheques} todayJalali={todayJalali} generatedAt={generatedAt} />;
    case "mismatches":
      return <MismatchesReport meta={meta} cheques={data.cheques} reviewItems={data.reviewItems} generatedAt={generatedAt} />;
    case "unreceived":
      return <UnreceivedReport meta={meta} intake={data.intake} deadlineDays={data.deadlineDays} generatedAt={generatedAt} />;
    case "clearing-match":
      return <ClearingMatchReport meta={meta} cheques={data.cheques} trend={data.trend} generatedAt={generatedAt} />;
    case "inquiry-ledger":
      return <InquiryLedgerReport meta={meta} ledger={data.ledger} generatedAt={generatedAt} />;
    case "cash-forecast":
      return <CashForecastReport meta={meta} cheques={data.cheques} todayJalali={todayJalali} generatedAt={generatedAt} />;
    case "party-statement":
      return <PartyStatementReport meta={meta} cheques={data.cheques} generatedAt={generatedAt} />;
    case "storage":
      return <StorageReport meta={meta} cheques={data.cheques} generatedAt={generatedAt} />;
    case "transfer-history":
      return <TransferHistoryReport meta={meta} cheques={data.cheques} generatedAt={generatedAt} />;
    default:
      return null;
  }
}

function FilterText({ id, label, value, onChange, placeholder }: { id: string; label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
    </div>
  );
}

function DueReceivedReport({ meta, cheques, todayJalali, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; todayJalali: string; generatedAt: string }) {
  const [bank, setBank] = React.useState("");
  const [party, setParty] = React.useState("");
  const rows = applyChequeFilters(
    cheques.filter((c) => c.kind === "received" && c.purpose === "payment" && ["held", "in_clearing", "registered"].includes(c.state)),
    { bank: bank || undefined, party: party || undefined },
  ).sort((a, b) => (a.due_date < b.due_date ? -1 : 1));
  const buckets: ChartItem[] = DUE_BUCKETS.map((bucket) => ({
    label: DUE_BUCKET_LABELS[bucket],
    value: rows.filter((r) => dueBucket(r.due_date, todayJalali) === bucket).length,
  }));
  const exportRows = () => {
    downloadCsv(
      `cheques-due-received-${new Date().toISOString().slice(0, 10)}.csv`,
      buildCsv(
        [
          { header: "شناسهٔ صیاد", value: (r) => r.sayad_id },
          { header: "وضعیت", value: (r) => r.state },
          { header: "صادرکننده", value: (r) => r.issuer_name },
          { header: "بانک", value: (r) => r.issuer_bank },
          { header: "مبلغ (ریال)", value: (r) => r.amount_irr },
          { header: "سررسید (شمسی)", value: (r) => r.due_date },
        ],
        rows,
      ),
    );
  };
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
        <ExportButton onExport={exportRows} disabled={rows.length === 0} />
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <FilterText id="r1-bank" label="بانک" value={bank} onChange={setBank} placeholder="مثلاً سامان" />
        <FilterText id="r1-party" label="طرف حساب" value={party} onChange={setParty} placeholder="نام صادرکننده" />
      </div>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <BarChart items={buckets} unit="چک" />
          <p className="text-sm text-muted-foreground">
            {faNumber(rows.length)} چک دریافتی تجاریِ باز · مجموع: <MoneyDisplay amountIrr={sumIrr(rows)} size="sm" />
          </p>
        </CardContent>
      </Card>
      <DataTable
        columns={chequeTableColumns(todayJalali)}
        rows={rows}
        rowKey={(r) => r.id}
        emptyText="چک دریافتی سررسیددارِ بازی نیست."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function IssuedReport({ meta, cheques, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; generatedAt: string }) {
  const [bank, setBank] = React.useState("");
  const rows = applyChequeFilters(
    cheques.filter((c) => c.kind === "issued" && c.purpose === "payment" && c.state === "issued"),
    { bank: bank || undefined },
  ).sort((a, b) => (a.due_date < b.due_date ? -1 : 1));
  const monthly = groupByMonth(rows, (r) => r.due_date);
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <div className="grid gap-4 sm:grid-cols-2">
        <FilterText id="r2-bank" label="بانک" value={bank} onChange={setBank} placeholder="مثلاً سامان" />
      </div>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <BarChart items={monthly} unit="چک" />
          <p className="text-sm text-muted-foreground">
            {faNumber(rows.length)} چک پرداختی صادرشده · مجموع خروجی موردانتظار: <MoneyDisplay amountIrr={sumIrr(rows)} size="sm" />
          </p>
        </CardContent>
      </Card>
      <DataTable
        columns={chequeTableColumns()}
        rows={rows}
        rowKey={(r) => r.id}
        emptyText="چک پرداختی صادرشده‌ای نیست."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function BouncedReport({ meta, cheques, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; generatedAt: string }) {
  const [bank, setBank] = React.useState("");
  const rows = applyChequeFilters(
    cheques.filter((c) => c.state === "bounced"),
    { bank: bank || undefined },
  );
  const reasonCounts = new Map<string, number>();
  for (const row of rows) reasonCounts.set(row.bounce_reason ?? "بدون دلیل ثبت‌شده", (reasonCounts.get(row.bounce_reason ?? "بدون دلیل ثبت‌شده") ?? 0) + 1);
  const reasons: ChartItem[] = [...reasonCounts.entries()].map(([label, value]) => ({ label, value }));
  const monthly = groupByMonth(rows, (r) => r.bounce_date);
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <FilterText id="r3-bank" label="بانک" value={bank} onChange={setBank} placeholder="مثلاً سامان" />
      <Card>
        <CardContent className="space-y-6 pt-6">
          <div>
            <h3 className="mb-3 font-medium">به تفکیک دلیل برگشت</h3>
            <DonutChart items={reasons} />
          </div>
          <div>
            <h3 className="mb-3 font-medium">روند ماهانهٔ برگشت (تعداد)</h3>
            <BarChart items={monthly} unit="چک" />
          </div>
          <p className="text-sm text-muted-foreground">
            {faNumber(rows.length)} چک برگشتی · مجموع: <MoneyDisplay amountIrr={sumIrr(rows)} size="sm" />
          </p>
        </CardContent>
      </Card>
      <DataTable
        columns={chequeTableColumns()}
        rows={rows}
        rowKey={(r) => r.id}
        emptyText="چک برگشتی نیست."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function InClearingReport({ meta, cheques, aging, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; aging: ClearingAgingResponse[]; generatedAt: string }) {
  const byId = new Map(cheques.map((c) => [c.id, c]));
  const buckets: ChartItem[] = CLEARING_AGE_BUCKETS.map((bucket) => ({
    label: CLEARING_AGE_BUCKET_LABELS[bucket],
    value: aging.filter((a) => clearingAgeBucket(a.days_in_clearing) === bucket).length,
  }));
  const staleCount = aging.filter((a) => a.stale).length;
  const windowUnset = aging.some((a) => a.window_unset);
  const columns: DataTableColumn<ClearingAgingResponse>[] = [
    { key: "sayad", header: "شناسهٔ صیاد", render: (a) => <Link href={`/cheques/${a.cheque_id}`} className="font-mono text-sm text-primary underline-offset-4 hover:underline" dir="ltr">{a.sayad_id}</Link> },
    { key: "bank", header: "بانک", render: (a) => byId.get(a.cheque_id)?.issuer_bank ?? "—" },
    { key: "amount", header: "مبلغ", render: (a) => { const c = byId.get(a.cheque_id); return c ? <MoneyDisplay amountIrr={c.amount_irr} size="sm" /> : "—"; } },
    { key: "days", header: "روزهای در کلر", render: (a) => faNumber(a.days_in_clearing) },
    { key: "stale", header: "کهنگی", render: (a) => (a.stale ? "کهنه — نیازمند پیگیری" : "—") },
  ];
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          <BarChart items={buckets} unit="چک" />
          <p className="text-sm text-muted-foreground">
            {faNumber(aging.length)} چک در جریان وصول · {faNumber(staleCount)} کهنه
            {windowUnset ? " · پنجرهٔ وصول در تنظیمات نصب تعیین نشده است — پرچم کهنگی تا تعیین پنجره محافظه‌کارانه است." : ""}
          </p>
          <p className="text-xs text-muted-foreground">چک کهنه هرگز خودکار برگشتی نمی‌شود؛ برگشت نیازمند گواهی عدم پرداخت بانک است.</p>
        </CardContent>
      </Card>
      <DataTable columns={columns} rows={[...aging].sort((a, b) => b.days_in_clearing - a.days_in_clearing)} rowKey={(a) => a.cheque_id} emptyText="چکی در جریان وصول نیست." />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function EndorsedReport({ meta, summary, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; summary: EndorsementSummaryResponse; generatedAt: string }) {
  const items: ChartItem[] = [
    { label: "تأمین‌کننده", value: summary.supplier.length, display: formatMoney(summary.supplier_total_irr) },
    { label: "شخص دیگر", value: summary.other.length, display: formatMoney(summary.other_total_irr) },
  ];
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          <DonutChart items={items} />
          <p className="text-sm text-muted-foreground">
            تأمین‌کننده: <MoneyDisplay amountIrr={summary.supplier_total_irr} size="sm" /> · شخص دیگر: <MoneyDisplay amountIrr={summary.other_total_irr} size="sm" />
          </p>
          <p className="text-xs text-muted-foreground">
            ظهرنویسی به شخص دیگر نیازمند تأیید حسابدار است و سندی برای آن حدس زده نمی‌شود؛ زنجیرهٔ انتقال هر چک در کارت چک دیده می‌شود.
          </p>
        </CardContent>
      </Card>
      <h3 className="font-medium">ظهرنویسی به تأمین‌کننده</h3>
      <DataTable columns={chequeTableColumns()} rows={summary.supplier} rowKey={(r) => r.id} emptyText="چکی به تأمین‌کننده ظهرنویسی نشده است." />
      <h3 className="font-medium">ظهرنویسی به شخص دیگر</h3>
      <DataTable columns={chequeTableColumns()} rows={summary.other} rowKey={(r) => r.id} emptyText="چکی به شخص دیگر ظهرنویسی نشده است." />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function CollateralReport({ meta, summary, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; summary: CollateralSummaryResponse; generatedAt: string }) {
  const donut: ChartItem[] = [
    { label: "دریافتی", value: summary.cheques.filter((c) => c.kind === "received").length, display: formatMoney(summary.received_total_irr) },
    { label: "پرداختی", value: summary.cheques.filter((c) => c.kind === "issued").length, display: formatMoney(summary.issued_total_irr) },
  ];
  const obligations = Object.entries(summary.by_obligation);
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          <DonutChart items={donut} />
          <p className="text-sm text-muted-foreground">
            ماندهٔ انتظامی — دریافتی: <MoneyDisplay amountIrr={summary.received_total_irr} size="sm" /> · پرداختی: <MoneyDisplay amountIrr={summary.issued_total_irr} size="sm" />
          </p>
          <p className="text-xs text-muted-foreground">
            چک پشتوانه خارج از ترازنامه است (حساب‌های انتظامی) و هرگز در گزارش‌های تجاری، ماندهٔ اسناد یا پیش‌بینی نقد نمی‌آید.
          </p>
        </CardContent>
      </Card>
      <h3 className="font-medium">به تفکیک تعهد تحت پوشش</h3>
      {obligations.length === 0 ? (
        <p className="text-sm text-muted-foreground">چک پشتوانه‌ای نیست.</p>
      ) : (
        obligations.map(([obligation, rows]) => (
          <Card key={obligation}>
            <CardHeader>
              <CardTitle className="text-base">{obligation === "null" ? "بدون تعهد ثبت‌شده" : obligation}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                {faNumber(rows.length)} چک · مجموع: <MoneyDisplay amountIrr={sumIrr(rows)} size="sm" />
              </p>
              <DataTable columns={chequeTableColumns()} rows={rows} rowKey={(r) => r.id} />
            </CardContent>
          </Card>
        ))
      )}
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function NotesBalanceReport({ meta, companyId, companyName }: { meta: (typeof CHEQUE_REPORTS)[number]; companyId: string | null; companyName: string | null }) {
  const router = useRouter();
  const [asOf, setAsOf] = React.useState<string | null>(() => todayIso());
  const [instrument, setInstrument] = React.useState("both");
  const [partyText, setPartyText] = React.useState("");
  const [report, setReport] = React.useState<Awaited<ReturnType<typeof api.getNotesBalance>> | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => {
    if (!companyId || !asOf) return;
    let cancelled = false;
    api
      .getNotesBalance({ company: companyId, as_of: asOf, instrument })
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
  }, [companyId, asOf, instrument, reloadKey, router]);

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message={persianApiErrorMessage(error, "دریافت ماندهٔ اسناد ناموفق بود.")} />;
  }

  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت ماندهٔ اسناد ناموفق بود.")}
        onRetry={() => {
          setError(null);
          setReport(null);
          setReloadKey((k) => k + 1);
        }}
      />
    );
  }

  const rows = report ? filterNotesRows(report.rows, { partyText }) : [];
  const receivableTotal = rows.filter((r) => r.instrument === "receivable").reduce((sum, r) => sum + r.total, 0);
  const payableTotal = rows.filter((r) => r.instrument === "payable").reduce((sum, r) => sum + r.total, 0);
  const filters: [string, string][] = [
    ["شرکت", companyName ?? ""],
    ["تاریخ", asOf ?? ""],
    ["نوع سند", notesInstrumentLabel(instrument)],
    ...(partyText.trim() ? ([["طرف حساب", partyText.trim()]] as [string, string][]) : []),
  ];
  const columns: DataTableColumn<Awaited<ReturnType<typeof api.getNotesBalance>>["rows"][number]>[] = [
    { key: "party", header: "طرف حساب", render: (row) => row.party_name || "بدون طرف حساب" },
    { key: "instrument", header: "نوع سند", render: (row) => notesInstrumentLabel(row.instrument) },
    {
      key: "account",
      header: "حساب",
      render: (row) => (
        <span>
          <span className="text-muted-foreground tabular-nums">{fa(row.account_code)} </span>
          {row.account_name}
        </span>
      ),
    },
    {
      key: "not_yet_due",
      header: "سررسید نشده",
      render: (row) => <span className="tabular-nums">{formatMoney(row.bucket_not_yet_due)}</span>,
      className: "text-end",
    },
    {
      key: "b0",
      header: "۰ تا ۳۰ روز",
      render: (row) => <span className="tabular-nums">{formatMoney(row.bucket_0_30)}</span>,
      className: "text-end",
    },
    {
      key: "b30",
      header: "۳۰ تا ۶۰ روز",
      render: (row) => <span className="tabular-nums">{formatMoney(row.bucket_30_60)}</span>,
      className: "text-end",
    },
    {
      key: "b60",
      header: "۶۰ تا ۹۰ روز",
      render: (row) => <span className="tabular-nums">{formatMoney(row.bucket_60_90)}</span>,
      className: "text-end",
    },
    {
      key: "b90",
      header: "۹۰ تا ۱۲۰ روز",
      render: (row) => <span className="tabular-nums">{formatMoney(row.bucket_90_120)}</span>,
      className: "text-end",
    },
    {
      key: "b120",
      header: "بیش از ۱۲۰ روز",
      render: (row) => <span className="tabular-nums">{formatMoney(row.bucket_120_plus)}</span>,
      className: "text-end",
    },
    {
      key: "total",
      header: "جمع",
      render: (row) => <span className="font-medium tabular-nums">{formatMoney(row.total)}</span>,
      className: "text-end",
    },
    { key: "count", header: "تعداد سند", render: (row) => faNumber(row.line_count), className: "text-end" },
  ];

  return (
    <div className="space-y-6" data-testid="cheque-report-notes-balance">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
        <ExportButton
          disabled={!report || rows.length === 0}
          onExport={() => {
            if (!report) return;
            downloadCsv(
              csvFilename("cheques-notes-balance", todayIso()),
              notesBalanceToCsv({ ...report, rows }, filters),
            );
          }}
        />
      </div>
      <div className="flex flex-wrap items-end gap-4 print:hidden">
        <div className="space-y-2">
          <Label htmlFor="notes-as-of">تا تاریخ</Label>
          <JalaliDatePicker
            id="notes-as-of"
            value={asOf}
            onChange={(iso) => {
              setAsOf(iso);
              setReport(null);
            }}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="notes-instrument">نوع سند</Label>
          <Select
            value={instrument}
            onValueChange={(v) => {
              setInstrument(v ?? "both");
              setReport(null);
            }}
          >
            <SelectTrigger id="notes-instrument">
              <SelectValue>{notesInstrumentLabel(instrument)}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="both">دریافتنی و پرداختنی</SelectItem>
              <SelectItem value="receivable">دریافتنی</SelectItem>
              <SelectItem value="payable">پرداختنی</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <FilterText id="notes-party" label="طرف حساب" value={partyText} onChange={setPartyText} placeholder="نام طرف حساب" />
      </div>
      {report === null ? (
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : report.accounts_read.length === 0 ? (
        <Card>
          <CardContent className="space-y-3 pt-6">
            <p className="font-medium">حساب اسناد در چارت نیست</p>
            <p className="text-sm text-muted-foreground">
              در چارت این شرکت حساب اسناد دریافتنی/پرداختنی (۱٫۲٫۴، ۱٫۲٫۵، ۲٫۱٫۵) نیست؛ گزارش خالی است و
              عددی نشان داده نمی‌شود.
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            در تاریخ {formatIsoJalali(report.as_of)} · جمع دریافتنی: {formatMoney(receivableTotal)} · جمع
            پرداختنی: {formatMoney(payableTotal)} · {faNumber(rows.length)} ردیف · حساب‌های خوانده‌شده:{" "}
            {report.accounts_read.join("، ")}
          </p>
          <Card>
            <CardContent className="pt-6">
              <BarChart
                items={agingChartItems(report.totals).map((item) => ({
                  label: item.label,
                  value: item.value,
                  display: formatMoney(item.value),
                }))}
              />
            </CardContent>
          </Card>
          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(row) => `${row.party_id ?? "none"}-${row.account_code}`}
            emptyText="سند بازی برای این فیلترها نیست."
            caption="قدمت هر سند از سررسید آن (یا تاریخ سند، اگر سررسید ندارد) تا تاریخ گزارش محاسبه می‌شود؛ سندهای تسویه‌شده تا آن تاریخ در گزارش نیستند."
          />
          <ReportFreshnessLine freshness={report.freshness} sourceLabel="ترایتون (مرجع)" />
        </>
      )}
    </div>
  );
}

function CheckbooksReport({ meta, checkbooks, failed, companyName, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; checkbooks: CheckbookResponse[] | null; failed: boolean; companyName: string | null; generatedAt: string }) {
  const totals = (checkbooks ?? []).reduce(
    (acc, c) => ({ used: acc.used + c.used_leaves, voided: acc.voided + c.voided_leaves, remaining: acc.remaining + c.remaining_leaves }),
    { used: 0, voided: 0, remaining: 0 },
  );
  const donut: ChartItem[] = [
    { label: "مصرف‌شده", value: totals.used },
    { label: "باطل‌شده", value: totals.voided },
    { label: "باقی‌مانده", value: totals.remaining },
  ];
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <p className="text-sm text-muted-foreground">
        شرکت: {companyName ?? "—"} · <Link href="/cheques/checkbooks" className="font-medium text-primary hover:underline">مشاهدهٔ دسته‌چک‌ها</Link>
      </p>
      {failed ? (
        <p className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive" role="alert">
          دریافت دسته‌چک‌ها ناموفق بود؛ از صفحهٔ دسته‌چک‌ها دوباره تلاش کنید.
        </p>
      ) : !checkbooks ? (
        <Skeleton className="h-40 w-full" />
      ) : checkbooks.length === 0 ? (
        <p className="text-sm text-muted-foreground">دسته‌چکی ثبت نشده است.</p>
      ) : (
        <>
          <Card>
            <CardContent className="pt-6">
              <DonutChart items={donut} />
            </CardContent>
          </Card>
          <DataTable
            columns={[
              { key: "name", header: "دسته‌چک", render: (c) => c.name },
              { key: "total", header: "کل برگ‌ها", render: (c) => faNumber(c.total_leaves) },
              { key: "used", header: "مصرف‌شده", render: (c) => faNumber(c.used_leaves) },
              { key: "voided", header: "باطل‌شده", render: (c) => faNumber(c.voided_leaves) },
              { key: "remaining", header: "باقی‌مانده", render: (c) => faNumber(c.remaining_leaves) },
              { key: "sayad", header: "ثبت‌شده در صیاد", render: (c) => (c.leaves_registered_in_sayad === null ? "نامشخص" : faNumber(c.leaves_registered_in_sayad)) },
            ]}
            rows={checkbooks}
            rowKey={(c) => c.checkbook_engine_id}
            emptyText="دسته‌چکی نیست."
          />
        </>
      )}
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function RegistryReport({ meta, cheques, todayJalali, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; todayJalali: string; generatedAt: string }) {
  const [state, setState] = React.useState("all");
  const [bank, setBank] = React.useState("");
  const [party, setParty] = React.useState("");
  const rows = applyChequeFilters(cheques, {
    bank: bank || undefined,
    party: party || undefined,
  }).filter((c) => state === "all" || c.state === state);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
        <Button variant="outline" size="sm" onClick={() => downloadCsv(`cheques-registry-${new Date().toISOString().slice(0, 10)}.csv`, buildCsv([
          { header: "شناسهٔ صیاد", value: (r) => r.sayad_id },
          { header: "وضعیت", value: (r) => r.state },
          { header: "نوع", value: (r) => r.kind },
          { header: "هدف", value: (r) => r.purpose },
          { header: "صادرکننده", value: (r) => r.issuer_name },
          { header: "بانک", value: (r) => r.issuer_bank },
          { header: "مبلغ (ریال)", value: (r) => r.amount_irr },
          { header: "سررسید (شمسی)", value: (r) => r.due_date },
          { header: "وضعیت صیاد", value: (r) => r.sayad_status },
          { header: "محل نگهداری", value: (r) => r.storage_location ?? "" },
          { header: "مبدأ", value: (r) => chequeSourceLabel(r) },
        ], rows))} disabled={rows.length === 0}>
          <Download className="size-4" aria-hidden />
          خروجی CSV
        </Button>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-2">
          <Label htmlFor="r9-state">وضعیت</Label>
          <Select value={state} onValueChange={(v) => setState(v ?? "all")}>
            <SelectTrigger id="r9-state">
              <SelectValue>{state === "all" ? "همهٔ وضعیت‌ها" : chequeStateLabel(state)}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همهٔ وضعیت‌ها</SelectItem>
              {CHEQUE_STATES.map((s) => (
                <SelectItem key={s} value={s}>{chequeStateLabel(s)}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <FilterText id="r9-bank" label="بانک" value={bank} onChange={setBank} />
        <FilterText id="r9-party" label="طرف حساب" value={party} onChange={setParty} />
      </div>
      <DataTable
        columns={chequeTableColumns(todayJalali)}
        rows={rows}
        rowKey={(r) => r.id}
        emptyText="با این فیلترها چکی پیدا نشد."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function MismatchesReport({ meta, cheques, reviewItems, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; reviewItems: ReviewItemResponse[]; generatedAt: string }) {
  const subtypeCounts = new Map<string, number>();
  for (const item of reviewItems) subtypeCounts.set(item.subtype ?? "بدون زیردسته", (subtypeCounts.get(item.subtype ?? "بدون زیردسته") ?? 0) + 1);
  const bars: ChartItem[] = [...subtypeCounts.entries()].map(([label, value]) => ({ label, value }));
  const flagged = cheques.filter((c) => c.mismatch_detail || c.receipt_missing_warning || c.sayad_unconfirmed);
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          <BarChart items={bars} unit="آیتم" />
          <p className="text-sm text-muted-foreground">
            {faNumber(reviewItems.length)} آیتم باز استثنای چک در صف بازبینی · روی مغایرت هرگز تأیید نکنید — رسیدگی در{" "}
            <Link href="/review-queue" className="font-medium text-primary hover:underline">صف بازبینی</Link> انجام می‌شود.
          </p>
        </CardContent>
      </Card>
      <h3 className="font-medium">چک‌های دارای پرچم مغایرت</h3>
      <DataTable
        columns={[
          { key: "sayad", header: "شناسهٔ صیاد", render: (c) => <Link href={`/cheques/${c.id}`} className="font-mono text-sm text-primary underline-offset-4 hover:underline" dir="ltr">{c.sayad_id}</Link> },
          { key: "flag", header: "پرچم", render: (c) => [c.mismatch_detail ? "مغایرت سه‌طرفه" : null, c.receipt_missing_warning ? "بدون رسید استعلام" : null, c.sayad_unconfirmed ? "ثبت‌نشده در صیاد" : null].filter(Boolean).join(" · ") },
          { key: "detail", header: "جزئیات مغایرت", render: (c) => c.mismatch_detail ?? "—" },
          { key: "amount", header: "مبلغ", render: (c) => <MoneyDisplay amountIrr={c.amount_irr} size="sm" /> },
        ]}
        rows={flagged}
        rowKey={(c) => c.id}
        emptyText="چکی با پرچم مغایرت نیست."
      />
      <h3 className="font-medium">آیتم‌های صف بازبینی (استثنای چک)</h3>
      <DataTable
        columns={[
          { key: "type", header: "زیردسته", render: (i) => i.subtype ?? "—" },
          { key: "reason", header: "دلیل", render: (i) => i.reason },
          { key: "severity", header: "شدت", render: (i) => i.severity },
          { key: "status", header: "وضعیت", render: (i) => i.status },
          { key: "age", header: "قدمت (ساعت)", render: (i) => faNumber(Math.round(i.age_hours)) },
        ]}
        rows={reviewItems}
        rowKey={(i) => i.id}
        emptyText="آیتم بازی در صف بازبینی نیست."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function UnreceivedReport({ meta, intake, deadlineDays, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; intake: ChequeResponse[]; deadlineDays: number; generatedAt: string }) {
  const states = ["waiting", "day-3", "day-n-minus-1", "expired"] as const;
  const bars: ChartItem[] = states.map((state) => ({
    label: INTAKE_REMINDER_LABELS[state],
    value: intake.filter((r) => intakeReminderState(r.age_days, deadlineDays) === state).length,
  }));
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          <BarChart items={bars} unit="چک" />
          <p className="text-sm text-muted-foreground">
            مهلت رسیدن برگ {faNumber(deadlineDays)} روز · {faNumber(intake.length)} چک در انتظار برگ ·{" "}
            <Link href="/cheques/intake" className="font-medium text-primary hover:underline">مشاهدهٔ صف intake</Link>
          </p>
        </CardContent>
      </Card>
      <DataTable
        columns={[
          { key: "sayad", header: "شناسهٔ صیاد", render: (r) => <Link href={`/cheques/${r.id}`} className="font-mono text-sm text-primary underline-offset-4 hover:underline" dir="ltr">{r.sayad_id}</Link> },
          { key: "issuer", header: "صادرکننده", render: (r) => r.issuer_name },
          { key: "amount", header: "مبلغ", render: (r) => <MoneyDisplay amountIrr={r.amount_irr} size="sm" /> },
          { key: "due", header: "سررسید", render: (r) => formatJalaliShort(r.due_date) },
          { key: "age", header: "قدمت", render: (r) => `${faNumber(r.age_days)} روز · ${INTAKE_REMINDER_LABELS[intakeReminderState(r.age_days, deadlineDays)]}` },
        ]}
        rows={intake}
        rowKey={(r) => r.id}
        emptyText="چک نرسیده‌ای نیست."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function ClearingMatchReport({ meta, cheques, trend, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; trend: MonthlyMatchTrendResponse; generatedAt: string }) {
  const [statementId, setStatementId] = React.useState("");
  const [bankKey, setBankKey] = React.useState("none");
  const [runResult, setRunResult] = React.useState<string | null>(null);
  const [runError, setRunError] = React.useState<string | null>(null);
  const [running, setRunning] = React.useState(false);
  // Donut + trend line render FROM the stored monthly history
  // (GET /cheques/reports/monthly-match-trend, US-553) — never
  // recomputed from the registry rows at read time.
  const donut: ChartItem[] = [
    { label: clearingConfidenceLabel("exact"), value: trend.total_exact },
    { label: clearingConfidenceLabel("probable"), value: trend.total_probable },
    { label: clearingConfidenceLabel("ambiguous"), value: trend.total_ambiguous },
    { label: clearingConfidenceLabel("unmatched"), value: trend.total_unmatched },
  ];
  const trendPoints = trend.months.map((month) => ({
    label: monthLabel(month.month_jalali),
    value: Number(month.match_rate),
  }));
  const unmatched = cheques.filter((c) => c.state === "in_clearing" && (c.clearing_confidence === null || c.clearing_confidence === "unmatched"));
  const runClearing = async (event: React.FormEvent) => {
    event.preventDefault();
    const id = Number(statementId);
    if (!Number.isInteger(id) || id <= 0) {
      setRunError("شناسهٔ صورت‌حساب باید عدد صحیح مثبت باشد.");
      return;
    }
    setRunning(true);
    setRunError(null);
    setRunResult(null);
    try {
      const result = await api.runChequeClearing(id, { bank_key: bankKey === "none" ? null : bankKey });
      setRunResult(
        `سطرها: ${faNumber(result.total_lines)} · دقیق: ${faNumber(result.exact_count)} · محتمل: ${faNumber(result.probable_count)} · مبهم: ${faNumber(result.ambiguous_count)} · بی‌تطبیق: ${faNumber(result.unmatched_count)} · وصول‌شده: ${faNumber(result.cleared_count)} · آیتم بازبینی: ${faNumber(result.review_items_filed)}`,
      );
    } catch (err: unknown) {
      setRunError(persianApiErrorMessage(err, "اجرای تطبیق وصول ناموفق بود."));
    } finally {
      setRunning(false);
    }
  };
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          {trend.total_lines === 0 ? (
            <p className="text-sm text-muted-foreground">
              هنوز هیچ تطبیق وصلی اجرا نشده است؛ پس از نخستین اجرای تطبیق، دونات نرخ و روند
              ماهانه اینجا از تاریخچهٔ ذخیره‌شده نمایش داده می‌شود.
            </p>
          ) : (
            <>
              <DonutChart items={donut} />
              <p className="text-sm text-muted-foreground">
                نرخ تطبیق کلی {faNumber(Number(trend.overall_match_rate))}٪ روی {faNumber(trend.total_lines)} سطر
                تطبیق‌یافته در {faNumber(trend.months.length)} ماه است؛ هر اجرای تطبیق وصول،
                نتیجه‌اش را در تاریخچهٔ ماهانه ثبت می‌کند.
              </p>
              <LineChart points={trendPoints} valueLabel="نرخ تطبیق ماهانه" />
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-start text-muted-foreground">
                      <th scope="col" className="py-2 pe-2 text-start font-medium">ماه</th>
                      <th scope="col" className="py-2 pe-2 text-start font-medium">دقیق</th>
                      <th scope="col" className="py-2 pe-2 text-start font-medium">محتمل</th>
                      <th scope="col" className="py-2 pe-2 text-start font-medium">مبهم</th>
                      <th scope="col" className="py-2 pe-2 text-start font-medium">بی‌تطبیق</th>
                      <th scope="col" className="py-2 pe-2 text-start font-medium">همهٔ سطرها</th>
                      <th scope="col" className="py-2 text-start font-medium">نرخ تطبیق</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trend.months.map((month) => (
                      <tr key={month.month_jalali} className="border-b border-border last:border-0">
                        <th scope="row" className="py-2 pe-2 text-start font-medium">{monthLabel(month.month_jalali)}</th>
                        <td className="py-2 pe-2">{faNumber(month.exact_count)}</td>
                        <td className="py-2 pe-2">{faNumber(month.probable_count)}</td>
                        <td className="py-2 pe-2">{faNumber(month.ambiguous_count)}</td>
                        <td className="py-2 pe-2">{faNumber(month.unmatched_count)}</td>
                        <td className="py-2 pe-2">{faNumber(month.total_lines)}</td>
                        <td className="py-2 font-medium">{faNumber(Number(month.match_rate))}٪</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <ReportFreshnessLine freshness={trend.freshness} sourceLabel="تاریخچهٔ ذخیره‌شدهٔ تطبیق" />
            </>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">اجرای تطبیق وصول روی یک صورت‌حساب</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={runClearing} className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="cm-statement">شناسهٔ صورت‌حساب (موتور)</Label>
              <Input id="cm-statement" dir="ltr" inputMode="numeric" value={statementId} onChange={(e) => setStatementId(e.target.value)} required />
            </div>
            <div className="space-y-2">
              <Label htmlFor="cm-bank">بانک</Label>
              <Select value={bankKey} onValueChange={(v) => setBankKey(v ?? "none")}>
                <SelectTrigger id="cm-bank">
                  <SelectValue>{bankKey === "none" ? "نامشخص" : "ملی (سقف اطمینان: محتمل)"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">نامشخص</SelectItem>
                  <SelectItem value="melli">ملی (سقف اطمینان: محتمل)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-end">
              <Button type="submit" disabled={running}>{running ? "در حال تطبیق…" : "اجرای تطبیق"}</Button>
            </div>
          </form>
          {runResult ? <p className="mt-4 text-sm" role="status">{runResult}</p> : null}
          {runError ? <p className="mt-4 text-sm text-destructive" role="alert">{runError}</p> : null}
        </CardContent>
      </Card>
      <h3 className="font-medium">چک‌های بی‌تطبیق در وصول</h3>
      <DataTable
        columns={chequeTableColumns()}
        rows={unmatched}
        rowKey={(r) => r.id}
        emptyText="چک بی‌تطبیقی در وصول نیست."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function InquiryLedgerReport({ meta, ledger, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; ledger: InquiryLedgerResponse; generatedAt: string }) {
  const [auditResult, setAuditResult] = React.useState<string | null>(null);
  const [auditError, setAuditError] = React.useState<string | null>(null);
  const [auditing, setAuditing] = React.useState(false);
  const byChannel: ChartItem[] = Object.entries(ledger.by_channel).map(([channel, count]) => ({
    label: channel,
    value: count,
  }));
  const colors: ChartItem[] = Object.entries(ledger.color_distribution).map(([code, count]) => ({
    label: issuerColorLabel(Number(code)),
    value: count,
  }));
  const runAudit = async () => {
    setAuditing(true);
    setAuditError(null);
    setAuditResult(null);
    try {
      const result = await api.runReceiptRuleAudit();
      setAuditResult(`بررسی شد: ${faNumber(result.checked)} · بدون رسید: ${faNumber(result.missing)} · آیتم تشکیل‌شده: ${faNumber(result.items_filed)}`);
    } catch (err: unknown) {
      setAuditError(persianApiErrorMessage(err, "حسابرسی قانون رسید ناموفق بود."));
    } finally {
      setAuditing(false);
    }
  };
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-6 pt-6">
          <p className="text-sm">
            کل رسیدها: {faNumber(ledger.total_receipts)} · هزینهٔ تجمیعی: <MoneyDisplay amountIrr={ledger.total_cost_irr} size="sm" />
          </p>
          <div>
            <h3 className="mb-3 font-medium">به تفکیک کانال</h3>
            <BarChart items={byChannel} unit="رسید" />
          </div>
          <div>
            <h3 className="mb-3 font-medium">توزیع رنگ</h3>
            <DonutChart items={colors} />
          </div>
          <p className="text-xs text-muted-foreground">
            هزینهٔ پیامک ۳٬۵۰۰ ریال، احرازینو رنگ ۱۷٬۰۰۰ ریال و بررسی صیاد ۶۹٬۰۰۰ ریال است؛ فینوتک طبق قرارداد.
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">حسابرسی قانون رسید اجباری</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            برای چک‌های «نزد ما» بدون رسید استعلام، آیتم «رسید استعلام نیست» در صف بازبینی تشکیل می‌شود (تکرار، آیتم جدید نمی‌سازد).
          </p>
          <Button variant="outline" onClick={runAudit} disabled={auditing}>
            {auditing ? "در حال حسابرسی…" : "اجرای حسابرسی"}
          </Button>
          {auditResult ? <p className="text-sm" role="status">{auditResult}</p> : null}
          {auditError ? <p className="text-sm text-destructive" role="alert">{auditError}</p> : null}
        </CardContent>
      </Card>
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function CashForecastReport({ meta, cheques, todayJalali, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; todayJalali: string; generatedAt: string }) {
  const weeks = cashForecast(cheques, todayJalali);
  const points = weeks.map((w) => ({
    label: formatJalaliShort(w.weekStart).slice(5),
    inflow: w.inflowIrr / 10,
    outflow: w.outflowIrr / 10,
  }));
  const totalIn = weeks.reduce((s, w) => s + w.inflowIrr, 0);
  const totalOut = weeks.reduce((s, w) => s + w.outflowIrr, 0);
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          <AreaChart points={points} inflowLabel="ورودی موردانتظار (وصول دریافتی‌ها)" outflowLabel="خروجی موردانتظار (پاس پرداختی‌ها)" />
          <p className="text-sm text-muted-foreground">
            ۱۲ هفتهٔ آینده · ورودی موردانتظار: <MoneyDisplay amountIrr={totalIn} size="sm" /> · خروجی موردانتظار: <MoneyDisplay amountIrr={totalOut} size="sm" /> · پشتوانه در پیش‌بینی نمی‌آید.
          </p>
        </CardContent>
      </Card>
      <DataTable
        columns={[
          { key: "week", header: "هفته (از)", render: (w) => formatJalaliShort(w.weekStart) },
          { key: "in", header: "ورودی موردانتظار", render: (w) => <MoneyDisplay amountIrr={w.inflowIrr} size="sm" /> },
          { key: "in-count", header: "تعداد ورودی", render: (w) => faNumber(w.inflowCount) },
          { key: "out", header: "خروجی موردانتظار", render: (w) => <MoneyDisplay amountIrr={w.outflowIrr} size="sm" /> },
          { key: "out-count", header: "تعداد خروجی", render: (w) => faNumber(w.outflowCount) },
          { key: "net", header: "خالص", render: (w) => <MoneyDisplay amountIrr={w.inflowIrr - w.outflowIrr} size="sm" /> },
        ]}
        rows={weeks}
        rowKey={(w) => w.weekStart}
        emptyText="چکی در ۱۲ هفتهٔ آینده سررسید نمی‌شود."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function PartyStatementReport({ meta, cheques, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; generatedAt: string }) {
  const parties = [...new Set(cheques.filter((c) => c.kind === "received").map((c) => c.issuer_name))].sort();
  const [party, setParty] = React.useState("");
  const rows = cheques.filter((c) => c.kind === "received" && c.issuer_name === party);
  const held = rows.filter((c) => c.state === "held");
  const inClearing = rows.filter((c) => c.state === "in_clearing");
  const bounced = rows.filter((c) => c.state === "bounced");
  const withColor = [...rows].filter((c) => c.issuer_color_code !== null).sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
  const latest = withColor[0];
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <div className="space-y-2">
        <Label htmlFor="r15-party">مشتری (صادرکنندهٔ چک دریافتی)</Label>
        <Select value={party} onValueChange={(v) => setParty(v ?? "")}>
          <SelectTrigger id="r15-party" className="w-80">
            <SelectValue placeholder="انتخاب مشتری">{party === "" ? "انتخاب مشتری" : party}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {parties.map((name) => (
              <SelectItem key={name} value={name}>{name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {party === "" ? (
        <p className="text-sm text-muted-foreground">برای تصمیم پیش‌پذیرش چک بعدی، مشتری را انتخاب کنید.</p>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">چک‌های نزد ما</p>
                <p className="mt-1"><MoneyDisplay amountIrr={sumIrr(held)} size="md" /></p>
                <p className="text-xs text-muted-foreground">{faNumber(held.length)} چک</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">در جریان وصول</p>
                <p className="mt-1"><MoneyDisplay amountIrr={sumIrr(inClearing)} size="md" /></p>
                <p className="text-xs text-muted-foreground">{faNumber(inClearing.length)} چک</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">برگشتی تاریخی</p>
                <p className="mt-1"><MoneyDisplay amountIrr={sumIrr(bounced)} size="md" /></p>
                <p className="text-xs text-muted-foreground">{faNumber(bounced.length)} چک</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <p className="text-sm text-muted-foreground">آخرین رنگ (با تاریخ)</p>
                <p className="mt-1 font-medium">
                  {latest ? `${issuerColorLabel(latest.issuer_color_code)} · ${formatIsoDate(latest.updated_at)}` : "استعلام نشده"}
                </p>
                <p className="text-xs text-muted-foreground">رنگ فقط سابقه است، نه تضمین.</p>
              </CardContent>
            </Card>
          </div>
          <DataTable
            columns={chequeTableColumns()}
            rows={rows}
            rowKey={(r) => r.id}
            emptyText="این مشتری چکی ندارد."
          />
        </>
      )}
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function StorageReport({ meta, cheques, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; generatedAt: string }) {
  const physical = cheques.filter((c) => ["registered", "held", "in_clearing"].includes(c.state));
  const byLocation = new Map<string, ChequeResponse[]>();
  for (const row of physical) {
    const key = row.storage_location ?? "unset";
    byLocation.set(key, [...(byLocation.get(key) ?? []), row]);
  }
  const donut: ChartItem[] = [...byLocation.entries()].map(([key, rows]) => ({
    label: key === "unset" ? "ثبت نشده" : storageLocationLabel(key),
    value: rows.length,
  }));
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <Card>
        <CardContent className="space-y-4 pt-6">
          <DonutChart items={donut} />
          <p className="text-sm text-muted-foreground">
            فقط چک‌هایی که برگ فیزیکی‌شان نزد ماست (ثبت‌شده، نزد ما، در وصول)؛ شمارش صندوق با همین فهرست توافق می‌کند.
          </p>
        </CardContent>
      </Card>
      <DataTable
        columns={[
          { key: "location", header: "محل نگهداری", render: (row: { location: string; rows: ChequeResponse[] }) => (row.location === "unset" ? "ثبت نشده" : storageLocationLabel(row.location)) },
          { key: "count", header: "تعداد برگ", render: (row) => faNumber(row.rows.length) },
          { key: "total", header: "مجموع مبالغ", render: (row) => <MoneyDisplay amountIrr={sumIrr(row.rows)} size="sm" /> },
        ]}
        rows={[...byLocation.entries()].map(([location, rows]) => ({ location, rows }))}
        rowKey={(row) => row.location}
        emptyText="برگی نزد ما نیست."
      />
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}

function TransferHistoryReport({ meta, cheques, generatedAt }: { meta: (typeof CHEQUE_REPORTS)[number]; cheques: ChequeResponse[]; generatedAt: string }) {
  const [selectedId, setSelectedId] = React.useState("");
  const [detail, setDetail] = React.useState<ChequeDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [detailError, setDetailError] = React.useState<string | null>(null);
  const endorsed = cheques.filter((c) => c.state === "endorsed" || c.endorsee_name !== null);
  const loadDetail = async (id: string) => {
    setSelectedId(id);
    if (!id) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    setDetailError(null);
    try {
      setDetail(await api.getCheque(id));
    } catch (err: unknown) {
      setDetailError(persianApiErrorMessage(err, "دریافت تاریخچهٔ چک ناموفق بود."));
    } finally {
      setDetailLoading(false);
    }
  };
  return (
    <div className="space-y-6">
      <ReportHeader number={meta.number} title={meta.title} meta={`منبع: ${meta.source} · نمودار: ${meta.chart} · فیلترها: ${meta.filters}`} />
      <DataTable
        columns={[
          { key: "sayad", header: "شناسهٔ صیاد", render: (c) => <Link href={`/cheques/${c.id}`} className="font-mono text-sm text-primary underline-offset-4 hover:underline" dir="ltr">{c.sayad_id}</Link> },
          { key: "from", header: "ظهرنویس", render: (c) => c.issuer_name },
          { key: "to", header: "گیرندهٔ نهایی", render: (c) => c.endorsee_name ?? "—" },
          { key: "type", header: "نوع گیرنده", render: (c) => (c.endorsee_type === "supplier" ? "تأمین‌کننده" : c.endorsee_type === "other" ? "شخص دیگر" : "—") },
          { key: "ref", header: "رسید انتقال صیاد", render: (c) => <span dir="ltr">{c.sayad_transfer_reference ?? "—"}</span> },
          { key: "amount", header: "مبلغ", render: (c) => <MoneyDisplay amountIrr={c.amount_irr} size="sm" /> },
        ]}
        rows={endorsed}
        rowKey={(c) => c.id}
        emptyText="چکی ظهرنویسی نشده است."
      />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">خط زمانی گذارها و نگهداری یک چک</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="r17-cheque">چک</Label>
            <Select value={selectedId} onValueChange={(v) => { void loadDetail(v ?? ""); }}>
              <SelectTrigger id="r17-cheque" className="w-80">
                <SelectValue placeholder="انتخاب چک">
                  {selectedId === "" ? "انتخاب چک" : (cheques.find((c) => c.id === selectedId)?.sayad_id ?? selectedId)}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {cheques.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    <span dir="ltr">{c.sayad_id}</span> — {c.issuer_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {detailLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : detailError ? (
            <p className="text-sm text-destructive" role="alert">{detailError}</p>
          ) : detail ? (
            <div className="space-y-4">
              <ol className="space-y-2">
                {lifecycleSteps(detail).map((step) => (
                  <li key={step.key} className="text-sm">
                    {step.done ? "✓ " : "○ "}
                    {step.label}
                    {step.date ? ` · ${formatJalaliShort(step.date)}` : ""}
                  </li>
                ))}
              </ol>
              <div>
                <h4 className="mb-2 font-medium">لاگ نگهداری</h4>
                {detail.custody_entries.length === 0 ? (
                  <p className="text-sm text-muted-foreground">جابه‌جایی نگهداری ثبت نشده است.</p>
                ) : (
                  <ul className="space-y-2 text-sm">
                    {detail.custody_entries.map((entry) => (
                      <li key={entry.id}>
                        {entry.from_location ? `${storageLocationLabel(entry.from_location)} ← ` : ""}
                        {storageLocationLabel(entry.to_location)} · {entry.reason} · {entry.actor} · {formatIsoDateTime(entry.at)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <p className="text-sm text-muted-foreground">
                سند مرتبط هر گذار: {Object.keys(detail.move_ids).length > 0 ? Object.entries(detail.move_ids).map(([key, id]) => `${key}: #${id}`).join(" · ") : "سندی ثبت نشده است."}
              </p>
            </div>
          ) : null}
        </CardContent>
      </Card>
      <FreshnessLine generatedAt={generatedAt} />
    </div>
  );
}
