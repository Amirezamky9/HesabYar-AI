import { describe, expect, it } from "vitest";
import {
  formatDecimal,
  formatInteger,
  formatIrr,
  formatMoney,
  formatToman,
  irrToToman,
  normalizeDigits,
  parseMoneyInput,
  tomanToIrr,
} from "./format-money";

describe("normalizeDigits", () => {
  it("converts Persian and Arabic-Indic digits to ASCII", () => {
    expect(normalizeDigits("۱۲۳۴۵۶۷۸۹۰")).toBe("1234567890");
    expect(normalizeDigits("١٢٣٤٥")).toBe("12345");
    expect(normalizeDigits("12۳۴")).toBe("1234");
  });
});

describe("irr/toman conversion", () => {
  it("divides by 10 for Toman display", () => {
    expect(irrToToman(12_450_000)).toBe(1_245_000);
    expect(irrToToman(138_555)).toBe(13_855.5);
  });

  it("converts Toman back to integer IRR", () => {
    expect(tomanToIrr(1_245_000)).toBe(12_450_000);
  });

  it("rejects fractional Toman that cannot map to integer IRR", () => {
    expect(() => tomanToIrr(1.55)).toThrow(RangeError);
  });
});

describe("formatInteger / formatDecimal", () => {
  it("groups with «٬» and renders Persian digits", () => {
    expect(formatInteger(12_450_000)).toBe("۱۲٬۴۵۰٬۰۰۰");
    expect(formatInteger(0)).toBe("۰");
    expect(formatInteger(-1_250)).toBe("-۱٬۲۵۰");
  });

  it("formats decimals with «٫» and trims trailing zeros", () => {
    expect(formatDecimal(13_855.5)).toBe("۱۳٬۸۵۵٫۵");
    expect(formatDecimal(1_245_000)).toBe("۱٬۲۴۵٬۰۰۰");
  });
});

describe("formatIrr / formatToman / formatMoney", () => {
  it("formats integer IRR without a unit", () => {
    expect(formatIrr(12_450_000)).toBe("۱۲٬۴۵۰٬۰۰۰");
  });

  it("rejects non-integer IRR", () => {
    expect(() => formatIrr(12.5)).toThrow(RangeError);
    expect(() => formatToman(12.5)).toThrow(RangeError);
  });

  it("formats Toman display-only", () => {
    expect(formatToman(12_450_000)).toBe("۱٬۲۴۵٬۰۰۰");
    expect(formatToman(138_555)).toBe("۱۳٬۸۵۵٫۵");
  });

  it("labels the unit explicitly", () => {
    expect(formatMoney(12_450_000)).toBe("۱٬۲۴۵٬۰۰۰ تومان");
    expect(formatMoney(12_450_000, { unit: "irr" })).toBe("۱۲٬۴۵۰٬۰۰۰ ریال");
  });
});

describe("parseMoneyInput", () => {
  it("parses Persian digits with separators as Toman by default", () => {
    expect(parseMoneyInput("۱۲۵٬۰۰۰")).toBe(1_250_000);
    expect(parseMoneyInput("125,000 تومان")).toBe(1_250_000);
    expect(parseMoneyInput("۱۲۵۰۰۰")).toBe(1_250_000);
  });

  it("parses IRR when told the input is IRR", () => {
    expect(parseMoneyInput("۱۲۵۰۰۰", "irr")).toBe(125_000);
  });

  it("returns null for empty or invalid input", () => {
    expect(parseMoneyInput("")).toBeNull();
    expect(parseMoneyInput("   ")).toBeNull();
    expect(parseMoneyInput("abc")).toBeNull();
    expect(parseMoneyInput("12.5")).toBeNull();
    expect(parseMoneyInput("۱۲.۵")).toBeNull();
  });
});
