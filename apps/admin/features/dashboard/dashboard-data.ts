/**
 * Dashboard data helpers (US-302) — pure functions over live API data.
 *
 * The dashboard shows ONLY figures computed from API responses (spec
 * §11.3). Aggregation uses normalized values (raw state/type strings,
 * integer IRR, ISO dates) — never localized display strings (§5.5
 * rule 10). Nothing here invents data: when a list is empty the summary
 * is an honest zero, and widgets without an API source are coming-soon
 * tiles, not numbers.
 */

import type {
  InvoiceResponse,
  JournalDraftResponse,
  Paginated,
  PaymentResponse,
} from "@/lib/api-client";

/**
 * Invoice states that count as open receivables/payables: the invoice is
 * real (validated or posted) and not yet paid or cancelled. Draft
 * invoices are not obligations yet; paid/cancelled ones are settled.
 */
export const OPEN_INVOICE_STATES = ["validated", "posted"] as const;

/** Customer invoice types (Tryton `account.invoice.type`). */
export const CUSTOMER_INVOICE_TYPES = ["out", "out_credit_note"] as const;
/** Supplier invoice types. */
export const SUPPLIER_INVOICE_TYPES = ["in", "in_credit_note"] as const;

export interface InvoiceSummary {
  /** Open customer invoices (count + total, credit notes subtracted). */
  receivableCount: number;
  receivableTotalIrr: number;
  /** Open supplier invoices (count + total, credit notes subtracted). */
  payableCount: number;
  payableTotalIrr: number;
  /** Invoices the summary was computed from. */
  consideredCount: number;
}

/**
 * Receivables/payables from the invoices list. Credit notes reduce the
 * matching side (a customer credit note reduces receivables). Amounts
 * stay integer IRR; display converts to labelled Toman.
 */
export function summarizeInvoices(invoices: InvoiceResponse[]): InvoiceSummary {
  let receivableCount = 0;
  let receivableTotalIrr = 0;
  let payableCount = 0;
  let payableTotalIrr = 0;

  for (const invoice of invoices) {
    if (!(OPEN_INVOICE_STATES as readonly string[]).includes(invoice.state)) continue;
    if (invoice.invoice_type === "out") {
      receivableCount += 1;
      receivableTotalIrr += invoice.total_amount;
    } else if (invoice.invoice_type === "out_credit_note") {
      receivableTotalIrr -= invoice.total_amount;
    } else if (invoice.invoice_type === "in") {
      payableCount += 1;
      payableTotalIrr += invoice.total_amount;
    } else if (invoice.invoice_type === "in_credit_note") {
      payableTotalIrr -= invoice.total_amount;
    }
    // Unknown invoice types are ignored, never silently counted.
  }

  return {
    receivableCount,
    receivableTotalIrr,
    payableCount,
    payableTotalIrr,
    consideredCount: invoices.length,
  };
}

export interface DraftSummary {
  count: number;
  totalDebitIrr: number;
  totalCreditIrr: number;
}

/** Unposted journal drafts: count + summed debit/credit (integer IRR). */
export function summarizeDrafts(drafts: JournalDraftResponse[]): DraftSummary {
  let totalDebitIrr = 0;
  let totalCreditIrr = 0;
  for (const draft of drafts) {
    totalDebitIrr += draft.total_debit;
    totalCreditIrr += draft.total_credit;
  }
  return { count: drafts.length, totalDebitIrr, totalCreditIrr };
}

/** Sort key for an invoice: ISO date (lexicographic = chronological), then id. */
function invoiceSortKey(invoice: InvoiceResponse): string {
  return invoice.invoice_date ?? invoice.accounting_date ?? "";
}

/**
 * Most recent invoices first (ISO date descending, id descending as the
 * tiebreak). Sorting uses the normalized ISO strings, never Jalali
 * display strings (§5.5 rule 10).
 */
export function recentInvoices(invoices: InvoiceResponse[], limit: number): InvoiceResponse[] {
  return [...invoices]
    .sort((a, b) => {
      const byDate = invoiceSortKey(b).localeCompare(invoiceSortKey(a));
      return byDate !== 0 ? byDate : b.id - a.id;
    })
    .slice(0, limit);
}

/** Most recent payments first (ISO date descending, id descending). */
export function recentPayments(payments: PaymentResponse[], limit: number): PaymentResponse[] {
  return [...payments]
    .sort((a, b) => {
      const byDate = b.date.localeCompare(a.date);
      return byDate !== 0 ? byDate : b.id - a.id;
    })
    .slice(0, limit);
}

export interface FetchAllResult<T> {
  items: T[];
  /** True when the safety cap stopped paging — totals may be partial. */
  truncated: boolean;
}

/**
 * Follow a cursor-paginated list endpoint to the end (or the safety
 * cap). Dashboard totals must be computed from the whole list, not just
 * the first page — but an unbounded loop is not acceptable either, so
 * the cap is explicit and `truncated` tells the widget to say so.
 */
export async function fetchAllPages<T>(
  fetchPage: (cursor?: string) => Promise<Paginated<T>>,
  opts: { maxPages?: number; maxItems?: number } = {},
): Promise<FetchAllResult<T>> {
  const maxPages = opts.maxPages ?? 10;
  const maxItems = opts.maxItems ?? 500;
  const items: T[] = [];
  let cursor: string | undefined;
  let truncated = false;
  let lastPageHadMore = false;

  for (let page = 0; page < maxPages; page += 1) {
    const result = await fetchPage(cursor);
    items.push(...result.items);
    if (items.length >= maxItems) {
      truncated = true;
      break;
    }
    const nextCursor = result.next_cursor;
    if (nextCursor === null) {
      lastPageHadMore = false;
      break;
    }
    lastPageHadMore = true;
    cursor = nextCursor;
  }

  // Ran out of pages while the API still has more.
  if (!truncated && lastPageHadMore) truncated = true;

  return { items: items.slice(0, maxItems), truncated };
}
