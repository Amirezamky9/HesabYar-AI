import { describe, expect, it } from "vitest";
import type { AccountResponse } from "@/lib/api-client";
import {
  accountTypeOptions,
  buildAccountTree,
  compareAccountCodes,
  filterAccounts,
  normalizeSearchQuery,
} from "./account-tree";
import { accountTypeLabel, hasAccountTypeLabel } from "./account-types";

function account(partial: Partial<AccountResponse>): AccountResponse {
  return {
    id: 1,
    company_id: "company-1",
    code: "",
    name: "Root",
    type: null,
    parent_id: null,
    closed: false,
    party_required: false,
    active: true,
    postable: false,
    ...partial,
  };
}

describe("compareAccountCodes", () => {
  it("sorts numerically per segment, not lexicographically", () => {
    const codes = ["1.2.10", "1.2.9", "1.10.0", "1.2.1", "2.0.0"];
    expect([...codes].sort(compareAccountCodes)).toEqual([
      "1.2.1",
      "1.2.9",
      "1.2.10",
      "1.10.0",
      "2.0.0",
    ]);
  });

  it("sorts the empty code (chart root) first", () => {
    expect(compareAccountCodes("", "1.0.0")).toBeLessThan(0);
    expect(compareAccountCodes("1.0.0", "")).toBeGreaterThan(0);
  });

  it("sorts a parent code before its children", () => {
    expect(compareAccountCodes("1.1.0", "1.1.0.1")).toBeLessThan(0);
  });
});

describe("buildAccountTree", () => {
  it("nests children under parents and sorts siblings by code", () => {
    const tree = buildAccountTree([
      account({ id: 3, code: "1.1.2", name: "Second", parent_id: 2 }),
      account({ id: 1, code: "", name: "Chart", parent_id: null }),
      account({ id: 4, code: "1.1.10", name: "Tenth", parent_id: 2 }),
      account({ id: 2, code: "1.1.0", name: "Assets", parent_id: 1 }),
    ]);
    expect(tree).toHaveLength(1);
    expect(tree[0].account.id).toBe(1);
    expect(tree[0].depth).toBe(0);
    expect(tree[0].children.map((n) => n.account.id)).toEqual([2]);
    expect(tree[0].children[0].depth).toBe(1);
    expect(tree[0].children[0].children.map((n) => n.account.code)).toEqual(["1.1.2", "1.1.10"]);
    expect(tree[0].children[0].children[0].depth).toBe(2);
  });

  it("keeps an account with a missing parent visible as a root", () => {
    const tree = buildAccountTree([
      account({ id: 1, code: "", name: "Chart", parent_id: null }),
      account({ id: 9, code: "9.9.9", name: "Orphan", parent_id: 999 }),
    ]);
    expect(tree.map((n) => n.account.id)).toEqual([1, 9]);
  });

  it("does not depend on input order", () => {
    const accounts = [
      account({ id: 5, code: "1.1.1", name: "Leaf", parent_id: 2 }),
      account({ id: 2, code: "1.1.0", name: "Mid", parent_id: 1 }),
      account({ id: 1, code: "", name: "Chart", parent_id: null }),
    ];
    const tree = buildAccountTree(accounts);
    expect(tree[0].children[0].children[0].account.id).toBe(5);
    expect(tree[0].children[0].children[0].depth).toBe(2);
  });
});

describe("normalizeSearchQuery / filterAccounts", () => {
  const accounts = [
    account({ id: 1, code: "1.1.1", name: "Cash and Cash Equivalents", type: "Cash and Cash Equivalents" }),
    account({ id: 2, code: "1.2.1", name: "Trade Receivables", type: "Accounts receivable" }),
    account({ id: 3, code: "2.1.1", name: "Trade Payables", type: "Accounts payable" }),
    account({ id: 4, code: "1.0.0", name: "Assets", type: null }),
  ];

  it("normalizes Persian digits before matching", () => {
    expect(normalizeSearchQuery("۱٫۱٫۱")).toBe("1.1.1");
    expect(filterAccounts(accounts, { query: "۱٫۱٫۱" }).map((a) => a.id)).toEqual([1]);
  });

  it("matches by code", () => {
    expect(filterAccounts(accounts, { query: "2.1.1" }).map((a) => a.id)).toEqual([3]);
  });

  it("matches a code substring anywhere in the code", () => {
    // "2.1" appears in both "1.2.1" and "2.1.1".
    expect(filterAccounts(accounts, { query: "2.1" }).map((a) => a.id)).toEqual([2, 3]);
  });

  it("matches by name, case-insensitive", () => {
    expect(filterAccounts(accounts, { query: "trade" }).map((a) => a.id)).toEqual([2, 3]);
  });

  it("filters by the raw type value, not the Persian label", () => {
    const found = filterAccounts(accounts, { query: "", type: "Accounts receivable" });
    expect(found.map((a) => a.id)).toEqual([2]);
    // The Persian label must NOT match as a type value.
    expect(filterAccounts(accounts, { query: "", type: "حساب‌های دریافتنی" })).toHaveLength(0);
  });

  it("filters untyped accounts with a null type", () => {
    expect(filterAccounts(accounts, { query: "", type: null }).map((a) => a.id)).toEqual([4]);
  });

  it("combines query and type", () => {
    expect(
      filterAccounts(accounts, { query: "trade", type: "Accounts payable" }).map((a) => a.id),
    ).toEqual([3]);
  });
});

describe("accountTypeOptions", () => {
  it("lists distinct types with counts, most common first", () => {
    const options = accountTypeOptions([
      account({ id: 1, type: "Revenue" }),
      account({ id: 2, type: "Revenue" }),
      account({ id: 3, type: null }),
      account({ id: 4, type: "Expenses" }),
    ]);
    expect(options).toEqual([
      { value: "Revenue", count: 2 },
      { value: null, count: 1 },
      { value: "Expenses", count: 1 },
    ]);
  });
});

describe("accountTypeLabel", () => {
  it("labels known types in Persian", () => {
    expect(accountTypeLabel("Accounts receivable")).toBe("حساب‌های دریافتنی");
    expect(accountTypeLabel("Revenue")).toBe("درآمد");
    expect(accountTypeLabel(null)).toBe("بدون نوع");
  });

  it("falls back to the raw string for unknown types", () => {
    expect(accountTypeLabel("Some Future Type")).toBe("Some Future Type");
    expect(hasAccountTypeLabel("Some Future Type")).toBe(false);
    expect(hasAccountTypeLabel("Revenue")).toBe(true);
  });
});
