/**
 * Shared API-error handling for the US-304 feature pages.
 *
 * The backend returns RFC 9457 problem details with a stable `code`;
 * pages map that code to a Persian message and never render raw JSON
 * (README rules). 401 always means "session gone" -> back to /login;
 * 403 means the operator key lacks the scope for this screen.
 */

import { ApiError } from "@/lib/api-client";

/** Persian message for an unknown failure, per screen. */
export function persianApiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.isUnauthorized) return "نشست شما به پایان رسیده است؛ دوباره وارد شوید.";
    if (err.isForbidden) return "کلید شما برای این بخش دسترسی ندارد.";
    if (err.isNotFound) return "این رکورد پیدا نشد؛ ممکن است حذف شده باشد.";
    switch (err.code) {
      case "COMPANY_REQUIRED":
        return "شرکتی انتخاب نشده است؛ ابتدا یک شرکت انتخاب کنید.";
      case "NOT_IMPLEMENTED":
        return "این قابلیت هنوز در دسترس نیست.";
      case "STANDARD_SOURCE_BLOCKED":
        return "منبع این استاندارد برای دورهٔ انتخاب‌شده معتبر نیست؛ تا وقتی متن رسمی بازبینی‌شده وارد و تأیید نشود، اعتبارسنجی برای این دوره مسدود است.";
      case "STANDARD_NOT_FOUND":
        return "این استاندارد در رجیستری پیدا نشد.";
      case "TAX_POLICY_SOURCE_BLOCKED":
        return "این پارامتر مالیاتی هنوز منبع رسمی تأییدشده ندارد و برای استفادهٔ تولیدی مسدود است؛ فقط به‌عنوان مرجع قابل مشاهده است.";
      case "TAX_POLICY_NOT_FOUND":
        return "برای این تاریخ و قاعده، نسخهٔ فعالی از سیاست مالیاتی پیدا نشد.";
      case "MOADIAN_PRODUCTION_NOT_CONNECTED":
        return "اتصال تولیدی مودیان هنوز برقرار نشده است؛ فقط شبیه‌ساز آزمایشی در دسترس است.";
      case "MOADIAN_PROFILE_SLUG_CONFLICT":
        return "پروفایلی با این شناسهٔ یکتا قبلاً ساخته شده است؛ شناسهٔ دیگری انتخاب کنید.";
      case "MOADIAN_DUPLICATE_TAXID":
        return "سندی با این شمارهٔ مالیاتی یکتا قبلاً ثبت شده است.";
      case "MOADIAN_INVALID_DOCUMENT":
        return "سند مودیان معتبر نیست؛ فیلدهای الزامی دستورالعمل را بررسی کنید.";
      case "MOADIAN_INVALID_STATE":
        return "وضعیت فعلی این رکورد اجازهٔ این عملیات را نمی‌دهد؛ برای ساخت سند یک پروفایل فعال انتخاب کنید.";
      case "TRIAL_BALANCE_UNBALANCED":
        return "تراز آزمایشی متوازن نیست؛ تا رفع مغایرت نمی‌توان اعتبارسنجی گزارش اجرا کرد.";
      case "REVIEW_ITEM_NOT_FOUND":
        return "این آیتم در صف پیدا نشد؛ ممکن است حذف شده باشد.";
      case "REVIEW_ACTION_NOT_PERMITTED":
        return "این اقدام برای این نوع آیتم مجاز نیست.";
      case "REVIEW_INVALID_STATE":
        return "این آیتم بسته شده است و دیگر اقدامی روی آن ممکن نیست.";
      case "REVIEW_INVALID_SLA_POLICY":
        return "مهلت نامعتبر است؛ مهلت هشدار باید کوچک‌تر از مهلت اصلی و هر دو بزرگ‌تر از صفر باشند.";
      case "MANUAL_ENTRY_INVALID":
        return "ورودی معتبر نیست؛ مقادیر را بررسی کنید — مثلاً جمع ردیف‌ها، مبلغ کل یا اینکه مبلغ هر ردیف عدد صحیح تومان باشد.";
      case "MANUAL_ENTRY_NOT_FOUND":
        return "این ورودی دستی پیدا نشد؛ ممکن است حذف شده باشد.";
      case "IDEMPOTENCY_CONFLICT":
        return "این درخواست قبلاً با دادهٔ متفاوتی ثبت شده است؛ صفحه را تازه‌سازی کنید و دوباره تلاش کنید.";
      case "IDEMPOTENCY_IN_PROGRESS":
        return "درخواست قبلی هنوز در حال پردازش است؛ چند لحظه صبر کنید و دوباره تلاش کنید.";
      case "CASHBOX_NOT_FOUND":
        return "این صندوق پیدا نشد؛ ممکن است حذف شده باشد.";
      case "CASHBOX_JOURNAL_CONFLICT":
        return "این دفتر روزانه قبلاً یک صندوق دارد؛ هر دفتر روزانه فقط یک صندوق دارد.";
      case "CASHBOX_INVALID":
        return "اطلاعات صندوق معتبر نیست؛ نام، نوع و سقف تنخواه را بررسی کنید.";
      case "CASHBOX_SETTINGS_INVALID":
        return "تنظیمات صندوق معتبر نیست؛ آستانه‌ها و برنامهٔ شمارش را بررسی کنید.";
      case "COUNT_SESSION_NOT_FOUND":
        return "این جلسهٔ شمارش پیدا نشد؛ ممکن است حذف شده باشد.";
      case "COUNT_SESSION_INVALID_STATE":
        return "وضعیت فعلی این جلسهٔ شمارش اجازهٔ این عملیات را نمی‌دهد.";
      case "COUNT_SESSION_ALREADY_OPEN":
        return "این صندوق در حال حاضر یک جلسهٔ شمارش باز دارد؛ اول آن را کامل کنید.";
      case "COUNT_SUPERVISOR_MUST_DIFFER":
        return "ناظر باید با صندوقدار متفاوت باشد؛ شمارش در حضور یک ناظر دیگر انجام می‌شود.";
      case "COUNT_MINUTES_REQUIRED":
        return "برای کسری یا اضافه، صورتجلسهٔ امضاشده لازم است؛ متن صورتجلسه را بنویسید.";
      case "COUNT_REVIEW_NOT_APPROVED":
        return "سند کسری/اضافه هنوز در صف بازبینی تأیید نشده است؛ اول مدیر آن را در صف بازبینی تأیید کند.";
      case "COUNT_DENOMINATIONS_MISMATCH":
        return "مجموع اسکناس‌ها و سکه‌ها با مبلغ شمارش‌شده برابر نیست.";
      case "COUNT_RECOUNT_NOT_REQUESTED":
        return "مدیر هنوز شمارش مجدد نخواسته است؛ درخواست شمارش مجدد از صف بازبینی می‌آید.";
      case "COUNT_WRONG_ACTOR":
        return "این امضا یا اقدام به شما تعلق ندارد؛ نقش درست را انتخاب کنید.";
      case "LEDGER_INVALID_INVOICE":
      case "LEDGER_INVALID_PAYMENT":
      case "LEDGER_INVALID_STATEMENT":
        return "مقدار فیلتر معتبر نیست.";
      case "LEDGER_INVOICE_NOT_FOUND":
      case "LEDGER_PAYMENT_NOT_FOUND":
      case "LEDGER_STATEMENT_NOT_FOUND":
        return "این رکورد پیدا نشد؛ ممکن است حذف شده باشد.";
      default:
        return fallback;
    }
  }
  return "ارتباط با سرور برقرار نشد؛ اتصال را بررسی کنید و دوباره تلاش کنید.";
}

/** True when the error is a permission denial (403) — a distinct UI state. */
export function isPermissionDenied(err: unknown): boolean {
  return err instanceof ApiError && err.isForbidden;
}

/** True when the error is a missing record (404) — a distinct UI state. */
export function isNotFoundError(err: unknown): boolean {
  return err instanceof ApiError && err.isNotFound;
}
