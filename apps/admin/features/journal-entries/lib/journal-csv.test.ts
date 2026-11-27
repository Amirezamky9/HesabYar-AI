import { describe, expect, it } from "vitest";
import type { JournalDraftResponse } from "@/lib/api-client";
import { journalsToCsv } from "./journal-csv";

function draft(overrides: Partial<JournalDraftResponse>): JournalDraftResponse {
  return {
    id: 1,
    company_id: "c1",
    engine_company_id: 1,
    journal_id: 1,
    period: { name: "۱۴۰۵/۰۸", start_date: "2026-10-23", end_date: "2026-11-21", state: "open" },
    date: "2026-11-17",
    state: "draft",
    description: "سند تست",
    lines: [],
    total_debit: 1000000,
    total_credit: 1000000,
    ...overrides,
  };
}

describe("journalsToCsv", () => {
  it("exports normalized values with Persian headers", () => {
    const csv = journalsToCsv([
      draft({ id: 5, description: "سند اول", total_debit: 100, total_credit: 100 }),
      draft({ id: 6, state: "posted", description: "سند دوم", total_debit: 200, total_credit: 200 }),
    ]);
    expect(csv).toBe(
      "\uFEFFشناسه,تاریخ,شرح,دوره,وضعیت,بدهکار (ریال),بستانکار (ریال)\r\n" +
        "5,2026-11-17,سند اول,۱۴۰۵/۰۸,draft,100,100\r\n" +
        "6,2026-11-17,سند دوم,۱۴۰۵/۰۸,posted,200,200\r\n",
    );
  });

  it("exports only the header row when the list is empty", () => {
    expect(journalsToCsv([])).toBe(
      "\uFEFFشناسه,تاریخ,شرح,دوره,وضعیت,بدهکار (ریال),بستانکار (ریال)\r\n",
    );
  });
});
