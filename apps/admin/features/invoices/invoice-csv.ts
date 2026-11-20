/**
 * Invoice CSV export (US-304): the visible list, normalized values —
 * integer IRR amounts, ISO dates, English type/state codes, ASCII ids.
 */

import type { InvoiceResponse } from "@/lib/api-client";
import { buildCsv, type CsvColumn } from "@/features/shared/csv";

export const INVOICE_CSV_COLUMNS: CsvColumn<InvoiceResponse>[] = [
  { header: "شناسه", value: (r) => r.id },
  { header: "شماره", value: (r) => r.number },
  { header: "نوع", value: (r) => r.invoice_type },
  { header: "طرف حساب", value: (r) => r.party_name },
  { header: "تاریخ فاکتور", value: (r) => r.invoice_date },
  { header: "وضعیت", value: (r) => r.state },
  { header: "مبلغ بدون مالیات (ریال)", value: (r) => r.untaxed_amount },
  { header: "مالیات (ریال)", value: (r) => r.tax_amount },
  { header: "مبلغ کل (ریال)", value: (r) => r.total_amount },
];

export function invoicesToCsv(invoices: InvoiceResponse[]): string {
  return buildCsv(INVOICE_CSV_COLUMNS, invoices);
}
