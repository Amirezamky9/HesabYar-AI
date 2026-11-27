/**
 * Installment plan detail (US-545, Phase 5-5 ۵ج).
 *
 * One plan: schedule table with computed leg states, receipts with
 * their allocations, linked cheque / BNPL references, the payoff
 * quote, the plan-change flow (early settlement — full or partial —
 * plus the amendment audit history), and the SMS reminder log with
 * the manual trigger.
 *
 * BNPL plans never show finance income, dunning or early
 * settlement — the gateway owns collection. A BNPL receipt can be
 * rejected (gateway cancellation) with a reason; its legs return
 * to `expected` and a review item is filed.
 *
 * Every receipt enforces the linkage guard: the original invoice
 * AND the accounting document (journal move id) are both required.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, api, type InstallmentPlanDetailResponse, type PayoffQuoteResponse, type ReminderLogResponse, type ScheduleAmendmentResponse, type SmsProviderResponse, type InstallmentContractResponse } from "@/lib/api-client";
import {
  AmountInput,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Checkbox,
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
  Textarea,
  type DataTableColumn,
} from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { formatIsoDateTime } from "@/lib/format-date";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import {
  isNotFoundError,
  isPermissionDenied,
  persianApiErrorMessage,
} from "@/features/shared/api-error";
import { formatJalaliShort, isoToJalaliString, todayJalaliString } from "../cheques/cheque-data";
import {
  amendmentKindLabel,
  bnplStatusLabel,
  lateFeeBasisLabel,
  legFinanceIncomeIrr,
  paymentMethodLabel,
  reminderStatusLabel,
  reminderTriggerLabel,
  settlementPolicyLabel,
} from "./installment-data";
import { PlanKindBadge, PlanStatusBadge, legTableColumns } from "./installment-shared";

type ActionId = "receipt" | "settlement" | "remind" | null;

const ACTION_LABELS: Record<Exclude<ActionId, null>, string> = {
  // Distinct from the form's submit button ("ثبت دریافت") so the
  // toggle and the submit never share an accessible name.
  receipt: "دریافت جدید",
  settlement: "تسویهٔ زودهنگام",
  remind: "یادآوری پیامکی",
};

interface AmendmentLegRow {
  leg_id: string;
  sequence: number;
  before_irr: number;
  after_irr: number;
}

const amendmentLegColumns: DataTableColumn<AmendmentLegRow>[] = [
  {
    key: "sequence",
    header: "قسط",
    render: (row) => faNumber(row.sequence),
  },
  {
    key: "before",
    header: "اصل قبل",
    render: (row) => <MoneyDisplay amountIrr={row.before_irr} size="sm" />,
  },
  {
    key: "after",
    header: "اصل بعد",
    render: (row) => <MoneyDisplay amountIrr={row.after_irr} size="sm" />,
  },
];

export function InstallmentDetailPage({ planId }: { planId: string }) {
  const router = useRouter();
  const [detail, setDetail] = React.useState<InstallmentPlanDetailResponse | null>(null);
  const [amendments, setAmendments] = React.useState<ScheduleAmendmentResponse[]>([]);
  const [reminders, setReminders] = React.useState<ReminderLogResponse[]>([]);
  const [quote, setQuote] = React.useState<PayoffQuoteResponse | null>(null);
  const [quoteError, setQuoteError] = React.useState(false);
  const [contract, setContract] = React.useState<InstallmentContractResponse | null>(null);
  const [providers, setProviders] = React.useState<SmsProviderResponse[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [action, setAction] = React.useState<ActionId>(null);
  const [actionError, setActionError] = React.useState<unknown>(null);
  const [actionDone, setActionDone] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [rejectingReceiptId, setRejectingReceiptId] = React.useState<string | null>(null);
  const [rejectReason, setRejectReason] = React.useState("");

  // Receipt form state.
  const [receiptAmount, setReceiptAmount] = React.useState<number | null>(null);
  const [receiptJournal, setReceiptJournal] = React.useState("");
  const [receiptDateIso, setReceiptDateIso] = React.useState<string | null>(null);
  const [receiptMethod, setReceiptMethod] = React.useState("cash");
  const [receiptChequeId, setReceiptChequeId] = React.useState("");
  const [receiptGatewayRef, setReceiptGatewayRef] = React.useState("");
  const [receiptReference, setReceiptReference] = React.useState("");
  const [receiptNotes, setReceiptNotes] = React.useState("");
  // Settlement form state.
  const [settlementType, setSettlementType] = React.useState<"full" | "partial">("full");
  const [settlementAmount, setSettlementAmount] = React.useState<number | null>(null);
  const [settlementJournal, setSettlementJournal] = React.useState("");
  const [settlementDateIso, setSettlementDateIso] = React.useState<string | null>(null);
  const [settlementMethod, setSettlementMethod] = React.useState("cash");
  // Reminder form state.
  const [remindLegId, setRemindLegId] = React.useState("");
  const [remindProviderId, setRemindProviderId] = React.useState("");
  const [remindPhone, setRemindPhone] = React.useState("");
  const [remindDryRun, setRemindDryRun] = React.useState(false);

  const todayJalali = React.useMemo(() => todayJalaliString(), []);

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getInstallmentPlan(planId)
      .then(async (fetched) => {
        if (cancelled) return;
        setDetail(fetched);
        setError(null);
        // Secondary reads never fail the page: a missing quote or
        // contract is shown in its own card.
        const [amendmentRows, reminderRows] = await Promise.all([
          api.listPlanAmendments(planId).catch(() => [] as ScheduleAmendmentResponse[]),
          api.listPlanReminders(planId, { limit: 50 }).catch(() => [] as ReminderLogResponse[]),
        ]);
        if (cancelled) return;
        setAmendments(amendmentRows);
        setReminders(reminderRows);
        if (fetched.plan.kind === "internal") {
          api
            .getPayoffQuote(planId)
            .then((q) => {
              if (!cancelled) {
                setQuote(q);
                setQuoteError(false);
              }
            })
            .catch(() => {
              if (!cancelled) setQuoteError(true);
            });
        }
        if (fetched.plan.contract_id) {
          api
            .getInstallmentContract(fetched.plan.contract_id)
            .then((c) => {
              if (!cancelled) setContract(c);
            })
            .catch(() => {
              if (!cancelled) setContract(null);
            });
        }
        api
          .listSmsProviders()
          .then((rows) => {
            if (!cancelled) setProviders(rows);
          })
          .catch(() => {
            if (!cancelled) setProviders([]);
          });
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
  }, [planId, router]);

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

  const isBnpl = detail?.plan.kind === "bnpl";
  const contractRate = contract ? Number(contract.finance_income_rate) : null;

  const receiptMethods = isBnpl ? ["bnpl"] : ["cash", "bank_transfer", "other", "cheque"];

  const unpaidLegs = React.useMemo(
    () =>
      (detail?.legs ?? []).filter(
        (leg) => leg.balance_irr > 0 && leg.status !== "pending_clearance",
      ),
    [detail],
  );

  const reloadAfterAction = (message: string) => {
    setActionDone(message);
    setAction(null);
    setRejectingReceiptId(null);
    load();
  };

  const submitReceipt = async () => {
    if (!detail) return;
    if (!receiptAmount || receiptAmount <= 0) {
      setActionError(new Error("مبلغ دریافت باید بزرگ‌تر از صفر باشد."));
      return;
    }
    const journalMoveId = Number(receiptJournal);
    if (!Number.isInteger(journalMoveId) || journalMoveId <= 0) {
      setActionError(new Error("شناسهٔ سند حسابداری (روزنامه) الزامی است؛ هر دریافت باید به فاکتور اصلی و سند حسابداری مرتبط باشد."));
      return;
    }
    if (receiptMethod === "cheque" && !receiptChequeId.trim()) {
      setActionError(new Error("دریافت با چک شناسهٔ چک را لازم دارد."));
      return;
    }
    if (receiptMethod === "bnpl" && !receiptGatewayRef.trim()) {
      setActionError(new Error("دریافت BNPL مرجع تراکنش درگاه را لازم دارد."));
      return;
    }
    setSubmitting(true);
    setActionError(null);
    try {
      await api.createInstallmentReceipt(planId, {
        invoice_id: detail.plan.invoice_id,
        journal_move_id: journalMoveId,
        amount_toman: receiptAmount,
        received_date: receiptDateIso ? isoToJalaliString(receiptDateIso) : undefined,
        payment_method: receiptMethod,
        cheque_id: receiptMethod === "cheque" ? receiptChequeId.trim() : undefined,
        gateway: receiptMethod === "bnpl" ? detail.plan.gateway_channel_key ?? undefined : undefined,
        gateway_reference: receiptMethod === "bnpl" ? receiptGatewayRef.trim() : undefined,
        reference: receiptReference.trim() || undefined,
        notes: receiptNotes.trim() || undefined,
      });
      reloadAfterAction("دریافت ثبت شد.");
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setActionError(err);
    } finally {
      setSubmitting(false);
    }
  };

  const submitSettlement = async () => {
    if (!detail) return;
    if (!settlementAmount || settlementAmount <= 0) {
      setActionError(new Error("مبلغ تسویه باید بزرگ‌تر از صفر باشد."));
      return;
    }
    const journalMoveId = Number(settlementJournal);
    if (!Number.isInteger(journalMoveId) || journalMoveId <= 0) {
      setActionError(new Error("شناسهٔ سند حسابداری (روزنامه) الزامی است؛ تسویه هم یک دریافت است و قاعدهٔ پیوند شامل آن می‌شود."));
      return;
    }
    setSubmitting(true);
    setActionError(null);
    try {
      const result = await api.createEarlySettlement(planId, {
        invoice_id: detail.plan.invoice_id,
        journal_move_id: journalMoveId,
        amount_toman: settlementAmount,
        settlement_type: settlementType,
        settlement_date: settlementDateIso ? isoToJalaliString(settlementDateIso) : undefined,
        payment_method: settlementMethod,
      });
      reloadAfterAction(
        result.settlement_type === "full"
          ? "تسویهٔ کامل انجام شد؛ برنامه بسته شد."
          : "تسویهٔ جزئی انجام شد؛ اقساط باقی‌مانده بازتوزیع و در تاریخچهٔ تغییرات ثبت شد.",
      );
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setActionError(err);
    } finally {
      setSubmitting(false);
    }
  };

  const submitReminder = async () => {
    setSubmitting(true);
    setActionError(null);
    try {
      const entry = await api.triggerPlanReminder(planId, {
        leg_id: remindLegId || undefined,
        provider_id: remindProviderId || undefined,
        recipient_phone: remindPhone.trim() || undefined,
        dry_run: remindDryRun,
      });
      reloadAfterAction(
        entry.status === "dry_run"
          ? "پیش‌نمایش یادآوری ثبت شد؛ چیزی ارسال نشد."
          : "یادآوری پیامکی ارسال شد.",
      );
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setActionError(err);
    } finally {
      setSubmitting(false);
    }
  };

  const submitRejectBnpl = async (receiptId: string) => {
    if (!rejectReason.trim()) {
      setActionError(new Error("دلیل رد درگاه را بنویسید."));
      return;
    }
    setSubmitting(true);
    setActionError(null);
    try {
      await api.rejectBnplReceipt(planId, receiptId, { reason: rejectReason.trim() });
      setRejectReason("");
      reloadAfterAction("دریافت BNPL رد شد؛ اقساطش دوباره به «در انتظار سررسید» برگشتند و یک آیتم در صف بازبینی ثبت شد.");
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setActionError(err);
    } finally {
      setSubmitting(false);
    }
  };

  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن برنامه‌های قسط را ندارد." />;
  }
  if (error && isNotFoundError(error)) {
    return <NotFoundState message="این برنامهٔ قسط پیدا نشد؛ ممکن است حذف شده باشد." />;
  }

  if (loading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-64" />
        <div className="space-y-2" aria-label="در حال بارگذاری">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      </div>
    );
  }
  if (error) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت جزئیات برنامهٔ قسط ناموفق بود.")}
        onRetry={load}
      />
    );
  }
  if (!detail) {
    return <NotFoundState message="این برنامهٔ قسط پیدا نشد؛ ممکن است حذف شده باشد." />;
  }

  const plan = detail.plan;

  const receiptColumns: DataTableColumn<InstallmentPlanDetailResponse["receipts"][number]>[] = [
    {
      key: "date",
      header: "تاریخ",
      render: (row) => formatJalaliShort(row.received_date),
    },
    {
      key: "method",
      header: "روش",
      render: (row) => (
        <span>
          {paymentMethodLabel(row.payment_method)}
          {row.is_early_settlement ? (
            <span className="block text-xs font-medium text-primary">تسویهٔ زودهنگام</span>
          ) : null}
        </span>
      ),
    },
    {
      key: "amount",
      header: "مبلغ",
      render: (row) => <MoneyDisplay amountIrr={row.amount_irr} size="sm" />,
    },
    {
      key: "allocated",
      header: "تخصیص‌یافته",
      render: (row) => <MoneyDisplay amountIrr={row.allocated_irr} size="sm" />,
    },
    {
      key: "advance",
      header: "پیش‌پرداخت",
      render: (row) => <MoneyDisplay amountIrr={row.advance_irr} size="sm" />,
    },
    {
      key: "collected",
      header: "وصول",
      render: (row) => (row.collected_at ? formatJalaliShort(row.collected_at) : "وصول نشده"),
    },
    {
      key: "gateway",
      header: "درگاه",
      render: (row) =>
        row.gateway ? (
          <span>
            <span dir="ltr">{row.gateway}</span>
            <span className="block text-xs text-muted-foreground" dir="ltr">
              {row.gateway_reference}
            </span>
            <span className="block text-xs">{bnplStatusLabel(row.bnpl_status)}</span>
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "journal",
      header: "سند",
      render: (row) => (
        <span dir="ltr" className="font-mono text-sm">
          {row.journal_move_id}
        </span>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">
            برنامهٔ قسط — <span dir="ltr">{plan.invoice_id}</span>
          </h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <PlanKindBadge kind={plan.kind} />
            <PlanStatusBadge status={detail.status} />
            {plan.customer_ref ? <span>مشتری: {plan.customer_ref}</span> : null}
            {plan.gateway_channel_key ? (
              <span>
                درگاه: <span dir="ltr">{plan.gateway_channel_key}</span>
              </span>
            ) : null}
          </p>
        </div>
        <Link
          href="/installments"
          className="inline-flex min-h-11 items-center rounded-md border px-4 text-sm font-medium"
        >
          بازگشت به مرکز اقساط
        </Link>
      </div>

      {actionDone ? (
        <p role="status" className="rounded-lg border border-success/40 bg-success/10 px-4 py-3 text-sm text-success-foreground">
          {actionDone}
        </p>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">اصل کل</p>
            <p className="mt-1">
              <MoneyDisplay amountIrr={plan.total_principal_irr} size="md" />
            </p>
            <p className="mt-1 text-xs text-muted-foreground">{faNumber(plan.leg_count)} قسط</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">دریافت‌شده</p>
            <p className="mt-1">
              <MoneyDisplay amountIrr={detail.total_received_irr} size="md" />
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              پیش‌پرداخت نگه‌داشته‌شده: <MoneyDisplay amountIrr={detail.total_advance_irr} size="sm" />
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-muted-foreground">مانده</p>
            <p className="mt-1">
              <MoneyDisplay amountIrr={detail.outstanding_irr} size="md" />
            </p>
            {detail.total_pending_clearance_irr > 0 ? (
              <p className="mt-1 text-xs font-medium text-warning-foreground">
                <MoneyDisplay amountIrr={detail.total_pending_clearance_irr} size="sm" /> در انتظار وصول چک
              </p>
            ) : null}
          </CardContent>
        </Card>
        {isBnpl ? (
          <Card>
            <CardContent className="pt-6">
              <p className="text-sm text-muted-foreground">طلب از درگاه</p>
              <p className="mt-1">
                <MoneyDisplay amountIrr={detail.gateway_receivable_irr} size="md" />
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                تسویه‌شده: <MoneyDisplay amountIrr={detail.gateway_settled_irr} size="sm" />
              </p>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardContent className="pt-6">
              <p className="text-sm text-muted-foreground">نقل‌قول تسویه</p>
              {quote ? (
                <p className="mt-1">
                  <MoneyDisplay amountIrr={quote.payoff_total_irr} size="md" />
                </p>
              ) : quoteError ? (
                <p className="mt-1 text-sm text-muted-foreground">نقل‌قول در دسترس نیست.</p>
              ) : (
                <Skeleton className="mt-1 h-8 w-32" />
              )}
              <p className="mt-1 text-xs text-muted-foreground">تا امروز، {formatJalaliShort(todayJalali)}</p>
            </CardContent>
          </Card>
        )}
      </div>

      {contract ? (
        <Card>
          <CardHeader>
            <CardTitle>قرارداد فروش اقساطی — {contract.name}</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <dt className="text-muted-foreground">کد و نسخه</dt>
                <dd className="font-medium">
                  <span dir="ltr">{contract.code}</span> — نسخهٔ {faNumber(contract.version)}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">نرخ سود تأمین مالی</dt>
                <dd className="font-medium">{faNumber(Math.round(Number(contract.finance_income_rate) * 100))}٪ از اصل</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">جریمهٔ تأخیر</dt>
                <dd className="font-medium">
                  {lateFeeBasisLabel(contract.late_fee_basis)}
                  {contract.late_fee_grace_days > 0 ? ` — ${faNumber(contract.late_fee_grace_days)} روز مهلت` : ""}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">مراحل پیگیری (روز)</dt>
                <dd className="font-medium">
                  {contract.dunning_offsets.length > 0
                    ? contract.dunning_offsets.map((d) => faNumber(d)).join("، ")
                    : "تنظیم نشده"}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">تسویهٔ زودهنگام — جریمه</dt>
                <dd className="font-medium">{settlementPolicyLabel(contract.early_settlement_fee_policy)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">تسویهٔ زودهنگام — سود</dt>
                <dd className="font-medium">{settlementPolicyLabel(contract.early_settlement_income_policy)}</dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>جدول سررسید</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={legTableColumns({
              showCheque: !isBnpl,
              incomeIrrOf:
                !isBnpl && contractRate !== null
                  ? (leg) => legFinanceIncomeIrr(leg.principal_irr, contractRate)
                  : undefined,
            })}
            rows={detail.legs}
            rowKey={(row) => row.id}
            emptyText="این برنامه قسطی ندارد."
            caption={`جدول سررسید — ${faNumber(detail.legs.length)} قسط`}
          />
          <p className="mt-4 text-xs text-muted-foreground">
            وضعیت قسط از روی دریافت‌ها، وضعیت چک و تاریخ امروز محاسبه می‌شود و هیچ‌وقت ذخیره نمی‌شود؛ قسطی که چکش
            هنوز وصول نشده «در انتظار وصول چک» است و معوق حساب نمی‌شود.
            {isBnpl
              ? " وضعیت قسط BNPL از قسط‌های تسویهٔ درگاه می‌آید: تأیید درگاه یعنی درگاه پرداخت را تأیید کرده و هنوز قسطی تسویه نکرده است."
              : ""}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>دریافت‌ها</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={receiptColumns}
            rows={detail.receipts}
            rowKey={(row) => row.id}
            emptyText="هنوز دریافتی برای این برنامه ثبت نشده است."
            caption={`دریافت‌ها — ${faNumber(detail.receipts.length)} دریافت`}
          />
          <p className="mt-4 text-xs text-muted-foreground">
            هر دریافت به فاکتور اصلی و سند حسابداری مرتبط است؛ «وصول» تاریخ رسیدن پول است — دریافت با چک فقط وقتی
            وصول می‌شود که چک وصول شود.
          </p>
          {isBnpl ? (
            <div className="mt-4 space-y-2">
              {detail.receipts
                .filter((r) => r.payment_method === "bnpl" && r.bnpl_status === "active")
                .map((receipt) => (
                  <div key={receipt.id} className="flex flex-wrap items-center gap-2 rounded-lg border p-4">
                    <p className="text-sm">
                      دریافت <span dir="ltr">{receipt.gateway_reference}</span> ({formatJalaliShort(receipt.received_date)})
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        setRejectingReceiptId(rejectingReceiptId === receipt.id ? null : receipt.id)
                      }
                    >
                      رد دریافت BNPL
                    </Button>
                    {rejectingReceiptId === receipt.id ? (
                      <div className="flex w-full flex-wrap items-end gap-2">
                        <div className="min-w-64 flex-1 space-y-2">
                          <Label htmlFor={`reject-reason-${receipt.id}`}>دلیل رد درگاه</Label>
                          <Input
                            id={`reject-reason-${receipt.id}`}
                            value={rejectReason}
                            onChange={(event) => setRejectReason(event.target.value)}
                            placeholder="دلیل درگاه یا اپراتور"
                          />
                        </div>
                        <Button
                          variant="destructive"
                          disabled={submitting}
                          onClick={() => submitRejectBnpl(receipt.id)}
                        >
                          ثبت رد
                        </Button>
                      </div>
                    ) : null}
                  </div>
                ))}
            </div>
          ) : null}
        </CardContent>
      </Card>

      {!isBnpl && quote ? (
        <Card>
          <CardHeader>
            <CardTitle>نقل‌قول تسویهٔ زودهنگام</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <dt className="text-muted-foreground">اصل باقی‌مانده</dt>
                <dd className="font-medium">
                  <MoneyDisplay amountIrr={quote.remaining_principal_irr} size="sm" />
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">سود تحقق‌یافتهٔ پرداخت‌نشده</dt>
                <dd className="font-medium">
                  <MoneyDisplay amountIrr={quote.income_realised_unpaid_irr} size="sm" />
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">سود تحقق‌نیافته</dt>
                <dd className="font-medium">
                  <MoneyDisplay amountIrr={quote.income_unrealised_irr} size="sm" /> (
                  {settlementPolicyLabel(quote.income_policy)})
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">سود قابل پرداخت در تسویه</dt>
                <dd className="font-medium">
                  <MoneyDisplay amountIrr={quote.income_due_irr} size="sm" />
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">جریمهٔ انباشته</dt>
                <dd className="font-medium">
                  <MoneyDisplay amountIrr={quote.late_fees_accrued_irr} size="sm" /> (
                  {settlementPolicyLabel(quote.fee_policy)})
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">پیش‌پرداخت نگه‌داشته‌شده (کسر می‌شود)</dt>
                <dd className="font-medium">
                  <MoneyDisplay amountIrr={quote.advance_held_irr} size="sm" />
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">مبلغ تسویه</dt>
                <dd className="text-lg font-bold">
                  <MoneyDisplay amountIrr={quote.payoff_total_irr} size="md" />
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">اقساط باز</dt>
                <dd className="font-medium">{faNumber(quote.open_leg_count)} قسط</dd>
              </div>
            </dl>
            <p className="mt-4 text-xs text-muted-foreground">
              مبلغ تسویه = اصل باقی‌مانده + سود قابل پرداخت + جریمهٔ قابل پرداخت − پیش‌پرداخت نگه‌داشته‌شده. سود
              تحقق‌یافته همیشه کامل حساب می‌شود، چون قبلاً به دست آمده است.
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>اقدام‌ها</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-2">
            {(Object.keys(ACTION_LABELS) as Exclude<ActionId, null>[]).map((id) => {
              if (id !== "receipt" && isBnpl) return null;
              return (
                <Button
                  key={id}
                  variant={action === id ? "default" : "outline"}
                  onClick={() => {
                    setAction(action === id ? null : id);
                    setActionError(null);
                    if (id === "receipt") setReceiptMethod(isBnpl ? "bnpl" : "cash");
                    if (id === "settlement" && quote) setSettlementAmount(quote.payoff_total_toman);
                  }}
                >
                  {ACTION_LABELS[id]}
                </Button>
              );
            })}
          </div>
          {isBnpl ? (
            <p className="text-xs text-muted-foreground">
              برنامهٔ BNPL تسویهٔ زودهنگام و یادآوری پیامکی ندارد؛ وصول اقساط با درگاه است و فقط دریافت از درگاه ثبت
              می‌شود.
            </p>
          ) : null}

          {action === "receipt" ? (
            <div className="space-y-4 rounded-lg border p-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="receipt-amount">مبلغ دریافت (تومان)</Label>
                  <AmountInput id="receipt-amount" value={receiptAmount} onChange={setReceiptAmount} min={1} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="receipt-journal">شناسهٔ سند حسابداری (روزنامه)</Label>
                  <Input
                    id="receipt-journal"
                    inputMode="numeric"
                    value={receiptJournal}
                    onChange={(event) => setReceiptJournal(event.target.value.replace(/[^0-9]/g, ""))}
                    placeholder="شناسهٔ عددی سند در دفتر کل"
                    dir="ltr"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="receipt-date">تاریخ دریافت</Label>
                  <JalaliDatePicker
                    id="receipt-date"
                    value={receiptDateIso}
                    onChange={setReceiptDateIso}
                    placeholder="امروز (پیش‌فرض)"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="receipt-method">روش پرداخت</Label>
                  <Select value={receiptMethod} onValueChange={(value) => setReceiptMethod(value ?? "cash")}>
                    <SelectTrigger id="receipt-method">
                      <SelectValue>{paymentMethodLabel(receiptMethod)}</SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {receiptMethods.map((method) => (
                        <SelectItem key={method} value={method}>
                          {paymentMethodLabel(method)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {receiptMethod === "cheque" ? (
                  <div className="space-y-2">
                    <Label htmlFor="receipt-cheque-id">شناسهٔ چک</Label>
                    <Input
                      id="receipt-cheque-id"
                      value={receiptChequeId}
                      onChange={(event) => setReceiptChequeId(event.target.value)}
                      placeholder="شناسهٔ چک دریافتی (نزد ما یا در جریان وصول)"
                      dir="ltr"
                    />
                    <p className="text-xs text-muted-foreground">
                      قسطی که با چک پرداخت می‌شود تا وصول چک «در انتظار وصول چک» می‌ماند و معوق حساب نمی‌شود.
                    </p>
                  </div>
                ) : null}
                {receiptMethod === "bnpl" ? (
                  <div className="space-y-2">
                    <Label htmlFor="receipt-gateway-ref">مرجع تراکنش درگاه</Label>
                    <Input
                      id="receipt-gateway-ref"
                      value={receiptGatewayRef}
                      onChange={(event) => setReceiptGatewayRef(event.target.value)}
                      placeholder="شناسهٔ تراکنش درگاه"
                      dir="ltr"
                    />
                    <p className="text-xs text-muted-foreground">
                      درگاه این برنامه <span dir="ltr">{plan.gateway_channel_key}</span> است؛ دریافت با درگاه دیگری
                      پذیرفته نمی‌شود.
                    </p>
                  </div>
                ) : null}
                <div className="space-y-2">
                  <Label htmlFor="receipt-reference">مرجع (اختیاری)</Label>
                  <Input
                    id="receipt-reference"
                    value={receiptReference}
                    onChange={(event) => setReceiptReference(event.target.value)}
                    placeholder="مرجع دلخواه اپراتور"
                  />
                </div>
                <div className="space-y-2 sm:col-span-2">
                  <Label htmlFor="receipt-notes">یادداشت (اختیاری)</Label>
                  <Textarea
                    id="receipt-notes"
                    value={receiptNotes}
                    onChange={(event) => setReceiptNotes(event.target.value)}
                    placeholder="یادداشت اپراتور"
                  />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                فاکتور این دریافت همان فاکتور برنامه است (<span dir="ltr">{plan.invoice_id}</span>)؛ دریافت با
                فاکتور دیگری پذیرفته نمی‌شود. تخصیص خودکار است: اول معوق‌ترین قسط، بعد زودترین قسط سررسیدشده؛
                اضافه‌پرداخت پیش‌پرداخت می‌ماند و هرگز قسط آینده را پیش‌پرداخت نمی‌کند.
              </p>
              <Button onClick={submitReceipt} disabled={submitting}>
                {submitting ? "در حال ثبت…" : "ثبت دریافت"}
              </Button>
            </div>
          ) : null}

          {action === "settlement" ? (
            <div className="space-y-4 rounded-lg border p-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="settlement-type">نوع تسویه</Label>
                  <Select
                    value={settlementType}
                    onValueChange={(value) => {
                      const next = (value as "full" | "partial") ?? "full";
                      setSettlementType(next);
                      if (next === "full" && quote) setSettlementAmount(quote.payoff_total_toman);
                    }}
                  >
                    <SelectTrigger id="settlement-type">
                      <SelectValue>{settlementType === "full" ? "کامل — بستن برنامه" : "جزئی — بازتوزیع اقساط"}</SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="full">کامل — بستن برنامه</SelectItem>
                      <SelectItem value="partial">جزئی — بازتوزیع اقساط باقی‌مانده</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="settlement-amount">مبلغ تسویه (تومان)</Label>
                  <AmountInput id="settlement-amount" value={settlementAmount} onChange={setSettlementAmount} min={1} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="settlement-journal">شناسهٔ سند حسابداری (روزنامه)</Label>
                  <Input
                    id="settlement-journal"
                    inputMode="numeric"
                    value={settlementJournal}
                    onChange={(event) => setSettlementJournal(event.target.value.replace(/[^0-9]/g, ""))}
                    placeholder="شناسهٔ عددی سند در دفتر کل"
                    dir="ltr"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="settlement-date">تاریخ تسویه</Label>
                  <JalaliDatePicker
                    id="settlement-date"
                    value={settlementDateIso}
                    onChange={setSettlementDateIso}
                    placeholder="امروز (پیش‌فرض)"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="settlement-method">روش پرداخت</Label>
                  <Select value={settlementMethod} onValueChange={(value) => setSettlementMethod(value ?? "cash")}>
                    <SelectTrigger id="settlement-method">
                      <SelectValue>{paymentMethodLabel(settlementMethod)}</SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {["cash", "bank_transfer", "other"].map((method) => (
                        <SelectItem key={method} value={method}>
                          {paymentMethodLabel(method)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                {settlementType === "full"
                  ? "تسویهٔ کامل همهٔ اقساط باز — حتی اقساط آینده — را می‌بندد و مبلغش باید دست‌کم برابر نقل‌قول تسویه باشد."
                  : "تسویهٔ جزئی فقط اصل می‌پردازد؛ اضافه‌پرداخت با پیش‌پرداخت نگه‌داشته‌شده جمع می‌شود و اقساط باقی‌مانده را بازتوزیع می‌کند (سررسیدها عوض نمی‌شوند). هر بازتوزیع در تاریخچهٔ تغییرات زیر ثبت می‌شود."}
              </p>
              <Button onClick={submitSettlement} disabled={submitting}>
                {submitting ? "در حال ثبت…" : "ثبت تسویه"}
              </Button>
            </div>
          ) : null}

          {action === "remind" ? (
            <div className="space-y-4 rounded-lg border p-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="remind-leg">قسط</Label>
                  <Select value={remindLegId || undefined} onValueChange={(value) => setRemindLegId(value ?? "")}>
                    <SelectTrigger id="remind-leg">
                      <SelectValue placeholder="زودترین قسط پرداخت‌نشده (پیش‌فرض)" />
                    </SelectTrigger>
                    <SelectContent>
                      {unpaidLegs.map((leg) => (
                        <SelectItem key={leg.id} value={leg.id}>
                          قسط {faNumber(leg.sequence)} — سررسید {formatJalaliShort(leg.due_date)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    قسط پرداخت‌شده و قسط در انتظار وصول چک یادآوری ندارد.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="remind-provider">ارائه‌دهندهٔ پیامک</Label>
                  <Select value={remindProviderId || undefined} onValueChange={(value) => setRemindProviderId(value ?? "")}>
                    <SelectTrigger id="remind-provider">
                      <SelectValue placeholder="پیش‌فرض نصب" />
                    </SelectTrigger>
                    <SelectContent>
                      {providers
                        .filter((p) => p.enabled)
                        .map((provider) => (
                          <SelectItem key={provider.id} value={provider.id}>
                            {provider.display_name_fa}
                          </SelectItem>
                        ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="remind-phone">شمارهٔ موبایل گیرنده (اختیاری)</Label>
                  <Input
                    id="remind-phone"
                    inputMode="tel"
                    value={remindPhone}
                    onChange={(event) => setRemindPhone(event.target.value)}
                    placeholder="۰۹xxxxxxxxx — پیش‌فرض: موبایل مشتری"
                    dir="ltr"
                  />
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="remind-dry-run"
                    checked={remindDryRun}
                    onCheckedChange={(checked) => setRemindDryRun(checked === true)}
                  />
                  <Label htmlFor="remind-dry-run">فقط پیش‌نمایش — چیزی ارسال نمی‌شود</Label>
                </div>
              </div>
              <Button onClick={submitReminder} disabled={submitting}>
                {submitting ? "در حال ارسال…" : remindDryRun ? "پیش‌نمایش یادآوری" : "ارسال یادآوری"}
              </Button>
            </div>
          ) : null}

          {actionError ? (
            <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
              {actionError instanceof Error && !(actionError instanceof ApiError)
                ? actionError.message
                : persianApiErrorMessage(actionError, "این اقدام ناموفق بود.")}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>تاریخچهٔ تغییرات برنامه</CardTitle>
        </CardHeader>
        <CardContent>
          {amendments.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              هنوز تغییری در برنامه ثبت نشده است؛ تسویهٔ جزئی زودهنگام اقساط باقی‌مانده را بازتوزیع می‌کند و هر
              بازتوزیع با اصل قبل و بعد هر قسط اینجا ثبت می‌شود.
            </p>
          ) : (
            <ol className="space-y-6">
              {amendments.map((amendment, index) => (
                <li key={amendment.id} className="space-y-2 rounded-lg border p-4">
                  <p className="font-medium">
                    تغییر {faNumber(index + 1)} — {amendmentKindLabel(amendment.kind)}
                  </p>
                  <p className="text-sm text-muted-foreground">
                    {formatIsoDateTime(amendment.created_at)} — پیش‌پرداخت اعمال‌شده:{" "}
                    <MoneyDisplay amountIrr={amendment.applied_advance_irr} size="sm" />
                    {amendment.receipt_id ? (
                      <>
                        {" "}
                        — رسید: <span dir="ltr" className="font-mono text-xs">{amendment.receipt_id}</span>
                      </>
                    ) : null}
                  </p>
                  <DataTable
                    columns={amendmentLegColumns}
                    rows={amendment.legs_after.map((after) => ({
                      leg_id: after.leg_id,
                      sequence: after.sequence,
                      before_irr:
                        amendment.legs_before.find((b) => b.leg_id === after.leg_id)?.principal_irr ?? 0,
                      after_irr: after.principal_irr,
                    }))}
                    rowKey={(row) => row.leg_id}
                    emptyText="بدون قسط"
                  />
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      {!isBnpl ? (
        <Card>
          <CardHeader>
            <CardTitle>لاگ یادآوری پیامکی</CardTitle>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={
                [
                  {
                    key: "created",
                    header: "تاریخ",
                    render: (row) => (row.created_at ? formatIsoDateTime(row.created_at) : "—"),
                  },
                  {
                    key: "offset",
                    header: "فاصله از سررسید",
                    render: (row) =>
                      row.offset_days === 0
                        ? "روز سررسید"
                        : row.offset_days < 0
                          ? `${faNumber(-row.offset_days)} روز پیش از سررسید`
                          : `${faNumber(row.offset_days)} روز پس از سررسید`,
                  },
                  {
                    key: "trigger",
                    header: "محرک",
                    render: (row) => reminderTriggerLabel(row.trigger),
                  },
                  {
                    key: "provider",
                    header: "ارائه‌دهنده",
                    render: (row) => <span dir="ltr">{row.provider_key}</span>,
                  },
                  {
                    key: "status",
                    header: "وضعیت",
                    render: (row) => reminderStatusLabel(row.status),
                  },
                  {
                    key: "message",
                    header: "متن",
                    render: (row) => <span className="line-clamp-2 text-xs">{row.message_text}</span>,
                  },
                ] as DataTableColumn<ReminderLogResponse>[]
              }
              rows={reminders}
              rowKey={(row) => row.id}
              emptyText="هنوز یادآوری‌ای برای این برنامه ثبت نشده است."
              caption={`لاگ یادآوری — ${faNumber(reminders.length)} ردیف`}
            />
          </CardContent>
        </Card>
      ) : null}

      <p className="text-xs text-muted-foreground">
        نسخهٔ قرارداد این برنامه در زمان ساخت ثابت شده است؛ نسخهٔ جدیدتر قرارداد هرگز برنامهٔ موجود را دوباره
        قیمت‌گذاری نمی‌کند. شناسهٔ برنامه: <span dir="ltr" className="font-mono">{plan.id}</span>
      </p>
    </div>
  );
}
