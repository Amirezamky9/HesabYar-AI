/**
 * Persian labels + pure helpers for manual entries (US-513, Phase 5
 * wave 2, stream S3).
 *
 * Every entry type, inbox status, automation decision and canonical
 * event type the manual-entry API returns gets one Persian label
 * (from phased design §7: «فروش و خرید ← فاکتور جدید (دستی)»).
 * Unknown values fall back to the raw code so a new backend label
 * never silently disappears.
 *
 * Money helpers work in whole Toman (the API unit): the operator
 * types Toman, the pipeline converts ×10 to Rial. Quantities are
 * exact decimals — a line total that is not a whole Toman is
 * rejected by the backend, so the form checks it first and names
 * the line.
 */

import type { StatusTone } from "@/components/ui";
import { normalizeDigits } from "@/lib/format-money";
import { faNumber } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

export const MANUAL_ENTRY_TYPES = ["order", "refund", "cancellation"] as const;

export type ManualEntryTypeCode = (typeof MANUAL_ENTRY_TYPES)[number];

export const MANUAL_ENTRY_TYPE_LABELS: Record<string, string> = {
  order: "سفارش",
  refund: "برگشت",
  cancellation: "لغو سفارش",
};

export function manualEntryTypeLabel(type: string | null | undefined): string {
  if (!type) return "نامشخص";
  return MANUAL_ENTRY_TYPE_LABELS[type] ?? type;
}

export const INBOX_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  received: { label: "دریافت‌شده", tone: "outline" },
  processing: { label: "در حال پردازش", tone: "outline" },
  processed: { label: "پردازش‌شده", tone: "success" },
  failed: { label: "ناموفق", tone: "destructive" },
  needs_review: { label: "نیازمند بررسی", tone: "warning" },
  dead_lettered: { label: "مرده", tone: "destructive" },
};

export function inboxStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return INBOX_STATUS_LABELS[status]?.label ?? status;
}

export function inboxStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return INBOX_STATUS_LABELS[status]?.tone ?? "outline";
}

export const DECISION_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  draft: { label: "پیش‌نویس", tone: "outline" },
  auto_post: { label: "ثبت خودکار", tone: "success" },
  needs_review: { label: "نیازمند بررسی", tone: "warning" },
  dead_letter: { label: "مرده", tone: "destructive" },
  replayed: { label: "بازپخش‌شده", tone: "outline" },
};

export function decisionLabel(decision: string | null | undefined): string {
  if (!decision) return "—";
  return DECISION_LABELS[decision]?.label ?? decision;
}

export function decisionTone(decision: string | null | undefined): StatusTone {
  if (!decision) return "outline";
  return DECISION_LABELS[decision]?.tone ?? "outline";
}

export const CANONICAL_EVENT_TYPE_LABELS: Record<string, string> = {
  "sales.order.completed": "تکمیل سفارش",
  "sales.order.cancelled": "لغو سفارش",
  "sales.refund.created": "ایجاد برگشت",
  "payment.captured": "دریافت پرداخت",
  "payment.refunded": "برگشت پرداخت",
  "payment.settlement.received": "دریافت تسویه",
  "purchase.invoice.received": "دریافت فاکتور خرید",
  "inventory.cost.recognized": "شناسایی بهای تمام‌شده",
  "bank.transaction.imported": "ایمپورت تراکنش بانکی",
};

export function eventTypeLabel(eventType: string | null | undefined): string {
  if (!eventType) return "نامشخص";
  return CANONICAL_EVENT_TYPE_LABELS[eventType] ?? eventType;
}

// ---------------------------------------------------------------------------
// Money + quantity helpers (whole Toman)
// ---------------------------------------------------------------------------

/** Parse a user-typed quantity (Persian/Arabic digits, «٫» or "." decimal). */
export function parseManualQuantity(raw: string): number | null {
  const ascii = normalizeDigits(raw).replace(/[٬,،\s]/g, "").replace("٫", ".").trim();
  if (!/^\d+(\.\d+)?$/.test(ascii)) return null;
  const value = Number(ascii);
  return Number.isFinite(value) && value > 0 ? value : null;
}

/** True when a decimal number is a whole number (no floating-point noise). */
export function isWholeToman(value: number): boolean {
  return Number.isFinite(value) && Math.abs(value - Math.round(value)) < 1e-9;
}

export interface ManualOrderLineDraft {
  /** Quantity as typed (Persian digits allowed). */
  quantity: string;
  /** Unit price in whole Toman, or null when unset. */
  unitPriceToman: number | null;
  /** Line discount in whole Toman, or null when unset. */
  discountToman: number | null;
}

export interface ManualOrderTotals {
  subtotalToman: number;
  totalToman: number;
  /** Line indexes (0-based) whose total is not a whole Toman. */
  fractionalLines: number[];
  /** True when every line parses and every line total is whole. */
  valid: boolean;
}

/**
 * Totals for a manual-order draft. Lines that do not parse contribute
 * zero and mark the draft invalid; a line total that is not a whole
 * Toman is reported by index so the form can point at it.
 */
export function computeOrderTotals(
  lines: ManualOrderLineDraft[],
  discountToman: number | null,
  shippingToman: number | null,
  taxToman: number | null,
): ManualOrderTotals {
  let subtotal = 0;
  let valid = lines.length > 0;
  const fractionalLines: number[] = [];
  lines.forEach((line, index) => {
    const quantity = parseManualQuantity(line.quantity);
    if (quantity === null || line.unitPriceToman === null || line.unitPriceToman < 0) {
      valid = false;
      return;
    }
    const discount = line.discountToman ?? 0;
    if (discount < 0) {
      valid = false;
      return;
    }
    const lineTotal = quantity * line.unitPriceToman - discount;
    if (lineTotal < 0) {
      valid = false;
      return;
    }
    if (!isWholeToman(lineTotal)) {
      fractionalLines.push(index);
      valid = false;
      return;
    }
    subtotal += Math.round(lineTotal);
  });
  const discount = discountToman ?? 0;
  const shipping = shippingToman ?? 0;
  const tax = taxToman ?? 0;
  if (discount < 0 || shipping < 0 || tax < 0) valid = false;
  const total = subtotal - discount + shipping + tax;
  if (total <= 0) valid = false;
  return { subtotalToman: subtotal, totalToman: total, fractionalLines, valid };
}

/** Whole-Toman amount with unit: 630000 -> «۶۳۰٬۰۰۰ تومان». */
export function formatTomanAmount(value: number): string {
  return `${faNumber(value)} تومان`;
}

/**
 * A picked Jalali date (Gregorian ISO `YYYY-MM-DD`) as an ISO 8601
 * timestamp at noon UTC — noon keeps the same Jalali day in Tehran
 * (UTC+3:30) however the backend renders it.
 */
export function occurredAtIso(dateIso: string | null): string | null {
  if (!dateIso) return null;
  return `${dateIso}T12:00:00Z`;
}
