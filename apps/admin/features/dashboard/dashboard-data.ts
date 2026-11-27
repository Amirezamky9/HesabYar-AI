/**
 * Dashboard data helpers (US-302) — pure functions over live API data.
 *
 * The dashboard shows ONLY figures computed from API responses (spec
 * §11.3). Aggregation uses normalized values (raw state/type strings,
 * integer IRR, ISO dates) — never localized display strings (§5.5
 * rule 10). Nothing here invents data: when a list is empty the summary
 * is an honest zero, and widgets without an API source are coming-soon
 * tiles, not numbers.
 *
 * US-551 adds the six unified-dashboard sections (§7.3): today's
 * money, money on the way, attention (review queue), treasury,
 * alerts and manual imports. The section summaries live here so
 * vitest can cover them without a backend; the widgets render them.
 */

import type {
  BankImportBatchResponse,
  CashBoxResponse,
  ChequeResponse,
  InstallmentLegResponse,
  InvoiceResponse,
  JournalDraftResponse,
  MaterialDetailResponse,
  Paginated,
  PaymentResponse,
  SettlementFileImportResponse,
  SettlementTrancheResponse,
} from "@/lib/api-client";
import type { StatusTone } from "@/components/ui";
import { JALALI_MONTHS, jalaliMonthLength, jalaliToIso, toJalali } from "@/lib/jalali";
import { dueBucket } from "@/features/cheques/cheque-data";
import { isTrancheOpen, parseDecimalString } from "@/features/settlement/settlement-data";

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

// ---------------------------------------------------------------------------
// US-551 — the six unified-dashboard sections (§7.3)
// ---------------------------------------------------------------------------

export interface JalaliMonthBounds {
  year: number;
  month: number;
  /** Jalali "YYYY-MM-DD" first/last day (leg + cheque due dates are Jalali). */
  startJalali: string;
  endJalali: string;
  /** Gregorian ISO first/last day (tranche expected_date + payments are ISO). */
  startIso: string;
  endIso: string;
  /** «آبان ۱۴۰۵» — Persian month name + Latin year (the widget localizes digits). */
  monthLabel: string;
}

/** The Jalali month containing `today`, in both calendars. */
export function jalaliMonthBounds(today: Date): JalaliMonthBounds {
  const { jy, jm } = toJalali(today);
  const lastDay = jalaliMonthLength(jy, jm);
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    year: jy,
    month: jm,
    startJalali: `${jy}-${pad(jm)}-01`,
    endJalali: `${jy}-${pad(jm)}-${pad(lastDay)}`,
    startIso: jalaliToIso(jy, jm, 1),
    endIso: jalaliToIso(jy, jm, lastDay),
    monthLabel: `${JALALI_MONTHS[jm - 1]} ${jy}`,
  };
}

// -- پولِ امروز ---------------------------------------------------------------

/** Only a succeeded payment is money that moved (draft/approved/processing are not). */
export const SUCCEEDED_PAYMENT_STATE = "succeeded";

export interface JournalTotal {
  journal: string;
  count: number;
  totalIrr: number;
}

export interface TodayMoneySummary {
  receiptCount: number;
  receiptTotalIrr: number;
  paymentCount: number;
  paymentTotalIrr: number;
  /** Today's receipts grouped by payment journal (the channel). */
  receiptsByJournal: JournalTotal[];
  /** Today's payments grouped by payment journal. */
  paymentsByJournal: JournalTotal[];
  /** Payments the summary was computed from. */
  consideredCount: number;
}

function groupByJournal(payments: PaymentResponse[]): JournalTotal[] {
  const byJournal = new Map<string, JournalTotal>();
  for (const payment of payments) {
    const journal = payment.journal_name || "";
    const row = byJournal.get(journal) ?? { journal, count: 0, totalIrr: 0 };
    row.count += 1;
    row.totalIrr += payment.amount;
    byJournal.set(journal, row);
  }
  return [...byJournal.values()].sort((a, b) => b.totalIrr - a.totalIrr);
}

/**
 * Today's receipts + payments from the payments list. The list is
 * newest-first, so today's rows are at the front; only `succeeded`
 * payments count — a draft or failed payment is not money.
 */
export function summarizeTodayPayments(
  payments: PaymentResponse[],
  todayIso: string,
): TodayMoneySummary {
  const today = payments.filter((p) => p.date === todayIso && p.state === SUCCEEDED_PAYMENT_STATE);
  const receipts = today.filter((p) => p.kind === "receivable");
  const paid = today.filter((p) => p.kind === "payable");
  return {
    receiptCount: receipts.length,
    receiptTotalIrr: receipts.reduce((sum, p) => sum + p.amount, 0),
    paymentCount: paid.length,
    paymentTotalIrr: paid.reduce((sum, p) => sum + p.amount, 0),
    receiptsByJournal: groupByJournal(receipts),
    paymentsByJournal: groupByJournal(paid),
    consideredCount: payments.length,
  };
}

// -- پولِ در راه ---------------------------------------------------------------

export interface TrancheSummary {
  /** Open tranches' outstanding net — the gateway receivables. */
  receivableCount: number;
  receivableTotalIrr: number;
  /** Open tranches due within the current Jalali month. */
  dueThisMonthCount: number;
  dueThisMonthTotalIrr: number;
  /** Overdue tranches (still receivable, surfaced as an alert too). */
  overdueCount: number;
  overdueTotalIrr: number;
  /** Tranches the summary was computed from. */
  consideredCount: number;
}

/**
 * Gateway receivables + this month's expected tranches + overdue
 * tranches. Openness follows the settlement center's `isTrancheOpen`
 * (settled/cancelled/written-off tranches owe nothing); every sum
 * uses `outstanding_net_irr` — money still owed, never gross.
 */
export function summarizeTranches(
  tranches: SettlementTrancheResponse[],
  monthStartIso: string,
  monthEndIso: string,
): TrancheSummary {
  const open = tranches.filter((t) => isTrancheOpen(t.state));
  const dueThisMonth = open.filter(
    (t) => t.expected_date >= monthStartIso && t.expected_date <= monthEndIso,
  );
  const overdue = open.filter((t) => t.state === "overdue");
  return {
    receivableCount: open.length,
    receivableTotalIrr: open.reduce((sum, t) => sum + t.outstanding_net_irr, 0),
    dueThisMonthCount: dueThisMonth.length,
    dueThisMonthTotalIrr: dueThisMonth.reduce((sum, t) => sum + t.outstanding_net_irr, 0),
    overdueCount: overdue.length,
    overdueTotalIrr: overdue.reduce((sum, t) => sum + t.outstanding_net_irr, 0),
    consideredCount: tranches.length,
  };
}

export interface ChequeTotal {
  count: number;
  totalIrr: number;
}

/** In-clearing RECEIVED commercial cheques — money on its way in. */
export function summarizeInClearingReceived(cheques: ChequeResponse[]): ChequeTotal {
  const rows = cheques.filter(
    (c) => c.kind === "received" && c.purpose === "payment" && c.state === "in_clearing",
  );
  return { count: rows.length, totalIrr: rows.reduce((sum, c) => sum + c.amount_irr, 0) };
}

/** Leg statuses that still owe money (a paid leg owes nothing; a
 * pending-clearance leg is money already received, clearing). An
 * overdue leg due this month still counts here — like an overdue
 * tranche, it is money due that has not arrived. */
export const UNPAID_LEG_STATUSES = ["expected", "due", "partially_paid", "overdue"] as const;

/**
 * Internal-plan legs due within the current Jalali month. BNPL legs
 * are excluded by the caller (their money arrives via gateway
 * tranches, counted above — summing both would double-count). Each
 * leg contributes its BALANCE, not its principal: a partially paid
 * leg owes only what is left.
 */
export function summarizeLegsDueThisMonth(
  legs: InstallmentLegResponse[],
  startJalali: string,
  endJalali: string,
): ChequeTotal {
  const rows = legs.filter(
    (leg) =>
      (UNPAID_LEG_STATUSES as readonly string[]).includes(leg.status) &&
      leg.due_date >= startJalali &&
      leg.due_date <= endJalali,
  );
  return { count: rows.length, totalIrr: rows.reduce((sum, leg) => sum + leg.balance_irr, 0) };
}

// -- خزانه ---------------------------------------------------------------------

export interface UncountedCashbox {
  id: string;
  nameFa: string;
  daysSinceLastCount: number | null;
}

export interface CashboxSummary {
  /** Active boxes only — a retired box's balance is stale. */
  boxCount: number;
  /** Sum of the KNOWN book balances (null balances excluded, counted below). */
  totalBalanceIrr: number;
  knownBalanceCount: number;
  unknownBalanceCount: number;
  uncounted: UncountedCashbox[];
}

/** Cashbox balances from the cash-box list (company-scoped). */
export function summarizeCashboxes(boxes: CashBoxResponse[]): CashboxSummary {
  const active = boxes.filter((box) => box.active);
  let totalBalanceIrr = 0;
  let knownBalanceCount = 0;
  let unknownBalanceCount = 0;
  const uncounted: UncountedCashbox[] = [];
  for (const box of active) {
    if (box.book_balance_irr === null) {
      unknownBalanceCount += 1;
    } else {
      knownBalanceCount += 1;
      totalBalanceIrr += box.book_balance_irr;
    }
    if (box.uncounted_alert) {
      uncounted.push({
        id: box.id,
        nameFa: box.name_fa,
        daysSinceLastCount: box.days_since_last_count,
      });
    }
  }
  return { boxCount: active.length, totalBalanceIrr, knownBalanceCount, unknownBalanceCount, uncounted };
}

export interface DueSoonSummary {
  /** The matching cheques, nearest due date first. */
  cheques: ChequeResponse[];
  count: number;
  totalIrr: number;
  nearestDueJalali: string | null;
}

/**
 * ISSUED commercial cheques due within 7 days (or overdue) — the
 * week's expected outflows. Collateral never enters (§2.2).
 */
export function summarizeIssuedDueSoon(
  cheques: ChequeResponse[],
  todayJalali: string,
): DueSoonSummary {
  const rows = cheques
    .filter(
      (c) =>
        c.kind === "issued" &&
        c.purpose === "payment" &&
        c.state === "issued" &&
        ["overdue", "due-7"].includes(dueBucket(c.due_date, todayJalali)),
    )
    .sort((a, b) => (a.due_date < b.due_date ? -1 : 1));
  return {
    cheques: rows,
    count: rows.length,
    totalIrr: rows.reduce((sum, c) => sum + c.amount_irr, 0),
    nearestDueJalali: rows[0]?.due_date ?? null,
  };
}

/**
 * RECEIVED commercial cheques due within 7 days (or overdue) — the
 * due-soon widget's rows and the matching alert's figures.
 */
export function summarizeReceivedDueSoon(
  cheques: ChequeResponse[],
  todayJalali: string,
): DueSoonSummary {
  const rows = cheques
    .filter(
      (c) =>
        c.kind === "received" &&
        c.purpose === "payment" &&
        ["held", "in_clearing", "registered"].includes(c.state) &&
        ["overdue", "due-7"].includes(dueBucket(c.due_date, todayJalali)),
    )
    .sort((a, b) => (a.due_date < b.due_date ? -1 : 1));
  return {
    cheques: rows,
    count: rows.length,
    totalIrr: rows.reduce((sum, c) => sum + c.amount_irr, 0),
    nearestDueJalali: rows[0]?.due_date ?? null,
  };
}

// -- هشدارها -------------------------------------------------------------------

export interface ReorderAlert {
  materialId: string;
  nameFa: string;
  quantity: string;
  minQuantity: string;
}

/**
 * Materials whose live stock crossed the reorder point (quantity <
 * min_quantity). Decimal strings parse via the settlement helper —
 * an unparseable quantity never raises a false alert. Inactive
 * materials are skipped.
 */
export function packagingReorderAlerts(materials: MaterialDetailResponse[]): ReorderAlert[] {
  const alerts: ReorderAlert[] = [];
  for (const detail of materials) {
    if (!detail.material.is_active) continue;
    if (detail.stock === null || detail.reorder_point === null) continue;
    const quantity = parseDecimalString(detail.stock.quantity);
    const minQuantity = parseDecimalString(detail.reorder_point.min_quantity);
    if (quantity === null || minQuantity === null) continue;
    if (quantity < minQuantity) {
      alerts.push({
        materialId: detail.material.id,
        nameFa: detail.material.name_fa,
        quantity: detail.stock.quantity,
        minQuantity: detail.reorder_point.min_quantity,
      });
    }
  }
  return alerts;
}

export type DashboardAlertKind =
  | "overdue-tranche"
  | "cheque-due-soon"
  | "overdue-installment"
  | "packaging-reorder"
  | "cashbox-uncounted"
  | "stale-inbox";

export type DashboardAlertSeverity = "critical" | "warning";

export interface DashboardAlert {
  id: string;
  kind: DashboardAlertKind;
  severity: DashboardAlertSeverity;
  count: number;
  /** Money at stake, when the alert carries an amount. */
  totalIrr: number | null;
  href: string;
}

export interface BuildAlertsInput {
  overdueTranches: ChequeTotal;
  chequesDueSoon: ChequeTotal;
  overdueInstallments: ChequeTotal;
  reorderCrossed: ReorderAlert[];
  uncountedCashboxes: UncountedCashbox[];
  /** Stuck inbox events (hours = the staleness threshold that caught them). */
  staleInbox: { count: number; hours: number };
}

/**
 * The alerts section, critical first then warning, in a fixed kind
 * order within each severity. Only non-empty alerts appear — an empty
 * alerts section means nothing needs attention, not missing data.
 * Severity: money already late (overdue tranche / installment) and a
 * stuck pipeline (inbox) are critical; things about to be late
 * (cheque due soon, reorder crossed, uncounted cashbox) warn.
 */
export function buildAlerts(input: BuildAlertsInput): DashboardAlert[] {
  const alerts: DashboardAlert[] = [];
  if (input.overdueInstallments.count > 0) {
    alerts.push({
      id: "overdue-installment",
      kind: "overdue-installment",
      severity: "critical",
      count: input.overdueInstallments.count,
      totalIrr: input.overdueInstallments.totalIrr,
      href: "/installments/reports?report=overdue",
    });
  }
  if (input.overdueTranches.count > 0) {
    alerts.push({
      id: "overdue-tranche",
      kind: "overdue-tranche",
      severity: "critical",
      count: input.overdueTranches.count,
      totalIrr: input.overdueTranches.totalIrr,
      href: "/settlements/schedule?state=overdue",
    });
  }
  if (input.staleInbox.count > 0) {
    alerts.push({
      id: "stale-inbox",
      kind: "stale-inbox",
      severity: "critical",
      count: input.staleInbox.count,
      totalIrr: null,
      href: "/review-queue",
    });
  }
  if (input.chequesDueSoon.count > 0) {
    alerts.push({
      id: "cheque-due-soon",
      kind: "cheque-due-soon",
      severity: "warning",
      count: input.chequesDueSoon.count,
      totalIrr: input.chequesDueSoon.totalIrr,
      href: "/cheques/reports?report=due-received",
    });
  }
  if (input.reorderCrossed.length > 0) {
    alerts.push({
      id: "packaging-reorder",
      kind: "packaging-reorder",
      severity: "warning",
      count: input.reorderCrossed.length,
      totalIrr: null,
      href: "/packaging",
    });
  }
  if (input.uncountedCashboxes.length > 0) {
    alerts.push({
      id: "cashbox-uncounted",
      kind: "cashbox-uncounted",
      severity: "warning",
      count: input.uncountedCashboxes.length,
      totalIrr: null,
      href: "/banking/cash-boxes",
    });
  }
  return alerts;
}

// -- ایمپورت‌های دستی ----------------------------------------------------------

export type ImportBatchKind = "statement" | "settlement";

export interface ImportBatchRow {
  id: string;
  kind: ImportBatchKind;
  title: string;
  status: string;
  createdAt: string | null;
  /** Statement batches: deposits − withdrawals is the batch's net effect. */
  depositsIrr: number | null;
  withdrawalsIrr: number | null;
  /** Settlement imports carry row counts, not money totals. */
  rowCount: number | null;
  matchedCount: number | null;
  errorCount: number | null;
  href: string;
}

/**
 * The most recent manual import batches across both import flows
 * (bank statements US-514 + settlement files US-523), newest first.
 * A batch with no created_at sorts last, never first. The US-554
 * batch model (one flow, accounting-effect preview) replaces this
 * merged view when it lands.
 */
export function importBatchRows(
  statementBatches: BankImportBatchResponse[],
  settlementImports: SettlementFileImportResponse[],
  limit: number,
): ImportBatchRow[] {
  const rows: ImportBatchRow[] = [
    ...statementBatches.map(
      (batch): ImportBatchRow => ({
        id: `statement:${batch.id}`,
        kind: "statement",
        title: batch.filename,
        status: batch.status,
        createdAt: batch.created_at,
        depositsIrr: batch.total_deposits_irr,
        withdrawalsIrr: batch.total_withdrawals_irr,
        rowCount: batch.total_rows,
        matchedCount: batch.new_rows,
        errorCount: batch.invalid_rows,
        href: "/banking/statements/import",
      }),
    ),
    ...settlementImports.map(
      (fileImport): ImportBatchRow => ({
        id: `settlement:${fileImport.id}`,
        kind: "settlement",
        title: fileImport.period_jalali
          ? `تسویهٔ دورهٔ ${fileImport.period_jalali}`
          : "ایمپورت تسویه",
        status: fileImport.status,
        createdAt: fileImport.created_at,
        depositsIrr: null,
        withdrawalsIrr: null,
        rowCount: fileImport.row_count,
        matchedCount: fileImport.matched_count,
        errorCount: fileImport.error_count,
        href: `/settlements/imports/${fileImport.id}`,
      }),
    ),
  ];
  rows.sort((a, b) => {
    if (a.createdAt === null && b.createdAt === null) return 0;
    if (a.createdAt === null) return 1;
    if (b.createdAt === null) return -1;
    return b.createdAt.localeCompare(a.createdAt);
  });
  return rows.slice(0, limit);
}

/** Statement batch statuses (US-514): awaiting_review / confirmed / cancelled. */
export const STATEMENT_BATCH_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  awaiting_review: { label: "در انتظار بازبینی", tone: "warning" },
  confirmed: { label: "تأیید و ثبت شد", tone: "success" },
  cancelled: { label: "لغوشده", tone: "outline" },
};

export function statementBatchStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return STATEMENT_BATCH_STATUS_LABELS[status]?.label ?? status;
}

export function statementBatchStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return STATEMENT_BATCH_STATUS_LABELS[status]?.tone ?? "outline";
}
