/**
 * Persian labels + pure helpers for the installments center (US-545, Phase 5-5 ۵ج).
 *
 * Every plan kind, plan status, leg status, payment method, BNPL
 * status, reminder status and amendment kind the API returns gets
 * one Persian label. Unknown values fall back to the raw code so a
 * new backend value never silently disappears.
 *
 * Pure helpers (overdue aging buckets, the collection forecast,
 * dunning grouping, the BNPL/internal split, per-customer
 * aggregation, early settlements, bounced-cheque legs, finance
 * income aggregation, the SMS log roll-up, installment aging) live
 * here so the reports page and the detail page share them — and so
 * vitest can cover them without a backend.
 *
 * Dates: the API travels Jalali "YYYY-MM-DD" strings (zero-padded),
 * so lexicographic comparison orders them and `jalaliToIso`
 * converts them to Gregorian for day arithmetic. Jalali helpers are
 * shared with the cheque center (`../cheques/cheque-data`).
 *
 * BNPL and internal installments are separate worlds (§7.5 #11):
 * a BNPL plan never accrues finance income and never enters
 * dunning — the gateway owns collection. Every helper that touches
 * income, dunning or the collection forecast takes internal plans
 * only, and says so.
 */

import type { StatusTone } from "@/components/ui";
import type {
  InstallmentLegResponse,
  InstallmentPlanDetailResponse,
  InstallmentPlanResponse,
  InstallmentReceiptResponse,
  OverdueLegResponse,
  PayoffQuoteResponse,
  ReminderLogResponse,
} from "@/lib/api-client";
import { faNumber } from "@/lib/utils";
import { JALALI_MONTHS, isoToJalali } from "@/lib/jalali";
import { daysToDue, jalaliDaysBetween } from "../cheques/cheque-data";

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

export const PLAN_KIND_LABELS: Record<string, string> = {
  internal: "داخلی",
  bnpl: "BNPL (درگاه)",
};

export function planKindLabel(kind: string | null | undefined): string {
  if (!kind) return "نامشخص";
  return PLAN_KIND_LABELS[kind] ?? kind;
}

export const PLAN_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  active: { label: "فعال", tone: "outline" },
  overdue: { label: "معوق", tone: "destructive" },
  paid: { label: "تسویه‌شده", tone: "success" },
};

export function planStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return PLAN_STATUS_LABELS[status]?.label ?? status;
}

export function planStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return PLAN_STATUS_LABELS[status]?.tone ?? "outline";
}

/** Internal leg states (computed, never stored) + BNPL leg states (US-543). */
export const LEG_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  expected: { label: "در انتظار سررسید", tone: "outline" },
  due: { label: "سررسیدشده", tone: "warning" },
  partially_paid: { label: "پرداخت جزئی", tone: "warning" },
  paid: { label: "پرداخت‌شده", tone: "success" },
  overdue: { label: "معوق", tone: "destructive" },
  pending_clearance: { label: "در انتظار وصول چک", tone: "warning" },
  gateway_confirmed: { label: "تأیید درگاه", tone: "warning" },
  settled_per_tranche: { label: "تسویهٔ جزئی درگاه", tone: "warning" },
};

export function legStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return LEG_STATUS_LABELS[status]?.label ?? status;
}

export function legStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return LEG_STATUS_LABELS[status]?.tone ?? "outline";
}

export const PAYMENT_METHOD_LABELS: Record<string, string> = {
  cash: "نقدی",
  bank_transfer: "انتقال بانکی",
  other: "سایر",
  cheque: "چک",
  bnpl: "BNPL",
};

export function paymentMethodLabel(method: string | null | undefined): string {
  if (!method) return "نامشخص";
  return PAYMENT_METHOD_LABELS[method] ?? method;
}

export const BNPL_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  active: { label: "فعال", tone: "outline" },
  rejected: { label: "ردشدهٔ درگاه", tone: "destructive" },
};

export function bnplStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return BNPL_STATUS_LABELS[status]?.label ?? status;
}

export function bnplStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return BNPL_STATUS_LABELS[status]?.tone ?? "outline";
}

export const REMINDER_STATUS_LABELS: Record<string, { label: string; tone: StatusTone }> = {
  queued: { label: "در صف", tone: "outline" },
  sent: { label: "ارسال‌شده", tone: "success" },
  delivered: { label: "تحویل‌شده", tone: "success" },
  failed: { label: "ناموفق", tone: "destructive" },
  skipped: { label: "ردشده (عدم ارسال)", tone: "outline" },
  dry_run: { label: "پیش‌نمایش", tone: "outline" },
};

export function reminderStatusLabel(status: string | null | undefined): string {
  if (!status) return "نامشخص";
  return REMINDER_STATUS_LABELS[status]?.label ?? status;
}

export function reminderStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "outline";
  return REMINDER_STATUS_LABELS[status]?.tone ?? "outline";
}

export const REMINDER_TRIGGER_LABELS: Record<string, string> = {
  scheduled: "زمان‌بندی‌شده",
  manual: "دستی",
};

export function reminderTriggerLabel(trigger: string | null | undefined): string {
  if (!trigger) return "نامشخص";
  return REMINDER_TRIGGER_LABELS[trigger] ?? trigger;
}

export const AMENDMENT_KIND_LABELS: Record<string, string> = {
  re_amortisation: "بازتوزیع اقساط (تسویهٔ جزئی زودهنگام)",
};

export function amendmentKindLabel(kind: string | null | undefined): string {
  if (!kind) return "نامشخص";
  return AMENDMENT_KIND_LABELS[kind] ?? kind;
}

export const LATE_FEE_BASIS_LABELS: Record<string, string> = {
  none: "بدون جریمه",
  daily_percent: "درصد روزانه",
  monthly_percent: "درصد ماهانه",
  fixed_per_day: "مبلغ ثابت روزانه",
};

export function lateFeeBasisLabel(basis: string | null | undefined): string {
  if (!basis) return "نامشخص";
  return LATE_FEE_BASIS_LABELS[basis] ?? basis;
}

/** Early-settlement policy values (fee: waived | kept; income: waived | discounted | full). */
export const SETTLEMENT_POLICY_LABELS: Record<string, string> = {
  waived: "بخشیده می‌شود",
  kept: "دریافت می‌شود",
  discounted: "با تخفیف",
  full: "کامل",
};

export function settlementPolicyLabel(policy: string | null | undefined): string {
  if (!policy) return "نامشخص";
  return SETTLEMENT_POLICY_LABELS[policy] ?? policy;
}

/** Dunning stage 0 = overdue but before the first reminder. */
export function dunningStageLabel(stage: number): string {
  if (stage === 0) return "مرحلهٔ ۰ — پیش از نخستین یادآوری";
  return `مرحلهٔ ${faNumber(stage)} پیگیری`;
}

// ---------------------------------------------------------------------------
// Overdue aging buckets (§7.5 #1: ۱–۳۰ / ۳۱–۶۰ / ۶۱–۹۰ / +۹۰ روز)
// ---------------------------------------------------------------------------

export type OverdueAgeBucket = "1-30" | "31-60" | "61-90" | "90+";

export const OVERDUE_AGE_BUCKET_LABELS: Record<OverdueAgeBucket, string> = {
  "1-30": "۱ تا ۳۰ روز",
  "31-60": "۳۱ تا ۶۰ روز",
  "61-90": "۶۱ تا ۹۰ روز",
  "90+": "بیش از ۹۰ روز",
};

export const OVERDUE_AGE_BUCKETS: OverdueAgeBucket[] = ["1-30", "31-60", "61-90", "90+"];

export function overdueAgeBucket(daysOverdue: number): OverdueAgeBucket {
  if (daysOverdue <= 30) return "1-30";
  if (daysOverdue <= 60) return "31-60";
  if (daysOverdue <= 90) return "61-90";
  return "90+";
}

export interface OverdueBucketTotal {
  bucket: OverdueAgeBucket;
  count: number;
  balanceIrr: number;
  lateFeesIrr: number;
}

/** Group overdue-report rows into the §7.5 aging buckets. */
export function bucketOverdueRows(rows: OverdueLegResponse[]): OverdueBucketTotal[] {
  const totals = new Map<OverdueAgeBucket, OverdueBucketTotal>(
    OVERDUE_AGE_BUCKETS.map((bucket) => [
      bucket,
      { bucket, count: 0, balanceIrr: 0, lateFeesIrr: 0 },
    ]),
  );
  for (const row of rows) {
    const total = totals.get(overdueAgeBucket(row.days_overdue));
    if (!total) continue;
    total.count += 1;
    total.balanceIrr += row.balance_irr;
    total.lateFeesIrr += row.late_fee_accrued_irr;
  }
  return OVERDUE_AGE_BUCKETS.map((bucket) => totals.get(bucket) as OverdueBucketTotal);
}

// ---------------------------------------------------------------------------
// Installment aging (report 11): every outstanding leg, current + overdue
// ---------------------------------------------------------------------------

export type AgingBucket = "current" | OverdueAgeBucket;

export const AGING_BUCKET_LABELS: Record<AgingBucket, string> = {
  current: "جاری (سررسید نرسیده)",
  ...OVERDUE_AGE_BUCKET_LABELS,
};

export const AGING_BUCKETS: AgingBucket[] = ["current", ...OVERDUE_AGE_BUCKETS];

/** The aging bucket of one outstanding leg (balance > 0). */
export function agingBucketForLeg(leg: InstallmentLegResponse, todayJalali: string): AgingBucket {
  const days = daysToDue(leg.due_date, todayJalali);
  if (days >= 0) return "current";
  return overdueAgeBucket(-days);
}

export interface AgingBucketTotal {
  bucket: AgingBucket;
  count: number;
  balanceIrr: number;
}

/**
 * Age every outstanding leg of the given plan details. BNPL legs are
 * excluded: the gateway owes the seller, not the customer, and the
 * BNPL aging lives in the settlement center (§7.5 #11).
 */
export function ageOutstandingLegs(
  details: InstallmentPlanDetailResponse[],
  todayJalali: string,
): AgingBucketTotal[] {
  const totals = new Map<AgingBucket, AgingBucketTotal>(
    AGING_BUCKETS.map((bucket) => [bucket, { bucket, count: 0, balanceIrr: 0 }]),
  );
  for (const detail of details) {
    if (detail.plan.kind === "bnpl") continue;
    for (const leg of detail.legs) {
      if (leg.balance_irr <= 0) continue;
      const total = totals.get(agingBucketForLeg(leg, todayJalali));
      if (!total) continue;
      total.count += 1;
      total.balanceIrr += leg.balance_irr;
    }
  }
  return AGING_BUCKETS.map((bucket) => totals.get(bucket) as AgingBucketTotal);
}

// ---------------------------------------------------------------------------
// Collection forecast (report 2): open internal legs by due month.
// ---------------------------------------------------------------------------

export interface ForecastBucket {
  /** "overdue" | "later" | a Jalali month key "YYYY-MM". */
  key: string;
  label: string;
  amountIrr: number;
  count: number;
}

/** «آبان ۱۴۰۵» for a Jalali month key "1405-08". */
export function jalaliMonthLabel(monthKey: string): string {
  const match = monthKey.match(/^(\d{4})-(\d{2})$/);
  if (!match) return monthKey;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return monthKey;
  return `${JALALI_MONTHS[month - 1]} ${faNumber(Number(match[1]))}`;
}

/**
 * Expected collections from open internal legs: overdue legs first
 * (collectable now), then the next `months` Jalali months by due
 * date, then everything further out. Pending-clearance legs are
 * excluded — their money is already in flight via cheque clearing,
 * not a collection to chase. BNPL legs are excluded (gateway money).
 */
export function collectionForecast(
  details: InstallmentPlanDetailResponse[],
  todayJalali: string,
  months = 6,
): ForecastBucket[] {
  const monthKeys: string[] = [];
  let cursor = todayJalali.slice(0, 7);
  for (let i = 0; i < months; i++) {
    monthKeys.push(cursor);
    const [y, m] = cursor.split("-").map(Number);
    cursor = m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, "0")}`;
  }
  const buckets = new Map<string, ForecastBucket>([
    ["overdue", { key: "overdue", label: "معوق — قابل وصول فوری", amountIrr: 0, count: 0 }],
    ...monthKeys.map(
      (key) => [key, { key, label: jalaliMonthLabel(key), amountIrr: 0, count: 0 }] as const,
    ),
    ["later", { key: "later", label: "بعد از آن", amountIrr: 0, count: 0 }],
  ]);
  const lastMonth = monthKeys[monthKeys.length - 1];
  for (const detail of details) {
    if (detail.plan.kind === "bnpl") continue;
    for (const leg of detail.legs) {
      if (leg.balance_irr <= 0) continue;
      if (leg.status === "pending_clearance") continue;
      const days = daysToDue(leg.due_date, todayJalali);
      let key: string;
      if (days < 0) {
        key = "overdue";
      } else {
        const monthKey = leg.due_date.slice(0, 7);
        key = monthKey <= lastMonth ? monthKey : "later";
      }
      const bucket = buckets.get(key);
      if (!bucket) continue;
      bucket.amountIrr += leg.balance_irr;
      bucket.count += 1;
    }
  }
  return [...buckets.values()];
}

// ---------------------------------------------------------------------------
// Dunning stages (report 4): group overdue rows by their stage.
// ---------------------------------------------------------------------------

export interface DunningStageTotal {
  stage: number;
  count: number;
  balanceIrr: number;
  lateFeesIrr: number;
}

export function groupOverdueByStage(rows: OverdueLegResponse[]): DunningStageTotal[] {
  const totals = new Map<number, DunningStageTotal>();
  for (const row of rows) {
    const total = totals.get(row.dunning_stage) ?? {
      stage: row.dunning_stage,
      count: 0,
      balanceIrr: 0,
      lateFeesIrr: 0,
    };
    total.count += 1;
    total.balanceIrr += row.balance_irr;
    total.lateFeesIrr += row.late_fee_accrued_irr;
    totals.set(row.dunning_stage, total);
  }
  return [...totals.values()].sort((a, b) => a.stage - b.stage);
}

// ---------------------------------------------------------------------------
// BNPL vs internal split (report 5, §7.5 #11)
// ---------------------------------------------------------------------------

export interface KindSplit {
  kind: string;
  planCount: number;
  principalIrr: number;
  receivedIrr: number;
  outstandingIrr: number;
}

export function splitPlansByKind(plans: InstallmentPlanResponse[]): KindSplit[] {
  const totals = new Map<string, KindSplit>();
  for (const plan of plans) {
    const total = totals.get(plan.kind) ?? {
      kind: plan.kind,
      planCount: 0,
      principalIrr: 0,
      receivedIrr: 0,
      outstandingIrr: 0,
    };
    total.planCount += 1;
    total.principalIrr += plan.total_principal_irr;
    total.receivedIrr += plan.total_received_irr;
    total.outstandingIrr += plan.outstanding_irr;
    totals.set(plan.kind, total);
  }
  // Internal first, BNPL second — the §7.5 order.
  return [...totals.values()].sort((a, b) => {
    if (a.kind === "internal") return -1;
    if (b.kind === "internal") return 1;
    return a.kind.localeCompare(b.kind);
  });
}

// ---------------------------------------------------------------------------
// Per-customer statement (report 6)
// ---------------------------------------------------------------------------

export interface CustomerStatement {
  customerRef: string;
  planCount: number;
  principalIrr: number;
  receivedIrr: number;
  outstandingIrr: number;
  overduePlanCount: number;
}

export function groupPlansByCustomer(plans: InstallmentPlanResponse[]): CustomerStatement[] {
  const totals = new Map<string, CustomerStatement>();
  for (const plan of plans) {
    const ref = plan.customer_ref ?? "بدون مرجع مشتری";
    const total = totals.get(ref) ?? {
      customerRef: ref,
      planCount: 0,
      principalIrr: 0,
      receivedIrr: 0,
      outstandingIrr: 0,
      overduePlanCount: 0,
    };
    total.planCount += 1;
    total.principalIrr += plan.total_principal_irr;
    total.receivedIrr += plan.total_received_irr;
    total.outstandingIrr += plan.outstanding_irr;
    if (plan.status === "overdue") total.overduePlanCount += 1;
    totals.set(ref, total);
  }
  return [...totals.values()].sort((a, b) => b.outstandingIrr - a.outstandingIrr);
}

// ---------------------------------------------------------------------------
// Early settlements (report 8, §7.5 #6)
// ---------------------------------------------------------------------------

export interface EarlySettlementRow {
  planId: string;
  invoiceId: string;
  customerRef: string | null;
  receipt: InstallmentReceiptResponse;
}

/** Every early-settlement receipt across the given plan details. */
export function extractEarlySettlements(
  details: InstallmentPlanDetailResponse[],
): EarlySettlementRow[] {
  const rows: EarlySettlementRow[] = [];
  for (const detail of details) {
    for (const receipt of detail.receipts) {
      if (!receipt.is_early_settlement) continue;
      rows.push({
        planId: detail.plan.id,
        invoiceId: detail.plan.invoice_id,
        customerRef: detail.plan.customer_ref,
        receipt,
      });
    }
  }
  return rows.sort((a, b) => (a.receipt.received_date < b.receipt.received_date ? 1 : -1));
}

// ---------------------------------------------------------------------------
// Cheque-paid legs (report 9, §7.5 #4)
// ---------------------------------------------------------------------------

export interface ChequeLegRow {
  planId: string;
  invoiceId: string;
  customerRef: string | null;
  leg: InstallmentLegResponse;
}

/** Legs carrying a linked cheque (any cheque state). */
export function extractChequeLegs(details: InstallmentPlanDetailResponse[]): ChequeLegRow[] {
  const rows: ChequeLegRow[] = [];
  for (const detail of details) {
    for (const leg of detail.legs) {
      if (!leg.linked_cheque) continue;
      rows.push({
        planId: detail.plan.id,
        invoiceId: detail.plan.invoice_id,
        customerRef: detail.plan.customer_ref,
        leg,
      });
    }
  }
  return rows;
}

/** Legs whose linked cheque bounced — the leg is unpaid again (the fee clock runs from the original due date). */
export function extractBouncedChequeLegs(
  details: InstallmentPlanDetailResponse[],
): ChequeLegRow[] {
  return extractChequeLegs(details).filter(
    (row) => row.leg.linked_cheque?.state === "bounced",
  );
}

// ---------------------------------------------------------------------------
// Finance income (report 3, §7.5 #8): aggregate live payoff quotes.
// ---------------------------------------------------------------------------

export interface FinanceIncomeTotals {
  planCount: number;
  realisedIrr: number;
  realisedUnpaidIrr: number;
  unrealisedIrr: number;
  incomeDueIrr: number;
}

/**
 * Aggregate payoff quotes across internal plans. A BNPL plan never
 * accrues finance income — the backend refuses its payoff quote,
 * so callers must only pass internal plans' quotes.
 */
export function aggregateFinanceIncome(quotes: PayoffQuoteResponse[]): FinanceIncomeTotals {
  return quotes.reduce<FinanceIncomeTotals>(
    (totals, quote) => ({
      planCount: totals.planCount + 1,
      realisedIrr: totals.realisedIrr + quote.income_realised_irr,
      realisedUnpaidIrr: totals.realisedUnpaidIrr + quote.income_realised_unpaid_irr,
      unrealisedIrr: totals.unrealisedIrr + quote.income_unrealised_irr,
      incomeDueIrr: totals.incomeDueIrr + quote.income_due_irr,
    }),
    { planCount: 0, realisedIrr: 0, realisedUnpaidIrr: 0, unrealisedIrr: 0, incomeDueIrr: 0 },
  );
}

/** Per-leg finance income (leg principal × contract rate, HALF_UP per leg — the engine's rule). */
export function legFinanceIncomeIrr(principalIrr: number, rate: number): number {
  return Math.round(principalIrr * rate);
}

// ---------------------------------------------------------------------------
// SMS reminder log (report 10, §7.5 #9)
// ---------------------------------------------------------------------------

export interface ReminderMonthTotal {
  monthKey: string;
  sent: number;
  failed: number;
}

/** ISO Gregorian "YYYY-MM-DD" -> Jalali month key "YYYY-MM". */
export function isoMonthToJalaliMonthKey(iso: string): string {
  const { jy, jm } = isoToJalali(iso);
  return `${jy}-${String(jm).padStart(2, "0")}`;
}

/** Monthly sends/failures from log rows (created_at is an ISO timestamp). */
export function groupRemindersByMonth(rows: ReminderLogResponse[]): ReminderMonthTotal[] {
  const totals = new Map<string, ReminderMonthTotal>();
  for (const row of rows) {
    if (!row.created_at) continue;
    const iso = row.created_at.slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) continue;
    const monthKey = isoMonthToJalaliMonthKey(iso);
    const total = totals.get(monthKey) ?? { monthKey, sent: 0, failed: 0 };
    if (row.status === "sent" || row.status === "delivered") total.sent += 1;
    if (row.status === "failed") total.failed += 1;
    totals.set(monthKey, total);
  }
  return [...totals.values()].sort((a, b) => (a.monthKey < b.monthKey ? -1 : 1));
}

export interface ReminderSuccessRate {
  sent: number;
  delivered: number;
  failed: number;
  /** Delivered + sent over sent + delivered + failed (dry runs and skips excluded). */
  rate: number | null;
  /** Known SMS cost in Toman (rows with a null cost are never guessed). */
  knownCostToman: number;
  unknownCostCount: number;
}

export function reminderSuccessRate(rows: ReminderLogResponse[]): ReminderSuccessRate {
  let sent = 0;
  let delivered = 0;
  let failed = 0;
  let knownCostToman = 0;
  let unknownCostCount = 0;
  for (const row of rows) {
    if (row.status === "sent") sent += 1;
    if (row.status === "delivered") delivered += 1;
    if (row.status === "failed") failed += 1;
    if (row.status === "sent" || row.status === "delivered") {
      if (row.cost_toman === null || row.cost_toman === undefined) {
        unknownCostCount += 1;
      } else {
        knownCostToman += row.cost_toman;
      }
    }
  }
  const attempts = sent + delivered + failed;
  return {
    sent,
    delivered,
    failed,
    rate: attempts === 0 ? null : (sent + delivered) / attempts,
    knownCostToman,
    unknownCostCount,
  };
}

// ---------------------------------------------------------------------------
// Totals + client-side filters
// ---------------------------------------------------------------------------

export function sumPlans(plans: InstallmentPlanResponse[]): {
  principalIrr: number;
  receivedIrr: number;
  outstandingIrr: number;
  pendingClearanceIrr: number;
} {
  return plans.reduce(
    (totals, plan) => ({
      principalIrr: totals.principalIrr + plan.total_principal_irr,
      receivedIrr: totals.receivedIrr + plan.total_received_irr,
      outstandingIrr: totals.outstandingIrr + plan.outstanding_irr,
      pendingClearanceIrr: totals.pendingClearanceIrr + plan.pending_clearance_irr,
    }),
    { principalIrr: 0, receivedIrr: 0, outstandingIrr: 0, pendingClearanceIrr: 0 },
  );
}

export interface PlanClientFilters {
  /** Substring of the customer reference (the list endpoint has no customer filter). */
  customer?: string;
}

/** Client-side filters over one fetched page (customer is not a server filter). */
export function applyPlanFilters(
  plans: InstallmentPlanResponse[],
  filters: PlanClientFilters,
): InstallmentPlanResponse[] {
  return plans.filter((plan) => {
    if (filters.customer && !(plan.customer_ref ?? "").includes(filters.customer)) return false;
    return true;
  });
}

/** Days from today until a leg's due date (negative = overdue). */
export function daysToLegDue(leg: InstallmentLegResponse, todayJalali: string): number {
  return daysToDue(leg.due_date, todayJalali);
}

/** Whole days between two Jalali dates (re-exported for the pages). */
export { jalaliDaysBetween };

// ---------------------------------------------------------------------------
// The 11 installment reports (§7.5) — catalog for the reports page.
// `backend` names the real endpoint ("endpoint") or a client-side
// aggregation over live data ("derived"). `section75` is the §7.5
// catalog number where the report has one; reports without a §7.5
// row say so in `note` instead of inventing a mapping. Every report
// is live — none of the 11 needs a coming-soon, because every source
// exists; the per-figure gaps (the monthly income trend, waived
// income per settlement, unknown SMS costs) are stated on the report
// itself, never filled with a guess.
// ---------------------------------------------------------------------------

export interface InstallmentReportMeta {
  id: string;
  number: number;
  title: string;
  /** §7.5 catalog number, or null when §7.5 has no row for this report. */
  section75: number | null;
  /** §7.5 page. */
  page: string;
  chart: string;
  source: string;
  backend: "endpoint" | "derived";
  filters: string;
  /** Honest caveat shown on the report (a sub-figure that is not available). */
  note: string | null;
}

export const INSTALLMENT_REPORTS: InstallmentReportMeta[] = [
  {
    id: "overdue",
    number: 1,
    title: "اقساط معوق",
    section75: 1,
    page: "گزارش‌ها ← اقساط ← معوق",
    chart: "میله‌ای aging معوق (۱–۳۰ / ۳۱–۶۰ / ۶۱–۹۰ / +۹۰ روز)",
    source: "هر دو",
    backend: "endpoint",
    filters: "مشتری، قرارداد، قدمت معوق، مبلغ",
    note: null,
  },
  {
    id: "forecast",
    number: 2,
    title: "پیش‌بینی وصول",
    section75: null,
    page: "گزارش‌ها ← اقساط ← پیش‌بینی وصول",
    chart: "میله‌ای ماهانهٔ وصول موردانتظار",
    source: "حساب‌یار (برنامه‌های قسط)",
    backend: "derived",
    filters: "بازهٔ سررسید",
    note: "در §۷٫۵ ردیف مستقلی ندارد؛ از سررسید اقساط باز داخلی محاسبه می‌شود. اقساط BNPL در آن نیست (طلب از درگاه است و پیش‌بینی تسویه‌اش در مرکز تسویه است) و اقساط در انتظار وصول چک هم نیست (پولشان در مسیر وصول است، نه در صف پیگیری).",
  },
  {
    id: "finance-income",
    number: 3,
    title: "سود تأمین مالی تحقق‌یافته",
    section75: 8,
    page: "حسابداری ← سود تأمین مالی + گزارش‌ها ← اقساط ← سود",
    chart: "میله‌ای انباشتهٔ تحقق‌یافته در برابر تحقق‌نیافته؛ خط روند ماهانه",
    source: "حساب‌یار (نقل‌قول تسویه)؛ سندهای ثبت‌شده: ترایتون (مرجع)",
    backend: "derived",
    filters: "قرارداد، دوره، وضعیت تحقق",
    note: "ارقام تحقق‌یافته/تحقق‌نیافته از نقل‌قول تسویهٔ زندهٔ هر برنامهٔ داخلی می‌آید (سود فقط هنگام وصول تحقق می‌یابد، نه با دریافت چک). خط روند ماهانه در دسترس نیست — تفکیک ماهانهٔ تحقق سود هنوز نقطهٔ پایانی ندارد و از دادهٔ جزئی بازسازی نمی‌شود.",
  },
  {
    id: "dunning",
    number: 4,
    title: "مراحل پیگیری (دونینگ)",
    section75: null,
    page: "گزارش‌ها ← اقساط ← پیگیری",
    chart: "میله‌ای به تفکیک مرحلهٔ پیگیری",
    source: "حساب‌یار (گزارش معوق)",
    backend: "derived",
    filters: "مرحله، قرارداد",
    note: "در §۷٫۵ ردیف مستقلی ندارد؛ از مرحلهٔ پیگیری هر قسط معوق در گزارش ۱ گروه‌بندی می‌شود. برنامهٔ BNPL هرگز وارد پیگیری نمی‌شود — وصول با درگاه است.",
  },
  {
    id: "bnpl-split",
    number: 5,
    title: "BNPL در برابر داخلی",
    section75: 11,
    page: "گزارش‌ها ← اقساط ← BNPL در برابر داخلی",
    chart: "میله‌ای مقایسه‌ای BNPL در برابر داخلی",
    source: "هر دو",
    backend: "derived",
    filters: "درگاه، بازه",
    note: "دو دنیا جدا نشان داده می‌شوند و هرگز با هم جمع نمی‌شوند؛ قسط مشتری←درگاه در دفتر حساب‌یار نیست و فقط طلب از درگاه است.",
  },
  {
    id: "customer-statement",
    number: 6,
    title: "صورت وضعیت مشتری",
    section75: null,
    page: "گزارش‌ها ← اقساط ← صورت وضعیت مشتری",
    chart: "جدول صورت وضعیت مشتری",
    source: "حساب‌یار (فهرست برنامه‌ها)",
    backend: "derived",
    filters: "مشتری",
    note: "در §۷٫۵ ردیف مستقلی ندارد؛ از گروه‌بندی برنامه‌ها بر اساس مرجع مشتری به دست می‌آید.",
  },
  {
    id: "plan-schedule",
    number: 7,
    title: "برنامهٔ اقساط هر قرارداد",
    section75: 2,
    page: "فروش و خرید ← اقساط ← جزئیات برنامه",
    chart: "گانت/خط زمانی سررسیدها؛ میله‌ای اصل در برابر سود",
    source: "هر دو",
    backend: "endpoint",
    filters: "قرارداد، مشتری، وضعیت قسط",
    note: "سود هر قسط از نرخ قرارداد همان برنامه محاسبه می‌شود (اصل قسط × نرخ)؛ برنامهٔ بدون قرارداد سود ندارد و فقط اصل نشان داده می‌شود.",
  },
  {
    id: "early-settlements",
    number: 8,
    title: "تسویه‌های زودهنگام",
    section75: 6,
    page: "گزارش‌ها ← اقساط ← تسویهٔ زودهنگام",
    chart: "جدول + مبلغ سود تعدیل‌شده",
    source: "هر دو",
    backend: "derived",
    filters: "قرارداد، بازه",
    note: "سود بخشیده‌شدهٔ هر تسویه ثبت نمی‌شود — نقل‌قول لحظهٔ تسویه ذخیره نمی‌شود؛ جدول سودِ واقعاً دریافت‌شده را نشان می‌دهد، نه سودِ صرفه‌جویی‌شده را.",
  },
  {
    id: "bounced-cheques",
    number: 9,
    title: "اقساط با چک برگشتی",
    section75: 4,
    page: "گزارش‌ها ← اقساط ← پرداخت با چک",
    chart: "جدول قسط‌های چک‌دار + وضعیت وصول",
    source: "هر دو",
    backend: "derived",
    filters: "قرارداد، وضعیت چک، وضعیت قسط",
    note: "قسطی که چکش برگشت بخورد دوباره معوق می‌شود و ساعت جریمه از سررسید اصلی حساب می‌شود؛ اقساط در انتظار وصول چک در جدول جداگانهٔ همین گزارش‌اند.",
  },
  {
    id: "sms-log",
    number: 10,
    title: "لاگ یادآوری پیامکی",
    section75: 9,
    page: "گزارش‌ها ← اقساط ← یادآوری‌ها",
    chart: "میله‌ای ارسال ماهانه + نرخ موفقیت",
    source: "حساب‌یار",
    backend: "endpoint",
    filters: "قرارداد، بازه، وضعیت ارسال",
    note: "هزینهٔ پیامک فقط برای ردیف‌هایی جمع می‌شود که ارائه‌دهنده واحد هزینه را گزارش کرده باشد؛ هزینه‌های نامشخص هرگز حدس زده نمی‌شوند.",
  },
  {
    id: "aging",
    number: 11,
    title: "قدمت اقساط (aging)",
    section75: 1,
    page: "گزارش‌ها ← اقساط ← قدمت",
    chart: "میله‌ای aging (جاری / ۱–۳۰ / ۳۱–۶۰ / ۶۱–۹۰ / +۹۰ روز)",
    source: "حساب‌یار (برنامه‌های قسط)",
    backend: "derived",
    filters: "قدمت، مشتری",
    note: "همان سطل‌های گزارش ۱، روی همهٔ اقساط مانده‌دار (نه فقط معوق‌ها)؛ اقساط BNPL در آن نیست — طلب از درگاه در مرکز تسویه قدمت‌بندی می‌شود.",
  },
];
