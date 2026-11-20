import { describe, expect, it } from "vitest";
import { formatIsoDate } from "@/lib/format-date";
import {
  isValidIsoDate,
  isoToJalali,
  jalaliToIso,
  todayIso,
} from "@/lib/jalali";

/**
 * Jalali date handling for the journal pages: the editor stores
 * Gregorian ISO and displays Jalali; the list renders Jalali from the
 * stored ISO. Conversions are deterministic (lib/jalali), never
 * inferred from an ambiguous string.
 */
describe("journal Jalali date handling", () => {
  it("converts the known 2026-11-17 date to ۲۶ آبان ۱۴۰۵", () => {
    expect(isoToJalali("2026-11-17")).toEqual({ jy: 1405, jm: 8, jd: 26 });
    expect(jalaliToIso(1405, 8, 26)).toBe("2026-11-17");
    expect(formatIsoDate("2026-11-17")).toBe("۲۶ آبان ۱۴۰۵");
  });

  it("round-trips Gregorian <-> Jalali across year boundaries", () => {
    const dates = ["2025-03-21", "2026-03-20", "2026-03-21", "2026-11-17", "2027-01-01"];
    for (const iso of dates) {
      const { jy, jm, jd } = isoToJalali(iso);
      expect(jalaliToIso(jy, jm, jd)).toBe(iso);
    }
  });

  it("rejects invalid ISO dates before they reach the API", () => {
    expect(isValidIsoDate("2026-13-01")).toBe(false);
    expect(isValidIsoDate("2026-02-30")).toBe(false);
    // A Jalali-looking string with slashes is not a strict ISO date.
    expect(isValidIsoDate("1405/08/26")).toBe(false);
    expect(isValidIsoDate("2026-11-17")).toBe(true);
  });

  it("defaults a new draft's date to a valid today in Asia/Tehran", () => {
    const today = todayIso();
    expect(isValidIsoDate(today)).toBe(true);
    // The displayed Jalali date is derived from the same ISO value.
    expect(formatIsoDate(today)).not.toBe("");
  });
});
