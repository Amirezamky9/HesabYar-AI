/**
 * Cheque detail (US-535, Phase 5-4).
 *
 * One cheque: lifecycle timeline + custody log + inquiry receipts +
 * endorsement history + linked journals, and the state transitions
 * for its current state through the existing REST endpoints
 * (cheques:transition). Accountant-gated actions never post a
 * journal silently:
 *
 * - call-in of a collateral cheque needs the approving accountant's
 *   name (required field; the two journals post in one transaction);
 * - endorsement to a non-supplier posts NO journal — the page says
 *   so and the record carries `journal_pending_review`;
 * - an unexpected call-in of an issued collateral cheque likewise
 *   leaves its commercial journal to the accountant.
 *
 * Inquiry runs use the simulators until the ۵ت remainder (B3) wires
 * live Fintech/Ehrazino transport — the page says so, and only
 * verdict/code/time/reference are ever stored.
 */

"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { CheckCircle2, Circle, TriangleAlert } from "lucide-react";
import { ApiError, api, type ChequeDetailResponse } from "@/lib/api-client";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Checkbox,
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
} from "@/components/ui";
import { faNumber } from "@/lib/utils";
import { formatIsoDateTime } from "@/lib/format-date";
import { ErrorState, NotFoundState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  FINNOTECH_SAYYAD_SCOPES,
  INQUIRY_CHANNEL_LABELS,
  INQUIRY_TYPE_LABELS,
  chequeKindLabel,
  chequePurposeLabel,
  formatJalaliLong,
  formatJalaliShort,
  inquiryChannelLabel,
  inquiryTypeLabel,
  isoToJalaliString,
  issuerColorLabel,
  lifecycleSteps,
  sayadStatusLabel,
  storageLocationLabel,
} from "./cheque-data";
import { ChequeStateBadge, chequeSourceLabel } from "./cheque-shared";

type ActionId =
  | "confirm-receipt"
  | "reject-mismatch"
  | "issue"
  | "deposit"
  | "endorse"
  | "return"
  | "call-in"
  | "clear"
  | "bounce"
  | "clearing-confirm"
  | "void"
  | "unexpected-call-in"
  | "endorsed-bounce"
  | "receipt"
  | "sayyad"
  | "ehrazino";

const ACTION_LABELS: Record<ActionId, string> = {
  "confirm-receipt": "تأیید رسید برگ",
  "reject-mismatch": "رد مغایرت",
  issue: "صدور چک",
  deposit: "خواباندن (ارسال به بانک)",
  endorse: "ظهرنویسی",
  return: "استرداد برگ",
  "call-in": "فراخوان چک پشتوانه",
  clear: "تأیید وصول",
  bounce: "ثبت برگشت",
  "clearing-confirm": "تأیید تطبیق وصول",
  void: "باطل کردن",
  "unexpected-call-in": "ثبت فراخوان ناخواسته",
  "endorsed-bounce": "ثبت برگشت چک ظهرنویسی‌شده",
  receipt: "ثبت رسید استعلام دستی",
  sayyad: "استعلام صیاد (فینوتک)",
  ehrazino: "استعلام احرازینو",
};

/** Which transitions the current state offers (§5 §2.1–§2.3). */
function actionsFor(cheque: ChequeDetailResponse): ActionId[] {
  switch (cheque.state) {
    case "registered":
      return cheque.kind === "issued"
        ? ["issue", "reject-mismatch"]
        : ["confirm-receipt", "reject-mismatch"];
    case "held":
      return cheque.purpose === "collateral"
        ? ["deposit", "endorse", "return", "call-in"]
        : ["deposit", "endorse", "return"];
    case "in_clearing":
      return ["clear", "bounce", "clearing-confirm"];
    case "endorsed":
      return ["endorsed-bounce"];
    case "issued":
      return cheque.purpose === "collateral" ? ["void", "unexpected-call-in"] : ["void"];
    case "bounced":
      return ["return"];
    default:
      return [];
  }
}

function Field({ label, htmlFor, children, hint }: { label: string; htmlFor: string; children: React.ReactNode; hint?: string }) {
  return (
    <div className="space-y-2">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

function ActionForm({
  title,
  onSubmit,
  busy,
  error,
  submitLabel,
  children,
}: {
  title: string;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  busy: boolean;
  error: string | null;
  submitLabel: string;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={onSubmit}
          className="grid gap-4 sm:grid-cols-2"
        >
          {children}
          {error ? (
            <p className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive sm:col-span-2" role="alert">
              {error}
            </p>
          ) : null}
          <div className="sm:col-span-2">
            <Button type="submit" disabled={busy}>
              {busy ? "در حال ثبت…" : submitLabel}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

export function ChequeDetailPage({ chequeId }: { chequeId: string }) {
  const router = useRouter();
  const [detail, setDetail] = React.useState<ChequeDetailResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [notFound, setNotFound] = React.useState(false);
  const [openAction, setOpenAction] = React.useState<ActionId | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);

  // Controlled selects inside the open form.
  const [endorseeType, setEndorseeType] = React.useState("supplier");
  const [storageLocation, setStorageLocation] = React.useState("");
  const [colorCode, setColorCode] = React.useState("");
  const [receiptChannel, setReceiptChannel] = React.useState("sms");
  const [receiptType, setReceiptType] = React.useState("color");
  const [sayyadScope, setSayyadScope] = React.useState(FINNOTECH_SAYYAD_SCOPES[3].scope);
  const [sayyadScenario, setSayyadScenario] = React.useState("none");
  const [ehrazinoKind, setEhrazinoKind] = React.useState("color");
  const [ehrazinoScenario, setEhrazinoScenario] = React.useState("none");
  const [bounceIso, setBounceIso] = React.useState<string | null>(null);
  const [endorsedBounceIso, setEndorsedBounceIso] = React.useState<string | null>(null);
  const [itemsMatched, setItemsMatched] = React.useState(true);
  const [sayadAccepted, setSayadAccepted] = React.useState(true);

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getCheque(chequeId)
      .then((row) => {
        if (cancelled) return;
        setDetail(row);
        setError(null);
        setNotFound(false);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        if (err instanceof ApiError && err.isNotFound) setNotFound(true);
        else setError(err);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [chequeId, router]);

  const refresh = React.useCallback(() => {
    load();
    setOpenAction(null);
  }, [load]);

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

  if (loading) {
    return (
      <div className="space-y-4" aria-label="در حال بارگذاری">
        <Skeleton className="h-8 w-72" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  if (notFound || (detail === null && !error)) {
    return <NotFoundState message="این چک پیدا نشد؛ ممکن است حذف شده باشد." />;
  }
  if (error && isPermissionDenied(error)) {
    return <PermissionDeniedState message="کلید شما دسترسی خواندن چک‌ها را ندارد." />;
  }
  if (error || detail === null) {
    return (
      <ErrorState
        message={persianApiErrorMessage(error, "دریافت جزئیات چک ناموفق بود.")}
        onRetry={load}
      />
    );
  }

  const cheque = detail;
  const steps = lifecycleSteps(cheque);
  const actions = actionsFor(cheque);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      setBusy(false);
      refresh();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.isUnauthorized) {
        router.push("/login");
        return;
      }
      setActionError(persianApiErrorMessage(err, "ثبت این اقدام ناموفق بود."));
      setBusy(false);
    }
  };

  const formData = (event: React.FormEvent<HTMLFormElement>) => new FormData(event.currentTarget);
  const text = (data: FormData, name: string): string => String(data.get(name) ?? "").trim();
  const optionalText = (data: FormData, name: string): string | null => {
    const value = text(data, name);
    return value === "" ? null : value;
  };
  const numberField = (data: FormData, name: string): number => Number(text(data, name));

  const submitFor = (action: ActionId) => async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = formData(event);
    switch (action) {
      case "confirm-receipt":
        return run(() =>
          api.confirmChequeReceipt(cheque.id, {
            items_matched: itemsMatched,
            sayad_accepted: sayadAccepted,
            color_code: colorCode === "" ? null : Number(colorCode),
            back_cheques_count: text(data, "back_cheques_count") === "" ? null : Number(text(data, "back_cheques_count")),
            in_transit_commitments_irr: text(data, "in_transit_commitments_irr") === "" ? null : Number(text(data, "in_transit_commitments_irr")),
            manager_approval: optionalText(data, "manager_approval"),
            storage_location: storageLocation === "" ? null : storageLocation,
          }),
        );
      case "reject-mismatch":
        return run(() => api.rejectChequeMismatch(cheque.id, { mismatch_detail: text(data, "mismatch_detail") }));
      case "issue":
        return run(() =>
          api.issueCheque(cheque.id, {
            destination_account_id: numberField(data, "destination_account_id"),
            destination_bank_name: optionalText(data, "destination_bank_name"),
          }),
        );
      case "deposit":
        return run(() =>
          api.depositCheque(cheque.id, {
            destination_account_id: numberField(data, "destination_account_id"),
            destination_bank_name: optionalText(data, "destination_bank_name"),
          }),
        );
      case "endorse":
        return run(() =>
          api.endorseCheque(cheque.id, {
            endorsee_name: text(data, "endorsee_name"),
            endorsee_type: endorseeType,
            sayad_transfer_reference: text(data, "sayad_transfer_reference"),
          }),
        );
      case "return":
        return run(() => api.returnCheque(cheque.id));
      case "call-in":
        return run(() => api.callInCheque(cheque.id, { accountant_approval: text(data, "accountant_approval") }));
      case "clear":
        return run(() => api.clearCheque(cheque.id, { statement_reference: optionalText(data, "statement_reference") }));
      case "bounce":
        if (!bounceIso) {
          setActionError("تاریخ برگشت را انتخاب کنید.");
          return;
        }
        return run(() =>
          api.bounceCheque(cheque.id, {
            bounce_reason: text(data, "bounce_reason"),
            bounce_date: isoToJalaliString(bounceIso),
            certificate_reference: optionalText(data, "certificate_reference"),
          }),
        );
      case "clearing-confirm":
        return run(() =>
          api.confirmClearingMatch(cheque.id, {
            statement_id: numberField(data, "statement_id"),
            line_id: numberField(data, "line_id"),
          }),
        );
      case "void":
        return run(() => api.voidIssuedCheque(cheque.id, { void_reason: text(data, "void_reason") }));
      case "unexpected-call-in":
        return run(() =>
          api.unexpectedCallInCheque(cheque.id, { statement_reference: text(data, "statement_reference") }),
        );
      case "endorsed-bounce":
        if (!endorsedBounceIso) {
          setActionError("تاریخ برگشت را انتخاب کنید.");
          return;
        }
        return run(() =>
          api.endorsedBounceCheque(cheque.id, {
            bounce_reason: text(data, "bounce_reason"),
            bounce_date: isoToJalaliString(endorsedBounceIso),
            certificate_reference: text(data, "certificate_reference"),
          }),
        );
      case "receipt":
        return run(() =>
          api.recordInquiryReceipt(cheque.id, {
            channel: receiptChannel,
            inquiry_type: receiptType,
            result_code: text(data, "result_code"),
            result_summary: text(data, "result_summary"),
            reference: text(data, "reference"),
            cost_irr: text(data, "cost_irr") === "" ? 0 : Number(text(data, "cost_irr")),
          }),
        );
      case "sayyad":
        return run(() =>
          api.runSayyadInquiry(cheque.id, { scope: sayyadScope, scenario: sayyadScenario === "none" ? null : sayyadScenario }),
        );
      case "ehrazino":
        return run(() =>
          api.runEhrazinoInquiry(cheque.id, { kind: ehrazinoKind, scenario: ehrazinoScenario === "none" ? null : ehrazinoScenario }),
        );
      default:
        return;
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <p className="text-sm text-muted-foreground">
          <Link href="/cheques" className="font-medium text-primary hover:underline">
            مرکز چک
          </Link>
          {" / "}جزئیات چک
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-bold" dir="ltr">
            {cheque.sayad_id}
          </h1>
          <ChequeStateBadge state={cheque.state} />
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          {chequeKindLabel(cheque.kind)} · {chequePurposeLabel(cheque.purpose)} · مبدأ: {chequeSourceLabel(cheque)}
        </p>
      </div>

      {cheque.warnings.length > 0 ? (
        <div className="space-y-2 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3" role="alert">
          {cheque.warnings.map((warning) => (
            <p key={warning} className="flex items-start gap-2 text-sm text-warning-foreground">
              <TriangleAlert className="mt-1 size-4 shrink-0" aria-hidden />
              {warning}
            </p>
          ))}
        </div>
      ) : null}
      {cheque.unexpected_call_in ? (
        <p className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive" role="alert">
          این چک پشتوانهٔ صادرشده بدون اطلاع فراخوان شده است؛ سند تجاری آن در انتظار حسابدار است و ثبت نشده است.
        </p>
      ) : null}
      {cheque.journal_pending_review ? (
        <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground" role="alert">
          سند تجاری این چک در انتظار حسابدار است — تا طراحی و تأیید حسابدار، سندی ثبت نمی‌شود.
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>مشخصات چک</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-x-8 gap-y-3 text-sm sm:grid-cols-2">
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">مبلغ</dt>
              <dd><MoneyDisplay amountIrr={cheque.amount_irr} size="sm" /></dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">صادرکننده</dt>
              <dd>{cheque.issuer_name}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">بانک صادرکننده</dt>
              <dd>{cheque.issuer_bank}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">ذی‌نفع</dt>
              <dd>{cheque.payee_name}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">تاریخ صدور</dt>
              <dd>{formatJalaliLong(cheque.issue_date)}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">سررسید</dt>
              <dd>{formatJalaliLong(cheque.due_date)}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">شمارهٔ سری برگ</dt>
              <dd dir="ltr">{cheque.leaf_number ?? "—"}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">وضعیت صیاد</dt>
              <dd>{sayadStatusLabel(cheque.sayad_status)}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">محل نگهداری</dt>
              <dd>{storageLocationLabel(cheque.storage_location)}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted-foreground">رنگ صادرکننده</dt>
              <dd>{issuerColorLabel(cheque.issuer_color_code)}{cheque.issuer_back_cheques !== null ? ` · ${faNumber(cheque.issuer_back_cheques)} چک برگشتی` : ""}</dd>
            </div>
            {cheque.secured_obligation ? (
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">تعهد تحت پوشش</dt>
                <dd>{cheque.secured_obligation}</dd>
              </div>
            ) : null}
            {cheque.endorsee_name ? (
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">ظهرنویس‌شده به</dt>
                <dd>{cheque.endorsee_name} ({cheque.endorsee_type === "supplier" ? "تأمین‌کننده" : "شخص دیگر"})</dd>
              </div>
            ) : null}
            {cheque.bounce_reason ? (
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">دلیل برگشت</dt>
                <dd>{cheque.bounce_reason}{cheque.bounce_date ? ` · ${formatJalaliShort(cheque.bounce_date)}` : ""}</dd>
              </div>
            ) : null}
            {cheque.invoice_id ? (
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">فاکتور مرتبط</dt>
                <dd dir="ltr">{cheque.invoice_id}</dd>
              </div>
            ) : null}
            {cheque.installment_leg_id ? (
              <div className="flex justify-between gap-4">
                <dt className="text-muted-foreground">قسط مرتبط</dt>
                <dd dir="ltr">{cheque.installment_leg_id}{cheque.installment_leg_status ? ` (${cheque.installment_leg_status})` : ""}</dd>
              </div>
            ) : null}
            {Object.keys(cheque.move_ids).length > 0 ? (
              <div className="flex justify-between gap-4 sm:col-span-2">
                <dt className="text-muted-foreground">سندهای حسابداری</dt>
                <dd dir="ltr">{Object.entries(cheque.move_ids).map(([key, id]) => `${key}: #${id}`).join(" · ")}</dd>
              </div>
            ) : null}
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>چرخهٔ حیات</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="space-y-3">
            {steps.map((step) => (
              <li key={step.key} className="flex items-start gap-3 text-sm">
                {step.done ? (
                  <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-success" aria-hidden />
                ) : (
                  <Circle className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden />
                )}
                <span className={step.done ? "font-medium" : "text-muted-foreground"}>
                  {step.label}
                  {step.date ? ` · ${formatJalaliShort(step.date)}` : ""}
                </span>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>لاگ نگهداری برگ</CardTitle>
        </CardHeader>
        <CardContent>
          {cheque.custody_entries.length === 0 ? (
            <p className="text-sm text-muted-foreground">هنوز جابه‌جایی نگهداری ثبت نشده است؛ جابه‌جایی صندوق↔خزانه رویداد نگهداری است، بدون سند.</p>
          ) : (
            <ol className="space-y-3">
              {cheque.custody_entries.map((entry) => (
                <li key={entry.id} className="flex flex-wrap items-start justify-between gap-2 text-sm">
                  <span>
                    {entry.from_location ? `${storageLocationLabel(entry.from_location)} ← ` : ""}
                    {storageLocationLabel(entry.to_location)}
                    {" · "}{entry.reason}
                    {" · "}{entry.actor}
                  </span>
                  <span className="text-xs text-muted-foreground">{formatIsoDateTime(entry.at)}</span>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>رسیدهای استعلام صیاد</CardTitle>
        </CardHeader>
        <CardContent>
          {cheque.inquiry_receipts.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              هنوز رسید استعلامی ثبت نشده است. {cheque.state === "held" && cheque.receipt_missing_warning ? "این چک بدون رسید استعلام ثبت شد — در گزارش مغایرت‌ها دیده می‌شود." : ""}
            </p>
          ) : (
            <ul className="space-y-3">
              {cheque.inquiry_receipts.map((receipt) => (
                <li key={receipt.id} className="flex flex-wrap items-start justify-between gap-2 border-b pb-3 text-sm last:border-0 last:pb-0">
                  <span>
                    {inquiryChannelLabel(receipt.channel)} · {inquiryTypeLabel(receipt.inquiry_type)}
                    <span className="block text-xs text-muted-foreground">
                      {receipt.result_code} · {receipt.result_summary} · مرجع: <span dir="ltr">{receipt.reference}</span>
                    </span>
                  </span>
                  <span className="text-xs text-muted-foreground">
                    <MoneyDisplay amountIrr={receipt.cost_irr} size="sm" />
                    {receipt.responded_at ? ` · ${formatIsoDateTime(receipt.responded_at)}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-4 text-xs text-muted-foreground">
            فقط رأی/کد/زمان ذخیره می‌شود، نه محتوای خام استعلام. استعلام‌های فینوتک و احرازینو تا ۵ت شبیه‌ساز هستند.
          </p>
        </CardContent>
      </Card>

      <div className="space-y-4">
        <h2 className="text-lg font-bold">اقدام‌ها</h2>
        {actions.length === 0 ? (
          <p className="text-sm text-muted-foreground">این چک در وضعیت پایانی است؛ اقدامی ندارد.</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {actions.map((action) => (
              <Button
                key={action}
                variant={openAction === action ? "default" : "outline"}
                onClick={() => {
                  setOpenAction(openAction === action ? null : action);
                  setActionError(null);
                }}
              >
                {ACTION_LABELS[action]}
              </Button>
            ))}
            <Button variant={openAction === "receipt" ? "default" : "outline"} onClick={() => { setOpenAction(openAction === "receipt" ? null : "receipt"); setActionError(null); }}>
              {ACTION_LABELS.receipt}
            </Button>
            <Button variant={openAction === "sayyad" ? "default" : "outline"} onClick={() => { setOpenAction(openAction === "sayyad" ? null : "sayyad"); setActionError(null); }}>
              {ACTION_LABELS.sayyad}
            </Button>
            <Button variant={openAction === "ehrazino" ? "default" : "outline"} onClick={() => { setOpenAction(openAction === "ehrazino" ? null : "ehrazino"); setActionError(null); }}>
              {ACTION_LABELS.ehrazino}
            </Button>
          </div>
        )}

        {openAction === "confirm-receipt" ? (
          <ActionForm title="تأیید رسید برگ (ثبت‌شده ← نزد ما)" onSubmit={submitFor("confirm-receipt")} busy={busy} error={actionError} submitLabel="تأیید رسید برگ">
            <div className="flex items-center gap-2 sm:col-span-2">
              <Checkbox id="confirm-items" checked={itemsMatched} onCheckedChange={(c) => setItemsMatched(c === true)} />
              <Label htmlFor="confirm-items">هر چهار قلم صیاد (شناسه، مبلغ، سررسید، هویت ذی‌نفع) با برگ مطابقت دارد</Label>
            </div>
            <div className="flex items-center gap-2 sm:col-span-2">
              <Checkbox id="confirm-sayad" checked={sayadAccepted} onCheckedChange={(c) => setSayadAccepted(c === true)} />
              <Label htmlFor="confirm-sayad">رسید در صیاد پذیرفته شد (اپ بانک / پیامک / فینوتک)</Label>
            </div>
            <Field label="رنگ صادرکننده (۱ سفید تا ۵ قرمز)" htmlFor="confirm-color">
              <Select value={colorCode} onValueChange={(v) => setColorCode(v ?? "")}>
                <SelectTrigger id="confirm-color">
                  <SelectValue placeholder="انتخاب رنگ">{colorCode === "" ? "انتخاب رنگ" : issuerColorLabel(Number(colorCode))}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {[1, 2, 3, 4, 5].map((code) => (
                    <SelectItem key={code} value={String(code)}>{issuerColorLabel(code)}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="تعداد چک‌های برگشتی صادرکننده" htmlFor="confirm-back">
              <Input id="confirm-back" name="back_cheques_count" dir="ltr" inputMode="numeric" placeholder="۰" />
            </Field>
            <Field label="تعهدات در راه صادرکننده (ریال)" htmlFor="confirm-transit">
              <Input id="confirm-transit" name="in_transit_commitments_irr" dir="ltr" inputMode="numeric" />
            </Field>
            <Field label="تأییدکنندهٔ مدیر (در صورت نیاز سیاست)" htmlFor="confirm-manager">
              <Input id="confirm-manager" name="manager_approval" />
            </Field>
            <Field label="محل نگهداری برگ" htmlFor="confirm-storage">
              <Select value={storageLocation} onValueChange={(v) => setStorageLocation(v ?? "")}>
                <SelectTrigger id="confirm-storage">
                  <SelectValue placeholder="پیش‌فرض نصب">{storageLocation === "" ? "پیش‌فرض نصب" : storageLocationLabel(storageLocation)}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="cashbox">صندوق</SelectItem>
                  <SelectItem value="vault">خزانه</SelectItem>
                  <SelectItem value="bank">بانک</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <p className="text-xs text-muted-foreground sm:col-span-2">
              بدون رسید استعلام، گذار با هشدار «بدون رسید» ثبت می‌شود و در گزارش مغایرت‌ها می‌آید.
            </p>
          </ActionForm>
        ) : null}

        {openAction === "reject-mismatch" ? (
          <ActionForm title="رد مغایرت (برگ با صیاد نمی‌خواند)" onSubmit={submitFor("reject-mismatch")} busy={busy} error={actionError} submitLabel="رد چک">
            <div className="sm:col-span-2">
              <Field label="چه چیزی مغایرت دارد (سایت در برابر برگ در برابر صیاد)" htmlFor="reject-detail">
                <Textarea id="reject-detail" name="mismatch_detail" />
              </Field>
            </div>
          </ActionForm>
        ) : null}

        {openAction === "issue" ? (
          <ActionForm title="صدور چک از دسته‌چک" onSubmit={submitFor("issue")} busy={busy} error={actionError} submitLabel="صدور چک">
            <Field label="شناسهٔ حساب بانکی مقصد (موتور)" htmlFor="issue-account">
              <Input id="issue-account" name="destination_account_id" dir="ltr" inputMode="numeric" required />
            </Field>
            <Field label="نام حساب مقصد (اختیاری)" htmlFor="issue-bank-name">
              <Input id="issue-bank-name" name="destination_bank_name" />
            </Field>
          </ActionForm>
        ) : null}

        {openAction === "deposit" ? (
          <ActionForm title="خواباندن چک (نزد ما ← در جریان وصول)" onSubmit={submitFor("deposit")} busy={busy} error={actionError} submitLabel="ارسال به بانک">
            <Field label="شناسهٔ حساب بانکی مقصد (موتور)" htmlFor="deposit-account">
              <Input id="deposit-account" name="destination_account_id" dir="ltr" inputMode="numeric" required />
            </Field>
            <Field label="نام حساب مقصد (اختیاری)" htmlFor="deposit-bank-name">
              <Input id="deposit-bank-name" name="destination_bank_name" />
            </Field>
          </ActionForm>
        ) : null}

        {openAction === "endorse" ? (
          <ActionForm title="ظهرنویسی چک" onSubmit={submitFor("endorse")} busy={busy} error={actionError} submitLabel="ثبت ظهرنویسی">
            <Field label="نام ظهرنویس‌شونده" htmlFor="endorse-name">
              <Input id="endorse-name" name="endorsee_name" required />
            </Field>
            <Field label="نوع ظهرنویس‌شونده" htmlFor="endorse-type">
              <Select value={endorseeType} onValueChange={(v) => setEndorseeType(v ?? "supplier")}>
                <SelectTrigger id="endorse-type">
                  <SelectValue>{endorseeType === "supplier" ? "تأمین‌کننده" : "شخص دیگر"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="supplier">تأمین‌کننده</SelectItem>
                  <SelectItem value="other">شخص دیگر</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <div className="sm:col-span-2">
              <Field label="رسید ثبت انتقال در صیاد" htmlFor="endorse-ref">
                <Input id="endorse-ref" name="sayad_transfer_reference" dir="ltr" required />
              </Field>
            </div>
            {endorseeType === "other" ? (
              <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground sm:col-span-2" role="note">
                ظهرنویسی به شخص دیگر نیازمند تأیید حسابدار است: وضعیت چک جابه‌جا می‌شود و انتقال در صیاد ثبت می‌شود، اما هیچ سندی حدس زده نمی‌شود — سند در انتظار حسابدار می‌ماند.
              </p>
            ) : null}
          </ActionForm>
        ) : null}

        {openAction === "return" ? (
          <ActionForm title="استرداد برگ به صادرکننده" onSubmit={submitFor("return")} busy={busy} error={actionError} submitLabel="استرداد برگ">
            <p className="text-sm text-muted-foreground sm:col-span-2">برگ به صادرکننده برمی‌گردد؛ از «نزد ما» سند ۶ و از «برگشتی» بدون سند.</p>
          </ActionForm>
        ) : null}

        {openAction === "call-in" ? (
          <ActionForm title="فراخوان چک پشتوانه (نیازمند تأیید حسابدار)" onSubmit={submitFor("call-in")} busy={busy} error={actionError} submitLabel="فراخوان چک">
            <div className="sm:col-span-2">
              <Field label="نام حسابدار تأییدکننده" htmlFor="callin-approval">
                <Input id="callin-approval" name="accountant_approval" required />
              </Field>
            </div>
            <p className="text-sm text-muted-foreground sm:col-span-2">
              فراخوان دو سند (۹الف + ۹ب) در یک تراکنش ثبت می‌کند و چک از انتظامی به تجاری می‌رود.
            </p>
          </ActionForm>
        ) : null}

        {openAction === "clear" ? (
          <ActionForm title="تأیید وصول (در جریان وصول ← وصول‌شده)" onSubmit={submitFor("clear")} busy={busy} error={actionError} submitLabel="تأیید وصول">
            <div className="sm:col-span-2">
              <Field label="مرجع صورت‌حساب بانکی (اختیاری — تطبیق خودکار وصول)" htmlFor="clear-ref">
                <Input id="clear-ref" name="statement_reference" dir="ltr" />
              </Field>
            </div>
          </ActionForm>
        ) : null}

        {openAction === "bounce" ? (
          <ActionForm title="ثبت برگشت چک" onSubmit={submitFor("bounce")} busy={busy} error={actionError} submitLabel="ثبت برگشت">
            <div className="sm:col-span-2">
              <Field label="دلیل برگشت" htmlFor="bounce-reason">
                <Input id="bounce-reason" name="bounce_reason" required />
              </Field>
            </div>
            <Field label="تاریخ برگشت" htmlFor="bounce-date">
              <JalaliDatePicker id="bounce-date" value={bounceIso} onChange={setBounceIso} />
            </Field>
            <Field label="مرجع گواهی عدم پرداخت بانک" htmlFor="bounce-cert">
              <Input id="bounce-cert" name="certificate_reference" dir="ltr" />
            </Field>
            <p className="text-sm text-muted-foreground sm:col-span-2">
              برگشت در دورهٔ جاری معکوس می‌شود و طلب به فاکتور اصلی برمی‌گردد.
            </p>
          </ActionForm>
        ) : null}

        {openAction === "clearing-confirm" ? (
          <ActionForm title="تأیید تطبیق وصول (پیشنهاد محتمل/مبهم)" onSubmit={submitFor("clearing-confirm")} busy={busy} error={actionError} submitLabel="تأیید تطبیق و وصول">
            <Field label="شناسهٔ صورت‌حساب (موتور)" htmlFor="clearing-statement">
              <Input id="clearing-statement" name="statement_id" dir="ltr" inputMode="numeric" required />
            </Field>
            <Field label="شناسهٔ سطر صورت‌حساب (موتور)" htmlFor="clearing-line">
              <Input id="clearing-line" name="line_id" dir="ltr" inputMode="numeric" required />
            </Field>
            <p className="text-sm text-muted-foreground sm:col-span-2">
              سطر باید پیشنهاد تطبیق برای همین چک داشته باشد؛ تطبیق مبهم هرگز خودکار انجام نمی‌شود — انسان انتخاب می‌کند.
            </p>
          </ActionForm>
        ) : null}

        {openAction === "void" ? (
          <ActionForm title="باطل کردن چک پرداختی" onSubmit={submitFor("void")} busy={busy} error={actionError} submitLabel="باطل کردن چک">
            <div className="sm:col-span-2">
              <Field label="دلیل باطل کردن" htmlFor="void-reason">
                <Textarea id="void-reason" name="void_reason" required />
              </Field>
            </div>
          </ActionForm>
        ) : null}

        {openAction === "unexpected-call-in" ? (
          <ActionForm title="ثبت فراخوان ناخواستهٔ چک پشتوانهٔ صادرشده" onSubmit={submitFor("unexpected-call-in")} busy={busy} error={actionError} submitLabel="ثبت فراخوان ناخواسته">
            <div className="sm:col-span-2">
              <Field label="مرجع برداشت از صورت‌حساب (مطابقت با شمارهٔ برگ)" htmlFor="uci-ref">
                <Input id="uci-ref" name="statement_reference" dir="ltr" required />
              </Field>
            </div>
            <p className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground sm:col-span-2" role="note">
              سند تجاری این فراخوان در انتظار حسابدار می‌ماند — ثبت نمی‌شود تا حسابدار آن را طراحی کند.
            </p>
          </ActionForm>
        ) : null}

        {openAction === "endorsed-bounce" ? (
          <ActionForm title="ثبت برگشت چک ظهرنویسی‌شده" onSubmit={submitFor("endorsed-bounce")} busy={busy} error={actionError} submitLabel="ثبت برگشت">
            <div className="sm:col-span-2">
              <Field label="دلیل برگشت" htmlFor="eb-reason">
                <Input id="eb-reason" name="bounce_reason" required />
              </Field>
            </div>
            <Field label="تاریخ برگشت" htmlFor="eb-date">
              <JalaliDatePicker id="eb-date" value={endorsedBounceIso} onChange={setEndorsedBounceIso} />
            </Field>
            <Field label="مرجع گواهی عدم پرداخت بانک" htmlFor="eb-cert">
              <Input id="eb-cert" name="certificate_reference" dir="ltr" required />
            </Field>
            <p className="text-sm text-muted-foreground sm:col-span-2">
              سندی حدس زده نمی‌شود؛ مدرک ثبت می‌شود و آیتم بحرانی در صف بازبینی تشکیل می‌شود.
            </p>
          </ActionForm>
        ) : null}

        {openAction === "receipt" ? (
          <ActionForm title="ثبت رسید استعلام دستی" onSubmit={submitFor("receipt")} busy={busy} error={actionError} submitLabel="ثبت رسید">
            <Field label="کانال استعلام" htmlFor="receipt-channel">
              <Select value={receiptChannel} onValueChange={(v) => setReceiptChannel(v ?? "sms")}>
                <SelectTrigger id="receipt-channel">
                  <SelectValue>{INQUIRY_CHANNEL_LABELS[receiptChannel] ?? receiptChannel}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(INQUIRY_CHANNEL_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>{label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="نوع استعلام" htmlFor="receipt-type">
              <Select value={receiptType} onValueChange={(v) => setReceiptType(v ?? "color")}>
                <SelectTrigger id="receipt-type">
                  <SelectValue>{INQUIRY_TYPE_LABELS[receiptType] ?? receiptType}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(INQUIRY_TYPE_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>{label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="کد نتیجه (مثلاً color=3)" htmlFor="receipt-code">
              <Input id="receipt-code" name="result_code" dir="ltr" required />
            </Field>
            <Field label="خلاصهٔ نتیجه" htmlFor="receipt-summary">
              <Input id="receipt-summary" name="result_summary" required />
            </Field>
            <Field label="مرجع / شمارهٔ پیگیری" htmlFor="receipt-ref">
              <Input id="receipt-ref" name="reference" dir="ltr" required />
            </Field>
            <Field label="هزینه (ریال) — پیامک ۳٬۵۰۰ ریال" htmlFor="receipt-cost">
              <Input id="receipt-cost" name="cost_irr" dir="ltr" inputMode="numeric" placeholder="0" />
            </Field>
          </ActionForm>
        ) : null}

        {openAction === "sayyad" ? (
          <ActionForm title="استعلام صیاد — فینوتک (شبیه‌ساز تا ۵ت)" onSubmit={submitFor("sayyad")} busy={busy} error={actionError} submitLabel="اجرای استعلام">
            <div className="sm:col-span-2">
              <Field label="اسکوپ فینوتک" htmlFor="sayyad-scope">
                <Select value={sayyadScope} onValueChange={(v) => setSayyadScope(v ?? FINNOTECH_SAYYAD_SCOPES[3].scope)}>
                  <SelectTrigger id="sayyad-scope">
                    <SelectValue>{FINNOTECH_SAYYAD_SCOPES.find((s) => s.scope === sayyadScope)?.label ?? sayyadScope}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {FINNOTECH_SAYYAD_SCOPES.map((s) => (
                      <SelectItem key={s.scope} value={s.scope}>{s.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            </div>
            <Field label="سناریوی تمرینی (اختیاری)" htmlFor="sayyad-scenario">
              <Select value={sayyadScenario} onValueChange={(v) => setSayyadScenario(v ?? "none")}>
                <SelectTrigger id="sayyad-scenario">
                  <SelectValue>{sayyadScenario === "none" ? "بدون سناریو" : sayyadScenario === "clean" ? "تمیز (سفید، بدون برگشتی)" : "قرمز"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">بدون سناریو</SelectItem>
                  <SelectItem value="clean">تمیز (سفید، بدون برگشتی)</SelectItem>
                  <SelectItem value="red">قرمز</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <p className="text-sm text-muted-foreground sm:col-span-2">
              بدون سناریو، شبیه‌ساز بدون دادهٔ از پیش‌نشده برای این شناسه «یافت نشد» می‌دهد — هرگز رأی نمی‌سازد.
            </p>
          </ActionForm>
        ) : null}

        {openAction === "ehrazino" ? (
          <ActionForm title="استعلام احرازینو (شبیه‌ساز تا ۵ت)" onSubmit={submitFor("ehrazino")} busy={busy} error={actionError} submitLabel="اجرای استعلام">
            <Field label="نوع استعلام" htmlFor="ehrazino-kind">
              <Select value={ehrazinoKind} onValueChange={(v) => setEhrazinoKind(v ?? "color")}>
                <SelectTrigger id="ehrazino-kind">
                  <SelectValue>{ehrazinoKind === "color" ? "رنگ (۱٬۷۰۰ تومان)" : "بررسی صیاد (۶٬۹۰۰ تومان)"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="color">رنگ (۱٬۷۰۰ تومان)</SelectItem>
                  <SelectItem value="sayad_check">بررسی صیاد (۶٬۹۰۰ تومان)</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field label="سناریوی تمرینی (اختیاری)" htmlFor="ehrazino-scenario">
              <Select value={ehrazinoScenario} onValueChange={(v) => setEhrazinoScenario(v ?? "none")}>
                <SelectTrigger id="ehrazino-scenario">
                  <SelectValue>{ehrazinoScenario === "none" ? "بدون سناریو" : ehrazinoScenario === "clean" ? "تمیز" : "قرمز"}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">بدون سناریو</SelectItem>
                  <SelectItem value="clean">تمیز</SelectItem>
                  <SelectItem value="red">قرمز</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <p className="text-sm text-muted-foreground sm:col-span-2">
              رنگ فقط سابقهٔ صادرکننده است — هرگز حکمی دربارهٔ این برگ نیست.
            </p>
          </ActionForm>
        ) : null}
      </div>
    </div>
  );
}
