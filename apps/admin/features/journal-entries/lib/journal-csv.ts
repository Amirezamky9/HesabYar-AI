/**
 * Journal CSV export (US-305): the visible draft list, normalized
 * values — ISO Gregorian dates, English state codes, integer IRR
 * totals, ASCII ids. Built on the shared CSV helper
 * (features/shared/csv.ts).
 */

import type { JournalDraftResponse } from "@/lib/api-client";
import { buildCsv, type CsvColumn } from "@/features/shared/csv";

export const JOURNAL_CSV_COLUMNS: CsvColumn<JournalDraftResponse>[] = [
  { header: "شناسه", value: (r) => r.id },
  { header: "تاریخ", value: (r) => r.date },
  { header: "شرح", value: (r) => r.description },
  { header: "دوره", value: (r) => r.period.name },
  { header: "وضعیت", value: (r) => r.state },
  { header: "بدهکار (ریال)", value: (r) => r.total_debit },
  { header: "بستانکار (ریال)", value: (r) => r.total_credit },
];

export function journalsToCsv(drafts: JournalDraftResponse[]): string {
  return buildCsv(JOURNAL_CSV_COLUMNS, drafts);
}
