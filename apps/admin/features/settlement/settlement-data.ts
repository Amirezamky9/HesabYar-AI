/**
 * Persian labels + pure helpers for the settlement center (US-525,
 * Phase 5-3 Wave C).
 *
 * Every tranche state, payment status, exception type, import status,
 * match outcome and contract field the settlement API returns gets one
 * Persian label (from phased design §3). Unknown values fall back to
 * the raw code so a new backend value never silently disappears.
 *
 * Pure helpers (aging buckets, overdue days, escalation, Jalali
 * periods) live here so the report aggregation and the pages share
 * them — and so vitest can cover them without a backend.
 */

import type { StatusTone } from "@/components/ui";
import { fa, faNumber } from "@/lib/utils";
import { JALALI_MONTHS, toJalali } from "@/lib/jalali";

/** Tranche lifecycle (§3.3), in happy-path order. */
export const TRANCHE_STATES = [
  "expected",
  "due",
  "confirmed",
  "received",
  "settled",
  "partially_confirmed",
  "partially_received",
  "overdue",
  "cancelled",
  "exception",
  "written_off",
] as const;

export const TRANCHE_STATE_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  expected: { label: "موردانتظار", tone: "outline" },
  due: { label: "سررسیدشده", tone: "warning" },
  confirmed: { label: "تأییدشده", tone: "outline" },
  received: { label: "دریافت‌شده", tone: "success" },
  settled: { label: "تسویه‌شده", tone: "success" },
  partially_confirmed: { label: "تأیید جزئی", tone: "warning" },
  partially_received: { label: "دریافت جزئی", tone: "warning" },
  overdue: { label: "سررسیدگذشته", tone: "destructive" },
  cancelled: { label: "لغوشده", tone: "outline" },
  exception: { label: "استثنا", tone: "destructive" },
  written_off: { label: "از قلم افتاده", tone: "outline" },
};

export function trancheStateLabel(state: string | null | undefined): string {
  if (!state) return "نامشخص";
  return TRANCHE_STATE_LABELS[state]?.label ?? state;
}

export function trancheStateTone(state: string | null | undefined): StatusTone {
  if (!state) return "outline";
  return TRANCHE_STATE_LABELS[state]?.tone ?? "outline";
}

/** A tranche still waiting on money (open states). */
export function isTrancheOpen(state: string): boolean {
  return (
    state === "expected" ||
    state === "due" ||
    state === "confirmed" ||
    state === "received" ||
    state === "partially_confirmed" ||
    state === "partially_received" ||
    state === "overdue" ||
    state === "exception"
  );
}

export const PAYMENT_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  scheduled: { label: "زمان‌بندی‌شده", tone: "outline" },
  settling: { label: "در حال تسویه", tone: "warning" },
  settled: { label: "تسویه‌شده", tone: "success" },
  partially_cancelled: { label: "لغو جزئی", tone: "warning" },
  cancelled: { label: "لغوشده", tone: "outline" },
};

export function paymentStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return PAYMENT_STATUS_LABELS[status]?.label ?? status;
}

export function paymentStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return PAYMENT_STATUS_LABELS[status]?.tone ?? "outline";
}

export const PAYMENT_SOURCE_LABELS: Record<string, string> = {
  store_webhook: "وب‌هوک فروشگاه",
  gateway_poll: "استعلام درگاه",
  manual_import: "ایمپورت دستی",
  manual_entry: "ثبت دستی",
};

export function paymentSourceLabel(source: string | null | undefined): string {
  if (!source) return "نامشخص";
  return PAYMENT_SOURCE_LABELS[source] ?? source;
}

/** The 11 settlement exception types (§3.7). */
export const SETTLEMENT_EXCEPTION_TYPES = [
  "late_tranche",
  "missing_deposit",
  "partial_tranche",
  "amount_mismatch",
  "fee_mismatch",
  "deposit_without_file",
  "file_without_deposit",
  "unknown_reference",
  "overpayment",
  "period_reassigned",
  "cancelled_after_settlement",
] as const;

export const SETTLEMENT_EXCEPTION_TYPE_LABELS: Record<string, string> = {
  late_tranche: "قسط دیرکرد",
  missing_deposit: "واریز نرسیده",
  partial_tranche: "قسط جزئی",
  amount_mismatch: "مغایرت مبلغ",
  fee_mismatch: "مغایرت کارمزد",
  deposit_without_file: "واریز بدون فایل",
  file_without_deposit: "فایل بدون واریز",
  unknown_reference: "مرجع ناشناخته",
  overpayment: "واریز اضافه",
  period_reassigned: "تغییر دورهٔ فروش",
  cancelled_after_settlement: "لغو پس از تسویه",
};

export function settlementExceptionTypeLabel(type: string | null | undefined): string {
  if (!type) return "نامشخص";
  return SETTLEMENT_EXCEPTION_TYPE_LABELS[type] ?? type;
}

export const EXCEPTION_RESOLUTION_LABELS: Record<string, string> = {
  confirmed_ok: "تأیید شد — مشکلی نبود",
  adjusted: "اصلاح شد",
  write_off: "از قلم افتاده",
  escalated_to_gateway: "ارجاع به درگاه",
  rescheduled: "زمان‌بندی مجدد شد",
};

export function exceptionResolutionLabel(resolution: string | null | undefined): string {
  if (!resolution) return "—";
  return EXCEPTION_RESOLUTION_LABELS[resolution] ?? resolution;
}

/**
 * Overdue escalation level (§3.7): 0 = none, 1 = 7 days, 2 = 30 days,
 * 3 = 60 days past due. The daily tick escalates `late_tranche`
 * exceptions through these levels.
 */
export const ESCALATION_LEVEL_LABELS: Record<number, string> = {
  0: "بدون تشدید",
  1: "سطح ۱ — ۷ روز گذشته",
  2: "سطح ۲ — ۳۰ روز گذشته",
  3: "سطح ۳ — ۶۰ روز گذشته",
};

export function escalationLevelLabel(level: number | null | undefined): string {
  if (level === null || level === undefined) return "نامشخص";
  return ESCALATION_LEVEL_LABELS[level] ?? `سطح ${faNumber(level)}`;
}

export function escalationLevelTone(level: number | null | undefined): StatusTone {
  if (level === 3) return "destructive";
  if (level === 2) return "warning";
  if (level === 1) return "outline";
  return "outline";
}

export const IMPORT_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  awaiting_review: { label: "در انتظار بازبینی", tone: "warning" },
  approved: { label: "تأییدشده", tone: "outline" },
  rejected: { label: "ردشده", tone: "outline" },
  exception: { label: "استثنا", tone: "destructive" },
  settled: { label: "تسویه‌شده", tone: "success" },
};

export function importStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return IMPORT_STATUS_LABELS[status]?.label ?? status;
}

export function importStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return IMPORT_STATUS_LABELS[status]?.tone ?? "outline";
}

export const FILE_TYPE_LABELS: Record<string, string> = {
  detail: "تفصیلی (به‌ازای هر تراکنش)",
  payouts: "واریزها (به‌ازای هر واریز)",
  statement: "صورت‌حساب",
};

export function fileTypeLabel(fileType: string | null | undefined): string {
  if (!fileType) return "نامشخص";
  return FILE_TYPE_LABELS[fileType] ?? fileType;
}

export const MATCH_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  matched: { label: "منطبق", tone: "success" },
  new_payment: { label: "پرداخت جدید", tone: "outline" },
  unknown_reference: { label: "مرجع ناشناخته", tone: "destructive" },
  duplicate: { label: "تکراری", tone: "warning" },
  amount_mismatch: { label: "مغایرت مبلغ", tone: "destructive" },
};

export function matchStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return MATCH_STATUS_LABELS[status]?.label ?? status;
}

export function matchStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return MATCH_STATUS_LABELS[status]?.tone ?? "outline";
}

export const DEPOSIT_DECISION_LABELS: Record<string, string> = {
  exact_reference: "مرجع دقیق",
  amount_and_date: "مبلغ و تاریخ",
  amount_only: "فقط مبلغ",
  none: "بی‌همتا",
  manual: "دستی",
};

export function depositDecisionLabel(method: string | null | undefined): string {
  if (!method) return "نامشخص";
  return DEPOSIT_DECISION_LABELS[method] ?? method;
}

export const SETTLEMENT_MODEL_LABELS: Record<string, string> = {
  tranches_4: "۴ قسط ماهانه",
  monthly_lump: "یکجا ماهانه",
  daily: "روزانه",
};

export function settlementModelLabel(model: string | null | undefined): string {
  if (!model) return "نامشخص";
  return SETTLEMENT_MODEL_LABELS[model] ?? model;
}

export const FEE_TIMING_LABELS: Record<string, string> = {
  proportional: "متناسب با هر قسط",
  upfront_first_tranche: "یکجا در قسط اول",
};

export function feeTimingLabel(timing: string | null | undefined): string {
  if (!timing) return "نامشخص";
  return FEE_TIMING_LABELS[timing] ?? timing;
}

export const FEE_ACCRUAL_LABELS: Record<string, string> = {
  at_sale: "در زمان فروش",
  at_settlement: "در زمان تسویه",
};

export function feeAccrualLabel(policy: string | null | undefined): string {
  if (!policy) return "نامشخص";
  return FEE_ACCRUAL_LABELS[policy] ?? policy;
}

export const AUTO_POST_LABELS: Record<string, string> = {
  draft_only: "فقط پیش‌نویس (بدون ثبت خودکار)",
  auto_post_within_policy: "ثبت خودکار در سقف سیاست",
};

export function autoPostLabel(policy: string | null | undefined): string {
  if (!policy) return "نامشخص";
  return AUTO_POST_LABELS[policy] ?? policy;
}

export const POSTING_KIND_LABELS: Record<string, string> = {
  fee_at_sale: "کارمزد در زمان فروش",
  fee_at_settlement: "کارمزد در زمان تسویه",
  clearing: "انتقال به حساب واسط",
  bank_receipt: "دریافت بانکی",
  fee_variance: "تعدیل مغایرت کارمزد",
  refund: "برگشت پس از تسویه",
  refund_adjustment: "تعدیل برگشت",
};

export function postingKindLabel(kind: string | null | undefined): string {
  if (!kind) return "نامشخص";
  return POSTING_KIND_LABELS[kind] ?? kind;
}

/**
 * Parse a §9.3 decimal string ("10" means 10%) to a number. Returns
 * null on anything unparseable — never NaN into the UI.
 */
export function parseDecimalString(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A fee percent as Persian text: "10" -> «۱۰٪», "7.5" -> «۷٫۵٪». */
export function formatPercentString(value: string | null | undefined): string {
  const parsed = parseDecimalString(value);
  if (parsed === null) return "—";
  return `${toFaDecimal(String(parsed))}٪`;
}

function toFaDecimal(text: string): string {
  const FA = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];
  return text.replace(/\d/g, (d) => FA[Number(d)]).replace(".", "٫");
}

/** Whole days between two Gregorian ISO dates (date-only, UTC). */
export function daysBetween(fromIso: string, toIso: string): number {
  const from = Date.parse(`${fromIso}T00:00:00Z`);
  const to = Date.parse(`${toIso}T00:00:00Z`);
  if (Number.isNaN(from) || Number.isNaN(to)) return 0;
  return Math.round((to - from) / 86_400_000);
}

/** Days past the due date (0 when not yet due). */
export function daysOverdue(expectedDateIso: string, todayIso: string): number {
  return Math.max(0, daysBetween(expectedDateIso, todayIso));
}

/** Today's Gregorian ISO date in Asia/Tehran. */
export function todayIsoTehran(now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Tehran",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** Aging buckets (§3.10): 0–30 / 31–60 / 61–90 / +90 days past due. */
export type AgingBucket = "0-30" | "31-60" | "61-90" | "90+";

export const AGING_BUCKET_LABELS: Record<AgingBucket, string> = {
  "0-30": "۰ تا ۳۰ روز",
  "31-60": "۳۱ تا ۶۰ روز",
  "61-90": "۶۱ تا ۹۰ روز",
  "90+": "بیش از ۹۰ روز",
};

export const AGING_BUCKETS: AgingBucket[] = ["0-30", "31-60", "61-90", "90+"];

export function agingBucket(daysPastDue: number): AgingBucket {
  if (daysPastDue <= 30) return "0-30";
  if (daysPastDue <= 60) return "31-60";
  if (daysPastDue <= 90) return "61-90";
  return "90+";
}

/** A Jalali sales period "YYYY/MM" (e.g. "1405/08") for a Gregorian date. */
export function jalaliPeriodOf(date = new Date()): string {
  const j = toJalali(date);
  return `${j.jy}/${String(j.jm).padStart(2, "0")}`;
}

/** "1405/08" -> «آبان ۱۴۰۵» (falls back to the raw period). */
export function jalaliPeriodLabel(period: string | null | undefined): string {
  if (!period) return "نامشخص";
  const match = period.match(/^(\d{4})\/(\d{1,2})$/);
  if (!match) return period;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return period;
  // Years render without a thousands separator: «آبان ۱۴۰۵».
  return `${JALALI_MONTHS[month - 1]} ${fa(match[1])}`;
}

/** Validate a Jalali period "YYYY/MM" (month 01–12). */
export function isValidJalaliPeriod(period: string): boolean {
  const match = period.trim().match(/^(\d{4})\/(\d{1,2})$/);
  if (!match) return false;
  const month = Number(match[2]);
  return month >= 1 && month <= 12;
}

/** Normalize a period to "YYYY/MM" with a zero-padded month. */
export function normalizeJalaliPeriod(period: string): string {
  const match = period.trim().match(/^(\d{4})\/(\d{1,2})$/);
  if (!match) return period.trim();
  return `${match[1]}/${match[2].padStart(2, "0")}`;
}
