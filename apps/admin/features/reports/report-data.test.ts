import { describe, expect, it } from "vitest";
import type { InvoiceResponse, PaymentResponse } from "@/lib/api-client";
import {
  invoicePeriod,
  paymentPeriod,
  sumInvoiceTotals,
  sumPaymentTotals,
  summarizeInvoicesByParty,
  summarizeInvoicesByState,
  summarizePaymentsByKind,
  summarizePaymentsByState,
} from "./report-data";

function invoice(overrides: Partial<InvoiceResponse>): InvoiceResponse {
  return {
    id: 1,
    company_id: "c1",
    engine_company_id: 1,
    invoice_type: "out",
    party_id: 12,
    party_name: "مشتری تست",
    number: "INV-1",
    invoice_date: "2026-11-17",
    accounting_date: "2026-11-17",
    state: "draft",
    description: "",
    reference: "",
    journal_id: null,
    account_id: 1,
    currency: "IRR",
    untaxed_amount: 1000000,
    tax_amount: 90000,
    total_amount: 1090000,
    lines: [],
    ...overrides,
  };
}

function payment(overrides: Partial<PaymentResponse>): PaymentResponse {
  return {
    id: 7,
    company_id: "c1",
    engine_company_id: 1,
    number: "PAY-1",
    kind: "receivable",
    party_id: 12,
    party_name: "مشتری تست",
    date: "2026-11-17",
    amount: 500000,
    state: "draft",
    reference: "",
    journal_name: "بانک",
    currency: "IRR",
    ...overrides,
  };
}

describe("summarizeInvoicesByState", () => {
  it("groups by state with raw sums and canonical order", () => {
    const rows = summarizeInvoicesByState([
      invoice({ id: 1, state: "posted", untaxed_amount: 100, tax_amount: 10, total_amount: 110 }),
      invoice({ id: 2, state: "draft", untaxed_amount: 200, tax_amount: 20, total_amount: 220 }),
      invoice({ id: 3, state: "posted", untaxed_amount: 300, tax_amount: 30, total_amount: 330 }),
    ]);
    expect(rows).toEqual([
      { state: "draft", count: 1, untaxedIrr: 200, taxIrr: 20, totalIrr: 220 },
      { state: "posted", count: 2, untaxedIrr: 400, taxIrr: 40, totalIrr: 440 },
    ]);
  });

  it("returns an empty list for no invoices", () => {
    expect(summarizeInvoicesByState([])).toEqual([]);
  });

  it("puts unknown states after the known ones", () => {
    const rows = summarizeInvoicesByState([
      invoice({ id: 1, state: "weird" }),
      invoice({ id: 2, state: "draft" }),
    ]);
    expect(rows.map((r) => r.state)).toEqual(["draft", "weird"]);
  });
});

describe("summarizeInvoicesByParty", () => {
  it("groups by party, largest total first", () => {
    const rows = summarizeInvoicesByParty([
      invoice({ id: 1, party_id: 12, party_name: "الف", total_amount: 100 }),
      invoice({ id: 2, party_id: 13, party_name: "ب", total_amount: 500 }),
      invoice({ id: 3, party_id: 12, party_name: "الف", total_amount: 200 }),
    ]);
    expect(rows).toEqual([
      { partyId: 13, partyName: "ب", count: 1, totalIrr: 500 },
      { partyId: 12, partyName: "الف", count: 2, totalIrr: 300 },
    ]);
  });

  it("returns an empty list for no invoices", () => {
    expect(summarizeInvoicesByParty([])).toEqual([]);
  });
});

describe("summarizePaymentsByState / summarizePaymentsByKind", () => {
  it("groups payments by state in canonical order", () => {
    const rows = summarizePaymentsByState([
      payment({ id: 1, state: "succeeded", amount: 100 }),
      payment({ id: 2, state: "draft", amount: 200 }),
      payment({ id: 3, state: "succeeded", amount: 300 }),
    ]);
    expect(rows).toEqual([
      { state: "draft", count: 1, totalIrr: 200 },
      { state: "succeeded", count: 2, totalIrr: 400 },
    ]);
  });

  it("groups payments by kind, receivable first", () => {
    const rows = summarizePaymentsByKind([
      payment({ id: 1, kind: "payable", amount: 100 }),
      payment({ id: 2, kind: "receivable", amount: 200 }),
    ]);
    expect(rows).toEqual([
      { kind: "receivable", count: 1, totalIrr: 200 },
      { kind: "payable", count: 1, totalIrr: 100 },
    ]);
  });

  it("returns empty lists for no payments", () => {
    expect(summarizePaymentsByState([])).toEqual([]);
    expect(summarizePaymentsByKind([])).toEqual([]);
  });
});

describe("report periods", () => {
  it("invoice period spans invoice dates, falling back to accounting dates", () => {
    const period = invoicePeriod([
      invoice({ invoice_date: "2026-11-10", accounting_date: "2026-11-09" }),
      invoice({ invoice_date: null, accounting_date: "2026-11-17" }),
      invoice({ invoice_date: "2026-11-12", accounting_date: null }),
    ]);
    expect(period).toEqual({ fromIso: "2026-11-10", toIso: "2026-11-17" });
  });

  it("payment period spans payment dates", () => {
    expect(paymentPeriod([payment({ date: "2026-11-05" }), payment({ date: "2026-11-17" })])).toEqual({
      fromIso: "2026-11-05",
      toIso: "2026-11-17",
    });
  });

  it("empty lists give a null period", () => {
    expect(invoicePeriod([])).toEqual({ fromIso: null, toIso: null });
    expect(paymentPeriod([])).toEqual({ fromIso: null, toIso: null });
  });
});

describe("totals", () => {
  it("sums invoice and payment amounts as integer IRR", () => {
    expect(sumInvoiceTotals([invoice({ total_amount: 100 }), invoice({ total_amount: 200 })])).toBe(300);
    expect(sumPaymentTotals([payment({ amount: 50 }), payment({ amount: 70 })])).toBe(120);
  });
});
