/**
 * Manual invoice entry (US-513, Phase 5 wave 2, stream S3) — the
 * dual-entry path: «فروش و خرید ← فاکتور جدید (دستی)» (phased design
 * §7).
 *
 * A manual order/refund/cancellation produces the SAME canonical
 * event a store webhook would produce (`source_system="manual"`) and
 * travels the SAME §10.3 pipeline (validation, mapping, policy gate,
 * audit log). The page shows both sides of that contract: the entry
 * forms (Toman amounts; the pipeline converts ×10 to Rial) and the
 * list of entries with each primary event's pipeline outcome, plus a
 * detail dialog tracing an entry to its events, decisions and engine
 * records.
 *
 * Data is live from the backend via the BFF. Nothing here is fake:
 * an empty list renders an honest empty state.
 */

"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Plus, Trash2 } from "lucide-react";
import {
  ApiError,
  api,
  type CreateManualCancellationRequest,
  type CreateManualOrderRequest,
  type CreateManualRefundRequest,
  type ManualEntryEventResponse,
  type ManualEntryResponse,
} from "@/lib/api-client";
import { formatIsoDateTime } from "@/lib/format-date";
import { todayIso } from "@/lib/jalali";
import { fa, faNumber } from "@/lib/utils";
import {
  AmountInput,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Checkbox,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Input,
  JalaliDatePicker,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
  toast,
} from "@/components/ui";
import { ErrorState, PermissionDeniedState } from "@/features/shared/states";
import { isPermissionDenied, persianApiErrorMessage } from "@/features/shared/api-error";
import {
  computeOrderTotals,
  decisionLabel,
  decisionTone,
  eventTypeLabel,
  formatTomanAmount,
  inboxStatusLabel,
  inboxStatusTone,
  manualEntryTypeLabel,
  occurredAtIso,
  parseManualQuantity,
  type ManualOrderLineDraft,
} from "./manual-entries-data";

const PAGE_SIZE = 20;

type EntryTab = "order" | "refund" | "cancellation";

interface OrderLineState extends ManualOrderLineDraft {
  key: number;
  description: string;
}

interface RefundLineState {
  key: number;
  description: string;
  lineTotalToman: number | null;
}

let lineKey = 0;

const EMPTY_ORDER_LINE = (): OrderLineState => ({
  key: lineKey++,
  description: "",
  quantity: "۱",
  unitPriceToman: null,
  discountToman: null,
});

const EMPTY_REFUND_LINE = (): RefundLineState => ({
  key: lineKey++,
  description: "",
  lineTotalToman: null,
});

function FieldError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="text-sm text-destructive">
      {message}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Order form
// ---------------------------------------------------------------------------

function OrderForm({ onCreated }: { onCreated: () => void }) {
  const [orderNumber, setOrderNumber] = React.useState("");
  const [customerName, setCustomerName] = React.useState("");
  const [customerRef, setCustomerRef] = React.useState("");
  const [date, setDate] = React.useState<string | null>(() => todayIso());
  const [lines, setLines] = React.useState<OrderLineState[]>([EMPTY_ORDER_LINE()]);
  const [discount, setDiscount] = React.useState<number | null>(null);
  const [shipping, setShipping] = React.useState<number | null>(null);
  const [tax, setTax] = React.useState<number | null>(null);
  const [note, setNote] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [touched, setTouched] = React.useState(false);

  const totals = computeOrderTotals(lines, discount, shipping, tax);
  const orderNumberError = touched && !orderNumber.trim() ? "شمارهٔ سفارش الزامی است." : null;
  const linesError =
    touched && lines.some((line) => !line.description.trim())
      ? "شرح هر ردیف الزامی است."
      : null;
  const quantityError =
    touched && lines.some((line) => parseManualQuantity(line.quantity) === null)
      ? "تعداد هر ردیف باید عددی بزرگ‌تر از صفر باشد."
      : null;
  const fractionalError =
    totals.fractionalLines.length > 0
      ? `مبلغ ردیف ${faNumber(totals.fractionalLines[0] + 1)} عدد صحیح تومان نیست؛ تعداد یا قیمت واحد را اصلاح کنید.`
      : null;
  const totalError =
    touched && totals.totalToman <= 0 ? "مبلغ کل سفارش باید بزرگ‌تر از صفر باشد." : null;
  const canSubmit =
    !submitting &&
    orderNumber.trim() !== "" &&
    lines.length > 0 &&
    lines.every((line) => line.description.trim() !== "") &&
    totals.valid;

  const updateLine = (key: number, patch: Partial<OrderLineState>) => {
    setLines((prev) => prev.map((line) => (line.key === key ? { ...line, ...patch } : line)));
  };

  async function submit() {
    setTouched(true);
    setSubmitError(null);
    if (!canSubmit) return;
    const body: CreateManualOrderRequest = {
      order_number: orderNumber.trim(),
      customer_name: customerName.trim() || null,
      customer_ref: customerRef.trim() || null,
      lines: lines.map((line) => ({
        description: line.description.trim(),
        // Fractions travel as decimal strings — never a binary float.
        quantity: (() => {
          const parsed = parseManualQuantity(line.quantity) ?? 0;
          return Number.isInteger(parsed) ? parsed : String(parsed);
        })(),
        unit_price_toman: line.unitPriceToman ?? 0,
        discount_toman: line.discountToman ?? 0,
      })),
      discount_toman: discount ?? 0,
      shipping_toman: shipping ?? 0,
      tax_toman: tax ?? 0,
      occurred_at: occurredAtIso(date),
      note: note.trim() || null,
    };
    setSubmitting(true);
    try {
      const created = await api.createManualOrder(body);
      toast.success("سفارش دستی ثبت شد؛ رویداد آن در صف پردازش قرار گرفت.");
      setOrderNumber("");
      setCustomerName("");
      setCustomerRef("");
      setDate(todayIso());
      setLines([EMPTY_ORDER_LINE()]);
      setDiscount(null);
      setShipping(null);
      setTax(null);
      setNote("");
      setTouched(false);
      onCreated();
      return created;
    } catch (err) {
      setSubmitError(persianApiErrorMessage(err, "ثبت سفارش ناموفق بود؛ دوباره تلاش کنید."));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="manual-order-number">شمارهٔ سفارش *</Label>
          <Input
            id="manual-order-number"
            data-testid="manual-order-number"
            value={orderNumber}
            onChange={(event) => setOrderNumber(event.target.value)}
            placeholder="مثلاً ORD-1001"
            dir="ltr"
            className="text-start"
          />
          <FieldError message={orderNumberError} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-order-date">تاریخ سفارش</Label>
          <JalaliDatePicker
            id="manual-order-date"
            value={date}
            onChange={setDate}
            placeholder="امروز"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-order-customer">نام مشتری</Label>
          <Input
            id="manual-order-customer"
            value={customerName}
            onChange={(event) => setCustomerName(event.target.value)}
            placeholder="اختیاری"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-order-customer-ref">مرجع مشتری</Label>
          <Input
            id="manual-order-customer-ref"
            value={customerRef}
            onChange={(event) => setCustomerRef(event.target.value)}
            placeholder="اختیاری"
            dir="ltr"
            className="text-start"
          />
        </div>
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between gap-4">
          <h3 className="font-medium">ردیف‌های سفارش</h3>
          <Button
            type="button"
            variant="outline"
            onClick={() => setLines((prev) => [...prev, EMPTY_ORDER_LINE()])}
            className="min-h-11"
          >
            <Plus aria-hidden />
            افزودن ردیف
          </Button>
        </div>
        {lines.map((line, index) => (
          <Card key={line.key} data-testid="manual-order-line">
            <CardContent className="space-y-4 pt-6">
              <div className="flex items-start justify-between gap-2">
                <p className="font-medium">ردیف {faNumber(index + 1)}</p>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`حذف ردیف ${faNumber(index + 1)}`}
                  disabled={lines.length === 1}
                  onClick={() => setLines((prev) => prev.filter((item) => item.key !== line.key))}
                  className="h-11 w-11 shrink-0"
                >
                  <Trash2 aria-hidden />
                </Button>
              </div>
              <div className="space-y-2">
                <Label htmlFor={`manual-order-line-desc-${index}`}>شرح *</Label>
                <Input
                  id={`manual-order-line-desc-${index}`}
                  data-testid={`manual-order-line-desc-${index}`}
                  value={line.description}
                  onChange={(event) => updateLine(line.key, { description: event.target.value })}
                  placeholder="نام کالا یا خدمت"
                />
              </div>
              <div className="grid gap-4 sm:grid-cols-3">
                <div className="space-y-2">
                  <Label htmlFor={`manual-order-line-qty-${index}`}>تعداد *</Label>
                  <Input
                    id={`manual-order-line-qty-${index}`}
                    data-testid={`manual-order-line-qty-${index}`}
                    value={line.quantity}
                    inputMode="decimal"
                    onChange={(event) => updateLine(line.key, { quantity: event.target.value })}
                    dir="ltr"
                    className="text-start"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor={`manual-order-line-price-${index}`}>قیمت واحد (تومان) *</Label>
                  <AmountInput
                    id={`manual-order-line-price-${index}`}
                    value={line.unitPriceToman}
                    onChange={(value) => updateLine(line.key, { unitPriceToman: value })}
                    unit="تومان"
                    min={0}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor={`manual-order-line-discount-${index}`}>تخفیف ردیف (تومان)</Label>
                  <AmountInput
                    id={`manual-order-line-discount-${index}`}
                    value={line.discountToman}
                    onChange={(value) => updateLine(line.key, { discountToman: value })}
                    unit="تومان"
                    min={0}
                  />
                </div>
              </div>
              {totals.fractionalLines.includes(index) ? (
                <p role="alert" className="text-sm text-destructive">
                  مبلغ این ردیف عدد صحیح تومان نیست؛ تعداد یا قیمت واحد را اصلاح کنید.
                </p>
              ) : null}
            </CardContent>
          </Card>
        ))}
        <FieldError message={linesError} />
        <FieldError message={quantityError} />
        <FieldError message={fractionalError} />
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-2">
          <Label htmlFor="manual-order-discount">تخفیف سفارش (تومان)</Label>
          <AmountInput id="manual-order-discount" value={discount} onChange={setDiscount} unit="تومان" min={0} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-order-shipping">هزینهٔ ارسال (تومان)</Label>
          <AmountInput id="manual-order-shipping" value={shipping} onChange={setShipping} unit="تومان" min={0} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-order-tax">مالیات (تومان)</Label>
          <AmountInput id="manual-order-tax" value={tax} onChange={setTax} unit="تومان" min={0} />
        </div>
      </div>

      <Card className="bg-muted/40" data-testid="manual-order-totals">
        <CardContent className="space-y-2 pt-6 text-sm">
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">جمع جزء</span>
            <span>{formatTomanAmount(totals.subtotalToman)}</span>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">تخفیف</span>
            <span>{formatTomanAmount(discount ?? 0)}</span>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">ارسال + مالیات</span>
            <span>{formatTomanAmount((shipping ?? 0) + (tax ?? 0))}</span>
          </div>
          <div className="flex items-center justify-between gap-4 border-t pt-2 font-medium">
            <span>مبلغ کل</span>
            <span data-testid="manual-order-total">{formatTomanAmount(totals.totalToman)}</span>
          </div>
        </CardContent>
      </Card>

      <div className="space-y-2">
        <Label htmlFor="manual-order-note">یادداشت</Label>
        <Textarea
          id="manual-order-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="اختیاری"
          rows={3}
        />
      </div>

      <FieldError message={totalError} />
      <FieldError message={submitError} />
      <Button
        type="button"
        onClick={submit}
        disabled={!canSubmit}
        data-testid="manual-order-submit"
        className="min-h-11 w-full sm:w-auto"
      >
        {submitting ? "در حال ثبت…" : "ثبت سفارش دستی"}
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Refund form
// ---------------------------------------------------------------------------

function RefundForm({ onCreated }: { onCreated: () => void }) {
  const [orderNumber, setOrderNumber] = React.useState("");
  const [refundAmount, setRefundAmount] = React.useState<number | null>(null);
  const [refundReference, setRefundReference] = React.useState("");
  const [refundPayment, setRefundPayment] = React.useState(true);
  const [lines, setLines] = React.useState<RefundLineState[]>([]);
  const [reason, setReason] = React.useState("");
  const [reasonCode, setReasonCode] = React.useState("");
  const [date, setDate] = React.useState<string | null>(() => todayIso());
  const [note, setNote] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [touched, setTouched] = React.useState(false);

  const linesSum = lines.reduce((sum, line) => sum + (line.lineTotalToman ?? 0), 0);
  const linesMismatch =
    lines.length > 0 && refundAmount !== null && linesSum !== refundAmount;
  const linesIncomplete =
    lines.length > 0 &&
    lines.some((line) => !line.description.trim() || line.lineTotalToman === null);
  const canSubmit =
    !submitting &&
    orderNumber.trim() !== "" &&
    refundAmount !== null &&
    refundAmount > 0 &&
    !linesMismatch &&
    !linesIncomplete;
  const orderNumberError = touched && !orderNumber.trim() ? "شمارهٔ سفارش الزامی است." : null;
  const amountError =
    touched && (refundAmount === null || refundAmount <= 0)
      ? "مبلغ برگشت باید بزرگ‌تر از صفر باشد."
      : null;

  async function submit() {
    setTouched(true);
    setSubmitError(null);
    if (!canSubmit) return;
    const body: CreateManualRefundRequest = {
      order_number: orderNumber.trim(),
      refund_amount_toman: refundAmount ?? 0,
      refund_reference: refundReference.trim() || null,
      refund_payment: refundPayment,
      lines: lines.map((line) => ({
        description: line.description.trim(),
        line_total_toman: line.lineTotalToman ?? 0,
      })),
      reason: reason.trim() || null,
      reason_code: reasonCode.trim() || null,
      occurred_at: occurredAtIso(date),
      note: note.trim() || null,
    };
    setSubmitting(true);
    try {
      await api.createManualRefund(body);
      toast.success("برگشت دستی ثبت شد؛ رویداد آن در صف پردازش قرار گرفت.");
      setOrderNumber("");
      setRefundAmount(null);
      setRefundReference("");
      setRefundPayment(true);
      setLines([]);
      setReason("");
      setReasonCode("");
      setDate(todayIso());
      setNote("");
      setTouched(false);
      onCreated();
    } catch (err) {
      setSubmitError(persianApiErrorMessage(err, "ثبت برگشت ناموفق بود؛ دوباره تلاش کنید."));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="manual-refund-order">شمارهٔ سفارش *</Label>
          <Input
            id="manual-refund-order"
            data-testid="manual-refund-order"
            value={orderNumber}
            onChange={(event) => setOrderNumber(event.target.value)}
            placeholder="سفارشی که برگشت می‌خورد"
            dir="ltr"
            className="text-start"
          />
          <FieldError message={orderNumberError} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-refund-date">تاریخ برگشت</Label>
          <JalaliDatePicker id="manual-refund-date" value={date} onChange={setDate} placeholder="امروز" />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-refund-amount">مبلغ برگشت (تومان) *</Label>
          <AmountInput
            id="manual-refund-amount"
            value={refundAmount}
            onChange={setRefundAmount}
            unit="تومان"
            min={0}
          />
          <FieldError message={amountError} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-refund-ref">مرجع برگشت</Label>
          <Input
            id="manual-refund-ref"
            value={refundReference}
            onChange={(event) => setRefundReference(event.target.value)}
            placeholder="خالی بماند: خودکار ساخته می‌شود"
            dir="ltr"
            className="text-start"
          />
        </div>
      </div>

      <div className="flex min-h-11 items-center gap-2">
        <Checkbox
          id="manual-refund-payment"
          data-testid="manual-refund-payment"
          checked={refundPayment}
          onCheckedChange={(checked) => setRefundPayment(checked === true)}
        />
        <Label htmlFor="manual-refund-payment" className="cursor-pointer">
          وجه هم برگشت داده شده است
        </Label>
      </div>
      <p className="-mt-4 text-sm text-muted-foreground">
        اگر تیک نخورده باشد، فقط یادداشت بستانکار (بدون برگشت پرداخت) ثبت می‌شود.
      </p>

      <div className="space-y-4">
        <div className="flex items-center justify-between gap-4">
          <h3 className="font-medium">ردیف‌های برگشت جزئی (اختیاری)</h3>
          <Button
            type="button"
            variant="outline"
            onClick={() => setLines((prev) => [...prev, EMPTY_REFUND_LINE()])}
            className="min-h-11"
          >
            <Plus aria-hidden />
            افزودن ردیف
          </Button>
        </div>
        {lines.map((line, index) => (
          <Card key={line.key}>
            <CardContent className="space-y-4 pt-6">
              <div className="flex items-start justify-between gap-2">
                <p className="font-medium">ردیف {faNumber(index + 1)}</p>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`حذف ردیف ${faNumber(index + 1)}`}
                  onClick={() => setLines((prev) => prev.filter((item) => item.key !== line.key))}
                  className="h-11 w-11 shrink-0"
                >
                  <Trash2 aria-hidden />
                </Button>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor={`manual-refund-line-desc-${index}`}>شرح *</Label>
                  <Input
                    id={`manual-refund-line-desc-${index}`}
                    value={line.description}
                    onChange={(event) =>
                      setLines((prev) =>
                        prev.map((item) =>
                          item.key === line.key ? { ...item, description: event.target.value } : item,
                        ),
                      )
                    }
                    placeholder="کالای برگشتی"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor={`manual-refund-line-amount-${index}`}>مبلغ ردیف (تومان) *</Label>
                  <AmountInput
                    id={`manual-refund-line-amount-${index}`}
                    value={line.lineTotalToman}
                    onChange={(value) =>
                      setLines((prev) =>
                        prev.map((item) =>
                          item.key === line.key ? { ...item, lineTotalToman: value } : item,
                        ),
                      )
                    }
                    unit="تومان"
                    min={0}
                  />
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
        {linesMismatch ? (
          <p role="alert" className="text-sm text-destructive">
            جمع ردیف‌ها ({formatTomanAmount(linesSum)}) با مبلغ برگشت برابر نیست.
          </p>
        ) : null}
        {linesIncomplete ? (
          <p role="alert" className="text-sm text-destructive">
            شرح و مبلغ هر ردیف برگشت الزامی است.
          </p>
        ) : null}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="manual-refund-reason">دلیل برگشت</Label>
          <Textarea
            id="manual-refund-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="اختیاری"
            rows={3}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-refund-reason-code">کد دلیل</Label>
          <Input
            id="manual-refund-reason-code"
            value={reasonCode}
            onChange={(event) => setReasonCode(event.target.value)}
            placeholder="کلید نگاشت در مرکز نگاشت"
            dir="ltr"
            className="text-start"
          />
          <Label htmlFor="manual-refund-note">یادداشت</Label>
          <Textarea
            id="manual-refund-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="اختیاری"
            rows={2}
          />
        </div>
      </div>

      <FieldError message={submitError} />
      <Button
        type="button"
        onClick={submit}
        disabled={!canSubmit}
        data-testid="manual-refund-submit"
        className="min-h-11 w-full sm:w-auto"
      >
        {submitting ? "در حال ثبت…" : "ثبت برگشت دستی"}
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cancellation form
// ---------------------------------------------------------------------------

function CancellationForm({ onCreated }: { onCreated: () => void }) {
  const [orderNumber, setOrderNumber] = React.useState("");
  const [totalAmount, setTotalAmount] = React.useState<number | null>(null);
  const [reason, setReason] = React.useState("");
  const [date, setDate] = React.useState<string | null>(() => todayIso());
  const [note, setNote] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [touched, setTouched] = React.useState(false);

  const canSubmit = !submitting && orderNumber.trim() !== "";
  const orderNumberError = touched && !orderNumber.trim() ? "شمارهٔ سفارش الزامی است." : null;

  async function submit() {
    setTouched(true);
    setSubmitError(null);
    if (!canSubmit) return;
    const body: CreateManualCancellationRequest = {
      order_number: orderNumber.trim(),
      total_amount_toman: totalAmount,
      reason: reason.trim() || null,
      occurred_at: occurredAtIso(date),
      note: note.trim() || null,
    };
    setSubmitting(true);
    try {
      await api.createManualCancellation(body);
      toast.success("لغو سفارش ثبت شد؛ رویداد آن در صف پردازش قرار گرفت.");
      setOrderNumber("");
      setTotalAmount(null);
      setReason("");
      setDate(todayIso());
      setNote("");
      setTouched(false);
      onCreated();
    } catch (err) {
      setSubmitError(persianApiErrorMessage(err, "ثبت لغو ناموفق بود؛ دوباره تلاش کنید."));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        لغو، سند اصلاحی (یادداشت بستانکار) تولید می‌کند؛ سفارش و فاکتور اصلی بازنویسی نمی‌شوند.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="manual-cancel-order">شمارهٔ سفارش *</Label>
          <Input
            id="manual-cancel-order"
            data-testid="manual-cancel-order"
            value={orderNumber}
            onChange={(event) => setOrderNumber(event.target.value)}
            placeholder="سفارشی که لغو می‌شود"
            dir="ltr"
            className="text-start"
          />
          <FieldError message={orderNumberError} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-cancel-date">تاریخ لغو</Label>
          <JalaliDatePicker id="manual-cancel-date" value={date} onChange={setDate} placeholder="امروز" />
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-cancel-amount">مبلغ سفارش (تومان)</Label>
          <AmountInput
            id="manual-cancel-amount"
            value={totalAmount}
            onChange={setTotalAmount}
            unit="تومان"
            min={0}
          />
          <p className="text-sm text-muted-foreground">اختیاری؛ اگر مبلغ را می‌دانید وارد کنید.</p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="manual-cancel-reason">دلیل لغو</Label>
          <Textarea
            id="manual-cancel-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="اختیاری"
            rows={3}
          />
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="manual-cancel-note">یادداشت</Label>
        <Textarea
          id="manual-cancel-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="اختیاری"
          rows={2}
        />
      </div>
      <FieldError message={submitError} />
      <Button
        type="button"
        onClick={submit}
        disabled={!canSubmit}
        data-testid="manual-cancel-submit"
        className="min-h-11 w-full sm:w-auto"
      >
        {submitting ? "در حال ثبت…" : "ثبت لغو سفارش"}
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Entry list + detail
// ---------------------------------------------------------------------------

function ManualEntryCard({
  entry,
  onOpen,
}: {
  entry: ManualEntryResponse;
  onOpen: () => void;
}) {
  const primary = entry.events[0];
  return (
    <Card data-testid="manual-entry-card">
      <CardContent className="space-y-4 pt-6">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{manualEntryTypeLabel(entry.entry_type)}</Badge>
          {primary?.status ? (
            <StatusBadge
              status={primary.status}
              label={inboxStatusLabel(primary.status)}
              tone={inboxStatusTone(primary.status)}
            />
          ) : null}
          {primary?.decision ? (
            <StatusBadge
              status={primary.decision}
              label={decisionLabel(primary.decision)}
              tone={decisionTone(primary.decision)}
            />
          ) : null}
        </div>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="font-medium" dir="ltr">
            {entry.order_number}
          </p>
          <p className="text-lg font-medium">{formatTomanAmount(entry.total_amount_toman)}</p>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground">
          <span>{formatIsoDateTime(entry.created_at)}</span>
          {primary?.engine_record_id ? <span>سند موتور: {fa(primary.engine_record_id)}</span> : null}
        </div>
        <Button type="button" variant="outline" onClick={onOpen} className="min-h-11 w-full sm:w-auto">
          جزئیات و ردیابی
        </Button>
      </CardContent>
    </Card>
  );
}

function payloadLines(payload: Record<string, unknown>): { description: string; total: number | null }[] {
  const raw = payload["lines"];
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => {
    if (typeof item !== "object" || item === null) return { description: "—", total: null };
    const record = item as Record<string, unknown>;
    const description = typeof record["description"] === "string" ? record["description"] : "—";
    const total = typeof record["line_total"] === "number" ? record["line_total"] : null;
    return { description, total };
  });
}

function ManualEntryDialog({
  entryId,
  onClose,
}: {
  entryId: string;
  onClose: () => void;
}) {
  const [entry, setEntry] = React.useState<ManualEntryResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<unknown>(null);
  const [retryKey, setRetryKey] = React.useState(0);

  // Loading state is set by the retry handler, never synchronously
  // inside this effect (the review-queue pattern).
  React.useEffect(() => {
    let cancelled = false;
    api
      .getManualEntry(entryId)
      .then((detail) => {
        if (!cancelled) setEntry(detail);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entryId, retryKey]);

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto" data-testid="manual-entry-dialog">
        <DialogHeader>
          <DialogTitle>جزئیات ورودی دستی</DialogTitle>
          <DialogDescription>ردیابی ورودی تا رویدادها، تصمیم‌ها و سند موتور.</DialogDescription>
        </DialogHeader>
        {loading ? (
          <div className="space-y-4">
            <Skeleton className="h-6 w-2/3" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : error ? (
          <ErrorState
            message={persianApiErrorMessage(error, "بارگذاری جزئیات ناموفق بود.")}
            onRetry={() => {
              setLoading(true);
              setError(null);
              setRetryKey((key) => key + 1);
            }}
          />
        ) : entry ? (
          <div className="space-y-6">
            <dl className="grid gap-2 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">نوع ورودی</dt>
                <dd className="font-medium">{manualEntryTypeLabel(entry.entry_type)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">شمارهٔ سفارش</dt>
                <dd className="font-medium" dir="ltr">
                  {entry.order_number}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">مبلغ</dt>
                <dd className="font-medium">{formatTomanAmount(entry.total_amount_toman)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">زمان ثبت</dt>
                <dd className="font-medium">{formatIsoDateTime(entry.created_at)}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">ثبت‌کننده</dt>
                <dd className="font-medium">{entry.entered_by_name ?? entry.entered_by}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">فروشگاه</dt>
                <dd className="font-medium" dir="ltr">
                  {entry.source_store_id}
                </dd>
              </div>
            </dl>
            {entry.note ? (
              <div className="space-y-1 text-sm">
                <p className="text-muted-foreground">یادداشت</p>
                <p>{entry.note}</p>
              </div>
            ) : null}

            {payloadLines(entry.payload).length > 0 ? (
              <div className="space-y-2">
                <h3 className="font-medium">ردیف‌ها</h3>
                <ul className="space-y-2 text-sm">
                  {payloadLines(entry.payload).map((line, index) => (
                    <li key={index} className="flex items-center justify-between gap-4 border-b pb-2 last:border-0">
                      <span>{line.description}</span>
                      <span>{line.total !== null ? formatTomanAmount(line.total) : "—"}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div className="space-y-4">
              <h3 className="font-medium">رویدادهای تولیدشده</h3>
              {entry.events.map((event: ManualEntryEventResponse) => (
                <Card key={event.inbox_event_id}>
                  <CardContent className="space-y-3 pt-6 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{eventTypeLabel(event.event_type)}</span>
                      {event.status ? (
                        <StatusBadge
                          status={event.status}
                          label={inboxStatusLabel(event.status)}
                          tone={inboxStatusTone(event.status)}
                        />
                      ) : null}
                      {event.decision ? (
                        <StatusBadge
                          status={event.decision}
                          label={decisionLabel(event.decision)}
                          tone={decisionTone(event.decision)}
                        />
                      ) : null}
                    </div>
                    <dl className="grid gap-2 sm:grid-cols-2">
                      <div>
                        <dt className="text-muted-foreground">شناسهٔ رویداد مبدأ</dt>
                        <dd dir="ltr" className="break-all">
                          {event.source_event_id}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">موجودیت خارجی</dt>
                        <dd dir="ltr" className="break-all">
                          {event.external_entity_type}:{event.external_entity_id}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">سند موتور</dt>
                        <dd>{event.engine_record_id ? fa(event.engine_record_id) : "—"}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">پردازش‌شده در</dt>
                        <dd>{event.processed_at ? formatIsoDateTime(event.processed_at) : "—"}</dd>
                      </div>
                    </dl>
                    {event.last_error ? (
                      <p role="alert" className="text-destructive">
                        {event.last_error}
                      </p>
                    ) : null}
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function ManualEntriesPage() {
  const router = useRouter();
  const [tab, setTab] = React.useState<EntryTab>("order");
  const [typeFilter, setTypeFilter] = React.useState<string>("all");
  const [entries, setEntries] = React.useState<ManualEntryResponse[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  const loadEntries = React.useCallback(async (cursor?: string) => {
    const params: { limit: number; cursor?: string; entry_type?: string } = { limit: PAGE_SIZE };
    if (cursor) params.cursor = cursor;
    if (typeFilter !== "all") params.entry_type = typeFilter;
    return api.listManualEntries(params);
  }, [typeFilter]);

  const refresh = React.useCallback(() => {
    setLoading(true);
    setError(null);
    setReloadKey((key) => key + 1);
  }, []);

  // Loading state is set by the handlers (filter change / retry /
  // created), never synchronously inside this effect.
  React.useEffect(() => {
    let cancelled = false;
    loadEntries()
      .then((page) => {
        if (cancelled) return;
        setEntries(page.items);
        setNextCursor(page.next_cursor);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized) {
          router.push("/login");
          return;
        }
        setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadEntries, reloadKey, router]);

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await loadEntries(nextCursor);
      setEntries((prev) => [...prev, ...page.items]);
      setNextCursor(page.next_cursor);
    } catch (err) {
      toast.error(persianApiErrorMessage(err, "بارگذاری ورودی‌های بیشتر ناموفق بود."));
    } finally {
      setLoadingMore(false);
    }
  }

  if (error && isPermissionDenied(error)) {
    return (
      <PermissionDeniedState message="کلید شما برای مشاهدهٔ ورودی‌های دستی دسترسی ندارد." />
    );
  }

  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <h1 className="text-2xl font-bold">فاکتور جدید (دستی)</h1>
        <p className="text-muted-foreground">
          ورودی دستی، همان رویداد متعارفی را تولید می‌کند که فروشگاه با وب‌هوک می‌فرستد و از همان
          خط لوله می‌گذرد: اعتبارسنجی، نگاشت، دروازهٔ سیاست و ممیزی. هر رکورد دستی تا این ورودی و
          هر رکورد خودکار تا رویداد مبدأش قابل ردیابی است.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>ثبت ورودی دستی</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs value={tab} onValueChange={(value) => setTab(value as EntryTab)}>
            <TabsList aria-label="نوع ورودی دستی" data-testid="manual-entry-tabs">
              <TabsTrigger value="order" data-testid="manual-order-tab">
                سفارش جدید
              </TabsTrigger>
              <TabsTrigger value="refund" data-testid="manual-refund-tab">
                برگشت جدید
              </TabsTrigger>
              <TabsTrigger value="cancellation" data-testid="manual-cancellation-tab">
                لغو سفارش
              </TabsTrigger>
            </TabsList>
            <TabsContent value="order" className="pt-6">
              <OrderForm onCreated={refresh} />
            </TabsContent>
            <TabsContent value="refund" className="pt-6">
              <RefundForm onCreated={refresh} />
            </TabsContent>
            <TabsContent value="cancellation" className="pt-6">
              <CancellationForm onCreated={refresh} />
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h2 className="text-xl font-bold">ورودی‌های دستی ثبت‌شده</h2>
          <Select
            value={typeFilter}
            onValueChange={(value) => {
              setTypeFilter(value ?? "all");
              refresh();
            }}
          >
            <SelectTrigger className="w-44" aria-label="فیلتر نوع ورودی">
              <SelectValue placeholder="همهٔ انواع" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">همهٔ انواع</SelectItem>
              <SelectItem value="order">سفارش</SelectItem>
              <SelectItem value="refund">برگشت</SelectItem>
              <SelectItem value="cancellation">لغو سفارش</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {loading ? (
          <div className="space-y-4" data-testid="manual-entries-loading">
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        ) : error ? (
          <ErrorState
            message={persianApiErrorMessage(error, "بارگذاری ورودی‌های دستی ناموفق بود.")}
            onRetry={refresh}
          />
        ) : entries.length === 0 ? (
          <Card data-testid="manual-entries-empty">
            <CardContent className="pt-6 text-center text-muted-foreground">
              هنوز ورودی دستی ثبت نشده است. اولین سفارش، برگشت یا لغو را از فرم بالا ثبت کنید.
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-4">
            {entries.map((entry) => (
              <ManualEntryCard
                key={entry.entry_id}
                entry={entry}
                onOpen={() => setSelectedId(entry.entry_id)}
              />
            ))}
            {nextCursor ? (
              <Button
                type="button"
                variant="outline"
                onClick={loadMore}
                disabled={loadingMore}
                className="min-h-11 w-full"
              >
                {loadingMore ? "در حال بارگذاری…" : "نمایش بیشتر"}
              </Button>
            ) : null}
          </div>
        )}
      </div>

      {selectedId ? (
        <ManualEntryDialog entryId={selectedId} onClose={() => setSelectedId(null)} />
      ) : null}
    </div>
  );
}
