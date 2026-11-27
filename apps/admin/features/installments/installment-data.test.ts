/**
 * Pure-helper tests for the installments center (US-545).
 *
 * Jalali fixtures are fixed strings, never "today": the bucket,
 * forecast and aging tests pass an explicit `todayJalali` so they
 * do not depend on the calendar date the suite runs on.
 */

import { describe, expect, it } from "vitest";
import type {
  InstallmentLegResponse,
  InstallmentPlanDetailResponse,
  InstallmentPlanResponse,
  InstallmentReceiptResponse,
  OverdueLegResponse,
  PayoffQuoteResponse,
  ReminderLogResponse,
} from "@/lib/api-client";
import {
  ageOutstandingLegs,
  aggregateFinanceIncome,
  applyPlanFilters,
  bucketOverdueRows,
  collectionForecast,
  dunningStageLabel,
  extractBouncedChequeLegs,
  extractChequeLegs,
  extractEarlySettlements,
  groupOverdueByStage,
  groupPlansByCustomer,
  groupRemindersByMonth,
  isoMonthToJalaliMonthKey,
  jalaliMonthLabel,
  legFinanceIncomeIrr,
  legStatusLabel,
  overdueAgeBucket,
  planKindLabel,
  planStatusLabel,
  reminderSuccessRate,
  splitPlansByKind,
} from "./installment-data";

/** Fixed "today" for every date test: 1405-08-29 (2026-11-20). */
const TODAY = "1405-08-29";

function plan(overrides: Partial<InstallmentPlanResponse>): InstallmentPlanResponse {
  return {
    id: "plan-1",
    invoice_id: "INV-1",
    contract_id: null,
    customer_ref: "CUST-1",
    company_engine_id: 1,
    total_principal_irr: 30_000_000,
    total_principal_toman: 3_000_000,
    leg_count: 3,
    interval_days: 30,
    first_due_date: "1405-07-29",
    auto_post: false,
    applied_advance_irr: 0,
    applied_advance_toman: 0,
    source: "manual",
    kind: "internal",
    gateway_channel_key: null,
    status: "active",
    total_received_irr: 0,
    total_received_toman: 0,
    outstanding_irr: 30_000_000,
    outstanding_toman: 3_000_000,
    pending_clearance_irr: 0,
    pending_clearance_toman: 0,
    created_at: "2026-11-20T00:00:00Z",
    updated_at: "2026-11-20T00:00:00Z",
    ...overrides,
  };
}

function leg(overrides: Partial<InstallmentLegResponse>): InstallmentLegResponse {
  return {
    id: "leg-1",
    plan_id: "plan-1",
    sequence: 1,
    due_date: "1405-07-29",
    principal_irr: 10_000_000,
    principal_toman: 1_000_000,
    received_irr: 0,
    received_toman: 0,
    balance_irr: 10_000_000,
    balance_toman: 1_000_000,
    status: "overdue",
    pending_clearance_irr: 0,
    pending_clearance_toman: 0,
    linked_cheque: null,
    ...overrides,
  };
}

function receipt(overrides: Partial<InstallmentReceiptResponse>): InstallmentReceiptResponse {
  return {
    id: "rcpt-1",
    plan_id: "plan-1",
    invoice_id: "INV-1",
    journal_move_id: 4242,
    amount_irr: 10_000_000,
    amount_toman: 1_000_000,
    received_date: "1405-08-29",
    payment_method: "cash",
    cheque_id: null,
    collected_at: "1405-08-29",
    allocations: [{ leg_id: "leg-1", amount_irr: 10_000_000, amount_toman: 1_000_000 }],
    allocated_irr: 10_000_000,
    advance_irr: 0,
    advance_toman: 0,
    finance_income_irr: 0,
    finance_income_toman: 0,
    late_fee_irr: 0,
    late_fee_toman: 0,
    applied_advance_irr: 0,
    applied_advance_toman: 0,
    is_early_settlement: false,
    reference: null,
    notes: null,
    gateway: null,
    gateway_channel_key: null,
    gateway_reference: null,
    gateway_payment_id: null,
    bnpl_status: null,
    created_at: "2026-11-20T00:00:00Z",
    ...overrides,
  };
}

function detail(
  planOverrides: Partial<InstallmentPlanResponse>,
  legs: InstallmentLegResponse[],
  receipts: InstallmentReceiptResponse[] = [],
): InstallmentPlanDetailResponse {
  const p = plan(planOverrides);
  return {
    plan: p,
    legs,
    receipts,
    status: p.status,
    total_received_irr: p.total_received_irr,
    total_received_toman: p.total_received_toman,
    total_advance_irr: 0,
    total_advance_toman: 0,
    outstanding_irr: p.outstanding_irr,
    outstanding_toman: p.outstanding_toman,
    total_pending_clearance_irr: p.pending_clearance_irr,
    total_pending_clearance_toman: p.pending_clearance_toman,
    gateway_receivable_irr: 0,
    gateway_receivable_toman: 0,
    gateway_settled_irr: 0,
    gateway_settled_toman: 0,
  };
}

function overdueRow(overrides: Partial<OverdueLegResponse>): OverdueLegResponse {
  return {
    plan_id: "plan-1",
    invoice_id: "INV-1",
    customer_ref: "CUST-1",
    contract_code: "STD",
    leg_id: "leg-1",
    sequence: 1,
    due_date: "1405-07-29",
    days_overdue: 30,
    principal_irr: 10_000_000,
    principal_toman: 1_000_000,
    received_irr: 0,
    received_toman: 0,
    balance_irr: 10_000_000,
    balance_toman: 1_000_000,
    dunning_stage: 2,
    next_reminder_date: "1405-09-06",
    late_fee_accrued_irr: 300_000,
    late_fee_accrued_toman: 30_000,
    ...overrides,
  };
}

function quote(overrides: Partial<PayoffQuoteResponse>): PayoffQuoteResponse {
  return {
    plan_id: "plan-1",
    as_of: "1405-08-29",
    contract_id: "contract-1",
    remaining_principal_irr: 20_000_000,
    remaining_principal_toman: 2_000_000,
    income_realised_irr: 1_000_000,
    income_realised_toman: 100_000,
    income_realised_unpaid_irr: 1_000_000,
    income_realised_unpaid_toman: 100_000,
    income_unrealised_irr: 2_000_000,
    income_unrealised_toman: 200_000,
    income_due_irr: 3_000_000,
    income_due_toman: 300_000,
    income_policy: "full",
    late_fees_accrued_irr: 300_000,
    late_fees_accrued_toman: 30_000,
    late_fees_due_irr: 300_000,
    late_fees_due_toman: 30_000,
    fee_policy: "kept",
    advance_held_irr: 0,
    advance_held_toman: 0,
    payoff_total_irr: 23_300_000,
    payoff_total_toman: 2_330_000,
    open_leg_count: 2,
    ...overrides,
  };
}

function reminder(overrides: Partial<ReminderLogResponse>): ReminderLogResponse {
  return {
    id: "rem-1",
    tenant_id: "tenant-1",
    installation_id: "inst-1",
    plan_id: "plan-1",
    leg_id: "leg-1",
    provider_id: null,
    provider_key: "simulator",
    recipient_phone: "09123456789",
    message_text: "یادآوری سررسید",
    offset_days: -3,
    trigger: "scheduled",
    status: "sent",
    provider_message_id: "msg-1",
    provider_status_raw: null,
    cost_toman: 100,
    provider_cost_raw: null,
    provider_cost_unit: "IRR",
    error_code: null,
    error_detail: null,
    sent_at: "2026-11-20T06:00:00Z",
    created_at: "2026-11-20T06:00:00Z",
    ...overrides,
  };
}

describe("labels", () => {
  it("labels plan kinds, statuses and leg states in Persian", () => {
    expect(planKindLabel("internal")).toBe("داخلی");
    expect(planKindLabel("bnpl")).toBe("BNPL (درگاه)");
    expect(planStatusLabel("overdue")).toBe("معوق");
    expect(planStatusLabel("paid")).toBe("تسویه‌شده");
    expect(legStatusLabel("pending_clearance")).toBe("در انتظار وصول چک");
    expect(legStatusLabel("gateway_confirmed")).toBe("تأیید درگاه");
    expect(legStatusLabel("settled_per_tranche")).toBe("تسویهٔ جزئی درگاه");
  });

  it("falls back to the raw code for unknown values", () => {
    expect(planKindLabel("mystery")).toBe("mystery");
    expect(legStatusLabel("mystery")).toBe("mystery");
    expect(planStatusLabel(null)).toBe("نامشخص");
  });

  it("labels dunning stage 0 as pre-first-reminder", () => {
    expect(dunningStageLabel(0)).toContain("پیش از نخستین یادآوری");
    expect(dunningStageLabel(2)).toContain("۲");
  });
});

describe("overdue aging buckets (§7.5 #1)", () => {
  it("buckets days overdue at 30 / 60 / 90", () => {
    expect(overdueAgeBucket(1)).toBe("1-30");
    expect(overdueAgeBucket(30)).toBe("1-30");
    expect(overdueAgeBucket(31)).toBe("31-60");
    expect(overdueAgeBucket(60)).toBe("31-60");
    expect(overdueAgeBucket(61)).toBe("61-90");
    expect(overdueAgeBucket(90)).toBe("61-90");
    expect(overdueAgeBucket(91)).toBe("90+");
  });

  it("groups overdue rows with counts, balances and fees", () => {
    const rows = [
      overdueRow({ days_overdue: 10, balance_irr: 1_000_000, late_fee_accrued_irr: 10_000 }),
      overdueRow({ leg_id: "leg-2", days_overdue: 45, balance_irr: 2_000_000, late_fee_accrued_irr: 20_000 }),
      overdueRow({ leg_id: "leg-3", days_overdue: 200, balance_irr: 3_000_000, late_fee_accrued_irr: 30_000 }),
    ];
    const totals = bucketOverdueRows(rows);
    expect(totals).toHaveLength(4);
    expect(totals[0]).toEqual({ bucket: "1-30", count: 1, balanceIrr: 1_000_000, lateFeesIrr: 10_000 });
    expect(totals[1]).toEqual({ bucket: "31-60", count: 1, balanceIrr: 2_000_000, lateFeesIrr: 20_000 });
    expect(totals[2]).toEqual({ bucket: "61-90", count: 0, balanceIrr: 0, lateFeesIrr: 0 });
    expect(totals[3]).toEqual({ bucket: "90+", count: 1, balanceIrr: 3_000_000, lateFeesIrr: 30_000 });
  });
});

describe("installment aging (report 11)", () => {
  it("ages outstanding legs into current + overdue buckets and skips BNPL", () => {
    const details = [
      detail(
        {},
        [
          leg({ id: "l1", due_date: "1405-07-28", status: "overdue", balance_irr: 10_000_000 }), // 31 days overdue (1405-07-29 is only 30 — Mehr has 30 days)
          leg({ id: "l2", sequence: 2, due_date: "1405-09-29", status: "expected", balance_irr: 10_000_000 }),
          leg({ id: "l3", sequence: 3, due_date: "1405-07-01", status: "paid", balance_irr: 0 }), // paid: skipped
        ],
      ),
      detail(
        { id: "plan-2", kind: "bnpl", gateway_channel_key: "snapppay" },
        [leg({ id: "l4", plan_id: "plan-2", due_date: "1405-07-29", status: "gateway_confirmed", balance_irr: 99_000_000 })],
      ),
    ];
    const totals = ageOutstandingLegs(details, TODAY);
    const byBucket = new Map(totals.map((t) => [t.bucket, t]));
    expect(byBucket.get("current")).toEqual({ bucket: "current", count: 1, balanceIrr: 10_000_000 });
    expect(byBucket.get("31-60")).toEqual({ bucket: "31-60", count: 1, balanceIrr: 10_000_000 });
    expect(byBucket.get("1-30")).toEqual({ bucket: "1-30", count: 0, balanceIrr: 0 });
    // The BNPL leg never enters the aging (its 99,000,000 is nowhere).
    expect(totals.reduce((sum, t) => sum + t.balanceIrr, 0)).toBe(20_000_000);
  });
});

describe("collection forecast (report 2)", () => {
  it("puts overdue legs first, then months, and skips pending-clearance + BNPL", () => {
    const details = [
      detail(
        {},
        [
          leg({ id: "l1", due_date: "1405-07-29", status: "overdue", balance_irr: 10_000_000 }),
          leg({ id: "l2", sequence: 2, due_date: "1405-08-30", status: "due", balance_irr: 5_000_000 }),
          leg({ id: "l3", sequence: 3, due_date: "1405-09-15", status: "expected", balance_irr: 7_000_000 }),
          leg({
            id: "l4",
            sequence: 4,
            due_date: "1405-08-28",
            status: "pending_clearance",
            balance_irr: 8_000_000,
            pending_clearance_irr: 8_000_000,
          }),
        ],
      ),
      detail(
        { id: "plan-2", kind: "bnpl", gateway_channel_key: "snapppay" },
        [leg({ id: "l5", plan_id: "plan-2", due_date: "1405-08-30", status: "gateway_confirmed", balance_irr: 50_000_000 })],
      ),
    ];
    const buckets = collectionForecast(details, TODAY, 2);
    const byKey = new Map(buckets.map((b) => [b.key, b]));
    expect(byKey.get("overdue")).toEqual({
      key: "overdue",
      label: "معوق — قابل وصول فوری",
      amountIrr: 10_000_000,
      count: 1,
    });
    expect(byKey.get("1405-08")).toEqual({
      key: "1405-08",
      label: "آبان ۱۴۰۵",
      amountIrr: 5_000_000,
      count: 1,
    });
    expect(byKey.get("1405-09")).toEqual({
      key: "1405-09",
      label: "آذر ۱۴۰۵",
      amountIrr: 7_000_000,
      count: 1,
    });
    // Pending-clearance (8,000,000) and BNPL (50,000,000) are nowhere.
    expect(buckets.reduce((sum, b) => sum + b.amountIrr, 0)).toBe(22_000_000);
  });

  it("labels Jalali months in Persian", () => {
    expect(jalaliMonthLabel("1405-08")).toBe("آبان ۱۴۰۵");
    expect(jalaliMonthLabel("bogus")).toBe("bogus");
  });
});

describe("dunning grouping (report 4)", () => {
  it("groups overdue rows by stage, sorted ascending", () => {
    const rows = [
      overdueRow({ dunning_stage: 2, balance_irr: 1_000_000, late_fee_accrued_irr: 10_000 }),
      overdueRow({ leg_id: "leg-2", dunning_stage: 0, balance_irr: 2_000_000, late_fee_accrued_irr: 0 }),
      overdueRow({ leg_id: "leg-3", dunning_stage: 2, balance_irr: 3_000_000, late_fee_accrued_irr: 30_000 }),
    ];
    expect(groupOverdueByStage(rows)).toEqual([
      { stage: 0, count: 1, balanceIrr: 2_000_000, lateFeesIrr: 0 },
      { stage: 2, count: 2, balanceIrr: 4_000_000, lateFeesIrr: 40_000 },
    ]);
  });
});

describe("BNPL vs internal split (report 5)", () => {
  it("splits plans by kind, internal first, never summed", () => {
    const plans = [
      plan({ id: "p1", total_principal_irr: 10_000_000, total_received_irr: 4_000_000, outstanding_irr: 6_000_000 }),
      plan({ id: "p2", kind: "bnpl", gateway_channel_key: "snapppay", total_principal_irr: 20_000_000, total_received_irr: 20_000_000, outstanding_irr: 0 }),
      plan({ id: "p3", total_principal_irr: 5_000_000, total_received_irr: 0, outstanding_irr: 5_000_000 }),
    ];
    expect(splitPlansByKind(plans)).toEqual([
      { kind: "internal", planCount: 2, principalIrr: 15_000_000, receivedIrr: 4_000_000, outstandingIrr: 11_000_000 },
      { kind: "bnpl", planCount: 1, principalIrr: 20_000_000, receivedIrr: 20_000_000, outstandingIrr: 0 },
    ]);
  });
});

describe("per-customer statement (report 6)", () => {
  it("groups by customer ref, largest outstanding first", () => {
    const plans = [
      plan({ id: "p1", customer_ref: "CUST-A", outstanding_irr: 1_000_000, status: "overdue" }),
      plan({ id: "p2", customer_ref: "CUST-B", outstanding_irr: 9_000_000, status: "active" }),
      plan({ id: "p3", customer_ref: "CUST-A", outstanding_irr: 2_000_000, status: "active" }),
      plan({ id: "p4", customer_ref: null, outstanding_irr: 5_000_000, status: "active" }),
    ];
    const statements = groupPlansByCustomer(plans);
    expect(statements.map((s) => s.customerRef)).toEqual(["CUST-B", "بدون مرجع مشتری", "CUST-A"]);
    expect(statements[2]).toEqual({
      customerRef: "CUST-A",
      planCount: 2,
      principalIrr: 60_000_000,
      receivedIrr: 0,
      outstandingIrr: 3_000_000,
      overduePlanCount: 1,
    });
  });
});

describe("early settlements (report 8)", () => {
  it("extracts only settlement receipts, newest first", () => {
    const details = [
      detail(
        {},
        [leg({})],
        [
          receipt({ id: "r1", is_early_settlement: false }),
          receipt({ id: "r2", is_early_settlement: true, received_date: "1405-08-20", finance_income_irr: 1_000_000 }),
          receipt({ id: "r3", is_early_settlement: true, received_date: "1405-08-29", finance_income_irr: 2_000_000 }),
        ],
      ),
    ];
    const rows = extractEarlySettlements(details);
    expect(rows.map((r) => r.receipt.id)).toEqual(["r3", "r2"]);
    expect(rows[0].invoiceId).toBe("INV-1");
  });
});

describe("cheque legs (report 9)", () => {
  const bounced = leg({
    id: "l1",
    linked_cheque: {
      cheque_id: "chk-1",
      sayad_id: "1234567890123456",
      due_date: "1405-08-01",
      state: "bounced",
      amount_irr: 10_000_000,
      amount_toman: 1_000_000,
    },
  });
  const pending = leg({
    id: "l2",
    sequence: 2,
    status: "pending_clearance",
    linked_cheque: {
      cheque_id: "chk-2",
      sayad_id: "1234567890123457",
      due_date: "1405-09-01",
      state: "held",
      amount_irr: 10_000_000,
      amount_toman: 1_000_000,
    },
  });

  it("lists every cheque-linked leg, and bounced ones separately", () => {
    const details = [detail({}, [bounced, pending, leg({ id: "l3", sequence: 3 })])];
    expect(extractChequeLegs(details).map((r) => r.leg.id)).toEqual(["l1", "l2"]);
    expect(extractBouncedChequeLegs(details).map((r) => r.leg.id)).toEqual(["l1"]);
  });
});

describe("finance income (report 3)", () => {
  it("aggregates realised/unrealised across quotes", () => {
    const totals = aggregateFinanceIncome([
      quote({}),
      quote({ plan_id: "plan-2", income_realised_irr: 500_000, income_realised_unpaid_irr: 0, income_unrealised_irr: 1_500_000, income_due_irr: 1_500_000 }),
    ]);
    expect(totals).toEqual({
      planCount: 2,
      realisedIrr: 1_500_000,
      realisedUnpaidIrr: 1_000_000,
      unrealisedIrr: 3_500_000,
      incomeDueIrr: 4_500_000,
    });
  });

  it("computes per-leg income as principal x rate", () => {
    expect(legFinanceIncomeIrr(10_000_000, 0.1)).toBe(1_000_000);
    expect(legFinanceIncomeIrr(10_000_000, 0)).toBe(0);
  });
});

describe("SMS reminder log (report 10)", () => {
  it("converts ISO timestamps to Jalali month keys", () => {
    // 2026-11-20 == 1405-08-29; 2026-10-23 == 1405-08-01.
    expect(isoMonthToJalaliMonthKey("2026-11-20")).toBe("1405-08");
    expect(isoMonthToJalaliMonthKey("2026-10-23")).toBe("1405-08");
    expect(isoMonthToJalaliMonthKey("2026-10-22")).toBe("1405-07");
  });

  it("groups sends/failures by Jalali month", () => {
    const rows = [
      reminder({ created_at: "2026-11-20T06:00:00Z", status: "sent" }),
      reminder({ id: "rem-2", created_at: "2026-11-21T06:00:00Z", status: "delivered" }),
      // Aban ends 1405-08-30 == 2026-11-21; 2026-11-22 is already Azar (1405-09).
      reminder({ id: "rem-3", created_at: "2026-11-21T08:00:00Z", status: "failed" }),
      reminder({ id: "rem-4", created_at: "2026-10-22T06:00:00Z", status: "sent" }),
      reminder({ id: "rem-5", created_at: null, status: "sent" }),
    ];
    expect(groupRemindersByMonth(rows)).toEqual([
      { monthKey: "1405-07", sent: 1, failed: 0 },
      { monthKey: "1405-08", sent: 2, failed: 1 },
    ]);
  });

  it("computes the success rate and sums only known costs", () => {
    const rows = [
      reminder({ status: "sent", cost_toman: 100 }),
      reminder({ id: "r2", status: "delivered", cost_toman: null }),
      reminder({ id: "r3", status: "failed", cost_toman: null }),
      reminder({ id: "r4", status: "dry_run", cost_toman: null }),
      reminder({ id: "r5", status: "skipped", cost_toman: null }),
    ];
    const rate = reminderSuccessRate(rows);
    expect(rate.sent).toBe(1);
    expect(rate.delivered).toBe(1);
    expect(rate.failed).toBe(1);
    expect(rate.rate).toBeCloseTo(2 / 3);
    expect(rate.knownCostToman).toBe(100);
    expect(rate.unknownCostCount).toBe(1);
  });

  it("returns a null rate with no attempts", () => {
    expect(reminderSuccessRate([]).rate).toBeNull();
  });
});

describe("client-side plan filters", () => {
  it("filters by customer substring over the fetched page", () => {
    const plans = [
      plan({ id: "p1", customer_ref: "CUST-ALPHA" }),
      plan({ id: "p2", customer_ref: "CUST-BETA" }),
      plan({ id: "p3", customer_ref: null }),
    ];
    expect(applyPlanFilters(plans, { customer: "ALPHA" }).map((p) => p.id)).toEqual(["p1"]);
    expect(applyPlanFilters(plans, {}).map((p) => p.id)).toEqual(["p1", "p2", "p3"]);
  });
});
