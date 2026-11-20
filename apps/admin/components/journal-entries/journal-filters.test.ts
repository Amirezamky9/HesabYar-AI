import { describe, expect, it } from "vitest";
import {
  filterAccounts,
  filterJournals,
  normalizeSearchText,
  periodOptions,
} from "@/features/journal-entries/lib/journal-filters";
import type { AccountResponse, JournalDraftResponse } from "@/lib/api-client";

function draft(overrides: Partial<JournalDraftResponse> = {}): JournalDraftResponse {
  return {
    id: 1,
    company_id: "company-1",
    engine_company_id: 1,
    journal_id: 1,
    period: { name: "آبان ۱۴۰۵", start_date: "2026-10-23", end_date: "2026-11-21", state: "open" },
    date: "2026-11-17",
    state: "draft",
    description: "خرید ملزومات",
    lines: [],
    total_debit: 1000,
    total_credit: 1000,
    ...overrides,
  };
}

function account(overrides: Partial<AccountResponse> = {}): AccountResponse {
  return {
    id: 1,
    company_id: "company-1",
    code: "1001",
    name: "صندوق",
    type: "asset",
    parent_id: null,
    closed: false,
    party_required: false,
    active: true,
    postable: true,
    ...overrides,
  };
}

describe("normalizeSearchText", () => {
  it("folds Persian digits to ASCII", () => {
    expect(normalizeSearchText("سند ۱۲۳")).toBe("سند 123");
  });

  it("unifies Arabic ي/ك with Persian ی/ک", () => {
    expect(normalizeSearchText("علي")).toBe("علی");
    expect(normalizeSearchText("كتاب")).toBe("کتاب");
  });

  it("trims and collapses whitespace", () => {
    expect(normalizeSearchText("  خرید   ملزومات  ")).toBe("خرید ملزومات");
  });
});

describe("filterJournals", () => {
  const drafts = [
    draft({ id: 1, state: "draft", description: "خرید ملزومات" }),
    draft({
      id: 2,
      state: "posted",
      description: "فروش کالا",
      period: { name: "مهر ۱۴۰۵", start_date: "2026-09-23", end_date: "2026-10-22", state: "closed" },
    }),
    draft({ id: 3, state: "draft", description: "پرداخت اجاره" }),
  ];

  it("returns everything with the default filters", () => {
    expect(filterJournals(drafts, { state: "all", period: "all", query: "" })).toHaveLength(3);
  });

  it("filters by state", () => {
    const posted = filterJournals(drafts, { state: "posted", period: "all", query: "" });
    expect(posted.map((d) => d.id)).toEqual([2]);
    const draftOnly = filterJournals(drafts, { state: "draft", period: "all", query: "" });
    expect(draftOnly.map((d) => d.id)).toEqual([1, 3]);
  });

  it("filters by period name", () => {
    const filtered = filterJournals(drafts, { state: "all", period: "مهر ۱۴۰۵", query: "" });
    expect(filtered.map((d) => d.id)).toEqual([2]);
  });

  it("searches the description, normalized", () => {
    const filtered = filterJournals(drafts, { state: "all", period: "all", query: "ملزومات" });
    expect(filtered.map((d) => d.id)).toEqual([1]);
  });

  it("searches by engine id, including Persian digits", () => {
    const filtered = filterJournals(drafts, { state: "all", period: "all", query: "۲" });
    expect(filtered.map((d) => d.id)).toEqual([2]);
  });

  it("combines state, period and query", () => {
    const filtered = filterJournals(drafts, { state: "draft", period: "آبان ۱۴۰۵", query: "اجاره" });
    expect(filtered.map((d) => d.id)).toEqual([3]);
  });
});

describe("periodOptions", () => {
  it("returns distinct period names in first-seen order", () => {
    const drafts = [
      draft({ period: { name: "آبان ۱۴۰۵", start_date: "2026-10-23", end_date: "2026-11-21", state: "open" } }),
      draft({ period: { name: "مهر ۱۴۰۵", start_date: "2026-09-23", end_date: "2026-10-22", state: "closed" } }),
      draft({ period: { name: "آبان ۱۴۰۵", start_date: "2026-10-23", end_date: "2026-11-21", state: "open" } }),
    ];
    expect(periodOptions(drafts)).toEqual(["آبان ۱۴۰۵", "مهر ۱۴۰۵"]);
  });
});

describe("filterAccounts", () => {
  const accounts = [
    account({ id: 1, code: "1001", name: "صندوق" }),
    account({ id: 2, code: "2001", name: "بستانکاران" }),
    account({ id: 3, code: "1000", name: "دارایی‌های جاری", postable: false }),
    account({ id: 4, code: "3001", name: "حساب بسته", closed: true }),
    account({ id: 5, code: "4001", name: "حساب غیرفعال", active: false }),
  ];

  it("offers only postable, active, open accounts", () => {
    expect(filterAccounts(accounts, "").map((a) => a.id)).toEqual([1, 2]);
  });

  it("matches by code", () => {
    expect(filterAccounts(accounts, "2001").map((a) => a.id)).toEqual([2]);
  });

  it("matches by name, normalized", () => {
    expect(filterAccounts(accounts, "صندوق").map((a) => a.id)).toEqual([1]);
  });

  it("matches Persian digits typed for a code", () => {
    expect(filterAccounts(accounts, "۱۰۰۱").map((a) => a.id)).toEqual([1]);
  });
});
