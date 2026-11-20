/**
 * Invoice labels (US-304). State labels come from the facade STATUS_MAP;
 * invoice types have no facade entry, so they live here.
 */

/** Invoice states the API accepts as a filter (docs/rest-api.md). */
export const INVOICE_STATES = ["draft", "validated", "posted", "paid", "cancelled"] as const;
export type InvoiceState = (typeof INVOICE_STATES)[number];

/** Invoice types: 'out' (customer) / 'in' (supplier). */
export const INVOICE_TYPES = ["out", "in"] as const;
export type InvoiceType = (typeof INVOICE_TYPES)[number];

export const INVOICE_TYPE_LABELS: Record<InvoiceType, string> = {
  out: "فروش",
  in: "خرید",
};

/** Line kinds the engine returns (schema invoices.py). */
export const INVOICE_LINE_TYPE_LABELS: Record<string, string> = {
  line: "ردیف",
  subtotal: "جمع جزء",
  title: "عنوان",
  comment: "توضیح",
};

export function invoiceLineTypeLabel(lineType: string): string {
  return INVOICE_LINE_TYPE_LABELS[lineType] ?? lineType;
}
