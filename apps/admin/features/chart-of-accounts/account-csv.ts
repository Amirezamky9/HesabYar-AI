/**
 * Chart-of-accounts CSV export (US-305): the filtered chart,
 * normalized values — ASCII ids/codes, English type codes and
 * true/false flags. Built on the shared CSV helper
 * (features/shared/csv.ts).
 */

import type { AccountResponse } from "@/lib/api-client";
import { buildCsv, type CsvColumn } from "@/features/shared/csv";

export const ACCOUNT_CSV_COLUMNS: CsvColumn<AccountResponse>[] = [
  { header: "شناسه", value: (r) => r.id },
  { header: "کد", value: (r) => r.code },
  { header: "نام", value: (r) => r.name },
  { header: "نوع", value: (r) => r.type },
  { header: "قابل‌ثبت", value: (r) => (r.postable ? "true" : "false") },
  { header: "فعال", value: (r) => (r.active ? "true" : "false") },
  { header: "بسته", value: (r) => (r.closed ? "true" : "false") },
];

export function accountsToCsv(accounts: AccountResponse[]): string {
  return buildCsv(ACCOUNT_CSV_COLUMNS, accounts);
}
