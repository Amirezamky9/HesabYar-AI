import { describe, expect, it } from "vitest";
import type { InvoiceResponse } from "@/lib/api-client";
import { filterInvoicesByQuery } from "./invoice-filters";
import { invoicesToCsv } from "./invoice-csv";

function makeInvoice(overrides: Partial<InvoiceResponse>): InvoiceResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    invoice_type: "out",
    party_id: 10,
    party_name: "علی کریمی",
    number: "INV-1405-001",
    invoice_date: "2026-11-17",
    accounting_date: null,
    state: "draft",
    description: "فروش کالا",
    reference: "ORD-9",
    journal_id: null,
    account_id: 3,
    currency: "IRR",
    unit: "rial",
    untaxed_amount: 1000000,
    tax_amount: 0,
    total_amount: 1000000,
    lines: [],
    ...overrides,
  };
}

describe("filterInvoicesByQuery", () => {
  const invoices = [
    makeInvoice({ id: 1, party_name: "علی کریمی", number: "INV-001" }),
    makeInvoice({ id: 2, party_name: "مریم رضایی", number: "INV-002", reference: "سفارش ویژه" }),
  ];

  it("returns everything on an empty query", () => {
    expect(filterInvoicesByQuery(invoices, "")).toHaveLength(2);
  });

  it("matches the party name with normalized ی/ک", () => {
    expect(filterInvoicesByQuery(invoices, "على كريمى")).toHaveLength(1);
    expect(filterInvoicesByQuery(invoices, "على كريمى")[0].id).toBe(1);
  });

  it("matches the invoice number and reference", () => {
    expect(filterInvoicesByQuery(invoices, "INV-002")).toHaveLength(1);
    expect(filterInvoicesByQuery(invoices, "سفارش ویژه")).toHaveLength(1);
  });

  it("matches Persian digits typed as ASCII", () => {
    const withDigits = [makeInvoice({ id: 3, party_name: "مشتری ۱۲۳" })];
    expect(filterInvoicesByQuery(withDigits, "123")).toHaveLength(1);
  });

  it("returns an empty list when nothing matches", () => {
    expect(filterInvoicesByQuery(invoices, "ناموجود")).toHaveLength(0);
  });
});

describe("invoicesToCsv", () => {
  it("exports normalized values: integer IRR, ISO date, English codes", () => {
    const csv = invoicesToCsv([
      makeInvoice({ id: 5, number: "INV-005", total_amount: 12500000, tax_amount: 500000 }),
    ]);
    expect(csv).not.toMatch(/[۰-۹]/);
    expect(csv).toContain("5,INV-005,out,علی کریمی,2026-11-17,draft,1000000,500000,12500000");
  });
});
