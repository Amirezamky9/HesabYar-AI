/**
 * Report CSV exports (US-305): the summary tables from the reports
 * page, built on the shared CSV helper (features/shared/csv.ts) —
 * normalized values only (integer IRR amounts, English state/kind
 * codes, ASCII ids), Persian headers, UTF-8 BOM for Excel.
 */

import { buildCsv, type CsvColumn } from "@/features/shared/csv";
import type {
  BalanceSheetResponse,
  CashFlowResponse,
  GeneralLedgerAccountResponse,
  GeneralLedgerResponse,
  IncomeStatementResponse,
  NotesBalanceResponse,
  TrialBalanceResponse,
} from "@/lib/api-client";
import { withCsvFilterHeader } from "./financial-report-data";
import type {
  InvoicePartyRow,
  InvoiceStateRow,
  PaymentKindRow,
  PaymentStateRow,
} from "./report-data";

export const INVOICE_STATE_CSV_COLUMNS: CsvColumn<InvoiceStateRow>[] = [
  { header: "وضعیت", value: (r) => r.state },
  { header: "تعداد فاکتور", value: (r) => r.count },
  { header: "مبلغ بدون مالیات (ریال)", value: (r) => r.untaxedIrr },
  { header: "مالیات (ریال)", value: (r) => r.taxIrr },
  { header: "مبلغ کل (ریال)", value: (r) => r.totalIrr },
];

export function invoiceStateSummaryToCsv(rows: InvoiceStateRow[]): string {
  return buildCsv(INVOICE_STATE_CSV_COLUMNS, rows);
}

export const INVOICE_PARTY_CSV_COLUMNS: CsvColumn<InvoicePartyRow>[] = [
  { header: "شناسه طرف حساب", value: (r) => r.partyId },
  { header: "طرف حساب", value: (r) => r.partyName },
  { header: "تعداد فاکتور", value: (r) => r.count },
  { header: "مبلغ کل (ریال)", value: (r) => r.totalIrr },
];

export function invoicePartySummaryToCsv(rows: InvoicePartyRow[]): string {
  return buildCsv(INVOICE_PARTY_CSV_COLUMNS, rows);
}

export const PAYMENT_STATE_CSV_COLUMNS: CsvColumn<PaymentStateRow>[] = [
  { header: "وضعیت", value: (r) => r.state },
  { header: "تعداد پرداخت", value: (r) => r.count },
  { header: "مبلغ کل (ریال)", value: (r) => r.totalIrr },
];

export function paymentStateSummaryToCsv(rows: PaymentStateRow[]): string {
  return buildCsv(PAYMENT_STATE_CSV_COLUMNS, rows);
}

export const PAYMENT_KIND_CSV_COLUMNS: CsvColumn<PaymentKindRow>[] = [
  { header: "نوع", value: (r) => r.kind },
  { header: "تعداد پرداخت", value: (r) => r.count },
  { header: "مبلغ کل (ریال)", value: (r) => r.totalIrr },
];

export function paymentKindSummaryToCsv(rows: PaymentKindRow[]): string {
  return buildCsv(PAYMENT_KIND_CSV_COLUMNS, rows);
}

/**
 * Financial-report exports (US-550, AC 7): every export prepends a
 * `#`-prefixed filter header (report title, company, period, applied
 * filters) above the normalized table, so the file states what it
 * was filtered by. Values stay normalized (integer IRR, ISO dates,
 * ASCII ids); headers are Persian.
 */

export type CsvFilters = [string, string][];

export function trialBalanceToCsv(response: TrialBalanceResponse, filters: CsvFilters): string {
  const body = buildCsv(
    [
      { header: "کد حساب", value: (r) => r.code },
      { header: "نام حساب", value: (r) => r.name },
      { header: "بدهکار (ریال)", value: (r) => r.total_debit },
      { header: "بستانکار (ریال)", value: (r) => r.total_credit },
      { header: "مانده (ریال)", value: (r) => r.balance },
    ],
    response.lines,
  );
  return withCsvFilterHeader(body, "تراز آزمایشی", filters);
}

export function generalLedgerSummaryToCsv(response: GeneralLedgerResponse, filters: CsvFilters): string {
  const body = buildCsv(
    [
      { header: "کد حساب", value: (r) => r.code },
      { header: "نام حساب", value: (r) => r.name },
      { header: "ماندهٔ ابتدا (ریال)", value: (r) => r.opening_balance },
      { header: "بدهکار (ریال)", value: (r) => r.total_debit },
      { header: "بستانکار (ریال)", value: (r) => r.total_credit },
      { header: "ماندهٔ انتها (ریال)", value: (r) => r.closing_balance },
    ],
    response.accounts,
  );
  return withCsvFilterHeader(body, "دفتر کل (خلاصهٔ حساب‌ها)", filters);
}

export function generalLedgerDetailToCsv(
  account: GeneralLedgerAccountResponse,
  dateFrom: string,
  dateTo: string,
  filters: CsvFilters,
): string {
  const body = buildCsv(
    [
      { header: "تاریخ (میلادی)", value: (r) => r.date },
      { header: "شمارهٔ سند", value: (r) => r.move_number },
      { header: "شرح", value: (r) => r.description },
      { header: "بدهکار (ریال)", value: (r) => r.debit },
      { header: "بستانکار (ریال)", value: (r) => r.credit },
      { header: "مانده (ریال)", value: (r) => r.balance },
    ],
    account.lines,
  );
  return withCsvFilterHeader(
    body,
    `دفتر کل — ${account.code} ${account.name}`,
    [...filters, ["دوره", `${dateFrom} تا ${dateTo}`]],
  );
}

interface StatementCsvRow {
  section: string;
  label: string;
  amount: number;
  comparison: number | null;
}

function statementLinesToCsvRows(
  sections: { key: string; title: string; lines: { label: string; code: string | null; depth: number; amount: number; amount_cmp: number | null }[]; total: number; total_cmp: number | null }[],
  sectionLabel: (key: string, fallback: string) => string,
): StatementCsvRow[] {
  const rows: StatementCsvRow[] = [];
  for (const section of sections) {
    for (const line of section.lines) {
      rows.push({
        section: sectionLabel(section.key, section.title),
        label: `${"  ".repeat(line.depth)}${line.code ? `${line.code} ` : ""}${line.label}`,
        amount: line.amount,
        comparison: line.amount_cmp,
      });
    }
    rows.push({
      section: sectionLabel(section.key, section.title),
      label: "جمع بخش",
      amount: section.total,
      comparison: section.total_cmp,
    });
  }
  return rows;
}

const STATEMENT_CSV_COLUMNS: CsvColumn<StatementCsvRow>[] = [
  { header: "بخش", value: (r) => r.section },
  { header: "ردیف", value: (r) => r.label },
  { header: "مبلغ (ریال)", value: (r) => r.amount },
  { header: "مبلغ دورهٔ مقایسه (ریال)", value: (r) => r.comparison },
];

export function balanceSheetToCsv(
  response: BalanceSheetResponse,
  sectionLabel: (key: string, fallback: string) => string,
  filters: CsvFilters,
): string {
  const body = buildCsv(STATEMENT_CSV_COLUMNS, statementLinesToCsvRows(response.sections, sectionLabel));
  return withCsvFilterHeader(body, "ترازنامه", filters);
}

export function incomeStatementToCsv(
  response: IncomeStatementResponse,
  sectionLabel: (key: string, fallback: string) => string,
  filters: CsvFilters,
): string {
  const statement = buildCsv(STATEMENT_CSV_COLUMNS, statementLinesToCsvRows(response.sections, sectionLabel));
  const monthly = buildCsv(
    [
      { header: "ماه شمسی", value: (r) => r.jalali_month },
      { header: "درآمد (ریال)", value: (r) => r.revenue },
      { header: "هزینه (ریال)", value: (r) => r.expense },
    ],
    response.monthly,
  );
  // Two blocks in one file: the statement, then the monthly breakdown.
  const combined = `${statement.replace(/\r\n$/, "")}\r\n\r\n${monthly.replace(/^\uFEFF/, "")}`;
  return withCsvFilterHeader(combined, "صورت سود و زیان", filters);
}

export function cashFlowToCsv(response: CashFlowResponse, filters: CsvFilters): string {
  const body = buildCsv(
    [
      { header: "بخش", value: (r) => r.key },
      { header: "ورودی (ریال)", value: (r) => r.inflow },
      { header: "خروجی (ریال)", value: (r) => r.outflow },
      { header: "خالص (ریال)", value: (r) => r.net },
      { header: "تعداد سند", value: (r) => r.line_count },
    ],
    response.sections,
  );
  return withCsvFilterHeader(body, "صورت جریان وجوه نقد", filters);
}

export function notesBalanceToCsv(response: NotesBalanceResponse, filters: CsvFilters): string {
  const body = buildCsv(
    [
      { header: "طرف حساب", value: (r) => r.party_name },
      { header: "نوع سند", value: (r) => r.instrument },
      { header: "کد حساب", value: (r) => r.account_code },
      { header: "سررسید نشده (ریال)", value: (r) => r.bucket_not_yet_due },
      { header: "۰ تا ۳۰ روز (ریال)", value: (r) => r.bucket_0_30 },
      { header: "۳۰ تا ۶۰ روز (ریال)", value: (r) => r.bucket_30_60 },
      { header: "۶۰ تا ۹۰ روز (ریال)", value: (r) => r.bucket_60_90 },
      { header: "۹۰ تا ۱۲۰ روز (ریال)", value: (r) => r.bucket_90_120 },
      { header: "بیش از ۱۲۰ روز (ریال)", value: (r) => r.bucket_120_plus },
      { header: "جمع (ریال)", value: (r) => r.total },
      { header: "تعداد سند", value: (r) => r.line_count },
    ],
    response.rows,
  );
  return withCsvFilterHeader(body, "ماندهٔ اسناد دریافتنی/پرداختنی", filters);
}
