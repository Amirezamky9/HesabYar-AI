/**
 * Persian labels + helpers for the review queue (US-504, Phase 5 F4).
 *
 * Every item type, severity, status, action and resolution the
 * review-queue API returns gets one Persian label (from phased design
 * §7.2). Unknown values fall back to the raw code so a new backend
 * label never silently disappears. Ages render in Persian digits with
 * Jalali-friendly units (دقیقه/ساعت/روز/ماه).
 */

import type { StatusTone } from "@/components/ui";
import { faNumber } from "@/lib/utils";

/** The nine item types (phased design §7.2), in table order. */
export const REVIEW_ITEM_TYPES = [
  "mapping",
  "settlement_variance",
  "cheque_exception",
  "bank_unmatched",
  "packaging_reorder",
  "installment_overdue",
  "manual_import_review",
  "cashbox_variance",
  "inbox_dead_letter",
] as const;

export type ReviewItemTypeCode = (typeof REVIEW_ITEM_TYPES)[number];

export const REVIEW_ITEM_TYPE_LABELS: Record<string, { label: string; description: string }> = {
  mapping: { label: "نگاشت نامشخص", description: "رویدادی که نگاشت آن مشخص نیست" },
  settlement_variance: { label: "مغایرت تسویه", description: "اختلاف مبلغ تسویهٔ موردانتظار و واقعی" },
  cheque_exception: { label: "استثنای چک", description: "برگشت، سررسیدگذشته یا مغایرت چک" },
  bank_unmatched: { label: "سطر بانکی بی‌همتا", description: "سطر صورت‌حساب بدون تطبیق" },
  packaging_reorder: { label: "نقطهٔ سفارش بسته‌بندی", description: "موجودی بسته‌بندی به نقطهٔ سفارش رسیده است" },
  installment_overdue: { label: "قسط معوق", description: "قسط فروش سررسیدگذشته" },
  manual_import_review: { label: "ایمپورت دستی در انتظار تأیید", description: "بچ ایمپورت نیازمند تأیید پیش‌نمایش" },
  cashbox_variance: { label: "مغایرت صندوق", description: "اختلاف شمارش صندوق با ماندهٔ دفتری" },
  inbox_dead_letter: { label: "رویداد مرده", description: "رویدادی که پردازش آن ناموفق مانده است" },
};

export function reviewTypeLabel(type: string | null | undefined): string {
  if (!type) return "نامشخص";
  return REVIEW_ITEM_TYPE_LABELS[type]?.label ?? type;
}

export function reviewTypeDescription(type: string | null | undefined): string | null {
  if (!type) return null;
  return REVIEW_ITEM_TYPE_LABELS[type]?.description ?? null;
}

export const REVIEW_SEVERITY_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  info: { label: "اطلاع", tone: "outline" },
  warning: { label: "هشدار", tone: "warning" },
  critical: { label: "بحرانی", tone: "destructive" },
};

export function reviewSeverityLabel(severity: string | null | undefined): string {
  if (!severity) return "نامشخص";
  return REVIEW_SEVERITY_LABELS[severity]?.label ?? severity;
}

export function reviewSeverityTone(severity: string | null | undefined): StatusTone {
  if (!severity) return "outline";
  return REVIEW_SEVERITY_LABELS[severity]?.tone ?? "outline";
}

export const REVIEW_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  open: { label: "باز", tone: "warning" },
  in_review: { label: "در حال بررسی", tone: "outline" },
  resolved: { label: "حل‌شده", tone: "success" },
  written_off: { label: "بسته‌شده", tone: "outline" },
};

export function reviewStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return REVIEW_STATUS_LABELS[status]?.label ?? status;
}

export function reviewStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return REVIEW_STATUS_LABELS[status]?.tone ?? "outline";
}

export const REVIEW_RESOLUTION_LABELS: Record<string, string> = {
  confirmed_ok: "تأیید شد — مشکلی نبود",
  adjusted: "اصلاح شد",
  write_off: "از قلم افتاده",
  escalated: "ارجاع شد",
  rescheduled: "زمان‌بندی مجدد شد",
  skipped: "رد شد",
  deferred: "به تعویق افتاد",
};

export function reviewResolutionLabel(resolution: string | null | undefined): string | null {
  if (!resolution) return null;
  return REVIEW_RESOLUTION_LABELS[resolution] ?? resolution;
}

/** Permitted actions per §7.2, plus the audit-only actions (filed/assign). */
export const REVIEW_ACTION_LABELS: Record<string, string> = {
  complete_mapping_retry: "تکمیل نگاشت و پردازش مجدد",
  auto_create_party_item: "ساخت خودکار طرف/کالا",
  skip: "رد کردن",
  accept_variance: "پذیرش مغایرت با دلیل",
  escalate_to_gateway: "رد و پیگیری از درگاه",
  fix_fee_mapping: "اصلاح نگاشت کارمزد",
  record_bounce: "ثبت برگشت چک",
  contact_customer: "تماس با مشتری",
  record_alternative_payment: "ثبت پرداخت جایگزین",
  manual_match: "نگاشت دستی",
  record_misc_income: "ثبت به‌عنوان درآمد متفرقه",
  snooze: "نادیده گرفتن موقت",
  approve_purchase: "تأیید درخواست خرید",
  change_reorder_point: "تغییر نقطهٔ سفارش",
  cash_purchase: "خرید نقدی از صندوق",
  send_reminder: "ارسال یادآوری",
  extend_due_date: "تمدید سررسید",
  activate_collateral_cheque: "فعال‌سازی چک پشتوانه",
  approve_preview: "تأیید پیش‌نمایش و ثبت",
  fix_rows_recalc: "اصلاح ردیف‌ها و محاسبهٔ مجدد",
  cancel_batch: "لغو بچ",
  approve_variance_entry: "تأیید سند کسری/اضافی",
  recount: "شمارش مجدد",
  escalate_to_manager: "ارجاع به مدیر",
  replay: "بازپخش دستی",
  fix_source_retry: "اصلاح دادهٔ مبدأ و پردازش مجدد",
  close_with_reason: "بستن با دلیل",
  filed: "ثبت در صف",
  assign: "تخصیص",
};

export function reviewActionLabel(action: string | null | undefined): string {
  if (!action) return "نامشخص";
  return REVIEW_ACTION_LABELS[action] ?? action;
}

/**
 * Age in hours → Persian duration («۴۵ دقیقه»، «۳ ساعت»، «۲ روز»، «۲ ماه»).
 * Under one hour shows minutes (at least ۱ دقیقه); months are 30-day
 * approximations, rounded down.
 */
export function formatAgeHours(ageHours: number | null | undefined): string {
  if (ageHours === null || ageHours === undefined || Number.isNaN(ageHours)) return "—";
  if (ageHours < 0) return "—";
  if (ageHours < 1) return `${faNumber(Math.max(1, Math.round(ageHours * 60)))} دقیقه`;
  if (ageHours < 24) return `${faNumber(Math.floor(ageHours))} ساعت`;
  const days = Math.floor(ageHours / 24);
  if (days < 30) return `${faNumber(days)} روز`;
  return `${faNumber(Math.floor(days / 30))} ماه`;
}

/** SLA state of an item, derived from the backend's computed flags. */
export type SlaState = "closed" | "breached" | "warn" | "ok";

export function slaState(item: {
  status: string;
  sla_breached: boolean;
  sla_warn: boolean;
}): SlaState {
  if (item.status === "resolved" || item.status === "written_off") return "closed";
  if (item.sla_breached) return "breached";
  if (item.sla_warn) return "warn";
  return "ok";
}

export const SLA_STATE_LABELS: Record<SlaState, { label: string; tone: StatusTone }> = {
  closed: { label: "بسته‌شده", tone: "outline" },
  breached: { label: "گذشته از مهلت", tone: "destructive" },
  warn: { label: "نزدیک مهلت", tone: "warning" },
  ok: { label: "در مهلت", tone: "success" },
};

/** True when the item still waits on a human (open or in review). */
export function isWaiting(item: { status: string }): boolean {
  return item.status === "open" || item.status === "in_review";
}
