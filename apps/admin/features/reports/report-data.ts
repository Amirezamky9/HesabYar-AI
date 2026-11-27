/**
 * Report aggregations (US-305) — pure functions over live API data.
 *
 * Only figures computable from list endpoints the API actually
 * enumerates are built here: invoice summaries (by state, by party)
 * and payment summaries (by state, by kind). Aggregation uses
 * normalized values (raw state/type strings, integer IRR, ISO dates)
 * — never localized display strings (§5.5 rule 10).
 *
 * Deliberately NOT here: the posted-move ledger, trial balance,
 * balance sheet, P&L and cash flow. The drafts endpoint returns
 * draft moves only (adapter list_journal_drafts filters state=draft)
 * and there is no journals-list or report endpoint (get_report was
 * deferred in Phase 2), so those views are honest coming-soon in the
 * reports page — never computed client-side from partial data.
 */

import type { InvoiceResponse, PaymentResponse } from "@/lib/api-client";

/** Canonical invoice-state order for the summary table. */
const INVOICE_STATE_ORDER = ["draft", "validated", "posted", "paid", "cancelled"] as const;
/** Canonical payment-state order for the summary table. */
const PAYMENT_STATE_ORDER = [
  "draft",
  "submitted",
  "approved",
  "processing",
  "succeeded",
  "failed",
] as const;

export interface InvoiceStateRow {
  state: string;
  count: number;
  untaxedIrr: number;
  taxIrr: number;
  totalIrr: number;
}

/**
 * Invoices grouped by state. Amounts are raw sums of the invoices in
 * each state (integer IRR); credit notes are NOT netted here — the
 * table states plainly that each row is the sum of that state's
 * invoices, so the reader is never misled about netting.
 */
export function summarizeInvoicesByState(invoices: InvoiceResponse[]): InvoiceStateRow[] {
  const byState = new Map<string, InvoiceStateRow>();
  for (const invoice of invoices) {
    const row = byState.get(invoice.state) ?? {
      state: invoice.state,
      count: 0,
      untaxedIrr: 0,
      taxIrr: 0,
      totalIrr: 0,
    };
    row.count += 1;
    row.untaxedIrr += invoice.untaxed_amount;
    row.taxIrr += invoice.tax_amount;
    row.totalIrr += invoice.total_amount;
    byState.set(invoice.state, row);
  }
  const order = (state: string): number => {
    const index = (INVOICE_STATE_ORDER as readonly string[]).indexOf(state);
    return index === -1 ? INVOICE_STATE_ORDER.length : index;
  };
  return [...byState.values()].sort(
    (a, b) => order(a.state) - order(b.state) || a.state.localeCompare(b.state),
  );
}

export interface InvoicePartyRow {
  partyId: number;
  partyName: string;
  count: number;
  totalIrr: number;
}

/** Invoices grouped by party, largest total first. */
export function summarizeInvoicesByParty(invoices: InvoiceResponse[]): InvoicePartyRow[] {
  const byParty = new Map<number, InvoicePartyRow>();
  for (const invoice of invoices) {
    const row = byParty.get(invoice.party_id) ?? {
      partyId: invoice.party_id,
      partyName: invoice.party_name,
      count: 0,
      totalIrr: 0,
    };
    row.count += 1;
    row.totalIrr += invoice.total_amount;
    byParty.set(invoice.party_id, row);
  }
  return [...byParty.values()].sort(
    (a, b) => b.totalIrr - a.totalIrr || a.partyName.localeCompare(b.partyName),
  );
}

export interface PaymentStateRow {
  state: string;
  count: number;
  totalIrr: number;
}

/** Payments grouped by state. */
export function summarizePaymentsByState(payments: PaymentResponse[]): PaymentStateRow[] {
  const byState = new Map<string, PaymentStateRow>();
  for (const payment of payments) {
    const row = byState.get(payment.state) ?? { state: payment.state, count: 0, totalIrr: 0 };
    row.count += 1;
    row.totalIrr += payment.amount;
    byState.set(payment.state, row);
  }
  const order = (state: string): number => {
    const index = (PAYMENT_STATE_ORDER as readonly string[]).indexOf(state);
    return index === -1 ? PAYMENT_STATE_ORDER.length : index;
  };
  return [...byState.values()].sort(
    (a, b) => order(a.state) - order(b.state) || a.state.localeCompare(b.state),
  );
}

export interface PaymentKindRow {
  kind: string;
  count: number;
  totalIrr: number;
}

/** Payments grouped by kind (receivable = money in, payable = money out). */
export function summarizePaymentsByKind(payments: PaymentResponse[]): PaymentKindRow[] {
  const byKind = new Map<string, PaymentKindRow>();
  for (const payment of payments) {
    const row = byKind.get(payment.kind) ?? { kind: payment.kind, count: 0, totalIrr: 0 };
    row.count += 1;
    row.totalIrr += payment.amount;
    byKind.set(payment.kind, row);
  }
  // Receivable first, then payable, then anything unknown.
  const order = (kind: string): number =>
    kind === "receivable" ? 0 : kind === "payable" ? 1 : 2;
  return [...byKind.values()].sort((a, b) => order(a.kind) - order(b.kind));
}

/** ISO date range covered by a set of rows (nulls when there are no dated rows). */
export interface ReportPeriod {
  fromIso: string | null;
  toIso: string | null;
}

function periodOf(dates: (string | null | undefined)[]): ReportPeriod {
  const dated = dates.filter((d): d is string => typeof d === "string" && d !== "");
  if (dated.length === 0) return { fromIso: null, toIso: null };
  // ISO dates sort lexicographically = chronologically.
  const sorted = [...dated].sort();
  return { fromIso: sorted[0], toIso: sorted[sorted.length - 1] };
}

/** Period covered by invoices (invoice date, falling back to accounting date). */
export function invoicePeriod(invoices: InvoiceResponse[]): ReportPeriod {
  return periodOf(invoices.map((i) => i.invoice_date ?? i.accounting_date));
}

/** Period covered by payments. */
export function paymentPeriod(payments: PaymentResponse[]): ReportPeriod {
  return periodOf(payments.map((p) => p.date));
}

/** Sum of total_amount over invoices (integer IRR). */
export function sumInvoiceTotals(invoices: InvoiceResponse[]): number {
  return invoices.reduce((sum, invoice) => sum + invoice.total_amount, 0);
}

/** Sum of amount over payments (integer IRR). */
export function sumPaymentTotals(payments: PaymentResponse[]): number {
  return payments.reduce((sum, payment) => sum + payment.amount, 0);
}
