import { describe, expect, it } from "vitest";
import type { AccountResponse } from "@/lib/api-client";
import { accountsToCsv } from "./account-csv";

function account(overrides: Partial<AccountResponse>): AccountResponse {
  return {
    id: 1,
    company_id: "c1",
    code: "1001",
    name: "موجودی نقد",
    type: "asset",
    parent_id: null,
    closed: false,
    party_required: false,
    active: true,
    postable: true,
    ...overrides,
  };
}

describe("accountsToCsv", () => {
  it("exports normalized values with Persian headers", () => {
    const csv = accountsToCsv([
      account({ id: 1, code: "1001", name: "موجودی نقد", type: "asset" }),
      account({ id: 2, code: "2001", name: "حساب‌های پرداختنی", type: "liability", postable: false }),
    ]);
    expect(csv).toBe(
      "\uFEFFشناسه,کد,نام,نوع,قابل‌ثبت,فعال,بسته\r\n" +
        "1,1001,موجودی نقد,asset,true,true,false\r\n" +
        "2,2001,حساب‌های پرداختنی,liability,false,true,false\r\n",
    );
  });

  it("exports an empty type as an empty cell", () => {
    const csv = accountsToCsv([account({ type: null })]);
    expect(csv).toContain("1,1001,موجودی نقد,,true,true,false");
  });
});
