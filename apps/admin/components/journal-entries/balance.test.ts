import { describe, expect, it } from "vitest";
import {
  emptyLines,
  fromDraftResponse,
  lineTotals,
  setLineAmount,
  toDraftLineRequests,
  validateLines,
  type JournalLineDraft,
} from "@/features/journal-entries/lib/balance";
import type { JournalDraftResponse } from "@/lib/api-client";

function line(overrides: Partial<JournalLineDraft> = {}): JournalLineDraft {
  return {
    key: `test-${Math.random().toString(36).slice(2)}`,
    accountId: 1,
    debit: 0,
    credit: 0,
    description: "",
    ...overrides,
  };
}

describe("lineTotals", () => {
  it("sums debit and credit and reports the difference", () => {
    const totals = lineTotals([
      line({ debit: 1_000_000 }),
      line({ credit: 600_000 }),
      line({ credit: 400_000 }),
    ]);
    expect(totals.totalDebit).toBe(1_000_000);
    expect(totals.totalCredit).toBe(1_000_000);
    expect(totals.difference).toBe(0);
    expect(totals.balanced).toBe(true);
  });

  it("is unbalanced when the totals differ", () => {
    const totals = lineTotals([line({ debit: 500 }), line({ credit: 400 })]);
    expect(totals.difference).toBe(100);
    expect(totals.balanced).toBe(false);
  });

  it("is unbalanced when both totals are zero", () => {
    const totals = lineTotals([line(), line()]);
    expect(totals.balanced).toBe(false);
  });
});

describe("validateLines", () => {
  it("accepts a balanced grid with accounts on every line", () => {
    const result = validateLines([
      line({ accountId: 10, debit: 250_000 }),
      line({ accountId: 20, credit: 250_000 }),
    ]);
    expect(result.valid).toBe(true);
    expect(result.errors).toEqual([]);
  });

  it("rejects fewer than two lines", () => {
    const result = validateLines([line({ debit: 100 })]);
    expect(result.valid).toBe(false);
    expect(result.errors).toContain("سند باید دست‌کم دو ردیف داشته باشد.");
  });

  it("rejects a line without an account", () => {
    const result = validateLines([
      line({ accountId: null, debit: 100 }),
      line({ credit: 100 }),
    ]);
    expect(result.valid).toBe(false);
    expect(result.errors).toContain("برای هر ردیف یک حساب انتخاب کنید.");
  });

  it("rejects a two-sided line", () => {
    const result = validateLines([
      line({ debit: 100, credit: 100 }),
      line({ credit: 100 }),
    ]);
    expect(result.valid).toBe(false);
    expect(result.errors).toContain("هر ردیف فقط یک طرف دارد: یا بدهکار یا بستانکار، نه هر دو.");
  });

  it("rejects an empty line", () => {
    const result = validateLines([line({ debit: 100 }), line()]);
    expect(result.valid).toBe(false);
    expect(result.errors).toContain("هر ردیف باید یک مبلغ بدهکار یا بستانکار داشته باشد.");
  });

  it("rejects unbalanced totals", () => {
    const result = validateLines([line({ debit: 100 }), line({ credit: 90 })]);
    expect(result.valid).toBe(false);
    expect(result.errors).toContain("مجموع بدهکار باید با مجموع بستانکار برابر باشد.");
  });

  it("rejects a zero-total grid", () => {
    const result = validateLines([line(), line()]);
    expect(result.valid).toBe(false);
    expect(result.errors).toContain("مجموع سند باید بزرگ‌تر از صفر باشد.");
  });
});

describe("setLineAmount", () => {
  it("setting a debit clears the credit (one-sided)", () => {
    const next = setLineAmount(line({ credit: 500 }), "debit", 700);
    expect(next.debit).toBe(700);
    expect(next.credit).toBe(0);
  });

  it("setting a credit clears the debit (one-sided)", () => {
    const next = setLineAmount(line({ debit: 500 }), "credit", 700);
    expect(next.credit).toBe(700);
    expect(next.debit).toBe(0);
  });

  it("clearing an amount to null leaves the other side alone", () => {
    const next = setLineAmount(line({ debit: 500 }), "debit", null);
    expect(next.debit).toBe(0);
    expect(next.credit).toBe(0);
  });

  it("clamps negative amounts to zero", () => {
    expect(setLineAmount(line(), "debit", -50).debit).toBe(0);
  });
});

describe("emptyLines / toDraftLineRequests / fromDraftResponse", () => {
  it("starts a new draft with two empty rows", () => {
    const rows = emptyLines();
    expect(rows).toHaveLength(2);
    expect(rows[0].key).not.toBe(rows[1].key);
  });

  it("maps grid rows to API payloads", () => {
    const requests = toDraftLineRequests([
      line({ accountId: 7, debit: 1000, description: "نقد" }),
      line({ accountId: 9, credit: 1000 }),
    ]);
    expect(requests).toEqual([
      { account_id: 7, debit: 1000, credit: 0, description: "نقد" },
      { account_id: 9, debit: 0, credit: 1000, description: "" },
    ]);
  });

  it("round-trips a saved draft back into editable rows", () => {
    const draft = {
      lines: [
        { account_id: 7, debit: 1000, credit: 0, description: "نقد", state: "draft" },
        { account_id: 9, debit: 0, credit: 1000, description: "", state: "draft" },
      ],
    } as JournalDraftResponse;
    const rows = fromDraftResponse(draft);
    expect(rows).toHaveLength(2);
    expect(rows[0].accountId).toBe(7);
    expect(rows[0].debit).toBe(1000);
    expect(rows[1].credit).toBe(1000);
    expect(validateLines(rows).valid).toBe(true);
  });
});
