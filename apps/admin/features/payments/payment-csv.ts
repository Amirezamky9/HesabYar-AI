/**
 * Payment CSV export (US-304): the visible list, normalized values —
 * integer IRR amounts, ISO dates, English kind/state codes, ASCII ids.
 */

import type { PaymentResponse } from "@/lib/api-client";
import { buildCsv, type CsvColumn } from "@/features/shared/csv";

export const PAYMENT_CSV_COLUMNS: CsvColumn<PaymentResponse>[] = [
  { header: "شناسه", value: (r) => r.id },
  { header: "شماره", value: (r) => r.number },
  { header: "نوع", value: (r) => r.kind },
  { header: "طرف حساب", value: (r) => r.party_name },
  { header: "تاریخ", value: (r) => r.date },
  { header: "مبلغ (ریال)", value: (r) => r.amount },
  { header: "وضعیت", value: (r) => r.state },
  { header: "مرجع", value: (r) => r.reference },
  { header: "دفتر پرداخت", value: (r) => r.journal_name },
];

export function paymentsToCsv(payments: PaymentResponse[]): string {
  return buildCsv(PAYMENT_CSV_COLUMNS, payments);
}
