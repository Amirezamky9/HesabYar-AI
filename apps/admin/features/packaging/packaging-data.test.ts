import { describe, expect, it } from "vitest";
import {
  costMethodLabel,
  countStateLabel,
  countStateTone,
  coverageTone,
  formatDaysOfCover,
  formatQuantity,
  parseDecimalInput,
  parseTomanInput,
  shipmentCostMethodLabel,
} from "./packaging-data";

describe("costMethodLabel", () => {
  it("labels average and fifo", () => {
    expect(costMethodLabel("average")).toBe("میانگین");
    expect(costMethodLabel("fifo")).toBe("فایفو (اول‌وارده، اول‌خارجه)");
  });

  it("falls back to the raw code", () => {
    expect(costMethodLabel("fixed")).toBe("fixed");
    expect(costMethodLabel(null)).toBe("نامشخص");
  });
});

describe("shipmentCostMethodLabel", () => {
  it("labels the three methods", () => {
    expect(shipmentCostMethodLabel("on_order")).toBe("روی سفارش");
    expect(shipmentCostMethodLabel("on_shipment")).toBe("روی ارسال");
    expect(shipmentCostMethodLabel("none")).toBe("بدون هزینهٔ ارسال");
  });
});

describe("countStateLabel / countStateTone", () => {
  it("labels and tones the three states", () => {
    expect(countStateLabel("draft")).toBe("پیش‌نویس");
    expect(countStateTone("draft")).toBe("warning");
    expect(countStateLabel("done")).toBe("تأییدشده");
    expect(countStateTone("done")).toBe("success");
    expect(countStateLabel("cancelled")).toBe("لغوشده");
    expect(countStateTone("cancelled")).toBe("outline");
  });
});

describe("formatDaysOfCover", () => {
  it("renders null as an honest dash", () => {
    expect(formatDaysOfCover(null)).toBe("—");
    expect(formatDaysOfCover(undefined)).toBe("—");
  });

  it("renders whole and fractional days in Persian digits", () => {
    expect(formatDaysOfCover(60)).toBe("≈ ۶۰ روز");
    expect(formatDaysOfCover(7.5)).toBe("≈ ۷٫۵ روز");
  });

  it("renders negative cover explicitly", () => {
    expect(formatDaysOfCover(-3)).toBe("منفی ۳ روز");
  });
});

describe("coverageTone", () => {
  it("is destructive when out of stock, warning under the threshold", () => {
    expect(coverageTone(null, 7, true)).toBe("destructive");
    expect(coverageTone(3, 7, false)).toBe("warning");
    expect(coverageTone(7, 7, false)).toBe("warning");
    expect(coverageTone(30, 7, false)).toBe("success");
    expect(coverageTone(null, 7, false)).toBe("outline");
  });
});

describe("formatQuantity", () => {
  it("renders Persian digits and trims trailing zeros", () => {
    expect(formatQuantity("60")).toBe("۶۰");
    expect(formatQuantity("1.50")).toBe("۱٫۵");
    expect(formatQuantity("0")).toBe("۰");
  });

  it("renders missing as a dash", () => {
    expect(formatQuantity(null)).toBe("—");
    expect(formatQuantity("")).toBe("—");
  });
});

describe("parseDecimalInput", () => {
  it("parses Latin and Persian digits", () => {
    expect(parseDecimalInput("10")).toBe("10");
    expect(parseDecimalInput("۲٫۵")).toBe("2.5");
    expect(parseDecimalInput("۱٬۰۰۰")).toBe("1000");
  });

  it("rejects invalid input", () => {
    expect(parseDecimalInput("")).toBeNull();
    expect(parseDecimalInput("abc")).toBeNull();
    expect(parseDecimalInput("-5")).toBeNull();
    expect(parseDecimalInput("1.2.3")).toBeNull();
  });
});

describe("parseTomanInput", () => {
  it("converts تومان to IRR", () => {
    expect(parseTomanInput("۷۵۰۰")).toBe(75000);
    expect(parseTomanInput("0")).toBe(0);
  });

  it("rejects invalid input", () => {
    expect(parseTomanInput("")).toBeNull();
    expect(parseTomanInput("7.5")).toBeNull();
    expect(parseTomanInput("-1")).toBeNull();
  });
});
