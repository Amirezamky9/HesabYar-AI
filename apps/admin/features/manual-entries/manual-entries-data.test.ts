/**
 * Vitest for the manual-entry data helpers (US-513): labels fall back
 * to the raw code, quantities parse Persian digits, order totals are
 * whole-Toman exact, and fractional line totals are reported by index.
 */

import { describe, expect, it } from "vitest";
import {
  computeOrderTotals,
  decisionLabel,
  eventTypeLabel,
  formatTomanAmount,
  inboxStatusLabel,
  inboxStatusTone,
  isWholeToman,
  manualEntryTypeLabel,
  occurredAtIso,
  parseManualQuantity,
  type ManualOrderLineDraft,
} from "./manual-entries-data";

describe("labels", () => {
  it("labels the three entry types in Persian", () => {
    expect(manualEntryTypeLabel("order")).toBe("سفارش");
    expect(manualEntryTypeLabel("refund")).toBe("برگشت");
    expect(manualEntryTypeLabel("cancellation")).toBe("لغو سفارش");
    expect(manualEntryTypeLabel("nope")).toBe("nope");
    expect(manualEntryTypeLabel(null)).toBe("نامشخص");
  });

  it("labels inbox statuses with tones", () => {
    expect(inboxStatusLabel("received")).toBe("دریافت‌شده");
    expect(inboxStatusLabel("processed")).toBe("پردازش‌شده");
    expect(inboxStatusLabel("needs_review")).toBe("نیازمند بررسی");
    expect(inboxStatusLabel("dead_lettered")).toBe("مرده");
    expect(inboxStatusTone("processed")).toBe("success");
    expect(inboxStatusTone("needs_review")).toBe("warning");
    expect(inboxStatusTone("nope")).toBe("outline");
  });

  it("labels decisions and event types", () => {
    expect(decisionLabel("draft")).toBe("پیش‌نویس");
    expect(decisionLabel("auto_post")).toBe("ثبت خودکار");
    expect(decisionLabel(null)).toBe("—");
    expect(eventTypeLabel("sales.order.completed")).toBe("تکمیل سفارش");
    expect(eventTypeLabel("payment.refunded")).toBe("برگشت پرداخت");
    expect(eventTypeLabel("unknown.type")).toBe("unknown.type");
  });
});

describe("parseManualQuantity", () => {
  it("parses Latin and Persian digits", () => {
    expect(parseManualQuantity("2")).toBe(2);
    expect(parseManualQuantity("۲")).toBe(2);
    expect(parseManualQuantity("۱٫۵")).toBe(1.5);
    expect(parseManualQuantity("1.5")).toBe(1.5);
    expect(parseManualQuantity(" ۳ ")).toBe(3);
  });

  it("rejects zero, negative and non-numeric input", () => {
    expect(parseManualQuantity("0")).toBeNull();
    expect(parseManualQuantity("-1")).toBeNull();
    expect(parseManualQuantity("")).toBeNull();
    expect(parseManualQuantity("دو")).toBeNull();
    expect(parseManualQuantity("1,5")).toBe(15);
  });
});

describe("isWholeToman", () => {
  it("tolerates floating-point noise around whole numbers", () => {
    expect(isWholeToman(0.1 * 3 + 0.7)).toBe(true); // 1.0000000000000002
    expect(isWholeToman(151.5)).toBe(false);
    expect(isWholeToman(Number.NaN)).toBe(false);
  });
});

describe("computeOrderTotals", () => {
  const line = (overrides: Partial<ManualOrderLineDraft> = {}): ManualOrderLineDraft => ({
    quantity: "۱",
    unitPriceToman: 175_000,
    discountToman: null,
    ...overrides,
  });

  it("sums lines and applies order-level amounts", () => {
    const totals = computeOrderTotals(
      [line({ quantity: "۲" }), line({ quantity: "۱", unitPriceToman: 240_000 })],
      10_000,
      50_000,
      0,
    );
    expect(totals.subtotalToman).toBe(590_000);
    expect(totals.totalToman).toBe(630_000);
    expect(totals.fractionalLines).toEqual([]);
    expect(totals.valid).toBe(true);
  });

  it("accepts a fractional quantity when the line total is whole", () => {
    const totals = computeOrderTotals(
      [line({ quantity: "۰٫۵", unitPriceToman: 200_000 })],
      null,
      null,
      null,
    );
    expect(totals.subtotalToman).toBe(100_000);
    expect(totals.valid).toBe(true);
  });

  it("reports a fractional line total by index", () => {
    const totals = computeOrderTotals(
      [line({ quantity: "۱٫۵", unitPriceToman: 101 })],
      null,
      null,
      null,
    );
    expect(totals.fractionalLines).toEqual([0]);
    expect(totals.valid).toBe(false);
  });

  it("rejects empty lines, bad quantities and non-positive totals", () => {
    expect(computeOrderTotals([], null, null, null).valid).toBe(false);
    expect(computeOrderTotals([line({ quantity: "صفر" })], null, null, null).valid).toBe(false);
    expect(computeOrderTotals([line({ unitPriceToman: null })], null, null, null).valid).toBe(false);
    expect(
      computeOrderTotals([line({ unitPriceToman: 0 })], null, null, null).valid,
    ).toBe(false);
    expect(
      computeOrderTotals([line({ discountToman: 200_000 })], null, null, null).valid,
    ).toBe(false);
  });
});

describe("formatTomanAmount + occurredAtIso", () => {
  it("formats whole Toman with Persian digits and the unit", () => {
    expect(formatTomanAmount(630_000)).toBe("۶۳۰٬۰۰۰ تومان");
    expect(formatTomanAmount(0)).toBe("۰ تومان");
  });

  it("sends the picked date at noon UTC", () => {
    expect(occurredAtIso("2026-11-18")).toBe("2026-11-18T12:00:00Z");
    expect(occurredAtIso(null)).toBeNull();
  });
});
