/**
 * Pure-helper tests for the cheque center (US-535).
 *
 * Jalali fixtures are fixed strings, never "today": the due-bucket,
 * forecast and reminder tests pass an explicit `todayJalali` so they
 * do not depend on the calendar date the suite runs on.
 */

import { describe, expect, it } from "vitest";
import type { ChequeResponse } from "@/lib/api-client";
import {
  addJalaliDays,
  cashForecast,
  clearingAgeBucket,
  daysToDue,
  dueBucket,
  formatJalaliLong,
  formatJalaliShort,
  intakeReminderState,
  isValidJalaliString,
  jalaliDaysBetween,
  lifecycleSteps,
  sumIrr,
} from "./cheque-data";

function cheque(overrides: Partial<ChequeResponse>): ChequeResponse {
  return {
    id: "chk-1",
    state: "held",
    kind: "received",
    purpose: "payment",
    sayad_id: "1234567890123456",
    sayad_status: "registered",
    sayad_unconfirmed: false,
    amount_irr: 10000000,
    amount_toman: 1000000,
    issue_date: "1405-08-01",
    due_date: "1405-08-28",
    issuer_name: "مشتری آزمایشی",
    issuer_national_id: "0012345678",
    issuer_bank: "056 سامان",
    payee_name: "فروشگاه",
    payee_national_id: "0012345679",
    order_id: "ORD-1",
    customer_ref: "CUST-1",
    leaf_number: "123456",
    issuer_branch: null,
    issuer_account_number: null,
    reason: null,
    invoice_id: null,
    installment_leg_id: null,
    installment_leg_status: null,
    scan_url: null,
    scan_ref: null,
    scan_filestore_ref: null,
    source_event_id: null,
    age_days: 2,
    storage_location: "vault",
    engine_id: null,
    move_ids: {},
    destination_bank_account_id: null,
    destination_bank_name: null,
    deposit_date: null,
    clearing_confidence: null,
    clearing_statement_ref: null,
    clearing_reconciliation_id: null,
    issuer_color_code: null,
    issuer_back_cheques: null,
    bounce_reason: null,
    bounce_date: null,
    bounce_certificate_reference: null,
    endorsee_name: null,
    endorsee_type: null,
    sayad_transfer_reference: null,
    checkbook_engine_id: null,
    secured_obligation: null,
    void_reason: null,
    call_in_approved_by: null,
    call_in_approved_at: null,
    unexpected_call_in: false,
    call_in_statement_reference: null,
    journal_pending_review: false,
    receipt_missing_warning: false,
    mismatch_detail: null,
    warnings: [],
    created_at: "2026-11-19T00:00:00Z",
    updated_at: "2026-11-19T00:00:00Z",
    ...overrides,
  };
}

describe("isValidJalaliString", () => {
  it("accepts a real Jalali date and rejects junk", () => {
    expect(isValidJalaliString("1405-08-28")).toBe(true);
    expect(isValidJalaliString("1405-13-01")).toBe(false);
    expect(isValidJalaliString("1405-08-32")).toBe(false);
    expect(isValidJalaliString("1405-00-10")).toBe(false);
    expect(isValidJalaliString("")).toBe(false);
    expect(isValidJalaliString(null)).toBe(false);
  });
});

describe("jalaliDaysBetween / daysToDue", () => {
  it("counts days across a Jalali month boundary", () => {
    // آبان has 30 days: 1405-08-28 -> 1405-09-05 is 7 days.
    expect(jalaliDaysBetween("1405-08-28", "1405-09-05")).toBe(7);
    expect(jalaliDaysBetween("1405-09-05", "1405-08-28")).toBe(-7);
    expect(jalaliDaysBetween("1405-08-28", "1405-08-28")).toBe(0);
  });

  it("is negative for an overdue due date", () => {
    expect(daysToDue("1405-08-20", "1405-08-28")).toBe(-8);
    expect(daysToDue("1405-08-28", "1405-08-28")).toBe(0);
  });
});

describe("addJalaliDays", () => {
  it("rolls over the month", () => {
    expect(addJalaliDays("1405-08-28", 7)).toBe("1405-09-05");
    expect(addJalaliDays("1405-08-28", 0)).toBe("1405-08-28");
  });
});

describe("formatJalaliShort / formatJalaliLong", () => {
  it("renders Persian digits and the month name", () => {
    expect(formatJalaliShort("1405-08-28")).toBe("۱۴۰۵/۰۸/۲۸");
    expect(formatJalaliLong("1405-08-28")).toBe("۲۸ آبان ۱۴۰۵");
    expect(formatJalaliShort(null)).toBe("—");
    expect(formatJalaliLong("not-a-date")).toBe("not-a-date");
  });
});

describe("dueBucket", () => {
  const today = "1405-08-28";
  it("buckets overdue / 7 / 14 / 30 / future", () => {
    expect(dueBucket("1405-08-20", today)).toBe("overdue");
    expect(dueBucket("1405-08-28", today)).toBe("due-7");
    expect(dueBucket("1405-09-05", today)).toBe("due-7");
    expect(dueBucket("1405-09-06", today)).toBe("due-14");
    expect(dueBucket("1405-09-12", today)).toBe("due-14");
    expect(dueBucket("1405-09-13", today)).toBe("due-30");
    expect(dueBucket("1405-09-28", today)).toBe("due-30");
    expect(dueBucket("1405-09-29", today)).toBe("future");
  });
});

describe("clearingAgeBucket", () => {
  it("buckets 0–7 / 8–14 / 15+", () => {
    expect(clearingAgeBucket(0)).toBe("0-7");
    expect(clearingAgeBucket(7)).toBe("0-7");
    expect(clearingAgeBucket(8)).toBe("8-14");
    expect(clearingAgeBucket(14)).toBe("8-14");
    expect(clearingAgeBucket(15)).toBe("15+");
  });
});

describe("intakeReminderState", () => {
  it("fires day-3, then day-(N-1), then expiry at the deadline (N=7)", () => {
    expect(intakeReminderState(0, 7)).toBe("waiting");
    expect(intakeReminderState(2, 7)).toBe("waiting");
    expect(intakeReminderState(3, 7)).toBe("day-3");
    expect(intakeReminderState(5, 7)).toBe("day-3");
    expect(intakeReminderState(6, 7)).toBe("day-n-minus-1");
    expect(intakeReminderState(7, 7)).toBe("expired");
    expect(intakeReminderState(9, 7)).toBe("expired");
  });
});

describe("sumIrr", () => {
  it("sums integer Rial without float drift", () => {
    expect(sumIrr([cheque({}), cheque({ amount_irr: 5000000 })])).toBe(15000000);
    expect(sumIrr([])).toBe(0);
  });
});

describe("cashForecast", () => {
  it("puts held received cheques in inflow weeks and issued cheques in outflow weeks", () => {
    const today = "1405-08-28";
    const rows = [
      cheque({ id: "a", due_date: "1405-08-30", amount_irr: 10000000, state: "held" }),
      cheque({ id: "b", due_date: "1405-09-10", amount_irr: 20000000, state: "in_clearing", kind: "received" }),
      cheque({ id: "c", due_date: "1405-09-02", amount_irr: 5000000, state: "issued", kind: "issued" }),
      // Collateral never enters the forecast, even when held.
      cheque({ id: "d", due_date: "1405-08-30", amount_irr: 999000000, purpose: "collateral", state: "held" }),
      // Cleared / overdue cheques are out of the 12-week window.
      cheque({ id: "e", due_date: "1405-08-30", amount_irr: 7000000, state: "cleared" }),
      cheque({ id: "f", due_date: "1405-08-20", amount_irr: 7000000, state: "held" }),
    ];
    const weeks = cashForecast(rows, today);
    expect(weeks).toHaveLength(12);
    expect(weeks[0].inflowIrr).toBe(10000000);
    expect(weeks[0].inflowCount).toBe(1);
    expect(weeks[0].outflowIrr).toBe(5000000);
    expect(weeks[1].inflowIrr).toBe(20000000);
    expect(weeks.slice(2).every((w) => w.inflowIrr === 0 && w.outflowIrr === 0)).toBe(true);
  });
});

describe("lifecycleSteps", () => {
  it("marks registered as the only done step for a registered cheque", () => {
    const steps = lifecycleSteps(cheque({ state: "registered" }));
    expect(steps[0]).toMatchObject({ key: "registered", done: true });
    expect(steps[1]).toMatchObject({ key: "held", done: false });
  });

  it("marks the terminal step for a bounced cheque", () => {
    const steps = lifecycleSteps(cheque({ state: "bounced", bounce_date: "1405-08-27" }));
    expect(steps.some((s) => s.key === "bounced" && s.done && s.date === "1405-08-27")).toBe(true);
    expect(steps.some((s) => s.key === "in_clearing" && s.done)).toBe(true);
  });

  it("shows the issued step (not clearing) for an issued cheque", () => {
    const steps = lifecycleSteps(cheque({ state: "issued", kind: "issued" }));
    expect(steps.map((s) => s.key)).toContain("issued");
    expect(steps.map((s) => s.key)).not.toContain("in_clearing");
  });
});
