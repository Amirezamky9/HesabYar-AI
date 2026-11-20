/**
 * Report CSV exports (US-305): the summary tables from the reports
 * page, built on the shared CSV helper (features/shared/csv.ts) —
 * normalized values only (integer IRR amounts, English state/kind
 * codes, ASCII ids), Persian headers, UTF-8 BOM for Excel.
 */

import { buildCsv, type CsvColumn } from "@/features/shared/csv";
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
