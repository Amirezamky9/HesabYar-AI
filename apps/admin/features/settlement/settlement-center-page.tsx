/**
 * Settlement center report (US-525) — the §11.2 payment-channel
 * settlement report (§3.10): per-channel summary, tranche status,
 * aging, cash-flow forecast, fee accuracy (§3.6) and the exception
 * queue, with §11.1 report metadata.
 *
 * Data is live from the backend via the BFF (payments, tranches, fee
 * records, events, exceptions, imports); aggregation is pure
 * (settlement-report.ts). Nothing here is fake: an empty installation
 * renders honest empty states, and a truncated fetch says so.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import {
  ApiError,
  api,
  type GatewayPaymentResponse,
  type PaymentChannelResponse,
  type SettlementEventResponse,
  type SettlementExceptionResponse,
  type SettlementFeeRecordResponse,
  type SettlementFileImportResponse,
  type SettlementTrancheResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { fa, faNumber } from "@/lib/utils";
import {
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  StatusBadge,
  type DataTableColumn,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  AGING_BUCKET_LABELS,
  escalationLevelLabel,
  escalationLevelTone,
  fileTypeLabel,
  importStatusLabel,
  importStatusTone,
  isValidJalaliPeriod,
  jalaliPeriodLabel,
  jalaliPeriodOf,
  normalizeJalaliPeriod,
  settlementExceptionTypeLabel,
  todayIsoTehran,
  trancheStateLabel,
  trancheStateTone,
} from "./settlement-data";
import {
  ageOutstanding,
  buildFeeAccuracyReport,
  buildSettlementWidgets,
  channelsById,
  channelNameOf,
  forecastTranches,
  summarizeChannels,
  summarizeTrancheStates,
  totalChannelSummary,
  type ChannelSummary,
  type FeeAccuracyRow,
  type FeeVarianceRow,
} from "./settlement-report";
import { fetchAllPages } from "./settlement-load";

const ALL_CHANNELS = "__all__";

/** §11.1 report metadata (static for this report version). */
const REPORT_VERSION = "۱٫۰";
const CALCULATION_VERSION = "US-525/1.0";

const CHANNEL_COLUMNS: DataTableColumn<ChannelSummary>[] = [
  { key: "channel", header: "کانال", render: (row) => row.channelName },
  {
    key: "gross",
    header: "فروش ناخالص",
    render: (row) => <span className="tabular-nums">{formatMoney(row.grossIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "fee",
    header: "کارمزد",
    render: (row) => <span className="tabular-nums">{formatMoney(row.feeIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "vat",
    header: "مالیات کارمزد",
    render: (row) => <span className="tabular-nums">{formatMoney(row.feeVatIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "deductions",
    header: "کسورات",
    render: (row) => <span className="tabular-nums">{formatMoney(row.deductionsIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "net",
    header: "خالص قابل تسویه",
    render: (row) => <span className="tabular-nums">{formatMoney(row.netReceivableIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "confirmed",
    header: "تأییدشده",
    render: (row) => <span className="tabular-nums">{formatMoney(row.confirmedNetIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "received",
    header: "دریافتی",
    render: (row) => <span className="tabular-nums">{formatMoney(row.receivedNetIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "outstanding",
    header: "ماندهٔ تسویه‌نشده",
    render: (row) => <span className="tabular-nums">{formatMoney(row.outstandingNetIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
];

const FEE_COLUMNS: DataTableColumn<FeeAccuracyRow>[] = [
  { key: "channel", header: "کانال", render: (row) => row.channelName },
  { key: "period", header: "دورهٔ فروش", render: (row) => jalaliPeriodLabel(row.salesPeriod) },
  {
    key: "expectedFee",
    header: "کارمزد موردانتظار",
    render: (row) => <span className="tabular-nums">{formatMoney(row.expectedFeeIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "actualFee",
    header: "کارمزد واقعی",
    render: (row) =>
      row.actualFeeIrr === null ? "—" : <span className="tabular-nums">{formatMoney(row.actualFeeIrr)}</span>,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "feeVariance",
    header: "مغایرت کارمزد",
    render: (row) =>
      row.feeVarianceIrr === null ? (
        "—"
      ) : (
        <span className="tabular-nums">
          {row.feeVarianceIrr < 0 ? "−" : ""}
          {formatMoney(Math.abs(row.feeVarianceIrr))}
        </span>
      ),
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "effectiveRate",
    header: "نرخ مؤثر واقعی",
    render: (row) =>
      row.effectiveRatePercent === null ? "—" : `${fa(row.effectiveRatePercent.toFixed(2).replace(".", "٫"))}٪`,
    className: "text-end",
    headerClassName: "text-end",
  },
  {
    key: "contractRate",
    header: "نرخ قرارداد",
    render: (row) =>
      row.contractRateMixed
        ? "چند نرخ"
        : row.contractRatePercent === null
          ? "—"
          : `${fa(String(row.contractRatePercent).replace(".", "٫"))}٪`,
    className: "text-end",
    headerClassName: "text-end",
  },
];

export function SettlementCenterPage() {
  const router = useRouter();
  const [channels, setChannels] = React.useState<PaymentChannelResponse[]>([]);
  const [payments, setPayments] = React.useState<GatewayPaymentResponse[]>([]);
  const [tranches, setTranches] = React.useState<SettlementTrancheResponse[]>([]);
  const [feeRecords, setFeeRecords] = React.useState<SettlementFeeRecordResponse[]>([]);
  const [events, setEvents] = React.useState<SettlementEventResponse[]>([]);
  const [exceptions, setExceptions] = React.useState<SettlementExceptionResponse[]>([]);
  const [imports, setImports] = React.useState<SettlementFileImportResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [truncated, setTruncated] = React.useState(false);
  const [generatedAt, setGeneratedAt] = React.useState<Date | null>(null);
  const [channelFilter, setChannelFilter] = React.useState(ALL_CHANNELS);
  const [periodInput, setPeriodInput] = React.useState("");
  const [periodFilter, setPeriodFilter] = React.useState("");
  const [periodError, setPeriodError] = React.useState<string | null>(null);

  const todayIso = React.useMemo(() => todayIsoTehran(), []);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const channelKey = channelFilter === ALL_CHANNELS ? undefined : channelFilter;
      const salesPeriod = periodFilter === "" ? undefined : periodFilter;
      const [channelPage, paymentResult, trancheResult, feeResult, eventResult, exceptionResult, importResult] =
        await Promise.all([
          api.listPaymentChannels({ limit: 100 }),
          fetchAllPages((cursor) =>
            api.listGatewayPayments({ channel_key: channelKey, sales_period: salesPeriod, limit: 100, cursor }),
          ),
          fetchAllPages((cursor) =>
            api.listSettlementTranches({ channel_key: channelKey, sales_period: salesPeriod, limit: 100, cursor }),
          ),
          fetchAllPages((cursor) =>
            api.listSettlementFeeRecords({ channel_key: channelKey, limit: 100, cursor }),
          ),
          fetchAllPages((cursor) =>
            api.listSettlementEvents({ channel_key: channelKey, limit: 100, cursor }),
          ),
          fetchAllPages((cursor) =>
            api.listSettlementExceptions({ status: "open", limit: 100, cursor }),
          ),
          fetchAllPages((cursor) =>
            api.listSettlementImports({ channel_key: channelKey, limit: 100, sort: "-created_at", cursor }),
          ),
        ]);
      setChannels(channelPage.items);
      setPayments(paymentResult.items);
      setTranches(trancheResult.items);
      setFeeRecords(feeResult.items);
      setEvents(eventResult.items);
      setExceptions(exceptionResult.items);
      setImports(importResult.items.slice(0, 10));
      setTruncated(
        paymentResult.truncated ||
          trancheResult.truncated ||
          feeResult.truncated ||
          eventResult.truncated ||
          exceptionResult.truncated ||
          importResult.truncated,
      );
      setGeneratedAt(new Date());
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [channelFilter, periodFilter, router]);

  // Loading state is set by load() from a timer callback, never
  // synchronously inside this effect (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  const channelsMap = React.useMemo(() => channelsById(channels), [channels]);
  const channelRows = React.useMemo(
    () => summarizeChannels(payments, tranches, events, channelsMap),
    [payments, tranches, events, channelsMap],
  );
  const total = React.useMemo(() => totalChannelSummary(channelRows), [channelRows]);
  const stateRows = React.useMemo(() => summarizeTrancheStates(tranches), [tranches]);
  const agingRows = React.useMemo(() => ageOutstanding(tranches, todayIso), [tranches, todayIso]);
  const forecastRows = React.useMemo(() => forecastTranches(tranches, todayIso), [tranches, todayIso]);
  const feeReport = React.useMemo(
    () => buildFeeAccuracyReport(feeRecords, payments, channelsMap),
    [feeRecords, payments, channelsMap],
  );
  const widgets = React.useMemo(
    () => buildSettlementWidgets(tranches, exceptions, todayIso),
    [tranches, exceptions, todayIso],
  );

  const applyPeriod = () => {
    const trimmed = periodInput.trim();
    if (trimmed === "") {
      setPeriodError(null);
      setPeriodFilter("");
      return;
    }
    if (!isValidJalaliPeriod(trimmed)) {
      setPeriodError("دوره را به قالب «۱۴۰۵/۰۸» وارد کنید.");
      return;
    }
    setPeriodError(null);
    setPeriodFilter(normalizeJalaliPeriod(trimmed));
  };

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(error, "دسترسی به مرکز تسویه ممکن نیست.")}
      />
    );
  }

  const scheduleLink = (params: string) => `/settlements/schedule${params}`;
  const hasData = payments.length > 0 || tranches.length > 0;

  const varianceColumns: DataTableColumn<FeeVarianceRow>[] = [
    {
      key: "payment",
      header: "پرداخت",
      render: (row) => (
        <Link href={`/settlements/payments/${row.paymentId}`} className="text-primary underline-offset-4 hover:underline">
          مشاهدهٔ پرداخت
        </Link>
      ),
    },
    { key: "channel", header: "کانال", render: (row) => row.channelName },
    { key: "period", header: "دورهٔ فروش", render: (row) => jalaliPeriodLabel(row.salesPeriod) },
    {
      key: "feeVariance",
      header: "مغایرت کارمزد",
      render: (row) => (
        <span className="tabular-nums">
          {row.feeVarianceIrr < 0 ? "−" : ""}
          {formatMoney(Math.abs(row.feeVarianceIrr))}
        </span>
      ),
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "vatVariance",
      header: "مغایرت مالیات کارمزد",
      render: (row) => (
        <span className="tabular-nums">
          {row.vatVarianceIrr < 0 ? "−" : ""}
          {formatMoney(Math.abs(row.vatVarianceIrr))}
        </span>
      ),
      className: "text-end",
      headerClassName: "text-end",
    },
    { key: "tranche", header: "قسط", render: (row) => (row.trancheId ? "قسط مشخص" : "کل پرداخت") },
  ];

  const exceptionColumns: DataTableColumn<SettlementExceptionResponse>[] = [
    { key: "type", header: "نوع", render: (row) => settlementExceptionTypeLabel(row.exception_type) },
    {
      key: "severity",
      header: "شدت",
      render: (row) => (
        <StatusBadge
          status={row.severity}
          label={row.severity === "critical" ? "بحرانی" : row.severity === "warning" ? "هشدار" : "اطلاع"}
          tone={row.severity === "critical" ? "destructive" : row.severity === "warning" ? "warning" : "outline"}
        />
      ),
    },
    {
      key: "escalation",
      header: "تشدید",
      render: (row) => (
        <StatusBadge
          status={`escalation-${row.escalation_level}`}
          label={escalationLevelLabel(row.escalation_level)}
          tone={escalationLevelTone(row.escalation_level)}
        />
      ),
    },
    {
      key: "expected",
      header: "مبلغ موردانتظار",
      render: (row) =>
        row.expected_amount_irr === null ? "—" : <span className="tabular-nums">{formatMoney(row.expected_amount_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "actual",
      header: "مبلغ واقعی",
      render: (row) =>
        row.actual_amount_irr === null ? "—" : <span className="tabular-nums">{formatMoney(row.actual_amount_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "created",
      header: "تاریخ",
      render: (row) => (row.created_at ? formatIsoDate(row.created_at.slice(0, 10)) : "—"),
    },
  ];

  const importColumns: DataTableColumn<SettlementFileImportResponse>[] = [
    {
      key: "channel",
      header: "کانال",
      render: (row) => channelNameOf(row.payment_channel_id, channelsMap),
    },
    { key: "period", header: "دوره", render: (row) => jalaliPeriodLabel(row.period_jalali) },
    { key: "type", header: "نوع فایل", render: (row) => fileTypeLabel(row.file_type) },
    {
      key: "status",
      header: "وضعیت",
      render: (row) => (
        <StatusBadge status={row.status} label={importStatusLabel(row.status)} tone={importStatusTone(row.status)} />
      ),
    },
    { key: "matched", header: "منطبق", render: (row) => faNumber(row.matched_count), className: "text-end", headerClassName: "text-end" },
    { key: "new", header: "جدید", render: (row) => faNumber(row.new_count), className: "text-end", headerClassName: "text-end" },
    { key: "errors", header: "خطا", render: (row) => faNumber(row.error_count), className: "text-end", headerClassName: "text-end" },
    {
      key: "detail",
      header: "",
      render: (row) => (
        <Link href={`/settlements/imports/${row.id}`} className="text-primary underline-offset-4 hover:underline">
          جزئیات
        </Link>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">مرکز تسویه</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            گزارش تسویهٔ کانال‌های پرداخت (§۱۱٫۲): قسط‌های موردانتظار، کارمزد،
            مغایرت‌ها و ایمپورت فایل‌ها در یک جا.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link href="/settlements/schedule" className={buttonVariants({ variant: "outline" })}>
            برنامهٔ قسط‌ها
          </Link>
          <Link href="/settlements/contracts" className={buttonVariants({ variant: "outline" })}>
            قراردادها
          </Link>
          <Link href="/settlements/import" className={buttonVariants({ variant: "default" })}>
            ایمپورت فایل تسویه
          </Link>
        </div>
      </div>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "بارگذاری گزارش تسویه ناموفق بود.")}
          onRetry={() => void load()}
        />
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>فیلتر گزارش</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="report-channel">کانال پرداخت</Label>
              <Select value={channelFilter} onValueChange={(v) => setChannelFilter(v ?? ALL_CHANNELS)}>
                <SelectTrigger id="report-channel">
                  {/* Explicit label: Base UI resolves the trigger label
                  from popup items (mounted on first open), so a
                  controlled value would otherwise display its raw code. */}
                  <SelectValue>
                    {channelFilter === ALL_CHANNELS
                      ? "همهٔ کانال‌ها"
                      : (channels.find((c) => c.channel_key === channelFilter)?.display_name_fa ?? channelFilter)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL_CHANNELS}>همهٔ کانال‌ها</SelectItem>
                  {channels.map((c) => (
                    <SelectItem key={c.id} value={c.channel_key}>
                      {c.display_name_fa}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="report-period">دورهٔ فروش (جلالی)</Label>
              <Input
                id="report-period"
                value={periodInput}
                onChange={(e) => setPeriodInput(e.target.value)}
                placeholder={jalaliPeriodOf()}
                inputMode="numeric"
                dir="ltr"
                aria-describedby={periodError ? "report-period-error" : undefined}
              />
              {periodError ? (
                <p id="report-period-error" className="text-sm text-destructive">
                  {periodError}
                </p>
              ) : null}
            </div>
            <div className="flex items-end">
              <Button onClick={applyPeriod} variant="outline">
                اعمال فیلتر
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {!hasData && !error ? (
        <Card>
          <CardHeader>
            <CardTitle>هنوز دادهٔ تسویه‌ای نیست</CardTitle>
            <CardDescription>
              هنوز پرداختی از هیچ درگاهی ثبت نشده است. پس از ثبت نخستین پرداخت،
              خلاصهٔ کانال‌ها، برنامهٔ قسط‌ها و دقت کارمزد اینجا دیده می‌شود.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            <Link href="/settlements/import" className={buttonVariants({ variant: "default" })}>
              ایمپورت فایل تسویه
            </Link>
            <Link href="/settlements/contracts" className={buttonVariants({ variant: "outline" })}>
              مدیریت قراردادها
            </Link>
          </CardContent>
        </Card>
      ) : null}

      {truncated ? (
        <p className="text-sm text-muted-foreground">
          فهرست‌ها بیش از ۱٬۰۰۰ ردیف دارند و گزارش روی ۱٬۰۰۰ ردیف نخست هر فهرست
          محاسبه شده است؛ برای دقت کامل، فیلتر کانال یا دوره را تنگ‌تر کنید.
        </p>
      ) : null}

      {hasData ? (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">قسط‌های سررسید این هفته</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                <p className="text-2xl font-bold tabular-nums">{formatMoney(widgets.dueThisWeekIrr)}</p>
                <p className="text-sm text-muted-foreground">{faNumber(widgets.dueThisWeekCount)} قسط</p>
                <Link href={scheduleLink("?state=due")} className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
                  مشاهده در برنامهٔ قسط‌ها
                  <ArrowLeft className="size-4" aria-hidden />
                </Link>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">قسط‌های سررسیدگذشته</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                <p className="text-2xl font-bold tabular-nums">{formatMoney(widgets.overdueIrr)}</p>
                <p className="text-sm text-muted-foreground">
                  {faNumber(widgets.overdueCount)} قسط
                  {widgets.oldestOverdueDays !== null
                    ? ` · قدیمی‌ترین ${faNumber(widgets.oldestOverdueDays)} روز`
                    : ""}
                </p>
                <Link href={scheduleLink("?state=overdue")} className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
                  مشاهده در برنامهٔ قسط‌ها
                  <ArrowLeft className="size-4" aria-hidden />
                </Link>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">در راه</CardTitle>
                <CardDescription>تأییدشده ولی نرسیده به بانک</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                <p className="text-2xl font-bold tabular-nums">{formatMoney(widgets.inTransitIrr)}</p>
                <Link href={scheduleLink("?state=confirmed")} className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
                  مشاهده در برنامهٔ قسط‌ها
                  <ArrowLeft className="size-4" aria-hidden />
                </Link>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">پیش‌بینی تسویهٔ ۳۰ روز آینده</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                <p className="text-2xl font-bold tabular-nums">{formatMoney(widgets.nextMonthIrr)}</p>
                <p className="text-sm text-muted-foreground">{faNumber(widgets.nextMonthCount)} قسط موردانتظار</p>
                <Link href={scheduleLink("")} className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
                  مشاهده در برنامهٔ قسط‌ها
                  <ArrowLeft className="size-4" aria-hidden />
                </Link>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>خلاصهٔ هر کانال و هر دوره</CardTitle>
              <CardDescription>
                فروش ناخالص، کارمزد، مالیات کارمزد، کسورات، خالص قابل تسویه،
                تأییدشده، دریافتی و ماندهٔ تسویه‌نشده — به‌ازای هر درگاه.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <DataTable
                columns={CHANNEL_COLUMNS}
                rows={channelRows}
                rowKey={(row) => row.channelKey}
                emptyText="در این فیلتر پرداختی نیست."
              />
              <dl className="grid gap-4 border-t pt-4 sm:grid-cols-3">
                <div>
                  <dt className="text-sm text-muted-foreground">جمع فروش ناخالص</dt>
                  <dd className="mt-1 font-medium tabular-nums">{formatMoney(total.grossIrr)}</dd>
                </div>
                <div>
                  <dt className="text-sm text-muted-foreground">جمع خالص قابل تسویه</dt>
                  <dd className="mt-1 font-medium tabular-nums">{formatMoney(total.netReceivableIrr)}</dd>
                </div>
                <div>
                  <dt className="text-sm text-muted-foreground">جمع ماندهٔ تسویه‌نشده</dt>
                  <dd className="mt-1 font-medium tabular-nums">{formatMoney(total.outstandingNetIrr)}</dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>وضعیت قسط‌ها</CardTitle>
                <CardDescription>تعداد و مبلغ قسط‌ها در هر وضعیت.</CardDescription>
              </CardHeader>
              <CardContent>
                <DataTable
                  columns={[
                    {
                      key: "state",
                      header: "وضعیت",
                      render: (row) => (
                        <StatusBadge status={row.state} label={trancheStateLabel(row.state)} tone={trancheStateTone(row.state)} />
                      ),
                    },
                    { key: "count", header: "تعداد", render: (row) => faNumber(row.count), className: "text-end", headerClassName: "text-end" },
                    {
                      key: "net",
                      header: "خالص",
                      render: (row) => <span className="tabular-nums">{formatMoney(row.netIrr)}</span>,
                      className: "text-end",
                      headerClassName: "text-end",
                    },
                    {
                      key: "outstanding",
                      header: "مانده",
                      render: (row) => <span className="tabular-nums">{formatMoney(row.outstandingIrr)}</span>,
                      className: "text-end",
                      headerClassName: "text-end",
                    },
                  ]}
                  rows={stateRows}
                  rowKey={(row) => row.state}
                  emptyText="قسطی نیست."
                />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>پیری مانده‌ها</CardTitle>
                <CardDescription>ماندهٔ تسویه‌نشده به تفکیک روزهای گذشته از موعد.</CardDescription>
              </CardHeader>
              <CardContent>
                <DataTable
                  columns={[
                    { key: "bucket", header: "بازه", render: (row) => AGING_BUCKET_LABELS[row.bucket] },
                    { key: "count", header: "تعداد", render: (row) => faNumber(row.count), className: "text-end", headerClassName: "text-end" },
                    {
                      key: "outstanding",
                      header: "مانده",
                      render: (row) => <span className="tabular-nums">{formatMoney(row.outstandingIrr)}</span>,
                      className: "text-end",
                      headerClassName: "text-end",
                    },
                  ]}
                  rows={agingRows}
                  rowKey={(row) => row.bucket}
                  emptyText="ماندهٔ سررسیدگذشته‌ای نیست."
                />
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>پیش‌بینی جریان نقد</CardTitle>
              <CardDescription>قسط‌های موردانتظار ۳۰ تا ۱۲۰ روز آینده (انباشته).</CardDescription>
            </CardHeader>
            <CardContent>
              <DataTable
                columns={[
                  { key: "horizon", header: "افق", render: (row) => `${faNumber(row.horizonDays)} روز آینده` },
                  { key: "count", header: "تعداد قسط", render: (row) => faNumber(row.count), className: "text-end", headerClassName: "text-end" },
                  {
                    key: "net",
                    header: "خالص موردانتظار",
                    render: (row) => <span className="tabular-nums">{formatMoney(row.netIrr)}</span>,
                    className: "text-end",
                    headerClassName: "text-end",
                  },
                ]}
                rows={forecastRows}
                rowKey={(row) => row.horizonDays}
                emptyText="قسط موردانتظاری نیست."
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>دقت کارمزد</CardTitle>
              <CardDescription>
                کارمزد موردانتظار در برابر واقعی به‌ازای هر درگاه و دوره؛ نرخ
                مؤثر واقعی در برابر نرخ قرارداد.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <DataTable
                columns={FEE_COLUMNS}
                rows={feeReport.rows}
                rowKey={(row) => `${row.channelKey}/${row.salesPeriod}`}
                emptyText="هنوز رکورد کارمزدی نیست."
              />
              {feeReport.variances.length > 0 ? (
                <div className="space-y-2">
                  <h3 className="text-base font-semibold">فهرست مغایرت‌های کارمزد</h3>
                  <DataTable
                    columns={varianceColumns}
                    rows={feeReport.variances}
                    rowKey={(row) => row.feeRecordId}
                    emptyText="مغایرت کارمزدی نیست."
                  />
                </div>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>استثناهای باز تسویه</CardTitle>
              <CardDescription>
                {faNumber(widgets.openExceptionCount)} استثنای باز
                {widgets.openExceptionCount > 0 ? (
                  <>
                    {" — "}
                    <Link href="/review-queue?type=settlement_variance" className="text-primary underline-offset-4 hover:underline">
                      رسیدگی در صف بازبینی
                    </Link>
                  </>
                ) : null}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <DataTable
                columns={exceptionColumns}
                rows={exceptions.slice(0, 10)}
                rowKey={(row) => row.id}
                emptyText="استثنای بازی نیست."
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>ایمپورت‌های اخیر</CardTitle>
              <CardDescription>فایل‌های تسویهٔ ایمپورت‌شده و وضعیت هر بچ.</CardDescription>
            </CardHeader>
            <CardContent>
              <DataTable
                columns={importColumns}
                rows={imports}
                rowKey={(row) => row.id}
                emptyText="هنوز فایلی ایمپورت نشده است."
              />
            </CardContent>
          </Card>

          <p className="text-sm text-muted-foreground">
            منبع: ترایتون (مرجع حسابداری) + حساب‌یار (رجیستری تسویه) · تولید:{" "}
            {generatedAt ? formatIsoDate(generatedAt.toISOString().slice(0, 10)) : "—"}
            {periodFilter !== "" ? ` · دورهٔ فروش: ${jalaliPeriodLabel(periodFilter)}` : ""}
            {channelFilter !== ALL_CHANNELS ? ` · کانال: ${channels.find((c) => c.channel_key === channelFilter)?.display_name_fa ?? channelFilter}` : ""}
            {" · "}نسخهٔ گزارش {REPORT_VERSION} · نسخهٔ محاسبه {CALCULATION_VERSION}
          </p>
        </>
      ) : null}
    </div>
  );
}
