/**
 * Persian labels for Tryton account types (US-302).
 *
 * The backend returns Tryton's English `account.type` names (long
 * descriptive strings from the universal chart). The filter and the
 * detail panel show a short Persian label for every type present in the
 * chart; an unknown type falls back to its raw string (shown LTR) so a
 * new backend type never silently disappears from the UI. Filtering
 * always compares the normalized raw type, never the label (§5.5
 * rule 10).
 */

/** Exact Tryton type string -> Persian label. */
export const ACCOUNT_TYPE_LABELS: Record<string, string> = {
  "Universal Chart of Account Types": "نوع نمودار حساب‌ها",
  "Cash and Cash Equivalents": "نقد و معادل‌های نقد",
  "Investments accounted for using the equity method": "سرمایه‌گذاری به روش ارزش ویژه",
  "Financial assets (excluding investments accounted for using the equity method, accounts receivables, and cash and cash equivalents), such as notes receivables":
    "دارایی‌های مالی (اسناد دریافتنی)",
  "Accounts receivable": "حساب‌های دریافتنی",
  Inventories: "موجودی کالا",
  "Prepaid expenses for future services that will be used within a year": "پیش‌پرداخت هزینه‌ها",
  "Revenue Earned In Arrears (Accrued Revenue) for services done but not yet received for the year":
    "درآمد تحقق‌یافتهٔ دریافت‌نشده",
  "Property, Plant and Equipment": "دارایی‌های ثابت مشهود",
  "Depreciation / amortisation": "استهلاک انباشته",
  "Intangible assets, such as patents, copyrights and goodwill": "دارایی‌های نامشهود",
  "Accounts payable": "حساب‌های پرداختنی",
  Expenses: "هزینه‌ها",
  "Unearned revenue for services paid for by customers but not yet provided": "درآمد دریافت‌شدهٔ تحقق‌نیافته",
  "Deferred tax liabilities and deferred tax assets": "مالیات انتقالی",
  "Financial liabilities (excluding provisions and accounts payables), such as promissory notes and corporate bonds":
    "بدهی‌های مالی",
  "Provisions for warranties or court decisions (contingent liabilities that are both probable and measurable)":
    "ذخیره‌ها",
  "Issued capital and reserves attributable to equity holders of the parent company (controlling interest)":
    "سرمایه و اندوخته‌ها",
  "Non-controlling interest in equity": "سهم اقلیت",
  Revenue: "درآمد",
  "Cost of Sales": "بهای تمام‌شدهٔ فروش",
  "Selling, General and Administrative expenses": "هزینه‌های فروش، عمومی و اداری",
  "Other revenues or gains": "سایر درآمدها",
  "Other expenses or losses": "سایر هزینه‌ها",
  "Finance costs": "هزینه‌های مالی",
  "Liabilities and assets for current tax": "مالیات جاری",
  "Income tax expense": "هزینهٔ مالیات بر درآمد",
};

/** Persian label for a type, or the raw string when unmapped. */
export function accountTypeLabel(type: string | null): string {
  if (type === null) return "بدون نوع";
  return ACCOUNT_TYPE_LABELS[type] ?? type;
}

/** True when the type has a Persian label (vs. a raw English fallback). */
export function hasAccountTypeLabel(type: string | null): boolean {
  return type === null || type in ACCOUNT_TYPE_LABELS;
}
