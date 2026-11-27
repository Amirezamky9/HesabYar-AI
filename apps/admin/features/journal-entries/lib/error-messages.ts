/**
 * API errors -> readable Persian messages (US-303).
 *
 * The backend returns RFC 9457 problem-details with a stable `code`;
 * the UI maps that code (and the HTTP status) to a Persian sentence and
 * never renders raw JSON or English detail strings. Unknown codes fall
 * back to a generic sentence that still names the stable code, so an
 * operator can report it.
 */

import { ApiError, type ValidationIssueResponse } from "@/lib/api-client";

/** Stable backend codes -> Persian (docs/rest-api.md error tables). */
const CODE_MESSAGES: Record<string, string> = {
  // Ledger
  LEDGER_UNBALANCED: "سند نامتوازن است؛ مجموع بدهکار باید با مجموع بستانکار برابر باشد.",
  LEDGER_INVALID_JOURNAL: "سند معتبر نیست؛ ردیف‌ها، حساب‌ها و تاریخ را بررسی کنید.",
  LEDGER_JOURNAL_NOT_FOUND: "این سند پیدا نشد؛ ممکن است حذف شده باشد یا به شرکت دیگری تعلق داشته باشد.",
  LEDGER_POSTED_IMMUTABLE: "سند ثبت‌شده قابل ویرایش نیست.",
  LEDGER_PERIOD_CLOSED: "تاریخ سند در یک دورهٔ مالی بسته است؛ تاریخ را به یک دورهٔ باز تغییر دهید.",
  ACCOUNT_NOT_FOUND: "یکی از حساب‌های انتخاب‌شده پیدا نشد.",
  COMPANY_NOT_FOUND: "شرکت پیدا نشد.",
  COMPANY_REQUIRED: "شرکت مشخص نیست؛ یک شرکت انتخاب کنید.",
  // Idempotency
  IDEMPOTENCY_KEY_REQUIRED: "کلید یکتایی درخواست ارسال نشد؛ دوباره تلاش کنید.",
  IDEMPOTENCY_CONFLICT: "این درخواست با داده‌های متفاوتی تکرار شده است؛ صفحه را تازه کنید و دوباره تلاش کنید.",
  IDEMPOTENCY_IN_PROGRESS: "همین درخواست در حال انجام است؛ چند لحظه صبر کنید و دوباره تلاش کنید.",
  // Engine / backend
  ENGINE_UNAVAILABLE: "موتور حسابداری در دسترس نیست؛ چند لحظه بعد دوباره تلاش کنید.",
  ENGINE_VALIDATION_FAILED: "موتور حسابداری داده‌ها را رد کرد؛ ورودی‌ها را بررسی کنید.",
  BACKEND_UNREACHABLE: "ارتباط با سرور حسابیار برقرار نشد؛ اتصال را بررسی کنید.",
  NOT_IMPLEMENTED: "این عملیات هنوز در دسترس نیست.",
  INVALID_FILTER: "فیلتر انتخاب‌شده معتبر نیست.",
  INVALID_CURSOR: "صفحه‌بندی نامعتبر است؛ فهرست را از ابتدا بارگذاری کنید.",
  UNAUTHORIZED: "نشست به پایان رسیده است؛ دوباره وارد شوید.",
  FORBIDDEN: "کلید شما دسترسی انجام این کار را ندارد.",
};

/** Validation-issue codes from POST .../validate -> Persian. */
const ISSUE_MESSAGES: Record<string, string> = {
  LEDGER_UNBALANCED: "مجموع بدهکار با مجموع بستانکار برابر نیست.",
  LEDGER_INVALID_JOURNAL: "ساختار سند معتبر نیست.",
  LEDGER_PERIOD_CLOSED: "تاریخ سند در یک دورهٔ مالی بسته است.",
  ACCOUNT_NOT_FOUND: "یکی از حساب‌ها پیدا نشد.",
};

/**
 * One Persian sentence for any failure from the API client. Never returns
 * raw JSON or an English backend string.
 */
export function apiErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const byCode = CODE_MESSAGES[error.code];
    if (byCode) return byCode;

    if (error.isUnauthorized) return CODE_MESSAGES.UNAUTHORIZED;
    if (error.isForbidden) {
      return "دسترسی ندارید؛ کلید شما مجوز این عملیات را ندارد.";
    }
    // FastAPI request-validation failures (422) carry a structured detail
    // the client cannot render; the operator just needs to re-check input.
    if (error.status === 422) {
      return "داده‌های واردشده معتبر نیست؛ ورودی‌ها را بررسی کنید.";
    }
    if (error.status === 404) return "رکورد موردنظر پیدا نشد.";
    if (error.status === 409) return "این عملیات با وضعیت فعلی سند سازگار نیست.";
    if (error.status >= 500) {
      return "خطای سرور رخ داد؛ چند لحظه بعد دوباره تلاش کنید.";
    }
    return `عملیات ناموفق بود. (کد خطا: ${error.code})`;
  }
  if (error instanceof Error && error.name === "TypeError") {
    return "ارتباط با سرور برقرار نشد؛ اتصال را بررسی کنید.";
  }
  return "خطای غیرمنتظره‌ای رخ داد؛ دوباره تلاش کنید.";
}

/** One Persian sentence for a single validation issue from the backend. */
export function validationIssueMessage(issue: ValidationIssueResponse): string {
  return ISSUE_MESSAGES[issue.code] ?? "سند یک ایراد اعتبارسنجی دارد.";
}

/** Audit action names (spec §14.3) -> Persian labels for the audit trail. */
export function auditActionLabel(action: string): string {
  switch (action) {
    case "ledger.draft.create":
      return "ایجاد پیش‌نویس";
    case "ledger.post":
      return "ثبت سند";
    case "ledger.reverse":
      return "برگشت سند";
    default:
      return action;
  }
}
