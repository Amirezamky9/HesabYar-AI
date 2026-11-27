/**
 * Unit tests for the cash-box labels + helpers (US-516).
 *
 * Every code the API returns gets a Persian label; unknown codes
 * fall back to the raw code so a new backend label never silently
 * disappears.
 */

import { describe, expect, it } from "vitest";
import {
  cashBoxKindLabel,
  countScheduleLabel,
  daysSinceLabel,
  denominationsTotal,
  sessionStatusLabel,
  sessionStatusTone,
  signRoleLabel,
  varianceDirection,
  varianceDirectionLabel,
  varianceDirectionTone,
  variancePolicyLabel,
} from "./cash-box-data";

describe("cashBoxKindLabel", () => {
  it("labels all three kinds in Persian", () => {
    expect(cashBoxKindLabel("store")).toBe("صندوق فروشگاه");
    expect(cashBoxKindLabel("petty_cash")).toBe("تنخواه");
    expect(cashBoxKindLabel("other")).toBe("صندوق دیگر");
  });

  it("falls back to the raw code for unknown kinds", () => {
    expect(cashBoxKindLabel("vault")).toBe("vault");
    expect(cashBoxKindLabel(null)).toBe("نامشخص");
  });
});

describe("sessionStatusLabel", () => {
  it("labels all four statuses in Persian", () => {
    expect(sessionStatusLabel("in_progress")).toBe("در جریان");
    expect(sessionStatusLabel("awaiting_supervisor")).toBe("در انتظار ناظر");
    expect(sessionStatusLabel("awaiting_manager")).toBe("در انتظار تأیید مدیر");
    expect(sessionStatusLabel("archived")).toBe("بایگانی‌شده");
  });

  it("falls back to the raw code for unknown statuses", () => {
    expect(sessionStatusLabel("cancelled")).toBe("cancelled");
    expect(sessionStatusLabel(undefined)).toBe("نامشخص");
  });

  it("gives archived a success tone and waiting states a warning tone", () => {
    expect(sessionStatusTone("archived")).toBe("success");
    expect(sessionStatusTone("awaiting_manager")).toBe("warning");
    expect(sessionStatusTone("in_progress")).toBe("outline");
  });
});

describe("variancePolicyLabel", () => {
  it("labels both policies in Persian", () => {
    expect(variancePolicyLabel("expense")).toBe("حساب کسر و اضافات صندوق");
    expect(variancePolicyLabel("receivable_from_cashier")).toBe("بدهکار موقت — کسر از صندوقدار");
  });

  it("falls back to the raw code for unknown policies", () => {
    expect(variancePolicyLabel("other")).toBe("other");
  });
});

describe("countScheduleLabel", () => {
  it("labels all three schedules in Persian", () => {
    expect(countScheduleLabel("daily")).toBe("روزانه");
    expect(countScheduleLabel("weekly")).toBe("هفتگی");
    expect(countScheduleLabel("period_end")).toBe("پایان دوره");
  });
});

describe("signRoleLabel", () => {
  it("labels all three roles in Persian", () => {
    expect(signRoleLabel("cashier")).toBe("صندوقدار");
    expect(signRoleLabel("supervisor")).toBe("ناظر");
    expect(signRoleLabel("manager")).toBe("مدیر");
  });
});

describe("varianceDirection", () => {
  it("classifies shortage, overage, balanced and uncounted", () => {
    expect(varianceDirection(-100_000)).toBe("shortage");
    expect(varianceDirection(50_000)).toBe("overage");
    expect(varianceDirection(0)).toBe("balanced");
    expect(varianceDirection(null)).toBe("unknown");
    expect(varianceDirection(undefined)).toBe("unknown");
  });

  it("labels and tones each direction", () => {
    expect(varianceDirectionLabel(-100_000)).toBe("کسری");
    expect(varianceDirectionTone(-100_000)).toBe("destructive");
    expect(varianceDirectionLabel(50_000)).toBe("اضافه");
    expect(varianceDirectionTone(50_000)).toBe("warning");
    expect(varianceDirectionLabel(0)).toBe("برابر");
    expect(varianceDirectionTone(0)).toBe("success");
    expect(varianceDirectionLabel(null)).toBe("شمارش نشده");
  });
});

describe("daysSinceLabel", () => {
  it("renders today, yesterday, N days ago and never", () => {
    expect(daysSinceLabel(null)).toBe("هرگز شمارش نشده");
    expect(daysSinceLabel(0)).toBe("امروز");
    expect(daysSinceLabel(1)).toBe("دیروز");
    expect(daysSinceLabel(10)).toBe("۱۰ روز پیش");
  });
});

describe("denominationsTotal", () => {
  it("sums denomination lines", () => {
    expect(
      denominationsTotal([
        { denomination_irr: 1_000_000, count: 4 },
        { denomination_irr: 500_000, count: 2 },
      ]),
    ).toBe(5_000_000);
    expect(denominationsTotal([])).toBe(0);
  });
});
