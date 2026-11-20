/**
 * Settlement report aggregation (§3.10 + §11.2 #10) — pure functions
 * over the settlement list endpoints.
 *
 * The backend has no dedicated settlement-report endpoint: the report
 * page fetches payments, tranches, fee records, events, exceptions and
 * imports (all BFF, cursor-paginated) and this module aggregates them
 * into the §3.10 sections. Every figure stays traceable to its source
 * rows (the UI links each section to the underlying schedule/payment
 * view), and every amount stays integer IRR.
 *
 * Fee accuracy (§3.6) aggregates whole-payment fee records only
 * (`tranche_id === null`): per-tranche records (policy B, fee at
 * settlement) would double-count the same fee if summed alongside the
 * payment-level record. The variance list still shows tranche-scoped
 * records, labelled with their tranche.
 */

import type {
  GatewayPaymentResponse,
  PaymentChannelResponse,
  SettlementEventResponse,
  SettlementExceptionResponse,
  SettlementFeeRecordResponse,
  SettlementTrancheResponse,
} from "@/lib/api-client";
import {
  AGING_BUCKETS,
  agingBucket,
  daysBetween,
  daysOverdue,
  isTrancheOpen,
  parseDecimalString,
  type AgingBucket,
} from "./settlement-data";

/** Payment channel id → channel (for names/keys). */
export type ChannelsById = Map<string, PaymentChannelResponse>;

export function channelsById(channels: PaymentChannelResponse[]): ChannelsById {
  return new Map(channels.map((c) => [c.id, c]));
}

export function channelKeyOf(
  paymentChannelId: string,
  channels: ChannelsById,
): string {
  return channels.get(paymentChannelId)?.channel_key ?? paymentChannelId;
}

export function channelNameOf(
  paymentChannelId: string,
  channels: ChannelsById,
): string {
  const channel = channels.get(paymentChannelId);
  return channel ? channel.display_name_fa : paymentChannelId;
}

/** One row of the per-channel summary (§3.10). */
export interface ChannelSummary {
  channelKey: string;
  channelName: string;
  paymentCount: number;
  trancheCount: number;
  grossIrr: number;
  feeIrr: number;
  feeVatIrr: number;
  deductionsIrr: number;
  netReceivableIrr: number;
  confirmedNetIrr: number;
  receivedNetIrr: number;
  outstandingNetIrr: number;
}

export function emptyChannelSummary(channelKey: string, channelName: string): ChannelSummary {
  return {
    channelKey,
    channelName,
    paymentCount: 0,
    trancheCount: 0,
    grossIrr: 0,
    feeIrr: 0,
    feeVatIrr: 0,
    deductionsIrr: 0,
    netReceivableIrr: 0,
    confirmedNetIrr: 0,
    receivedNetIrr: 0,
    outstandingNetIrr: 0,
  };
}

/**
 * Per-channel summary: gross/fee/VAT/net from payments, confirmed /
 * received / outstanding from tranches, documented deductions from
 * settlement events. Rows with no channel row (unknown channel id)
 * keep the raw id as their key so nothing disappears.
 */
export function summarizeChannels(
  payments: GatewayPaymentResponse[],
  tranches: SettlementTrancheResponse[],
  events: SettlementEventResponse[],
  channels: ChannelsById,
): ChannelSummary[] {
  const byKey = new Map<string, ChannelSummary>();
  const get = (paymentChannelId: string): ChannelSummary => {
    const key = channelKeyOf(paymentChannelId, channels);
    let row = byKey.get(key);
    if (!row) {
      row = emptyChannelSummary(key, channelNameOf(paymentChannelId, channels));
      byKey.set(key, row);
    }
    return row;
  };
  const paymentById = new Map(payments.map((p) => [p.id, p]));

  for (const payment of payments) {
    const row = get(payment.payment_channel_id);
    row.paymentCount += 1;
    row.grossIrr += payment.gross_irr;
    row.feeIrr += payment.fee_irr;
    row.feeVatIrr += payment.fee_vat_irr;
    row.netReceivableIrr += payment.net_irr;
  }
  for (const tranche of tranches) {
    const payment = paymentById.get(tranche.gateway_payment_id);
    if (!payment) continue;
    const row = get(payment.payment_channel_id);
    row.trancheCount += 1;
    row.confirmedNetIrr += tranche.confirmed_net_irr;
    row.receivedNetIrr += tranche.received_net_irr;
    row.outstandingNetIrr += tranche.outstanding_net_irr;
  }
  for (const event of events) {
    get(event.payment_channel_id).deductionsIrr += event.deductions_irr;
  }
  return [...byKey.values()].sort((a, b) => a.channelName.localeCompare(b.channelName, "fa"));
}

/** Sum of channel rows (the report total). */
export function totalChannelSummary(rows: ChannelSummary[]): ChannelSummary {
  const total = emptyChannelSummary("total", "جمع کل");
  for (const row of rows) {
    total.paymentCount += row.paymentCount;
    total.trancheCount += row.trancheCount;
    total.grossIrr += row.grossIrr;
    total.feeIrr += row.feeIrr;
    total.feeVatIrr += row.feeVatIrr;
    total.deductionsIrr += row.deductionsIrr;
    total.netReceivableIrr += row.netReceivableIrr;
    total.confirmedNetIrr += row.confirmedNetIrr;
    total.receivedNetIrr += row.receivedNetIrr;
    total.outstandingNetIrr += row.outstandingNetIrr;
  }
  return total;
}

/** Tranche counts + net amounts per state. */
export interface TrancheStateSummary {
  state: string;
  count: number;
  netIrr: number;
  outstandingIrr: number;
}

export function summarizeTrancheStates(
  tranches: SettlementTrancheResponse[],
): TrancheStateSummary[] {
  const byState = new Map<string, TrancheStateSummary>();
  for (const tranche of tranches) {
    let row = byState.get(tranche.state);
    if (!row) {
      row = { state: tranche.state, count: 0, netIrr: 0, outstandingIrr: 0 };
      byState.set(tranche.state, row);
    }
    row.count += 1;
    row.netIrr += tranche.net_irr;
    row.outstandingIrr += tranche.outstanding_net_irr;
  }
  return [...byState.values()].sort((a, b) => b.netIrr - a.netIrr);
}

/** One aging bucket of outstanding (unreceived) tranche money. */
export interface AgingRow {
  bucket: AgingBucket;
  count: number;
  outstandingIrr: number;
}

/** Outstanding tranches past their due date, bucketed by days overdue. */
export function ageOutstanding(
  tranches: SettlementTrancheResponse[],
  todayIso: string,
): AgingRow[] {
  const rows = new Map<AgingBucket, AgingRow>(
    AGING_BUCKETS.map((bucket) => [bucket, { bucket, count: 0, outstandingIrr: 0 }]),
  );
  for (const tranche of tranches) {
    if (!isTrancheOpen(tranche.state) || tranche.outstanding_net_irr <= 0) continue;
    const overdue = daysOverdue(tranche.expected_date, todayIso);
    if (overdue <= 0) continue;
    const row = rows.get(agingBucket(overdue));
    if (!row) continue;
    row.count += 1;
    row.outstandingIrr += tranche.outstanding_net_irr;
  }
  return AGING_BUCKETS.map((bucket) => rows.get(bucket) as AgingRow);
}

/** Cash-flow forecast: expected tranches due within each horizon. */
export interface ForecastRow {
  horizonDays: number;
  count: number;
  netIrr: number;
}

export const FORECAST_HORIZONS = [30, 60, 90, 120] as const;

export function forecastTranches(
  tranches: SettlementTrancheResponse[],
  todayIso: string,
): ForecastRow[] {
  const rows = FORECAST_HORIZONS.map((horizonDays) => ({ horizonDays, count: 0, netIrr: 0 }));
  for (const tranche of tranches) {
    if (!isTrancheOpen(tranche.state) || tranche.outstanding_net_irr <= 0) continue;
    const daysUntilDue = daysBetween(todayIso, tranche.expected_date);
    if (daysUntilDue < 0) continue;
    for (const row of rows) {
      if (daysUntilDue <= row.horizonDays) {
        row.count += 1;
        row.netIrr += tranche.outstanding_net_irr;
      }
    }
  }
  return rows;
}

/** One row of the fee-accuracy report (§3.6). */
export interface FeeAccuracyRow {
  channelKey: string;
  channelName: string;
  salesPeriod: string;
  paymentCount: number;
  grossIrr: number;
  expectedFeeIrr: number;
  actualFeeIrr: number | null;
  feeVarianceIrr: number | null;
  expectedVatIrr: number;
  actualVatIrr: number | null;
  vatVarianceIrr: number | null;
  /** Effective real rate (actual fee / gross × 100), null without actuals. */
  effectiveRatePercent: number | null;
  /** Contract rate when every record in the group agrees, else null (mixed). */
  contractRatePercent: number | null;
  contractRateMixed: boolean;
}

/** One fee record whose actual differs from its expected figure. */
export interface FeeVarianceRow {
  feeRecordId: string;
  paymentId: string;
  trancheId: string | null;
  channelKey: string;
  channelName: string;
  salesPeriod: string;
  feeVarianceIrr: number;
  vatVarianceIrr: number;
  source: string;
}

export interface FeeAccuracyReport {
  rows: FeeAccuracyRow[];
  variances: FeeVarianceRow[];
}

/**
 * Fee accuracy (§3.6): expected vs actual fee/VAT per channel + sales
 * period, effective real rate vs contract rate, and the fee-variance
 * list. Aggregates whole-payment records only (see the module doc).
 */
export function buildFeeAccuracyReport(
  feeRecords: SettlementFeeRecordResponse[],
  payments: GatewayPaymentResponse[],
  channels: ChannelsById,
): FeeAccuracyReport {
  const paymentById = new Map(payments.map((p) => [p.id, p]));
  const groups = new Map<string, FeeAccuracyRow & { contractRates: Set<string> }>();
  const variances: FeeVarianceRow[] = [];

  for (const record of feeRecords) {
    const payment = paymentById.get(record.gateway_payment_id);
    if (!payment) continue;
    const channelKey = channelKeyOf(payment.payment_channel_id, channels);
    const channelName = channelNameOf(payment.payment_channel_id, channels);
    const feeVariance = record.fee_variance_irr ?? 0;
    const vatVariance = record.vat_variance_irr ?? 0;
    if (feeVariance !== 0 || vatVariance !== 0) {
      variances.push({
        feeRecordId: record.id,
        paymentId: record.gateway_payment_id,
        trancheId: record.tranche_id,
        channelKey,
        channelName,
        salesPeriod: payment.sales_period_jalali,
        feeVarianceIrr: feeVariance,
        vatVarianceIrr: vatVariance,
        source: record.source,
      });
    }
    // Aggregation: whole-payment records only (no double counting).
    if (record.tranche_id !== null) continue;
    const groupKey = `${channelKey}\n${payment.sales_period_jalali}`;
    let group = groups.get(groupKey);
    if (!group) {
      group = {
        channelKey,
        channelName,
        salesPeriod: payment.sales_period_jalali,
        paymentCount: 0,
        grossIrr: 0,
        expectedFeeIrr: 0,
        actualFeeIrr: null,
        feeVarianceIrr: null,
        expectedVatIrr: 0,
        actualVatIrr: null,
        vatVarianceIrr: null,
        effectiveRatePercent: null,
        contractRatePercent: null,
        contractRateMixed: false,
        contractRates: new Set<string>(),
      };
      groups.set(groupKey, group);
    }
    group.paymentCount += 1;
    group.grossIrr += payment.gross_irr;
    group.expectedFeeIrr += record.expected_fee_irr;
    group.expectedVatIrr += record.expected_vat_irr;
    group.contractRates.add(record.fee_percent_applied);
    if (record.actual_fee_irr !== null) {
      group.actualFeeIrr = (group.actualFeeIrr ?? 0) + record.actual_fee_irr;
    }
    if (record.actual_vat_irr !== null) {
      group.actualVatIrr = (group.actualVatIrr ?? 0) + record.actual_vat_irr;
    }
    if (record.fee_variance_irr !== null) {
      group.feeVarianceIrr = (group.feeVarianceIrr ?? 0) + record.fee_variance_irr;
    }
    if (record.vat_variance_irr !== null) {
      group.vatVarianceIrr = (group.vatVarianceIrr ?? 0) + record.vat_variance_irr;
    }
  }

  const rows = [...groups.values()]
    .map(({ contractRates, ...group }) => {
      const rates = [...contractRates]
        .map((rate) => parseDecimalString(rate))
        .filter((rate): rate is number => rate !== null);
      const distinct = [...new Set(rates)];
      return {
        ...group,
        effectiveRatePercent:
          group.actualFeeIrr !== null && group.grossIrr > 0
            ? (group.actualFeeIrr / group.grossIrr) * 100
            : null,
        contractRatePercent: distinct.length === 1 ? distinct[0] : null,
        contractRateMixed: distinct.length > 1,
      };
    })
    .sort((a, b) =>
      a.channelName === b.channelName
        ? a.salesPeriod.localeCompare(b.salesPeriod)
        : a.channelName.localeCompare(b.channelName, "fa"),
    );
  variances.sort((a, b) => Math.abs(b.feeVarianceIrr) - Math.abs(a.feeVarianceIrr));
  return { rows, variances };
}

/** Dashboard widgets (§11.3) for the settlement center. */
export interface SettlementWidgets {
  /** Open tranches due within 7 days (count + outstanding). */
  dueThisWeekCount: number;
  dueThisWeekIrr: number;
  /** Open tranches past due (count + outstanding + oldest overdue days). */
  overdueCount: number;
  overdueIrr: number;
  oldestOverdueDays: number | null;
  /** Confirmed but not yet received (money in transit). */
  inTransitIrr: number;
  /** Expected tranches due in the next 30 days. */
  nextMonthIrr: number;
  nextMonthCount: number;
  /** Open exceptions (count + total expected amount). */
  openExceptionCount: number;
  openExceptionIrr: number;
}

export function buildSettlementWidgets(
  tranches: SettlementTrancheResponse[],
  exceptions: SettlementExceptionResponse[],
  todayIso: string,
): SettlementWidgets {
  let dueThisWeekCount = 0;
  let dueThisWeekIrr = 0;
  let overdueCount = 0;
  let overdueIrr = 0;
  let oldestOverdueDays: number | null = null;
  let inTransitIrr = 0;
  let nextMonthIrr = 0;
  let nextMonthCount = 0;
  for (const tranche of tranches) {
    if (!isTrancheOpen(tranche.state) || tranche.outstanding_net_irr <= 0) continue;
    const daysUntilDue = daysBetween(todayIso, tranche.expected_date);
    inTransitIrr += Math.max(0, tranche.confirmed_net_irr - tranche.received_net_irr);
    if (daysUntilDue < 0) {
      overdueCount += 1;
      overdueIrr += tranche.outstanding_net_irr;
      const overdue = -daysUntilDue;
      oldestOverdueDays = oldestOverdueDays === null ? overdue : Math.max(oldestOverdueDays, overdue);
    } else {
      if (daysUntilDue <= 7) {
        dueThisWeekCount += 1;
        dueThisWeekIrr += tranche.outstanding_net_irr;
      }
      if (daysUntilDue <= 30) {
        nextMonthCount += 1;
        nextMonthIrr += tranche.outstanding_net_irr;
      }
    }
  }
  const openExceptions = exceptions.filter((e) => e.status === "open" || e.status === "in_review");
  return {
    dueThisWeekCount,
    dueThisWeekIrr,
    overdueCount,
    overdueIrr,
    oldestOverdueDays,
    inTransitIrr,
    nextMonthIrr,
    nextMonthCount,
    openExceptionCount: openExceptions.length,
    openExceptionIrr: openExceptions.reduce((sum, e) => sum + (e.expected_amount_irr ?? 0), 0),
  };
}
