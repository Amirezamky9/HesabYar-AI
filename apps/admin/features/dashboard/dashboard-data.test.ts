import { describe, expect, it } from "vitest";
import type { InvoiceResponse, JournalDraftResponse, Paginated, PaymentResponse } from "@/lib/api-client";
import {
  fetchAllPages,
  recentInvoices,
  recentPayments,
  summarizeDrafts,
  summarizeInvoices,
} from "./dashboard-data";

function invoice(partial: Partial<InvoiceResponse>): InvoiceResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    invoice_type: "out",
    party_id: 1,
    party_name: "مشتری نمونه",
    number: "1",
    invoice_date: "2026-11-01",
    accounting_date: "2026-11-01",
    state: "posted",
    description: "",
    reference: "",
    journal_id: null,
    account_id: 1,
    currency: "IRR",
    untaxed_amount: 1000,
    tax_amount: 0,
    total_amount: 1000,
    lines: [],
    ...partial,
  };
}

function draft(partial: Partial<JournalDraftResponse>): JournalDraftResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    journal_id: 1,
    period: { name: "2026-11", start_date: "2026-11-01", end_date: "2026-11-30", state: "open" },
    date: "2026-11-01",
    state: "draft",
    description: "",
    lines: [],
    total_debit: 1000,
    total_credit: 1000,
    ...partial,
  };
}

function payment(partial: Partial<PaymentResponse>): PaymentResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    number: "1",
    kind: "receivable",
    party_id: 1,
    party_name: "مشتری نمونه",
    date: "2026-11-01",
    amount: 1000,
    state: "posted",
    reference: "",
    journal_name: "نقد",
    currency: "IRR",
    ...partial,
  };
}

describe("summarizeInvoices", () => {
  it("returns honest zeros for an empty list", () => {
    expect(summarizeInvoices([])).toEqual({
      receivableCount: 0,
      receivableTotalIrr: 0,
      payableCount: 0,
      payableTotalIrr: 0,
      consideredCount: 0,
    });
  });

  it("counts only validated/posted out invoices as receivables", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "out", state: "posted", total_amount: 10_000 }),
      invoice({ id: 2, invoice_type: "out", state: "validated", total_amount: 5_000 }),
      invoice({ id: 3, invoice_type: "out", state: "draft", total_amount: 99_000 }),
      invoice({ id: 4, invoice_type: "out", state: "paid", total_amount: 99_000 }),
      invoice({ id: 5, invoice_type: "out", state: "cancelled", total_amount: 99_000 }),
    ]);
    expect(summary.receivableCount).toBe(2);
    expect(summary.receivableTotalIrr).toBe(15_000);
    expect(summary.payableCount).toBe(0);
    expect(summary.consideredCount).toBe(5);
  });

  it("counts only validated/posted in invoices as payables", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "in", state: "posted", total_amount: 7_000 }),
      invoice({ id: 2, invoice_type: "in", state: "draft", total_amount: 99_000 }),
    ]);
    expect(summary.payableCount).toBe(1);
    expect(summary.payableTotalIrr).toBe(7_000);
    expect(summary.receivableCount).toBe(0);
  });

  it("subtracts credit notes from the matching side", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "out", state: "posted", total_amount: 10_000 }),
      invoice({ id: 2, invoice_type: "out_credit_note", state: "posted", total_amount: 2_000 }),
      invoice({ id: 3, invoice_type: "in", state: "posted", total_amount: 8_000 }),
      invoice({ id: 4, invoice_type: "in_credit_note", state: "posted", total_amount: 3_000 }),
    ]);
    expect(summary.receivableTotalIrr).toBe(8_000);
    expect(summary.payableTotalIrr).toBe(5_000);
    // Credit notes reduce the total but are not counted as open invoices.
    expect(summary.receivableCount).toBe(1);
    expect(summary.payableCount).toBe(1);
  });

  it("ignores unknown invoice types instead of counting them", () => {
    const summary = summarizeInvoices([
      invoice({ id: 1, invoice_type: "mystery", state: "posted", total_amount: 50_000 }),
    ]);
    expect(summary.receivableTotalIrr).toBe(0);
    expect(summary.payableTotalIrr).toBe(0);
  });
});

describe("summarizeDrafts", () => {
  it("sums integer IRR debit/credit across drafts", () => {
    expect(
      summarizeDrafts([
        draft({ id: 1, total_debit: 10_000, total_credit: 10_000 }),
        draft({ id: 2, total_debit: 2_500, total_credit: 2_500 }),
      ]),
    ).toEqual({ count: 2, totalDebitIrr: 12_500, totalCreditIrr: 12_500 });
  });

  it("returns honest zeros for an empty list", () => {
    expect(summarizeDrafts([])).toEqual({ count: 0, totalDebitIrr: 0, totalCreditIrr: 0 });
  });
});

describe("recentInvoices / recentPayments", () => {
  it("sorts by normalized ISO date descending, id descending on ties", () => {
    const sorted = recentInvoices(
      [
        invoice({ id: 1, invoice_date: "2026-11-01" }),
        invoice({ id: 2, invoice_date: "2026-11-03" }),
        invoice({ id: 3, invoice_date: "2026-11-03" }),
        invoice({ id: 4, invoice_date: null, accounting_date: "2026-10-01" }),
      ],
      10,
    );
    expect(sorted.map((i) => i.id)).toEqual([3, 2, 1, 4]);
  });

  it("respects the limit", () => {
    const sorted = recentPayments(
      [
        payment({ id: 1, date: "2026-11-01" }),
        payment({ id: 2, date: "2026-11-02" }),
        payment({ id: 3, date: "2026-11-03" }),
      ],
      2,
    );
    expect(sorted.map((p) => p.id)).toEqual([3, 2]);
  });
});

describe("fetchAllPages", () => {
  it("follows cursors until the list ends", async () => {
    const pages: Paginated<number>[] = [
      { items: [1, 2], next_cursor: "c2" },
      { items: [3], next_cursor: null },
    ];
    const seen: (string | undefined)[] = [];
    const result = await fetchAllPages<number>(async (cursor) => {
      seen.push(cursor);
      return pages[cursor === undefined ? 0 : 1];
    });
    expect(result).toEqual({ items: [1, 2, 3], truncated: false });
    expect(seen).toEqual([undefined, "c2"]);
  });

  it("reports truncated when the page cap stops paging", async () => {
    const result = await fetchAllPages<number>(
      async () => ({ items: [1], next_cursor: "more" }),
      { maxPages: 2 },
    );
    expect(result.truncated).toBe(true);
    expect(result.items).toEqual([1, 1]);
  });

  it("reports truncated when the item cap is hit", async () => {
    const result = await fetchAllPages<number>(
      async () => ({ items: [1, 2, 3], next_cursor: "more" }),
      { maxItems: 2 },
    );
    expect(result.truncated).toBe(true);
    expect(result.items).toEqual([1, 2]);
  });

  it("is not truncated when the last page ends the list exactly at the cap", async () => {
    const result = await fetchAllPages<number>(
      async (cursor) =>
        cursor === undefined
          ? { items: [1], next_cursor: "c2" }
          : { items: [2], next_cursor: null },
      { maxPages: 2 },
    );
    expect(result).toEqual({ items: [1, 2], truncated: false });
  });
});
