/**
 * Persian labels for the compliance center (US-405).
 *
 * Every verification label, validation outcome and Moadian lifecycle
 * state the Phase 4 APIs return gets one Persian label, one tone and —
 * for gated/blocked items — the reason shown to the operator, in
 * Persian. Unknown values fall back to the raw code so a new backend
 * label never silently disappears.
 */

import type { StatusTone } from "@/components/ui";

/** Verification labels (docs/CURRENT_RULES_1405.md §1 + UNVERIFIED). */
export const VERIFICATION_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  VERIFIED_TEXT: { label: "متن تأییدشده", tone: "success" },
  CORROBORATED_CURRENT: { label: "تأیید با منبع مکمل", tone: "warning" },
  UNVERIFIED: { label: "تأییدنشده", tone: "outline" },
  DISCOVERY_ONLY: { label: "فقط برای یافتن منبع", tone: "outline" },
  BLOCKED_SOURCE_GAP: { label: "مسدود — کمبود منبع", tone: "destructive" },
  TRANSITIONAL: { label: "گذاری", tone: "warning" },
};

export function verificationLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return VERIFICATION_STATUS_LABELS[status]?.label ?? status;
}

export function verificationTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return VERIFICATION_STATUS_LABELS[status]?.tone ?? "outline";
}

/**
 * Why a gated/blocked item cannot be used for production, in Persian.
 * Shown next to the badge — never just a red label with no reason.
 */
export function verificationReason(status: string | null | undefined): string | null {
  switch (status) {
    case "BLOCKED_SOURCE_GAP":
      return "متن رسمی بازبینی‌شدهٔ این استاندارد هنوز وارد و تأیید نشده است؛ استفاده برای دوره‌های جاری مسدود است.";
    case "CORROBORATED_CURRENT":
      return "این پارامتر فقط با گزارش‌های خبری تأیید شده و منبع رسمی آن هنوز پین نشده است؛ برای استفادهٔ تولیدی مسدود است و فقط مرجع است.";
    case "UNVERIFIED":
      return "متن موجود است اما منبع رسمی آن هنوز پین و تأیید نشده است؛ به‌عنوان تأییدشده حساب نمی‌شود.";
    case "DISCOVERY_ONLY":
      return "این مورد فقط برای یافتن سند رسمی است و هرگز قابل اجرا نیست.";
    case "TRANSITIONAL":
      return "اثر اجرایی این قاعده به ابلاغیه‌ها و آماده‌سازی سامانه بستگی دارد.";
    default:
      return null;
  }
}

/** True when the label means "never use for production". */
export function isProductionBlocked(status: string | null | undefined): boolean {
  return status !== "VERIFIED_TEXT";
}

/** Validation-run per-rule outcomes (US-401: exactly these four). */
export const VALIDATION_OUTCOME_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  pass: { label: "قبول", tone: "success" },
  fail: { label: "رد", tone: "destructive" },
  not_applicable: { label: "نامربوط", tone: "outline" },
  error: { label: "خطا", tone: "warning" },
};

export function validationOutcomeLabel(outcome: string): string {
  return VALIDATION_OUTCOME_LABELS[outcome]?.label ?? outcome;
}

export function validationOutcomeTone(outcome: string): StatusTone {
  return VALIDATION_OUTCOME_LABELS[outcome]?.tone ?? "outline";
}

/** Moadian submission lifecycle (US-404). */
export const SUBMISSION_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  draft: { label: "پیش‌نویس", tone: "secondary" },
  approved: { label: "تأییدشده", tone: "warning" },
  queued: { label: "در صف", tone: "warning" },
  submitted: { label: "ارسال‌شده", tone: "default" },
  accepted: { label: "پذیرفته‌شده", tone: "success" },
  rejected: { label: "ردشده", tone: "destructive" },
  failed: { label: "ناموفق", tone: "destructive" },
};

export function submissionStatusLabel(status: string): string {
  return SUBMISSION_STATUS_LABELS[status]?.label ?? status;
}

export function submissionStatusTone(status: string): StatusTone {
  return SUBMISSION_STATUS_LABELS[status]?.tone ?? "outline";
}

/** Moadian document lifecycle. */
export const DOCUMENT_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  draft: { label: "پیش‌نویس", tone: "secondary" },
  submitted: { label: "ارسال‌شده", tone: "default" },
  accepted: { label: "پذیرفته‌شده", tone: "success" },
  rejected: { label: "ردشده", tone: "destructive" },
};

export function documentStatusLabel(status: string): string {
  return DOCUMENT_STATUS_LABELS[status]?.label ?? status;
}

export function documentStatusTone(status: string): StatusTone {
  return DOCUMENT_STATUS_LABELS[status]?.tone ?? "outline";
}

/** Official Moadian inquiry statuses (docs/moadian.md — SUCCESS etc. are corroborated, not instruction text). */
export const OFFICIAL_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  PENDING: { label: "در انتظار پردازش", tone: "warning" },
  SUCCESS: { label: "موفق", tone: "success" },
  FAILED: { label: "ناموفق", tone: "destructive" },
  IN_PROGRESS: { label: "در حال پردازش", tone: "warning" },
  NOT_FOUND: { label: "یافت نشد", tone: "outline" },
  TIMEOUT: { label: "پایان مهلت", tone: "warning" },
};

export function officialStatusLabel(status: string): string {
  return OFFICIAL_STATUS_LABELS[status]?.label ?? status;
}

export function officialStatusTone(status: string): StatusTone {
  return OFFICIAL_STATUS_LABELS[status]?.tone ?? "outline";
}

/** Moadian environments and sender modes. */
export const MOADIAN_ENVIRONMENT_LABELS: Record<string, string> = {
  test: "آزمایشی (شبیه‌ساز)",
  production: "تولیدی",
};

export const SENDER_MODE_LABELS: Record<string, string> = {
  "self-tsp": "مستقیم (مودی)",
  tsp: "شرکت معتمد",
};

/** Tax rule keys (docs/tax-policy.md §"Seeding") — the API's rule_name is already Persian; these are fallbacks. */
export const TAX_RULE_LABELS: Record<string, string> = {
  vat_general_rate: "نرخ عمومی مالیات بر ارزش افزوده",
  art84_salary_exemption: "معافیت حقوق (ماده ۸۴)",
  art101_exemption: "معافیت (ماده ۱۰۱)",
  art100_flat_tax_ceiling: "سقف مالیات مقطوع (ماده ۱۰۰)",
  growth_threshold_art105_131: "آستانهٔ رشد (مواد ۱۰۵ و ۱۳۱)",
};

export function taxRuleLabel(ruleKey: string, ruleName?: string | null): string {
  if (ruleName) return ruleName;
  return TAX_RULE_LABELS[ruleKey] ?? ruleKey;
}
