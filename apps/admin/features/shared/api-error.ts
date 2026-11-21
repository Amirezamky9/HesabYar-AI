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
    // No generic 404 return here: a specific code (BANK_RULE_NOT_FOUND,
    // REVIEW_ITEM_NOT_FOUND, ...) has its own Persian message in the
    // switch, and an early 404 return would shadow every one of them.
    // Unknown 404 codes still get the generic message (switch default).
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
      case "REVIEW_REPLAY_NOT_POSSIBLE":
        return "این آیتم رویداد مبدأیی ندارد که بازپخش شود؛ بازپخش فقط برای آیتم‌های دارای رویداد ممکن است.";
      case "EVENT_NOT_DEAD_LETTERED":
        return "این رویداد در وضعیت مرده نیست؛ فقط رویداد مرده قابل بازپخش است.";
      case "EVENT_NOT_REPROCESSABLE":
        return "این رویداد قابل پردازش مجدد نیست؛ یا قبلاً پردازش شده یا در حال پردازش است.";
      case "INBOX_EVENT_NOT_FOUND":
        return "رویداد مبدأ این آیتم پیدا نشد؛ ممکن است حذف شده باشد.";
      case "INTEGRATION_MAPPING_INVALID":
        return "نگاشت معتبر نیست؛ نوع نگاشت، کلید مبدأ و مرجع مقصد را بررسی کنید.";
      case "INTEGRATION_MAPPING_NOT_PROMOTABLE":
        return "این نگاشت هنوز قابل ارتقا به خودکار نیست؛ نگاشت باید فعال و از نوع پیشنهادی باشد و دست‌کم ۳ بار تأیید شده باشد.";
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
      case "INSTALLMENT_PLAN_NOT_FOUND":
        return "این برنامهٔ قسط پیدا نشد؛ ممکن است حذف شده باشد.";
      case "INSTALLMENT_PLAN_EXISTS":
        return "برای این فاکتور قبلاً یک برنامهٔ قسط ساخته شده است؛ هر فاکتور فقط یک برنامه دارد.";
      case "INSTALLMENT_RECEIPT_LINKAGE_REQUIRED":
        return "هر دریافت قسط باید هم به فاکتور اصلی و هم به سند حسابداری (روزنامه) مرتبط باشد؛ هر دو مرجع را وارد کنید.";
      case "INSTALLMENT_INVOICE_MISMATCH":
        return "فاکتور واردشده با فاکتور این برنامه یکی نیست.";
      case "INSTALLMENT_VALIDATION_FAILED":
        return "اطلاعات واردشده معتبر نیست؛ مبلغ‌ها، تاریخ‌ها و تعداد اقساط را بررسی کنید.";
      case "INSTALLMENT_SETTLEMENT_AMOUNT_INSUFFICIENT":
        return "مبلغ تسویه کمتر از مبلغ تسویهٔ زودهنگام است؛ مبلغ را با نقل‌قول تسویه مقایسه کنید.";
      case "INSTALLMENT_CHEQUE_NOT_RECEIVABLE":
        return "این چک نمی‌تواند پشتوانهٔ دریافت باشد؛ فقط چک دریافتی تجاری در وضعیت «نزد ما» یا «در جریان وصول» قابل قبول است.";
      case "INSTALLMENT_CHEQUE_ALREADY_USED":
        return "این چک قبلاً پشتوانهٔ یک دریافت دیگر شده است.";
      case "INSTALLMENT_CHEQUE_AMOUNT_EXCEEDED":
        return "مبلغ دریافت از مبلغ چک بیشتر است.";
      case "INSTALLMENT_CHEQUE_LINK_MISMATCH":
        return "این چک قبلاً به قسط دیگری مرتبط شده است.";
      case "INSTALLMENT_BNPL_PLAN_REQUIRED":
        return "دریافت BNPL فقط روی برنامهٔ BNPL ممکن است.";
      case "INSTALLMENT_BNPL_PLAN_METHOD_NOT_ALLOWED":
        return "روی برنامهٔ BNPL فقط دریافت از درگاه ممکن است؛ نقدی، بانکی و چک قابل قبول نیست.";
      case "INSTALLMENT_BNPL_GATEWAY_MISMATCH":
        return "درگاه این دریافت با درگاه برنامه یکی نیست.";
      case "INSTALLMENT_BNPL_ALREADY_REJECTED":
        return "این دریافت BNPL قبلاً رد شده است.";
      case "INSTALLMENT_LATE_FEE_ACCOUNT_MISSING":
        return "حساب جریمه یا حساب دریافتنی در قرارداد تنظیم نشده است؛ اول قرارداد را کامل کنید.";
      case "INSTALLMENT_BNPL_EARLY_SETTLEMENT_NOT_ALLOWED":
        return "برنامهٔ BNPL تسویهٔ زودهنگام ندارد؛ وصول با درگاه است.";
      case "SMS_REMINDER_ALREADY_SENT":
        return "برای این قسط و این فاصلهٔ زمانی قبلاً یادآوری ارسال شده است.";
      case "SMS_LEG_NOT_REMINDABLE":
        return "این قسط قابل یادآوری نیست؛ یا پرداخت شده یا در انتظار وصول چک است.";
      case "SMS_NO_UNPAID_LEG":
        return "این برنامه قسط پرداخت‌نشده‌ای ندارد.";
      case "SMS_RECIPIENT_PHONE_MISSING":
        return "شمارهٔ موبایل مشتری ثبت نشده است؛ شماره را وارد کنید.";
      case "SMS_NO_PROVIDER_AVAILABLE":
        return "ارائه‌دهندهٔ پیامک فعالی تنظیم نشده است؛ اول یک ارائه‌دهنده را فعال کنید.";
      case "SMS_PROVIDER_NOT_ENABLED":
        return "این ارائه‌دهندهٔ پیامک فعال نیست.";
      case "INSTALLMENT_PLAN_ALREADY_SETTLED":
        return "این برنامه قبلاً تسویه شده است.";
      case "INSTALLMENT_NOT_A_BNPL_RECEIPT":
        return "این دریافت، دریافت BNPL نیست.";
      case "INSTALLMENT_PAYMENT_METHOD_UNSUPPORTED":
        return "روش پرداخت برای این برنامه پشتیبانی نمی‌شود.";
      case "LEDGER_INVALID_INVOICE":
      case "LEDGER_INVALID_PAYMENT":
      case "LEDGER_INVALID_STATEMENT":
        return "مقدار فیلتر معتبر نیست.";
      case "LEDGER_INVOICE_NOT_FOUND":
      case "LEDGER_PAYMENT_NOT_FOUND":
      case "LEDGER_STATEMENT_NOT_FOUND":
        return "این رکورد پیدا نشد؛ ممکن است حذف شده باشد.";
      case "RULE_NOT_FOUND":
        return "این قانون پیدا نشد؛ ممکن است حذف شده باشد.";
      case "RULE_INVALID":
        return "تعریف قانون معتبر نیست؛ رویداد، شرط‌ها و اقدام را بررسی کنید — اقدام باید از مجموعهٔ مجاز باشد و شرط‌ها باید در قالب مجاز باشند.";
      case "RULE_EXECUTION_NOT_FOUND":
        return "این اجرای قانون پیدا نشد؛ ممکن است حذف شده باشد.";
      case "EVENT_NOT_FOUND":
        return "رویداد گذشته پیدا نشد؛ شناسهٔ رویداد را بررسی کنید.";
      case "RULE_EVENT_INPUT_CONFLICT":
        return "برای آزمایش یا اجرا، یا شناسهٔ رویداد گذشته را بدهید یا نمونهٔ آزمایشی را؛ هر دو با هم قابل قبول نیست.";
      case "RULE_EVENT_INPUT_REQUIRED":
        return "برای آزمایش یا اجرا، شناسهٔ رویداد گذشته یا نمونهٔ آزمایشی لازم است.";
      case "BANK_RULE_NOT_FOUND":
        return "این قاعدهٔ بانکی پیدا نشد؛ ممکن است حذف شده باشد.";
      case "BANK_RULE_INVALID":
        return "تعریف قاعدهٔ بانکی معتبر نیست؛ دست‌کم یک شرط لازم است و حداقل مبلغ نمی‌تواند بیشتر از حداکثر باشد.";
      case "BANK_RULE_NOT_PROMOTABLE":
        return "این قاعده هنوز سه تأیید نگرفته است؛ ارتقا به خودکار ممکن نیست.";
      case "BANK_RULE_SUGGESTION_NOT_FOUND":
        return "این پیشنهاد پیدا نشد؛ ممکن است حذف شده باشد.";
      case "BANK_RULE_SUGGESTION_NOT_PENDING":
        return "این پیشنهاد قبلاً بررسی شده است؛ دیگر در انتظار نیست.";
      case "BANK_RULE_NOTHING_TO_UNDO":
        return "برای این ردیف کدگذاری خودکاری وجود ندارد که بتوان آن را برگرداند.";
      default:
        if (err.isNotFound) return "این رکورد پیدا نشد؛ ممکن است حذف شده باشد.";
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
