import { describe, expect, it } from "vitest";
import type {
  GatewayPaymentResponse,
  PaymentChannelResponse,
  SettlementEventResponse,
  SettlementExceptionResponse,
  SettlementFeeRecordResponse,
  SettlementTrancheResponse,
} from "@/lib/api-client";
import {
  ageOutstanding,
  buildFeeAccuracyReport,
  buildSettlementWidgets,
  channelsById,
  forecastTranches,
  summarizeChannels,
  summarizeTrancheStates,
  totalChannelSummary,
} from "./settlement-report";

const CHANNELS: PaymentChannelResponse[] = [
  {
    id: "ch-snapp",
    channel_key: "snapppay",
    display_name_fa: "اسنپ‌پی",
    channel_type: "bnpl",
    is_builtin: true,
    enabled: true,
    status: "active",
    merchant_ref: null,
    settlement_iban: null,
    environment: "production",
    notes: null,
    created_at: null,
    updated_at: null,
  },
  {
    id: "ch-zarin",
    channel_key: "zarinpal",
    display_name_fa: "زرین‌پال",
    channel_type: "gateway",
    is_builtin: true,
    enabled: true,
    status: "active",
    merchant_ref: null,
    settlement_iban: null,
    environment: "production",
    notes: null,
    created_at: null,
    updated_at: null,
  },
];

function payment(overrides: Partial<GatewayPaymentResponse> = {}): GatewayPaymentResponse {
  return {
    id: "pay-1",
    payment_channel_id: "ch-snapp",
    gateway_transaction_id: "TXN-1",
    store_order_id: null,
    store_payment_id: null,
    tryton_invoice_id: null,
    tryton_journal_id: null,
    customer_party_id: null,
    gross_irr: 100_000_000,
    fee_irr: 10_000_000,
    fee_vat_irr: 1_000_000,
    net_irr: 89_000_000,
    contract_version_id: "cv-1",
    fee_rate_snapshot: "10",
    fee_vat_rate_snapshot: "10",
    sales_period_jalali: "1405/08",
    gateway_confirmed_at: "2026-11-19T12:00:00Z",
    source: "store_webhook",
    status: "scheduled",
    refunds_deducted_irr: 0,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

function tranche(overrides: Partial<SettlementTrancheResponse> = {}): SettlementTrancheResponse {
  return {
    id: "tr-1",
    gateway_payment_id: "pay-1",
    tranche_index: 1,
    tranche_count: 4,
    sales_period_jalali: "1405/08",
    expected_window_start: "2026-12-01",
    expected_date: "2026-12-05",
    gross_irr: 25_000_000,
    fee_irr: 2_500_000,
    fee_vat_irr: 250_000,
    net_irr: 22_250_000,
    confirmed_net_irr: 0,
    received_net_irr: 0,
    outstanding_net_irr: 22_250_000,
    state: "expected",
    tryton_receivable_line_id: null,
    variance_irr: null,
    variance_reason: null,
    match_method: null,
    match_confidence: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

function feeRecord(overrides: Partial<SettlementFeeRecordResponse> = {}): SettlementFeeRecordResponse {
  return {
    id: "fee-1",
    gateway_payment_id: "pay-1",
    tranche_id: null,
    contract_version_id: "cv-1",
    fee_percent_applied: "10",
    fee_vat_rate_applied: "10",
    expected_fee_irr: 10_000_000,
    actual_fee_irr: null,
    fee_variance_irr: null,
    expected_vat_irr: 1_000_000,
    actual_vat_irr: null,
    vat_variance_irr: null,
    tryton_fee_journal_id: null,
    source: "calculated",
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

function event(overrides: Partial<SettlementEventResponse> = {}): SettlementEventResponse {
  return {
    id: "ev-1",
    payment_channel_id: "ch-snapp",
    payout_date: "2026-12-05",
    net_irr: 22_250_000,
    source: "file",
    deductions_irr: 0,
    tracking_ref: null,
    destination_iban: null,
    gateway_batch_ref: null,
    import_batch_id: null,
    canonical_event_id: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

function exception(overrides: Partial<SettlementExceptionResponse> = {}): SettlementExceptionResponse {
  return {
    id: "ex-1",
    exception_type: "late_tranche",
    severity: "warning",
    status: "open",
    tranche_id: null,
    settlement_event_id: null,
    bank_deposit_id: null,
    gateway_payment_id: null,
    expected_amount_irr: 1_000_000,
    actual_amount_irr: null,
    escalation_level: 1,
    resolution: null,
    resolved_by: null,
    resolved_at: null,
    note: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

const TODAY = "2026-11-19";

describe("summarizeChannels", () => {
  it("aggregates payments, tranches and deductions per channel", () => {
    const rows = summarizeChannels(
      [payment(), payment({ id: "pay-2", payment_channel_id: "ch-zarin", gross_irr: 50_000_000, fee_irr: 1_000_000, fee_vat_irr: 100_000, net_irr: 48_900_000 })],
      [
        tranche({ confirmed_net_irr: 22_250_000, received_net_irr: 0, outstanding_net_irr: 22_250_000, state: "confirmed" }),
        tranche({ id: "tr-2", gateway_payment_id: "pay-2", net_irr: 48_900_000, outstanding_net_irr: 48_900_000 }),
      ],
      [event({ deductions_irr: 500_000 })],
      channelsById(CHANNELS),
    );
    expect(rows).toHaveLength(2);
    const snapp = rows.find((r) => r.channelKey === "snapppay");
    expect(snapp?.paymentCount).toBe(1);
    expect(snapp?.grossIrr).toBe(100_000_000);
    expect(snapp?.feeIrr).toBe(10_000_000);
    expect(snapp?.deductionsIrr).toBe(500_000);
    expect(snapp?.confirmedNetIrr).toBe(22_250_000);
    expect(snapp?.outstandingNetIrr).toBe(22_250_000);

    const total = totalChannelSummary(rows);
    expect(total.paymentCount).toBe(2);
    expect(total.grossIrr).toBe(150_000_000);
    expect(total.outstandingNetIrr).toBe(71_150_000);
  });

  it("ignores tranches whose payment is missing", () => {
    const rows = summarizeChannels(
      [payment()],
      [tranche({ gateway_payment_id: "pay-gone", net_irr: 5_000_000, outstanding_net_irr: 5_000_000 })],
      [],
      channelsById(CHANNELS),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].trancheCount).toBe(0);
    expect(rows[0].outstandingNetIrr).toBe(0);
  });
});

describe("summarizeTrancheStates", () => {
  it("counts and sums per state", () => {
    const rows = summarizeTrancheStates([
      tranche({ state: "expected" }),
      tranche({ id: "tr-2", state: "expected", net_irr: 10_000_000, outstanding_net_irr: 10_000_000 }),
      tranche({ id: "tr-3", state: "settled", net_irr: 5_000_000, outstanding_net_irr: 0 }),
    ]);
    const expected = rows.find((r) => r.state === "expected");
    expect(expected?.count).toBe(2);
    expect(expected?.netIrr).toBe(32_250_000);
    const settled = rows.find((r) => r.state === "settled");
    expect(settled?.outstandingIrr).toBe(0);
  });
});

describe("ageOutstanding", () => {
  it("buckets only open, past-due, outstanding tranches", () => {
    const rows = ageOutstanding(
      [
        tranche({ id: "a", expected_date: "2026-11-01", outstanding_net_irr: 1_000_000 }), // 18d
        tranche({ id: "b", expected_date: "2026-09-01", outstanding_net_irr: 2_000_000 }), // 79d
        tranche({ id: "c", expected_date: "2026-12-01", outstanding_net_irr: 3_000_000 }), // future
        tranche({ id: "d", expected_date: "2026-10-01", outstanding_net_irr: 0 }), // nothing owed
        tranche({ id: "e", expected_date: "2026-10-01", outstanding_net_irr: 4_000_000, state: "settled" }), // closed
      ],
      TODAY,
    );
    const byBucket = new Map(rows.map((r) => [r.bucket, r]));
    expect(byBucket.get("0-30")?.outstandingIrr).toBe(1_000_000);
    expect(byBucket.get("61-90")?.outstandingIrr).toBe(2_000_000);
    expect(byBucket.get("31-60")?.outstandingIrr).toBe(0);
    expect(byBucket.get("90+")?.count).toBe(0);
  });
});

describe("forecastTranches", () => {
  it("counts expected tranches within each horizon (cumulative)", () => {
    const rows = forecastTranches(
      [
        tranche({ expected_date: "2026-12-05", outstanding_net_irr: 1_000_000 }), // 16d
        tranche({ id: "t2", expected_date: "2027-02-01", outstanding_net_irr: 2_000_000 }), // 74d
        tranche({ id: "t3", expected_date: "2026-11-01", outstanding_net_irr: 3_000_000 }), // overdue: excluded
      ],
      TODAY,
    );
    const byHorizon = new Map(rows.map((r) => [r.horizonDays, r]));
    expect(byHorizon.get(30)?.netIrr).toBe(1_000_000);
    expect(byHorizon.get(60)?.netIrr).toBe(1_000_000);
    expect(byHorizon.get(90)?.netIrr).toBe(3_000_000);
    expect(byHorizon.get(120)?.netIrr).toBe(3_000_000);
  });
});

describe("buildFeeAccuracyReport", () => {
  it("compares expected vs actual fee/VAT and effective vs contract rate", () => {
    const { rows, variances } = buildFeeAccuracyReport(
      [
        feeRecord({
          actual_fee_irr: 10_500_000,
          fee_variance_irr: 500_000,
          actual_vat_irr: 1_050_000,
          vat_variance_irr: 50_000,
          source: "file",
        }),
      ],
      [payment()],
      channelsById(CHANNELS),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].expectedFeeIrr).toBe(10_000_000);
    expect(rows[0].actualFeeIrr).toBe(10_500_000);
    expect(rows[0].feeVarianceIrr).toBe(500_000);
    expect(rows[0].effectiveRatePercent).toBeCloseTo(10.5);
    expect(rows[0].contractRatePercent).toBe(10);
    expect(rows[0].contractRateMixed).toBe(false);
    expect(variances).toHaveLength(1);
    expect(variances[0].feeVarianceIrr).toBe(500_000);
  });

  it("skips tranche-scoped records in the aggregation but lists their variances", () => {
    const { rows, variances } = buildFeeAccuracyReport(
      [
        feeRecord(),
        feeRecord({
          id: "fee-2",
          tranche_id: "tr-1",
          expected_fee_irr: 2_500_000,
          actual_fee_irr: 2_600_000,
          fee_variance_irr: 100_000,
          expected_vat_irr: 250_000,
        }),
      ],
      [payment()],
      channelsById(CHANNELS),
    );
    // Only the whole-payment record counts toward the totals.
    expect(rows[0].expectedFeeIrr).toBe(10_000_000);
    expect(rows[0].actualFeeIrr).toBe(null);
    // But the tranche record's variance still surfaces.
    expect(variances).toHaveLength(1);
    expect(variances[0].trancheId).toBe("tr-1");
  });

  it("flags mixed contract rates instead of inventing one", () => {
    const { rows } = buildFeeAccuracyReport(
      [
        feeRecord(),
        feeRecord({ id: "fee-2", gateway_payment_id: "pay-2", fee_percent_applied: "5" }),
      ],
      [payment(), payment({ id: "pay-2" })],
      channelsById(CHANNELS),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].contractRatePercent).toBe(null);
    expect(rows[0].contractRateMixed).toBe(true);
  });
});

describe("buildSettlementWidgets", () => {
  it("computes due-this-week, overdue, in-transit and next-month figures", () => {
    const widgets = buildSettlementWidgets(
      [
        tranche({ expected_date: "2026-11-22", outstanding_net_irr: 1_000_000 }), // 3d
        tranche({ id: "t2", expected_date: "2026-11-01", outstanding_net_irr: 2_000_000, confirmed_net_irr: 2_000_000, received_net_irr: 500_000, state: "overdue" }),
        tranche({ id: "t3", expected_date: "2026-12-10", outstanding_net_irr: 3_000_000, confirmed_net_irr: 3_000_000, received_net_irr: 0, state: "confirmed" }),
        tranche({ id: "t4", expected_date: "2027-03-01", outstanding_net_irr: 4_000_000 }),
      ],
      [exception(), exception({ id: "ex-2", status: "resolved", expected_amount_irr: 9_000_000 })],
      TODAY,
    );
    expect(widgets.dueThisWeekCount).toBe(1);
    expect(widgets.dueThisWeekIrr).toBe(1_000_000);
    expect(widgets.overdueCount).toBe(1);
    expect(widgets.overdueIrr).toBe(2_000_000);
    expect(widgets.oldestOverdueDays).toBe(18);
    expect(widgets.inTransitIrr).toBe(1_500_000 + 3_000_000);
    expect(widgets.nextMonthCount).toBe(2);
    expect(widgets.nextMonthIrr).toBe(4_000_000);
    expect(widgets.openExceptionCount).toBe(1);
    expect(widgets.openExceptionIrr).toBe(1_000_000);
  });
});
