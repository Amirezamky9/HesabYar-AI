/**
 * Persian labels + helpers for packaging & shipping (US-515, Phase 5 wave 2).
 *
 * Every cost method, shipment-charge method and count state the
 * packaging API returns gets one Persian label. Unknown values fall
 * back to the raw code so a new backend label never silently
 * disappears. Days-of-cover renders in Persian digits; null (no
 * consumption in the window) renders as "—", never a fake number.
 * Decimals arrive from the API as JSON strings.
 */

import type { StatusTone } from "@/components/ui";
import { fa } from "@/lib/utils";
import { formatDecimal, normalizeDigits } from "@/lib/format-money";
import { ApiError } from "@/lib/api-client";
import { persianApiErrorMessage } from "@/features/shared/api-error";

export const COST_METHOD_LABELS: Record<string, string> = {
  average: "میانگین",
  fifo: "فایفو (اول‌وارده، اول‌خارجه)",
};

export function costMethodLabel(method: string | null | undefined): string {
  if (!method) return "نامشخص";
  return COST_METHOD_LABELS[method] ?? method;
}

export const SHIPMENT_COST_METHOD_LABELS: Record<string, string> = {
  on_order: "روی سفارش",
  on_shipment: "روی ارسال",
  none: "بدون هزینهٔ ارسال",
};

export function shipmentCostMethodLabel(method: string | null | undefined): string {
  if (!method) return "نامشخص";
  return SHIPMENT_COST_METHOD_LABELS[method] ?? method;
}

export const COUNT_STATE_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  draft: { label: "پیش‌نویس", tone: "warning" },
  done: { label: "تأییدشده", tone: "success" },
  cancelled: { label: "لغوشده", tone: "outline" },
};

export function countStateLabel(state: string | null | undefined): string {
  if (!state) return "نامشخص";
  return COUNT_STATE_LABELS[state]?.label ?? state;
}

export function countStateTone(state: string | null | undefined): StatusTone {
  if (!state) return "outline";
  return COUNT_STATE_LABELS[state]?.tone ?? "outline";
}

/** "—" when null (nothing to project from); otherwise "≈ ۶۰ روز". */
export function formatDaysOfCover(days: number | null | undefined): string {
  if (days === null || days === undefined) return "—";
  if (days < 0) return `منفی ${formatDecimal(Math.abs(days), 1)} روز`;
  return `≈ ${formatDecimal(days, 1)} روز`;
}

/** Badge tone for a days-of-cover value against the alert threshold. */
export function coverageTone(
  days: number | null | undefined,
  alertDays: number,
  isOutOfStock: boolean,
): StatusTone {
  if (isOutOfStock) return "destructive";
  if (days === null || days === undefined) return "outline";
  if (days <= alertDays) return "warning";
  return "success";
}

/** A decimal string ("60", "1.5") rendered with Persian digits. */
export function formatQuantity(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const trimmed = value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
  return fa(trimmed).replace(".", "٫");
}

/** Normalize a typed number: Persian digits → Latin, «٫» → ".", separators dropped. */
function normalizeNumberInput(raw: string): string {
  return normalizeDigits(raw.trim()).replace(/٫/g, ".").replace(/٬|,/g, "");
}

/** Parse a user-typed decimal (Persian digits allowed); null when invalid. */
export function parseDecimalInput(raw: string): string | null {
  const normalized = normalizeNumberInput(raw);
  if (!/^\d+(\.\d+)?$/.test(normalized)) return null;
  const value = Number(normalized);
  if (!Number.isFinite(value) || value < 0) return null;
  return normalized;
}

/** Parse a user-typed money amount in تومان; null when invalid. Returns IRR. */
export function parseTomanInput(raw: string): number | null {
  const normalized = normalizeNumberInput(raw);
  if (!/^\d+$/.test(normalized)) return null;
  const toman = Number(normalized);
  if (!Number.isFinite(toman) || toman < 0) return null;
  return toman * 10;
}

/** Persian messages for the packaging error codes (docs/packaging.md). */
export function packagingApiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case "PACKAGING_SETUP_REQUIRED":
        return "پیکربندی بسته‌بندی کامل نیست؛ ابتدا دسته‌بندی مواد و انبار بسته‌بندی را در تنظیمات مشخص کنید.";
      case "PACKAGING_FIFO_UNAVAILABLE":
        return "روش فایفو نیازمند ماژول product_cost_fifo است که فعال نشده است؛ روش میانگین را انتخاب کنید.";
      case "PACKAGING_MATERIAL_NOT_FOUND":
        return "این مادهٔ بسته‌بندی پیدا نشد؛ ممکن است حذف شده باشد.";
      case "PACKAGING_COUNT_NOT_FOUND":
        return "این شمارش پیدا نشد؛ ممکن است حذف شده باشد.";
      case "PACKAGING_CARRIER_RATE_NOT_FOUND":
        return "این نرخ باربری پیدا نشد؛ ممکن است حذف شده باشد.";
      case "PACKAGING_COST_CENTER_EXISTS":
        return "برای این سفارش قبلاً مرکز هزینه ساخته شده است.";
      case "PACKAGING_INVALID_COUNT":
        return "همهٔ ردیف‌ها باید شمارش شوند؛ ردیف شمارش‌نشده باقی مانده است.";
      case "PACKAGING_INVALID_CONSUMPTION":
        return "مصرف نامعتبر است؛ مقدار باید بزرگ‌تر از صفر باشد.";
      case "PACKAGING_INVALID_SHIPMENT_COST":
        return "هزینهٔ ارسال نامعتبر است؛ مبلغ‌ها را بررسی کنید.";
      case "PACKAGING_INVALID_QUOTE":
        return "محاسبهٔ قیمت نامعتبر است؛ ورودی‌ها را بررسی کنید.";
      case "LEDGER_STOCK_LOCATION_NOT_FOUND":
        return "انبار انتخاب‌شده در ترایتون پیدا نشد؛ شناسهٔ انبار را بررسی کنید.";
      case "LEDGER_ANALYTIC_ACCOUNT_NOT_FOUND":
        return "حساب تحلیلی در ترایتون پیدا نشد؛ شناسهٔ ریشهٔ تحلیلی را بررسی کنید.";
      case "LEDGER_INVENTORY_NOT_FOUND":
        return "شمارش انبار در ترایتون پیدا نشد.";
      case "LEDGER_ORDER_POINT_NOT_FOUND":
        return "نقطهٔ سفارش در ترایتون پیدا نشد.";
      default:
        break;
    }
  }
  return persianApiErrorMessage(err, fallback);
}
