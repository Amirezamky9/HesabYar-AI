/**
 * Tranche schedule view (US-525) — every channel's expected tranches
 * per channel/period: expected/due/confirmed/received/settled with
 * overdue alerts (7/30/60-day escalation from the daily tick).
 *
 * Data is live via the BFF. The tranche list IS the schedule API the
 * 5ح cash calendar will consume (`due_from`/`due_to` window filters);
 * this page is its first consumer. Exceptions resolve in the UNIFIED
 * review queue (settlement_variance) — this page links there rather
 * than growing a second resolution flow.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ApiError,
  api,
  type GatewayPaymentResponse,
  type PaymentChannelResponse,
  type SettlementExceptionResponse,
  type SettlementTrancheResponse,
  type TickResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { fa, faNumber } from "@/lib/utils";
import {
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  StatusBadge,
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  TRANCHE_STATES,
  daysOverdue,
  escalationLevelLabel,
  escalationLevelTone,
  isTrancheOpen,
  isValidJalaliPeriod,
  jalaliPeriodLabel,
  normalizeJalaliPeriod,
  todayIsoTehran,
  trancheStateLabel,
  trancheStateTone,
} from "./settlement-data";
import { channelsById, channelNameOf } from "./settlement-report";
import { fetchAllPages } from "./settlement-load";

const ALL = "__all__";
const PAGE_SIZE = 20;

export interface SettlementScheduleInitialFilters {
  channel?: string;
  period?: string;
  state?: string;
}

export function SettlementSchedulePage({ initial }: { initial?: SettlementScheduleInitialFilters }) {
  const router = useRouter();
  const [channels, setChannels] = React.useState<PaymentChannelResponse[]>([]);
  const [tranches, setTranches] = React.useState<SettlementTrancheResponse[]>([]);
  const [paymentsById, setPaymentsById] = React.useState<Map<string, GatewayPaymentResponse>>(new Map());
  const [lateExceptions, setLateExceptions] = React.useState<SettlementExceptionResponse[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [channelFilter, setChannelFilter] = React.useState(initial?.channel ?? ALL);
  const [periodInput, setPeriodInput] = React.useState(initial?.period ?? "");
  const [periodFilter, setPeriodFilter] = React.useState(initial?.period ?? "");
  const [periodError, setPeriodError] = React.useState<string | null>(null);
  const [stateFilter, setStateFilter] = React.useState(initial?.state ?? ALL);
  const [tickResult, setTickResult] = React.useState<TickResponse | null>(null);
  const [tickBusy, setTickBusy] = React.useState(false);
  const [tickError, setTickError] = React.useState<string | null>(null);
  const [writeOffTranche, setWriteOffTranche] = React.useState<SettlementTrancheResponse | null>(null);
  const [approvedBy, setApprovedBy] = React.useState("");
  const [writeOffReason, setWriteOffReason] = React.useState("");
  const [writeOffBusy, setWriteOffBusy] = React.useState(false);
  const [writeOffError, setWriteOffError] = React.useState<string | null>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  const todayIso = React.useMemo(() => todayIsoTehran(), []);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const channelKey = channelFilter === ALL ? undefined : channelFilter;
      const salesPeriod = periodFilter === "" ? undefined : periodFilter;
      const state = stateFilter === ALL ? undefined : stateFilter;
      const [channelPage, tranchePage, paymentResult, lateResult] = await Promise.all([
        api.listPaymentChannels({ limit: 100 }),
        api.listSettlementTranches({
          channel_key: channelKey,
          sales_period: salesPeriod,
          state,
          limit: PAGE_SIZE,
        }),
        fetchAllPages((cursor) =>
          api.listGatewayPayments({ channel_key: channelKey, sales_period: salesPeriod, limit: 100, cursor }),
        ),
        fetchAllPages((cursor) =>
          api.listSettlementExceptions({ exception_type: "late_tranche", status: "open", limit: 100, cursor }),
        ),
      ]);
      setChannels(channelPage.items);
      setTranches(tranchePage.items);
      setNextCursor(tranchePage.next_cursor);
      setPaymentsById(new Map(paymentResult.items.map((p) => [p.id, p])));
      setLateExceptions(lateResult.items);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [channelFilter, periodFilter, stateFilter, router]);

  // Loading state is set by load() from a timer callback, never
  // synchronously inside this effect (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load, reloadKey]);

  const channelsMap = React.useMemo(() => channelsById(channels), [channels]);

  const handleNextPage = async () => {
    if (!nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await api.listSettlementTranches({
        channel_key: channelFilter === ALL ? undefined : channelFilter,
        sales_period: periodFilter === "" ? undefined : periodFilter,
        state: stateFilter === ALL ? undefined : stateFilter,
        limit: PAGE_SIZE,
        cursor: nextCursor,
      });
      setTranches((prev) => [...prev, ...page.items]);
      setNextCursor(page.next_cursor);
    } catch (err: unknown) {
      setError(err);
    } finally {
      setLoadingMore(false);
    }
  };

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

  const handleTick = async () => {
    setTickBusy(true);
    setTickError(null);
    setTickResult(null);
    try {
      const result = await api.runSettlementTick();
      setTickResult(result);
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setTickError(persianApiErrorMessage(err, "اجرای تیک روزانه ناموفق بود."));
    } finally {
      setTickBusy(false);
    }
  };

  const handleWriteOff = async () => {
    if (!writeOffTranche) return;
    if (approvedBy.trim() === "" || writeOffReason.trim() === "") {
      setWriteOffError("تأییدکننده و دلیل از قلم افتادن الزامی‌اند.");
      return;
    }
    setWriteOffBusy(true);
    setWriteOffError(null);
    try {
      await api.writeOffTranche(writeOffTranche.id, {
        approved_by: approvedBy.trim(),
        reason: writeOffReason.trim(),
      });
      setWriteOffTranche(null);
      setApprovedBy("");
      setWriteOffReason("");
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setWriteOffError(persianApiErrorMessage(err, "از قلم افتادن قسط ناموفق بود."));
    } finally {
      setWriteOffBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(error, "دسترسی به برنامهٔ قسط‌ها ممکن نیست.")}
      />
    );
  }

  const columns: DataTableColumn<SettlementTrancheResponse>[] = [
    {
      key: "channel",
      header: "کانال",
      render: (row) => {
        const payment = paymentsById.get(row.gateway_payment_id);
        return payment ? channelNameOf(payment.payment_channel_id, channelsMap) : "—";
      },
    },
    {
      key: "payment",
      header: "پرداخت",
      render: (row) => (
        <Link href={`/settlements/payments/${row.gateway_payment_id}`} className="text-primary underline-offset-4 hover:underline">
          {paymentsById.get(row.gateway_payment_id)?.gateway_transaction_id ?? "مشاهده"}
        </Link>
      ),
    },
    {
      key: "leg",
      header: "قسط",
      render: (row) => `${faNumber(row.tranche_index)} از ${faNumber(row.tranche_count)}`,
    },
    { key: "period", header: "دورهٔ فروش", render: (row) => jalaliPeriodLabel(row.sales_period_jalali) },
    { key: "due", header: "سررسید", render: (row) => formatIsoDate(row.expected_date) },
    {
      key: "net",
      header: "خالص",
      render: (row) => <span className="tabular-nums">{formatMoney(row.net_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "confirmed",
      header: "تأییدشده",
      render: (row) => <span className="tabular-nums">{formatMoney(row.confirmed_net_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "received",
      header: "دریافتی",
      render: (row) => <span className="tabular-nums">{formatMoney(row.received_net_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "outstanding",
      header: "مانده",
      render: (row) => <span className="tabular-nums">{formatMoney(row.outstanding_net_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "state",
      header: "وضعیت",
      render: (row) => (
        <StatusBadge status={row.state} label={trancheStateLabel(row.state)} tone={trancheStateTone(row.state)} />
      ),
    },
    {
      key: "overdue",
      header: "روزهای گذشته",
      render: (row) => {
        const overdue = daysOverdue(row.expected_date, todayIso);
        if (!isTrancheOpen(row.state) || overdue <= 0) return "—";
        return faNumber(overdue);
      },
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "actions",
      header: "",
      render: (row) =>
        isTrancheOpen(row.state) ? (
          <Button variant="ghost" size="sm" onClick={() => setWriteOffTranche(row)}>
            از قلم افتادن
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">برنامهٔ قسط‌های تسویه</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            قسط‌های موردانتظار هر کانال و دوره: موردانتظار، سررسیدشده، تأییدشده،
            دریافتی و تسویه‌شده — با هشدار سررسیدگذشته.
          </p>
        </div>
        <Button onClick={handleTick} disabled={tickBusy} variant="outline">
          {tickBusy ? "در حال اجرا…" : "اجرای تیک روزانه"}
        </Button>
      </div>

      {error ? (
        <ErrorState
          message={persianApiErrorMessage(error, "بارگذاری برنامهٔ قسط‌ها ناموفق بود.")}
          onRetry={() => setReloadKey((k) => k + 1)}
        />
      ) : null}

      {tickResult ? (
        <p className="text-sm text-muted-foreground">
          تیک روزانه: {faNumber(tickResult.moved_to_due)} قسط به سررسید رسید،{" "}
          {faNumber(tickResult.moved_to_overdue)} قسط سررسیدگذشت،{" "}
          {faNumber(tickResult.exceptions_created)} استثنای جدید و{" "}
          {faNumber(tickResult.exceptions_escalated)} تشدید.
        </p>
      ) : null}
      {tickError ? <p className="text-sm text-destructive">{tickError}</p> : null}

      <Card>
        <CardHeader>
          <CardTitle>فیلتر برنامه</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1.5">
              <Label htmlFor="schedule-channel">کانال پرداخت</Label>
              <Select value={channelFilter} onValueChange={(v) => setChannelFilter(v ?? ALL)}>
                <SelectTrigger id="schedule-channel">
                  {/* Explicit label: Base UI resolves the trigger label
                  from popup items (mounted on first open), so a
                  controlled value would otherwise display its raw code. */}
                  <SelectValue>
                    {channelFilter === ALL
                      ? "همهٔ کانال‌ها"
                      : (channels.find((c) => c.channel_key === channelFilter)?.display_name_fa ?? channelFilter)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>همهٔ کانال‌ها</SelectItem>
                  {channels.map((c) => (
                    <SelectItem key={c.id} value={c.channel_key}>
                      {c.display_name_fa}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="schedule-period">دورهٔ فروش (جلالی)</Label>
              <Input
                id="schedule-period"
                value={periodInput}
                onChange={(e) => setPeriodInput(e.target.value)}
                placeholder="۱۴۰۵/۰۸"
                inputMode="numeric"
                dir="ltr"
                aria-describedby={periodError ? "schedule-period-error" : undefined}
              />
              {periodError ? (
                <p id="schedule-period-error" className="text-sm text-destructive">
                  {periodError}
                </p>
              ) : null}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="schedule-state">وضعیت قسط</Label>
              <Select value={stateFilter} onValueChange={(v) => setStateFilter(v ?? ALL)}>
                <SelectTrigger id="schedule-state">
                  <SelectValue>
                    {stateFilter === ALL ? "همهٔ وضعیت‌ها" : trancheStateLabel(stateFilter)}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>همهٔ وضعیت‌ها</SelectItem>
                  {TRANCHE_STATES.map((state) => (
                    <SelectItem key={state} value={state}>
                      {trancheStateLabel(state)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-end">
              <Button onClick={applyPeriod} variant="outline">
                اعمال فیلتر
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {lateExceptions.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>هشدار سررسیدگذشته</CardTitle>
            <CardDescription>
              {faNumber(lateExceptions.length)} قسط دیرکرد باز — تشدید در ۷، ۳۰ و ۶۰
              روز. رسیدگی در{" "}
              <Link href="/review-queue?type=settlement_variance" className="text-primary underline-offset-4 hover:underline">
                صف بازبینی
              </Link>
              .
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={[
                {
                  key: "tranche",
                  header: "قسط",
                  render: (row) =>
                    row.tranche_id ? (
                      <Link href={`/settlements/payments/${row.gateway_payment_id ?? ""}`} className="text-primary underline-offset-4 hover:underline">
                        مشاهدهٔ پرداخت
                      </Link>
                    ) : (
                      "—"
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
                  key: "created",
                  header: "تاریخ",
                  render: (row) => (row.created_at ? formatIsoDate(row.created_at.slice(0, 10)) : "—"),
                },
              ]}
              rows={lateExceptions.slice(0, 10)}
              rowKey={(row) => row.id}
              emptyText="قسط دیرکردی نیست."
            />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>قسط‌ها</CardTitle>
          <CardDescription>
            {tranches.length > 0
              ? `${faNumber(tranches.length)} قسط در این فیلتر`
              : "در این فیلتر قسطی نیست."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={columns}
            rows={tranches}
            rowKey={(row) => row.id}
            emptyText="در این فیلتر قسطی نیست."
            hasNextPage={nextCursor !== null}
            onNextPage={handleNextPage}
            loadingMore={loadingMore}
          />
        </CardContent>
      </Card>

      <Dialog
        open={writeOffTranche !== null}
        onOpenChange={(open) => {
          if (!open) {
            setWriteOffTranche(null);
            setWriteOffError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>از قلم افتادن قسط</DialogTitle>
            <DialogDescription>
              {writeOffTranche
                ? `قسط ${fa(writeOffTranche.tranche_index)} از ${fa(writeOffTranche.tranche_count)} — ماندهٔ ${formatMoney(writeOffTranche.outstanding_net_irr)}. این کار نیازمند تأیید مدیر است و در حسابرسی ثبت می‌شود.`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="writeoff-approver">تأییدکننده</Label>
              <Input
                id="writeoff-approver"
                value={approvedBy}
                onChange={(e) => setApprovedBy(e.target.value)}
                placeholder="نام تأییدکننده"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="writeoff-reason">دلیل</Label>
              <Textarea
                id="writeoff-reason"
                value={writeOffReason}
                onChange={(e) => setWriteOffReason(e.target.value)}
                placeholder="چرا این قسط از قلم می‌افتد؟"
              />
            </div>
            {writeOffError ? <p className="text-sm text-destructive">{writeOffError}</p> : null}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setWriteOffTranche(null)} disabled={writeOffBusy}>
              انصراف
            </Button>
            <Button onClick={handleWriteOff} disabled={writeOffBusy}>
              {writeOffBusy ? "در حال ثبت…" : "از قلم افتادن"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
