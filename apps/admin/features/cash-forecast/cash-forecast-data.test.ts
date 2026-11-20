import { describe, expect, it } from "vitest";
import type { ForecastCalendarDayResponse } from "@/lib/api-client";
import {
  addDaysIso,
  calendarWeeks,
  compareJalaliMonths,
  forecastFetchRange,
  forecastSourceLabel,
  horizonBucketLabel,
  horizonBucketTotals,
  jalaliMonthLabel,
  jalaliMonthOfIso,
  jalaliMonthRange,
  shiftJalaliMonth,
} from "./cash-forecast-data";

function day(date: string, overrides: Partial<ForecastCalendarDayResponse> = {}): ForecastCalendarDayResponse {
  return {
    date,
    inflow_confirmed_irr: 0,
    outflow_confirmed_irr: 0,
    inflow_forecast_irr: 0,
    outflow_forecast_irr: 0,
    item_count: 0,
    ...overrides,
  };
}

describe("jalaliMonthRange", () => {
  it("returns the Gregorian span of Aban 1405 (30 days)", () => {
    expect(jalaliMonthRange({ jy: 1405, jm: 8 })).toEqual({
      fromIso: "2026-10-23",
      toIso: "2026-11-21",
    });
  });

  it("returns the Gregorian span of Esfand 1405 (29 days, non-leap)", () => {
    expect(jalaliMonthRange({ jy: 1405, jm: 12 })).toEqual({
      fromIso: "2027-02-19",
      toIso: "2027-03-20",
    });
  });
});

describe("shiftJalaliMonth", () => {
  it("shifts within a year", () => {
    expect(shiftJalaliMonth({ jy: 1405, jm: 8 }, 1)).toEqual({ jy: 1405, jm: 9 });
    expect(shiftJalaliMonth({ jy: 1405, jm: 8 }, -2)).toEqual({ jy: 1405, jm: 6 });
  });

  it("rolls over the year boundary", () => {
    expect(shiftJalaliMonth({ jy: 1405, jm: 12 }, 1)).toEqual({ jy: 1406, jm: 1 });
    expect(shiftJalaliMonth({ jy: 1405, jm: 1 }, -1)).toEqual({ jy: 1404, jm: 12 });
  });
});

describe("addDaysIso", () => {
  it("shifts across month and year boundaries", () => {
    expect(addDaysIso("2026-11-20", 120)).toBe("2027-03-20");
    expect(addDaysIso("2026-11-20", -180)).toBe("2026-05-24");
    expect(addDaysIso("2026-12-31", 1)).toBe("2027-01-01");
  });
});

describe("forecastFetchRange", () => {
  it("covers 180 days back and 120 days forward", () => {
    expect(forecastFetchRange("2026-11-20")).toEqual({
      fromIso: "2026-05-24",
      toIso: "2027-03-20",
    });
  });
});

describe("compareJalaliMonths", () => {
  it("orders months across the year boundary", () => {
    expect(compareJalaliMonths({ jy: 1405, jm: 8 }, { jy: 1405, jm: 9 })).toBeLessThan(0);
    expect(compareJalaliMonths({ jy: 1405, jm: 12 }, { jy: 1406, jm: 1 })).toBeLessThan(0);
    expect(compareJalaliMonths({ jy: 1405, jm: 8 }, { jy: 1405, jm: 8 })).toBe(0);
    expect(compareJalaliMonths({ jy: 1406, jm: 1 }, { jy: 1405, jm: 12 })).toBeGreaterThan(0);
  });
});

describe("jalaliMonthOfIso", () => {
  it("converts a Gregorian date to its Jalali month", () => {
    expect(jalaliMonthOfIso("2026-11-20")).toEqual({ jy: 1405, jm: 8 });
    expect(jalaliMonthOfIso("2026-11-22")).toEqual({ jy: 1405, jm: 9 });
  });
});

describe("jalaliMonthLabel", () => {
  it("labels a month key in Persian", () => {
    expect(jalaliMonthLabel("1405-08")).toBe("آبان ۱۴۰۵");
    expect(jalaliMonthLabel("1406-01")).toBe("فروردین ۱۴۰۶");
  });

  it("passes an invalid key through unchanged", () => {
    expect(jalaliMonthLabel("bogus")).toBe("bogus");
    expect(jalaliMonthLabel("1405-13")).toBe("1405-13");
  });
});

describe("forecastSourceLabel", () => {
  it("labels every source in Persian", () => {
    expect(forecastSourceLabel("settlement_tranche")).toBe("تسویهٔ درگاه");
    expect(forecastSourceLabel("installment_leg")).toBe("قسط فروش");
    expect(forecastSourceLabel("cheque_received")).toBe("چک دریافتی");
    expect(forecastSourceLabel("cheque_issued")).toBe("چک صادره");
    expect(forecastSourceLabel("invoice_receivable")).toBe("فاکتور مشتری");
    expect(forecastSourceLabel("invoice_payable")).toBe("فاکتور تأمین‌کننده");
    expect(forecastSourceLabel("packaging_purchase")).toBe("خرید بسته‌بندی");
    expect(forecastSourceLabel("expected_sale")).toBe("فروش موردانتظار");
  });
});

describe("horizonBucketLabel", () => {
  it("labels every horizon bucket in Persian", () => {
    expect(horizonBucketLabel("overdue")).toBe("سررسید گذشته");
    expect(horizonBucketLabel("0_30")).toBe("۰ تا ۳۰ روز آینده");
    expect(horizonBucketLabel("31_60")).toBe("۳۱ تا ۶۰ روز آینده");
    expect(horizonBucketLabel("61_90")).toBe("۶۱ تا ۹۰ روز آینده");
    expect(horizonBucketLabel("91_120")).toBe("۹۱ تا ۱۲۰ روز آینده");
    expect(horizonBucketLabel("beyond_120")).toBe("بیش از ۱۲۰ روز");
  });
});

describe("horizonBucketTotals", () => {
  it("sums both columns", () => {
    expect(
      horizonBucketTotals({
        key: "0_30",
        from_days: 0,
        to_days: 30,
        inflow_confirmed_irr: 1_000_000,
        outflow_confirmed_irr: 400_000,
        inflow_forecast_irr: 200_000,
        outflow_forecast_irr: 100_000,
        item_count: 4,
      }),
    ).toEqual({ inflow: 1_200_000, outflow: 500_000, net: 700_000 });
  });
});

describe("calendarWeeks", () => {
  it("returns no weeks for an empty month", () => {
    expect(calendarWeeks([])).toEqual([]);
  });

  it("lays Aban 1405 out Saturday-first with leading blanks", () => {
    // Aban 1405 starts Friday 2026-10-23 (weekday 6) — six leading blanks.
    const realDays: ForecastCalendarDayResponse[] = [];
    const start = new Date("2026-10-23T00:00:00Z");
    for (let i = 0; i < 30; i++) {
      const d = new Date(start.getTime() + i * 86_400_000);
      realDays.push(day(d.toISOString().slice(0, 10)));
    }
    const weeks = calendarWeeks(realDays);
    expect(weeks).toHaveLength(6);
    for (const week of weeks) {
      expect(week).toHaveLength(7);
    }
    expect(weeks[0].slice(0, 6).every((cell) => cell.kind === "blank")).toBe(true);
    const firstDay = weeks[0][6];
    expect(firstDay.kind).toBe("day");
    if (firstDay.kind === "day") {
      expect(firstDay.jalaliDay).toBe(1);
      expect(firstDay.day.date).toBe("2026-10-23");
    }
    const lastWeek = weeks[weeks.length - 1];
    const lastDay = lastWeek.find((cell) => cell.kind === "day" && cell.jalaliDay === 30);
    expect(lastDay).toBeDefined();
  });

  it("carries the day totals into the cells", () => {
    const days = [day("2026-11-22", { inflow_confirmed_irr: 5_000_000, item_count: 2 })];
    const weeks = calendarWeeks(days);
    const cell = weeks.flat().find((c) => c.kind === "day");
    expect(cell?.kind).toBe("day");
    if (cell?.kind === "day") {
      expect(cell.day.inflow_confirmed_irr).toBe(5_000_000);
      expect(cell.day.item_count).toBe(2);
      expect(cell.jalaliDay).toBe(1);
    }
  });
});
