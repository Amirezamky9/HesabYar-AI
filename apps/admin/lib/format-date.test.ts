import { describe, expect, it } from "vitest";
import { formatIsoDate, formatIsoDateNumeric, formatIsoDateTime } from "./format-date";

describe("formatIsoDate", () => {
  it("renders Jalali", () => {
    expect(formatIsoDate("2026-11-17")).toBe("۲۶ آبان ۱۴۰۵");
    expect(formatIsoDate("2026-11-17", { weekday: true })).toBe("سه‌شنبه، ۲۶ آبان ۱۴۰۵");
  });

  it("renders empty for null/undefined", () => {
    expect(formatIsoDate(null)).toBe("");
    expect(formatIsoDate(undefined)).toBe("");
  });

  it("renders numeric Jalali", () => {
    expect(formatIsoDateNumeric("2026-11-17")).toBe("۱۴۰۵/۰۸/۲۶");
    expect(formatIsoDateNumeric(null)).toBe("");
  });
});

describe("formatIsoDateTime", () => {
  it("renders Jalali date + Tehran time", () => {
    // 2026-11-17T10:30:00Z = 14:00 Tehran (+3:30).
    expect(formatIsoDateTime("2026-11-17T10:30:00Z")).toBe("۲۶ آبان ۱۴۰۵، ۱۴:۰۰");
  });

  it("renders empty for null/undefined and throws on garbage", () => {
    expect(formatIsoDateTime(null)).toBe("");
    expect(() => formatIsoDateTime("not-a-date")).toThrow(RangeError);
  });
});
