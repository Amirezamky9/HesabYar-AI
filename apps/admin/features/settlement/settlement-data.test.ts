import { describe, expect, it } from "vitest";
import {
  SETTLEMENT_EXCEPTION_TYPES,
  TRANCHE_STATES,
  agingBucket,
  daysBetween,
  daysOverdue,
  escalationLevelLabel,
  formatPercentString,
  importStatusLabel,
  isTrancheOpen,
  isValidJalaliPeriod,
  jalaliPeriodLabel,
  jalaliPeriodOf,
  matchStatusLabel,
  normalizeJalaliPeriod,
  parseDecimalString,
  paymentStatusLabel,
  settlementExceptionTypeLabel,
  settlementModelLabel,
  trancheStateLabel,
} from "./settlement-data";

describe("settlement labels", () => {
  it("covers all eleven §3.3 tranche states in Persian", () => {
    expect(TRANCHE_STATES).toHaveLength(11);
    for (const state of TRANCHE_STATES) {
      expect(trancheStateLabel(state)).not.toBe(state);
    }
    expect(trancheStateLabel("expected")).toBe("موردانتظار");
    expect(trancheStateLabel("overdue")).toBe("سررسیدگذشته");
    expect(trancheStateLabel("settled")).toBe("تسویه‌شده");
    expect(trancheStateLabel("nope")).toBe("nope");
    expect(trancheStateLabel(null)).toBe("نامشخص");
  });

  it("labels payment statuses and exception types in Persian", () => {
    expect(paymentStatusLabel("scheduled")).toBe("زمان‌بندی‌شده");
    expect(paymentStatusLabel("settling")).toBe("در حال تسویه");
    expect(paymentStatusLabel("settled")).toBe("تسویه‌شده");
    expect(SETTLEMENT_EXCEPTION_TYPES).toHaveLength(11);
    for (const type of SETTLEMENT_EXCEPTION_TYPES) {
      expect(settlementExceptionTypeLabel(type)).not.toBe(type);
    }
    expect(settlementExceptionTypeLabel("fee_mismatch")).toBe("مغایرت کارمزد");
    expect(settlementExceptionTypeLabel("late_tranche")).toBe("قسط دیرکرد");
  });

  it("labels import and match statuses", () => {
    expect(importStatusLabel("awaiting_review")).toBe("در انتظار بازبینی");
    expect(importStatusLabel("settled")).toBe("تسویه‌شده");
    expect(matchStatusLabel("matched")).toBe("منطبق");
    expect(matchStatusLabel("amount_mismatch")).toBe("مغایرت مبلغ");
    expect(matchStatusLabel("new_payment")).toBe("پرداخت جدید");
  });

  it("labels settlement models, falling back to the raw code", () => {
    expect(settlementModelLabel("tranches_4")).toBe("۴ قسط ماهانه");
    expect(settlementModelLabel("monthly_lump")).toBe("یکجا ماهانه");
    expect(settlementModelLabel("weekly_custom")).toBe("weekly_custom");
  });

  it("labels escalation levels 0–3", () => {
    expect(escalationLevelLabel(0)).toBe("بدون تشدید");
    expect(escalationLevelLabel(1)).toContain("۷ روز");
    expect(escalationLevelLabel(2)).toContain("۳۰ روز");
    expect(escalationLevelLabel(3)).toContain("۶۰ روز");
  });
});

describe("tranche openness", () => {
  it("treats settled/cancelled/written-off as closed", () => {
    expect(isTrancheOpen("settled")).toBe(false);
    expect(isTrancheOpen("cancelled")).toBe(false);
    expect(isTrancheOpen("written_off")).toBe(false);
    expect(isTrancheOpen("expected")).toBe(true);
    expect(isTrancheOpen("overdue")).toBe(true);
    expect(isTrancheOpen("confirmed")).toBe(true);
  });
});

describe("decimal strings (§9.3)", () => {
  it("parses fee percents and rejects junk", () => {
    expect(parseDecimalString("10")).toBe(10);
    expect(parseDecimalString("7.5")).toBe(7.5);
    expect(parseDecimalString(null)).toBe(null);
    expect(parseDecimalString("")).toBe(null);
    expect(parseDecimalString("abc")).toBe(null);
  });

  it("formats percents in Persian digits", () => {
    expect(formatPercentString("10")).toBe("۱۰٪");
    expect(formatPercentString("7.5")).toBe("۷٫۵٪");
    expect(formatPercentString(null)).toBe("—");
  });
});

describe("dates and aging", () => {
  it("counts whole days between ISO dates", () => {
    expect(daysBetween("2026-11-01", "2026-11-19")).toBe(18);
    expect(daysBetween("2026-11-19", "2026-11-01")).toBe(-18);
    expect(daysBetween("2026-11-19", "2026-11-19")).toBe(0);
    expect(daysBetween("junk", "2026-11-19")).toBe(0);
  });

  it("counts overdue days, never negative", () => {
    expect(daysOverdue("2026-11-01", "2026-11-19")).toBe(18);
    expect(daysOverdue("2026-11-19", "2026-11-19")).toBe(0);
    expect(daysOverdue("2026-12-01", "2026-11-19")).toBe(0);
  });

  it("buckets aging at 30/60/90 days", () => {
    expect(agingBucket(0)).toBe("0-30");
    expect(agingBucket(30)).toBe("0-30");
    expect(agingBucket(31)).toBe("31-60");
    expect(agingBucket(60)).toBe("31-60");
    expect(agingBucket(61)).toBe("61-90");
    expect(agingBucket(90)).toBe("61-90");
    expect(agingBucket(91)).toBe("90+");
  });

  it("formats Jalali periods and validates them", () => {
    expect(jalaliPeriodLabel("1405/08")).toBe("آبان ۱۴۰۵");
    expect(jalaliPeriodLabel("1405/01")).toBe("فروردین ۱۴۰۵");
    expect(jalaliPeriodLabel("junk")).toBe("junk");
    expect(isValidJalaliPeriod("1405/08")).toBe(true);
    expect(isValidJalaliPeriod("1405/8")).toBe(true);
    expect(isValidJalaliPeriod("1405/13")).toBe(false);
    expect(isValidJalaliPeriod("1405-08")).toBe(false);
    expect(normalizeJalaliPeriod("1405/8")).toBe("1405/08");
    // 2026-11-19 is 28 Aban 1405.
    expect(jalaliPeriodOf(new Date("2026-11-19T12:00:00Z"))).toBe("1405/08");
  });
});
