/**
 * Gateway payment detail (US-525) — drill-down from the settlement
 * report and schedule: one payment, its tranche schedule, its fee
 * record (expected vs actual), its posting ledger and its exceptions.
 *
 * Actions: cancel/refund (with reason) and sales-period correction
 * (with reason). Both are human decisions with an audit trail.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import {
  ApiError,
  api,
  type PaymentScheduleResponse,
  type SettlementExceptionResponse,
  type SettlementFeeRecordResponse,
  type SettlementPostingResponse,
  type SettlementTrancheResponse,
} from "@/lib/api-client";
import { formatIsoDate } from "@/lib/format-date";
import { formatMoney } from "@/lib/format-money";
import { faNumber } from "@/lib/utils";
import {
  Button,
  buttonVariants,
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
  Skeleton,
  StatusBadge,
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import {
  isNotFoundError,
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import {
  escalationLevelLabel,
  formatPercentString,
  isValidJalaliPeriod,
  jalaliPeriodLabel,
  normalizeJalaliPeriod,
  paymentSourceLabel,
  paymentStatusLabel,
  paymentStatusTone,
  postingKindLabel,
  settlementExceptionTypeLabel,
  trancheStateLabel,
  trancheStateTone,
} from "./settlement-data";
import { channelNameOf, channelsById, type ChannelsById } from "./settlement-report";
import { fetchAllPages } from "./settlement-load";

export function SettlementPaymentDetail({ paymentId }: { paymentId: string }) {
  const router = useRouter();
  const [schedule, setSchedule] = React.useState<PaymentScheduleResponse | null>(null);
  const [channelsMap, setChannelsMap] = React.useState<ChannelsById>(new Map());
  const [feeRecords, setFeeRecords] = React.useState<SettlementFeeRecordResponse[]>([]);
  const [postings, setPostings] = React.useState<SettlementPostingResponse[]>([]);
  const [exceptions, setExceptions] = React.useState<SettlementExceptionResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);

  const [cancelOpen, setCancelOpen] = React.useState(false);
  const [cancelReason, setCancelReason] = React.useState("");
  const [cancelBusy, setCancelBusy] = React.useState(false);
  const [cancelError, setCancelError] = React.useState<string | null>(null);

  const [reassignOpen, setReassignOpen] = React.useState(false);
  const [newPeriod, setNewPeriod] = React.useState("");
  const [reassignReason, setReassignReason] = React.useState("");
  const [reassignBusy, setReassignBusy] = React.useState(false);
  const [reassignError, setReassignError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [paymentSchedule, channelPage, feePage, postingList, exceptionResult] = await Promise.all([
        api.getGatewayPayment(paymentId),
        api.listPaymentChannels({ limit: 100 }),
        api.listSettlementFeeRecords({ gateway_payment_id: paymentId, limit: 100 }),
        api.listPaymentPostings(paymentId),
        fetchAllPages((cursor) => api.listSettlementExceptions({ status: "open", limit: 100, cursor })),
      ]);
      setSchedule(paymentSchedule);
      setChannelsMap(channelsById(channelPage.items));
      setFeeRecords(feePage.items);
      setPostings(postingList);
      setExceptions(exceptionResult.items.filter((e) => e.gateway_payment_id === paymentId));
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.replace("/login");
        return;
      }
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [paymentId, router]);

  // Loading state is set by load() from a timer callback, never
  // synchronously inside this effect (react-hooks/set-state-in-effect).
  React.useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load, reloadKey]);

  const handleCancel = async () => {
    if (cancelReason.trim() === "") {
      setCancelError("دلیل لغو الزامی است.");
      return;
    }
    setCancelBusy(true);
    setCancelError(null);
    try {
      await api.cancelGatewayPayment(paymentId, { reason: cancelReason.trim() });
      setCancelOpen(false);
      setCancelReason("");
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setCancelError(persianApiErrorMessage(err, "لغو پرداخت ناموفق بود."));
    } finally {
      setCancelBusy(false);
    }
  };

  const handleReassign = async () => {
    if (!isValidJalaliPeriod(newPeriod.trim())) {
      setReassignError("دوره را به قالب «۱۴۰۵/۰۸» وارد کنید.");
      return;
    }
    if (reassignReason.trim() === "") {
      setReassignError("دلیل اصلاح دوره الزامی است.");
      return;
    }
    setReassignBusy(true);
    setReassignError(null);
    try {
      await api.reassignSalesPeriod(paymentId, {
        new_sales_period_jalali: normalizeJalaliPeriod(newPeriod.trim()),
        reason: reassignReason.trim(),
      });
      setReassignOpen(false);
      setNewPeriod("");
      setReassignReason("");
      setReloadKey((k) => k + 1);
    } catch (err: unknown) {
      setReassignError(persianApiErrorMessage(err, "اصلاح دورهٔ فروش ناموفق بود."));
    } finally {
      setReassignBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="space-y-6" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState
        message={persianApiErrorMessage(error, "دسترسی به این پرداخت ممکن نیست.")}
      />
    );
  }

  if (error && isNotFoundError(error)) {
    return <NotFoundState message="این پرداخت پیدا نشد." />;
  }

  if (error || !schedule) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "بارگذاری پرداخت ناموفق بود.")}
        onRetry={() => setReloadKey((k) => k + 1)}
      />
    );
  }

  const payment = schedule.payment;
  const feeRecord = feeRecords.find((r) => r.tranche_id === null) ?? feeRecords[0] ?? null;
  const canCancel = payment.status === "scheduled" || payment.status === "settling";

  const trancheColumns: DataTableColumn<SettlementTrancheResponse>[] = [
    { key: "leg", header: "قسط", render: (row) => `${faNumber(row.tranche_index)} از ${faNumber(row.tranche_count)}` },
    { key: "due", header: "سررسید", render: (row) => formatIsoDate(row.expected_date) },
    {
      key: "gross",
      header: "ناخالص",
      render: (row) => <span className="tabular-nums">{formatMoney(row.gross_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    {
      key: "fee",
      header: "کارمزد",
      render: (row) => <span className="tabular-nums">{formatMoney(row.fee_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
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
  ];

  const postingColumns: DataTableColumn<SettlementPostingResponse>[] = [
    { key: "kind", header: "نوع", render: (row) => postingKindLabel(row.kind) },
    {
      key: "status",
      header: "وضعیت",
      render: (row) => (
        <StatusBadge
          status={row.status}
          label={row.status === "posted" ? "ثبت‌شده" : "پیش‌نویس"}
          tone={row.status === "posted" ? "success" : "outline"}
        />
      ),
    },
    {
      key: "amount",
      header: "مبلغ",
      render: (row) => <span className="tabular-nums">{formatMoney(row.amount_irr)}</span>,
      className: "text-end",
      headerClassName: "text-end",
    },
    { key: "move", header: "سند ترایتون", render: (row) => (row.tryton_move_id === null ? "—" : faNumber(row.tryton_move_id)) },
    { key: "reason", header: "دلیل", render: (row) => row.reason ?? "—" },
    { key: "created", header: "تاریخ", render: (row) => (row.created_at ? formatIsoDate(row.created_at.slice(0, 10)) : "—") },
  ];

  const exceptionColumns: DataTableColumn<SettlementExceptionResponse>[] = [
    { key: "type", header: "نوع", render: (row) => settlementExceptionTypeLabel(row.exception_type) },
    {
      key: "escalation",
      header: "تشدید",
      render: (row) => escalationLevelLabel(row.escalation_level),
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
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link
          href="/settlements/schedule"
          className={buttonVariants({ variant: "ghost", size: "icon" })}
          aria-label="بازگشت به برنامهٔ قسط‌ها"
        >
          <ArrowRight className="size-4" aria-hidden />
        </Link>
        <div>
          <h1 className="text-2xl font-bold">
            پرداخت <span dir="ltr">{payment.gateway_transaction_id}</span>
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {channelNameOf(payment.payment_channel_id, channelsMap)} · دورهٔ{" "}
            {jalaliPeriodLabel(payment.sales_period_jalali)} · {paymentSourceLabel(payment.source)}
          </p>
        </div>
        <StatusBadge status={payment.status} label={paymentStatusLabel(payment.status)} tone={paymentStatusTone(payment.status)} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>مبالغ پرداخت</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-3">
            <div>
              <dt className="text-sm text-muted-foreground">فروش ناخالص</dt>
              <dd className="mt-1 font-medium tabular-nums">{formatMoney(payment.gross_irr)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">کارمزد</dt>
              <dd className="mt-1 font-medium tabular-nums">{formatMoney(payment.fee_irr)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">مالیات کارمزد</dt>
              <dd className="mt-1 font-medium tabular-nums">{formatMoney(payment.fee_vat_irr)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">خالص قابل تسویه</dt>
              <dd className="mt-1 font-medium tabular-nums">{formatMoney(payment.net_irr)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">نرخ کارمزد اعمال‌شده</dt>
              <dd className="mt-1 font-medium tabular-nums">{formatPercentString(payment.fee_rate_snapshot)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">نرخ مالیات کارمزد اعمال‌شده</dt>
              <dd className="mt-1 font-medium tabular-nums">{formatPercentString(payment.fee_vat_rate_snapshot)}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">تأیید درگاه</dt>
              <dd className="mt-1 font-medium">{formatIsoDate(payment.gateway_confirmed_at.slice(0, 10))}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">سفارش فروشگاه</dt>
              <dd className="mt-1 font-medium">{payment.store_order_id ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">کسور برگشت</dt>
              <dd className="mt-1 font-medium tabular-nums">{formatMoney(payment.refunds_deducted_irr)}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>برنامهٔ قسط‌ها</CardTitle>
          <CardDescription>
            {faNumber(schedule.tranches.length)} قسط از روی نسخهٔ فعال قرارداد در
            زمان ثبت پرداخت ساخته شد.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={trancheColumns}
            rows={schedule.tranches}
            rowKey={(row) => row.id}
            emptyText="این پرداخت قسطی ندارد."
          />
        </CardContent>
      </Card>

      {feeRecord ? (
        <Card>
          <CardHeader>
            <CardTitle>رکورد کارمزد</CardTitle>
            <CardDescription>
              کارمزد موردانتظار از قرارداد در برابر کارمزد واقعی از فایل/پنل
              درگاه.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 sm:grid-cols-3">
              <div>
                <dt className="text-sm text-muted-foreground">کارمزد موردانتظار</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(feeRecord.expected_fee_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">کارمزد واقعی</dt>
                <dd className="mt-1 font-medium tabular-nums">
                  {feeRecord.actual_fee_irr === null ? "—" : formatMoney(feeRecord.actual_fee_irr)}
                </dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مغایرت کارمزد</dt>
                <dd className="mt-1 font-medium tabular-nums">
                  {feeRecord.fee_variance_irr === null ? "—" : formatMoney(feeRecord.fee_variance_irr)}
                </dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مالیات موردانتظار</dt>
                <dd className="mt-1 font-medium tabular-nums">{formatMoney(feeRecord.expected_vat_irr)}</dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مالیات واقعی</dt>
                <dd className="mt-1 font-medium tabular-nums">
                  {feeRecord.actual_vat_irr === null ? "—" : formatMoney(feeRecord.actual_vat_irr)}
                </dd>
              </div>
              <div>
                <dt className="text-sm text-muted-foreground">مغایرت مالیات</dt>
                <dd className="mt-1 font-medium tabular-nums">
                  {feeRecord.vat_variance_irr === null ? "—" : formatMoney(feeRecord.vat_variance_irr)}
                </dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>سندهای حسابداری تسویه</CardTitle>
          <CardDescription>دفتر حسابرسی سندهای §۳٫۵ برای این پرداخت.</CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={postingColumns}
            rows={postings}
            rowKey={(row) => row.id}
            emptyText="هنوز سندی برای این پرداخت ثبت نشده است."
          />
        </CardContent>
      </Card>

      {exceptions.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>استثناهای باز این پرداخت</CardTitle>
            <CardDescription>
              رسیدگی در{" "}
              <Link href="/review-queue?type=settlement_variance" className="text-primary underline-offset-4 hover:underline">
                صف بازبینی
              </Link>
              .
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={exceptionColumns}
              rows={exceptions}
              rowKey={(row) => row.id}
              emptyText="استثنای بازی نیست."
            />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>اقدام‌ها</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-4">
          <Button variant="outline" onClick={() => setReassignOpen(true)}>
            اصلاح دورهٔ فروش
          </Button>
          <Button variant="outline" onClick={() => setCancelOpen(true)} disabled={!canCancel}>
            لغو / برگشت پرداخت
          </Button>
        </CardContent>
      </Card>

      <Dialog open={cancelOpen} onOpenChange={(open) => { if (!open) { setCancelOpen(false); setCancelError(null); } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>لغو / برگشت پرداخت</DialogTitle>
            <DialogDescription>
              قسط‌های باز لغو می‌شوند؛ مبلغی که درگاه قبلاً پرداخته از تسویه‌های
              بعدی کسر می‌شود.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="cancel-reason">دلیل لغو</Label>
            <Textarea
              id="cancel-reason"
              value={cancelReason}
              onChange={(e) => setCancelReason(e.target.value)}
              placeholder="چرا این پرداخت لغو می‌شود؟"
            />
          </div>
          {cancelError ? <p className="text-sm text-destructive">{cancelError}</p> : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCancelOpen(false)} disabled={cancelBusy}>
              انصراف
            </Button>
            <Button onClick={handleCancel} disabled={cancelBusy}>
              {cancelBusy ? "در حال ثبت…" : "لغو پرداخت"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={reassignOpen} onOpenChange={(open) => { if (!open) { setReassignOpen(false); setReassignError(null); } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>اصلاح دورهٔ فروش</DialogTitle>
            <DialogDescription>
              قسط‌های باز از روی دورهٔ جدید دوباره ساخته می‌شوند؛ قسط‌های
              تأیید/دریافت‌شده دست نمی‌خورند.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="reassign-period">دورهٔ جدید (جلالی)</Label>
              <Input
                id="reassign-period"
                value={newPeriod}
                onChange={(e) => setNewPeriod(e.target.value)}
                placeholder="۱۴۰۵/۰۸"
                inputMode="numeric"
                dir="ltr"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="reassign-reason">دلیل اصلاح</Label>
              <Textarea
                id="reassign-reason"
                value={reassignReason}
                onChange={(e) => setReassignReason(e.target.value)}
                placeholder="چرا دورهٔ فروش اصلاح می‌شود؟"
              />
            </div>
            {reassignError ? <p className="text-sm text-destructive">{reassignError}</p> : null}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setReassignOpen(false)} disabled={reassignBusy}>
              انصراف
            </Button>
            <Button onClick={handleReassign} disabled={reassignBusy}>
              {reassignBusy ? "در حال ثبت…" : "اصلاح دوره"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
