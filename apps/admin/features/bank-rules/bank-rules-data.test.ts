/**
 * Pure-helper tests for the bank auto-coding rules UI (US-565).
 *
 * The Persian pattern summary, the تومان ⇄ Rial boundary, the
 * validation (mirroring BANK_RULE_INVALID), and the update diff
 * (only changed fields, explicit nulls clearing conditions).
 */

import { describe, expect, it } from "vitest";
import type { BankRuleResponse } from "@/lib/api-client";
import {
  PROMOTION_THRESHOLD,
  bankRuleFormToCreate,
  bankRuleFormToUpdate,
  bankRuleToForm,
  bankRuleUpdateResetsTrust,
  describeBankRuleFa,
  emptyBankRuleForm,
  formatConfirmationsFa,
  promotionHintFa,
  validateBankRuleForm,
} from "./bank-rules-data";

function rule(overrides: Partial<BankRuleResponse> = {}): BankRuleResponse {
  return {
    id: "rule-1",
    tenant_id: "tenant-1",
    company_id: "company-1",
    bank_key: "",
    name: "کارمزد اسنپ‌پی",
    description_contains: "اسنپ",
    counterparty_contains: null,
    amount_min_irr: null,
    amount_max_irr: null,
    direction: "out",
    target_account_id: 601,
    target_party_id: null,
    target_kind: "account",
    mode: "suggest",
    confirmations: 1,
    auto_eligible: false,
    priority: 100,
    enabled: true,
    created_by: "operator",
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

describe("describeBankRuleFa", () => {
  it("summarises description + direction", () => {
    expect(describeBankRuleFa(rule())).toBe("شرح شامل «اسنپ» • خروجی");
  });

  it("includes counterparty, amount range and bank key", () => {
    expect(
      describeBankRuleFa(
        rule({
          counterparty_contains: "اسنپ‌پی",
          amount_min_irr: 10_000,
          amount_max_irr: 100_000,
          bank_key: "melli",
          direction: "any",
        }),
      ),
    ).toBe(
      "شرح شامل «اسنپ» • طرف حساب شامل «اسنپ‌پی» • از ۱٬۰۰۰ تومان تا ۱۰٬۰۰۰ تومان • بانک melli",
    );
  });

  it("renders a one-sided range", () => {
    expect(describeBankRuleFa(rule({ amount_min_irr: 50_000 }))).toContain(
      "از ۵٬۰۰۰ تومان به بالا",
    );
    expect(
      describeBankRuleFa(rule({ amount_min_irr: null, amount_max_irr: 50_000 })),
    ).toContain("تا ۵٬۰۰۰ تومان");
  });

  it("falls back to بدون شرط for a condition-less rule", () => {
    expect(
      describeBankRuleFa(
        rule({ description_contains: null, direction: "any" }),
      ),
    ).toBe("بدون شرط");
  });
});

describe("promotion helpers", () => {
  it("uses the threshold of 3", () => {
    expect(PROMOTION_THRESHOLD).toBe(3);
  });

  it("formats confirmations in Persian digits", () => {
    expect(formatConfirmationsFa(rule({ confirmations: 2 }))).toBe("۲ از ۳");
  });

  it("explains each promotion state", () => {
    expect(promotionHintFa(rule({ mode: "auto", confirmations: 3 }))).toContain("خودکار");
    expect(promotionHintFa(rule({ confirmations: 3, auto_eligible: true }))).toContain(
      "آمادهٔ ارتقاست",
    );
    expect(promotionHintFa(rule({ confirmations: 1 }))).toBe(
      "۲ تأیید دیگر تا آماده‌شدن برای ارتقا مانده است.",
    );
  });
});

describe("validateBankRuleForm", () => {
  it("requires a name, a target account and at least one condition", () => {
    const errors = validateBankRuleForm(emptyBankRuleForm());
    expect(errors.name).toBeDefined();
    expect(errors.targetAccountId).toBeDefined();
    expect(errors.form).toContain("دست‌کم یک شرط");
  });

  it("accepts a form with one condition", () => {
    const values = {
      ...emptyBankRuleForm(),
      name: "کارمزد",
      descriptionContains: "اسنپ",
      targetAccountId: 601,
    };
    expect(validateBankRuleForm(values)).toEqual({});
  });

  it("rejects an inverted amount range", () => {
    const values = {
      ...emptyBankRuleForm(),
      name: "کارمزد",
      descriptionContains: "اسنپ",
      targetAccountId: 601,
      amountMinToman: "10000",
      amountMaxToman: "1000",
    };
    const errors = validateBankRuleForm(values);
    expect(errors.amountMaxToman).toContain("نمی‌تواند کمتر از حداقل باشد");
  });

  it("rejects non-numeric amounts and priority", () => {
    const values = {
      ...emptyBankRuleForm(),
      name: "کارمزد",
      descriptionContains: "اسنپ",
      targetAccountId: 601,
      amountMinToman: "ده هزار",
      priority: "اول",
    };
    const errors = validateBankRuleForm(values);
    expect(errors.amountMinToman).toBeDefined();
    expect(errors.priority).toBeDefined();
  });

  it("accepts Persian digits and separators", () => {
    const values = {
      ...emptyBankRuleForm(),
      name: "کارمزد",
      descriptionContains: "اسنپ",
      targetAccountId: 601,
      amountMinToman: "۱٬۰۰۰",
      amountMaxToman: "۱۰٬۰۰۰",
    };
    expect(validateBankRuleForm(values)).toEqual({});
  });
});

describe("bankRuleFormToCreate / bankRuleToForm", () => {
  it("converts تومان to whole Rial ×10 and trims text", () => {
    const request = bankRuleFormToCreate(
      {
        ...emptyBankRuleForm(),
        name: "  کارمزد  ",
        descriptionContains: "اسنپ",
        amountMinToman: "1000",
        amountMaxToman: "۱٬۰۰۰",
        targetAccountId: 601,
        targetPartyId: "42",
        priority: "5",
      },
      "company-1",
    );
    expect(request.name).toBe("کارمزد");
    expect(request.amount_min_irr).toBe(10_000);
    expect(request.amount_max_irr).toBe(10_000);
    expect(request.target_party_id).toBe(42);
    expect(request.priority).toBe(5);
    expect(request.counterparty_contains).toBeNull();
  });

  it("round-trips a stored rule through the form", () => {
    const original = rule({ amount_min_irr: 10_000, amount_max_irr: 100_000 });
    const form = bankRuleToForm(original);
    expect(form.amountMinToman).toBe("1000");
    expect(form.amountMaxToman).toBe("10000");
    expect(form.descriptionContains).toBe("اسنپ");
    const back = bankRuleFormToCreate(form, "company-1");
    expect(back.amount_min_irr).toBe(10_000);
    expect(back.amount_max_irr).toBe(100_000);
  });
});

describe("bankRuleFormToUpdate", () => {
  it("sends only changed fields", () => {
    const original = rule();
    const update = bankRuleFormToUpdate(
      { ...bankRuleToForm(original), name: "تغییر نام" },
      original,
    );
    expect(update).toEqual({ name: "تغییر نام" });
    expect(bankRuleUpdateResetsTrust(update)).toBe(false);
  });

  it("clears a condition with an explicit null and flags the trust reset", () => {
    const original = rule();
    const update = bankRuleFormToUpdate(
      { ...bankRuleToForm(original), descriptionContains: "" },
      original,
    );
    expect(update).toEqual({ description_contains: null });
    expect(bankRuleUpdateResetsTrust(update)).toBe(true);
  });

  it("flags target changes as trust-resetting", () => {
    expect(
      bankRuleUpdateResetsTrust({ target_account_id: 501 }),
    ).toBe(true);
    expect(bankRuleUpdateResetsTrust({ priority: 5 })).toBe(false);
  });
});
