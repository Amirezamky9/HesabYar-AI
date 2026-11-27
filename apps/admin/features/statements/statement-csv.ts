/**
 * Bank statement CSV export (US-304): the visible list, normalized
 * values — integer IRR amounts, ISO dates, English state codes.
 */

import type { BankStatementResponse } from "@/lib/api-client";
import { buildCsv, type CsvColumn } from "@/features/shared/csv";

export const STATEMENT_CSV_COLUMNS: CsvColumn<BankStatementResponse>[] = [
  { header: "شناسه", value: (r) => r.id },
  { header: "نام", value: (r) => r.name },
  { header: "دفتر صورت‌حساب", value: (r) => r.journal_name },
  { header: "تاریخ", value: (r) => r.date },
  { header: "وضعیت", value: (r) => r.state },
  { header: "ماندهٔ اول (ریال)", value: (r) => r.start_balance },
  { header: "ماندهٔ آخر (ریال)", value: (r) => r.end_balance },
  { header: "مانده (ریال)", value: (r) => r.balance },
  { header: "مبلغ کل (ریال)", value: (r) => r.total_amount },
  { header: "تعداد ردیف‌ها", value: (r) => r.number_of_lines },
];

export function statementsToCsv(statements: BankStatementResponse[]): string {
  return buildCsv(STATEMENT_CSV_COLUMNS, statements);
}
