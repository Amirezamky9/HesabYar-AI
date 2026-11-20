import { describe, expect, it } from "vitest";
import {
  buildMoadianPayload,
  distinctTaxRuleKeys,
  newFixtureTaxid,
  registryCoverage,
  validateMoadianForm,
  type MoadianDocumentForm,
} from "./compliance-data";
import {
  documentStatusLabel,
  isProductionBlocked,
  officialStatusLabel,
  submissionStatusLabel,
  taxRuleLabel,
  validationOutcomeLabel,
  verificationLabel,
  verificationReason,
} from "./labels";
import type { StandardListItemResponse, TaxPolicyVersionResponse } from "@/lib/api-client";

const standards = (rows: Partial<StandardListItemResponse>[]): StandardListItemResponse[] =>
  rows.map((r) => ({
    standard_code: "1",
    title: "استاندارد ۱",
    title_en: null,
    ifrs_equivalent: null,
    phase: 1,
    slug: "01",
    source_authority: null,
    source_url: null,
    latest_revision_id: null,
    latest_verification_status: null,
    latest_effective_from: null,
    latest_effective_to: null,
    rules_count: 0,
    ...r,
  }));

describe("registryCoverage", () => {
  it("counts corpus, with-rules and implemented from the live list", () => {
    const coverage = registryCoverage(
      standards([
        { standard_code: "1", rules_count: 36, latest_verification_status: "UNVERIFIED" },
        { standard_code: "15", rules_count: 0, latest_verification_status: "BLOCKED_SOURCE_GAP" },
        { standard_code: "43", rules_count: 0, latest_verification_status: "VERIFIED_TEXT" },
      ]),
    );
    expect(coverage).toEqual({
      corpusStandards: 3,
      standardsWithRules: 1,
      rulesImplemented: 36,
      rulesVerified: 0,
    });
  });

  it("counts verified rules only under VERIFIED_TEXT revisions", () => {
    const coverage = registryCoverage(
      standards([{ standard_code: "43", rules_count: 5, latest_verification_status: "VERIFIED_TEXT" }]),
    );
    expect(coverage.rulesVerified).toBe(5);
  });
});

describe("distinctTaxRuleKeys", () => {
  it("keeps first-seen order and drops duplicates", () => {
    const versions = [
      { rule_key: "vat_general_rate" },
      { rule_key: "art84_salary_exemption" },
      { rule_key: "vat_general_rate" },
    ] as TaxPolicyVersionResponse[];
    expect(distinctTaxRuleKeys(versions)).toEqual(["vat_general_rate", "art84_salary_exemption"]);
  });
});

describe("verification labels", () => {
  it("labels every known status in Persian and falls back to the raw code", () => {
    expect(verificationLabel("VERIFIED_TEXT")).toBe("متن تأییدشده");
    expect(verificationLabel("BLOCKED_SOURCE_GAP")).toBe("مسدود — کمبود منبع");
    expect(verificationLabel("CORROBORATED_CURRENT")).toBe("تأیید با منبع مکمل");
    expect(verificationLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
    expect(verificationLabel(null)).toBe("نامشخص");
  });

  it("explains every gated status in Persian and blocks production for all but VERIFIED_TEXT", () => {
    for (const status of ["BLOCKED_SOURCE_GAP", "CORROBORATED_CURRENT", "UNVERIFIED", "DISCOVERY_ONLY", "TRANSITIONAL"]) {
      expect(verificationReason(status)).toMatch(/./);
      expect(isProductionBlocked(status)).toBe(true);
    }
    expect(verificationReason("VERIFIED_TEXT")).toBeNull();
    expect(isProductionBlocked("VERIFIED_TEXT")).toBe(false);
  });
});

describe("outcome and lifecycle labels", () => {
  it("labels validation outcomes", () => {
    expect(validationOutcomeLabel("pass")).toBe("قبول");
    expect(validationOutcomeLabel("fail")).toBe("رد");
    expect(validationOutcomeLabel("not_applicable")).toBe("نامربوط");
    expect(validationOutcomeLabel("error")).toBe("خطا");
  });

  it("labels submission, document and official statuses", () => {
    expect(submissionStatusLabel("accepted")).toBe("پذیرفته‌شده");
    expect(submissionStatusLabel("queued")).toBe("در صف");
    expect(documentStatusLabel("draft")).toBe("پیش‌نویس");
    expect(officialStatusLabel("PENDING")).toBe("در انتظار پردازش");
    expect(officialStatusLabel("SUCCESS")).toBe("موفق");
  });

  it("prefers the API rule_name and falls back to the known key label", () => {
    expect(taxRuleLabel("vat_general_rate", "نرخ عمومی")).toBe("نرخ عمومی");
    expect(taxRuleLabel("vat_general_rate")).toBe("نرخ عمومی مالیات بر ارزش افزوده");
    expect(taxRuleLabel("unknown_rule")).toBe("unknown_rule");
  });
});

const validForm: MoadianDocumentForm = {
  taxid: "TS00000000000000000001",
  serial: "0002F2B4E7",
  sellerEconomicCode: "32652362589632",
  totalBeforeDiscount: 1000000,
  vatAmount: 90000,
  totalBill: 1090000,
  lineDescription: "پاستیل میوه‌ای",
  lineAmount: 1,
  lineFee: 1000000,
  vatRate: 0.09,
};

describe("newFixtureTaxid", () => {
  it("returns a 22-character TS id", () => {
    expect(newFixtureTaxid(1780000000000, () => 0.5)).toHaveLength(22);
    expect(newFixtureTaxid(1780000000000, () => 0.5)).toMatch(/^TS\d{20}$/);
  });
});

describe("buildMoadianPayload", () => {
  it("builds the Table 7 payload from the form values", () => {
    const payload = buildMoadianPayload(validForm) as {
      header: Record<string, unknown>;
      body: Record<string, unknown>[];
    };
    expect(payload.header.taxid).toBe("TS00000000000000000001");
    expect(payload.header.inno).toBe("0002F2B4E7");
    expect(payload.header.tins).toBe(32652362589632);
    expect(payload.header.tbill).toBe(1090000);
    expect(payload.header.inty).toBe(1);
    expect(payload.body).toHaveLength(1);
    expect(payload.body[0].sstt).toBe("پاستیل میوه‌ای");
    expect(payload.body[0].vam).toBe(90000);
  });
});

describe("validateMoadianForm", () => {
  it("accepts the valid form and reports the first problem otherwise", () => {
    expect(validateMoadianForm(validForm)).toBeNull();
    expect(validateMoadianForm({ ...validForm, taxid: "short" })).toContain("۲۲ کاراکتر");
    expect(validateMoadianForm({ ...validForm, serial: "x" })).toContain("۱۰ کاراکتر");
    expect(validateMoadianForm({ ...validForm, sellerEconomicCode: "123" })).toContain("۱۰ یا ۱۴ رقم");
    expect(validateMoadianForm({ ...validForm, totalBill: 0 })).toContain("مبلغ صورت‌حساب");
  });
});
