import { describe, expect, it } from "vitest";
import {
  assertValidJalali,
  formatIsoJalali,
  formatIsoJalaliNumeric,
  formatJalali,
  isValidIsoDate,
  isoToJalali,
  jalaliToIso,
  parseIsoDate,
  toGregorian,
  toJalali,
} from "./jalali";

describe("isoToJalali / jalaliToIso (deterministic anchors)", () => {
  // Tue 2026-11-17 = 26 Aban 1405 (verified against the known weekday).
  it("maps 2026-11-17 to 1405-08-26", () => {
    expect(isoToJalali("2026-11-17")).toEqual({ jy: 1405, jm: 8, jd: 26 });
  });

  it("maps 1405-08-26 back to 2026-11-17", () => {
    expect(jalaliToIso(1405, 8, 26)).toBe("2026-11-17");
  });

  it("maps Nowruz 1405 (2026-03-21) to 1405-01-01", () => {
    expect(isoToJalali("2026-03-21")).toEqual({ jy: 1405, jm: 1, jd: 1 });
    expect(jalaliToIso(1405, 1, 1)).toBe("2026-03-21");
  });

  it("round-trips across a leap day (Esfand 1403 has 30 days)", () => {
    expect(jalaliToIso(1403, 12, 30)).toBe("2025-03-20");
    expect(isoToJalali("2025-03-20")).toEqual({ jy: 1403, jm: 12, jd: 30 });
  });

  it("round-trips every day of 1405", () => {
    for (let m = 1; m <= 12; m++) {
      const len = m <= 6 ? 31 : m <= 11 ? 30 : 29; // 1405 is not a leap year
      for (let d = 1; d <= len; d++) {
        const iso = jalaliToIso(1405, m, d);
        expect(isoToJalali(iso)).toEqual({ jy: 1405, jm: m, jd: d });
      }
    }
  });
});

describe("parseIsoDate / isValidIsoDate", () => {
  it("parses strict ISO dates", () => {
    expect(parseIsoDate("2026-11-17")).toEqual({ gy: 2026, gm: 11, gd: 17 });
  });

  it("rejects ambiguous and malformed strings — never infers a calendar", () => {
    for (const bad of [
      "1405/08/26", // Jalali-looking, not ISO
      "11/17/2026",
      "17-11-2026",
      "2026-11-17T00:00:00Z", // a timestamp, not a date
      "2026-2-3", // unpadded
      "2026-02-30", // impossible day
      "2026-13-01",
      "",
      "۱۴۰۵-۰۸-۲۶", // Persian digits are not ISO
    ]) {
      expect(isValidIsoDate(bad)).toBe(false);
      expect(() => parseIsoDate(bad)).toThrow(RangeError);
    }
  });
});

describe("assertValidJalali", () => {
  it("accepts real Jalali dates", () => {
    expect(() => assertValidJalali(1405, 8, 26)).not.toThrow();
    expect(() => assertValidJalali(1403, 12, 30)).not.toThrow(); // leap year
  });

  it("rejects impossible Jalali dates", () => {
    expect(() => assertValidJalali(1405, 12, 30)).toThrow(RangeError); // 1405 not leap
    expect(() => assertValidJalali(1405, 13, 1)).toThrow(RangeError);
    expect(() => assertValidJalali(1405, 8, 0)).toThrow(RangeError);
  });
});

describe("formatIsoJalali", () => {
  it("formats with weekday", () => {
    expect(formatIsoJalali("2026-11-17", { weekday: true })).toBe("سه‌شنبه، ۲۶ آبان ۱۴۰۵");
  });

  it("formats without weekday or year", () => {
    expect(formatIsoJalali("2026-11-17")).toBe("۲۶ آبان ۱۴۰۵");
    expect(formatIsoJalali("2026-11-17", { year: false })).toBe("۲۶ آبان");
  });

  it("formats numeric Jalali", () => {
    expect(formatIsoJalaliNumeric("2026-11-17")).toBe("۱۴۰۵/۰۸/۲۶");
  });
});

describe("VibeFarsi Date helpers stay consistent with the ISO helpers", () => {
  it("toJalali agrees with isoToJalali for local noon", () => {
    const d = new Date(2026, 10, 17, 12, 0, 0); // local noon, Nov 17 2026
    expect(toJalali(d)).toEqual(isoToJalali("2026-11-17"));
    expect(formatJalali(d, { weekday: true })).toBe(formatIsoJalali("2026-11-17", { weekday: true }));
  });

  it("toGregorian agrees with jalaliToIso", () => {
    const d = toGregorian(1405, 8, 26);
    expect([d.getFullYear(), d.getMonth() + 1, d.getDate()]).toEqual([2026, 11, 17]);
  });
});
