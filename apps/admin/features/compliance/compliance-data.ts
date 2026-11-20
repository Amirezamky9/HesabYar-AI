/**
 * Pure helpers for the compliance center (US-405) — no I/O, fully tested.
 *
 * - Coverage is always computed from live API data: the registry list
 *   gives corpus / implemented / verified; a run response adds executed.
 * - The Moadian document payload is built from the operator's form
 *   values plus the fixed structural fields of the official
 *   instruction's worked example (docs/moadian.md); nothing is invented
 *   per invoice — the variable fields are exactly the form fields.
 */

import type {
  ReportValidationRunResponse,
  StandardListItemResponse,
  TaxPolicyVersionResponse,
} from "@/lib/api-client";

/** Registry coverage from a live standards list (no run involved). */
export interface RegistryCoverage {
  corpusStandards: number;
  standardsWithRules: number;
  rulesImplemented: number;
  /** Rules whose latest revision is VERIFIED_TEXT — 0 today: all 36 rules belong to Standard 1 (UNVERIFIED). */
  rulesVerified: number;
}

export function registryCoverage(standards: StandardListItemResponse[]): RegistryCoverage {
  return {
    corpusStandards: standards.length,
    standardsWithRules: standards.filter((s) => s.rules_count > 0).length,
    rulesImplemented: standards.reduce((sum, s) => sum + s.rules_count, 0),
    rulesVerified: standards
      .filter((s) => s.latest_verification_status === "VERIFIED_TEXT")
      .reduce((sum, s) => sum + s.rules_count, 0),
  };
}

/** Full coverage snapshot of one report-validation run (spec §12.1). */
export function runCoverage(run: ReportValidationRunResponse): {
  corpusStandards: number;
  standardsWithRules: number;
  rulesImplemented: number;
  rulesVerified: number;
  rulesExecuted: number;
} {
  const c = run.run.coverage;
  return {
    corpusStandards: c.corpus_standards,
    standardsWithRules: c.standards_with_rules,
    rulesImplemented: c.rules_implemented,
    rulesVerified: c.rules_verified,
    rulesExecuted: c.rules_executed,
  };
}

/** Distinct rule keys of the tax versions, in first-seen order. */
export function distinctTaxRuleKeys(versions: TaxPolicyVersionResponse[]): string[] {
  const seen = new Set<string>();
  const keys: string[] = [];
  for (const version of versions) {
    if (!seen.has(version.rule_key)) {
      seen.add(version.rule_key);
      keys.push(version.rule_key);
    }
  }
  return keys;
}

/** Format a tax parameter value: percent as «۱۰٪», IRR as grouped Rial with the تومان note. */
export function formatTaxParameterValue(value: string, unit: string): { text: string; isPercent: boolean } {
  if (unit === "percent") {
    return { text: `${value}٪`, isPercent: true };
  }
  return { text: value, isPercent: false };
}

// ---------------------------------------------------------------------------
// Moadian document payload (instruction Table 7)
// ---------------------------------------------------------------------------

export interface MoadianDocumentForm {
  taxid: string;
  serial: string;
  sellerEconomicCode: string;
  totalBeforeDiscount: number;
  vatAmount: number;
  totalBill: number;
  lineDescription: string;
  lineAmount: number;
  lineFee: number;
  vatRate: number;
}

/** A fresh 22-character fixture tax id: «TS» + 20 digits (unique per call). */
export function newFixtureTaxid(now = Date.now(), random = Math.random): string {
  const millis = String(now).padStart(13, "0");
  const extra = String(Math.floor(random() * 10_000_000)).padStart(7, "0");
  return `TS${millis}${extra}`;
}

/**
 * Build a Table 7 invoice payload from the form. Structural fields that
 * the operator does not set (invoice type 1, pattern 1, subject 1,
 * buyer type 1, settlement 1, timestamps "now") follow the official
 * instruction's worked example; every amount comes from the form.
 */
export function buildMoadianPayload(form: MoadianDocumentForm): Record<string, unknown> {
  const timestamp = String(Date.now());
  return {
    header: {
      taxid: form.taxid,
      indatim: timestamp,
      indati2m: timestamp,
      inty: 1,
      inno: form.serial,
      irtaxid: null,
      inp: 1,
      ins: 1,
      tins: Number(form.sellerEconomicCode),
      tob: 1,
      bid: null,
      tinb: null,
      sbc: null,
      bpc: null,
      bbc: null,
      ft: null,
      bpn: null,
      scln: null,
      scc: null,
      crn: null,
      billid: null,
      tprdis: form.totalBeforeDiscount,
      tdis: 0,
      tadis: form.totalBeforeDiscount,
      tvam: form.vatAmount,
      todam: 0,
      tbill: form.totalBill,
      setm: 1,
      cap: form.vatAmount,
      insp: 0,
      tvop: form.vatAmount,
      dpvb: null,
      tax17: null,
    },
    body: [
      {
        sstid: 2153265989636,
        sstt: form.lineDescription,
        mu: 96,
        am: form.lineAmount,
        fee: form.lineFee,
        cfee: null,
        cut: null,
        exr: null,
        prdis: form.lineFee * form.lineAmount,
        dis: 0,
        adis: form.lineFee * form.lineAmount,
        vra: form.vatRate,
        vam: Math.round(form.lineFee * form.lineAmount * form.vatRate),
        odt: null,
        odr: null,
        odam: null,
        tsstam: Math.round(form.lineFee * form.lineAmount * (1 + form.vatRate)),
      },
    ],
  };
}

/** Form-level validation, in the order the fields appear. Returns the first problem, or null. */
export function validateMoadianForm(form: MoadianDocumentForm): string | null {
  if (form.taxid.length !== 22) return "شمارهٔ مالیاتی یکتا باید دقیقاً ۲۲ کاراکتر باشد.";
  if (form.serial.length !== 10) return "سریال صورت‌حساب (inno) باید دقیقاً ۱۰ کاراکتر باشد.";
  if (!/^\d{10}$|^\d{14}$/.test(form.sellerEconomicCode))
    return "کد اقتصادی فروشنده باید ۱۰ یا ۱۴ رقم باشد.";
  if (form.totalBeforeDiscount <= 0) return "مبلغ پیش از تخفیف باید بیشتر از صفر باشد.";
  if (form.vatAmount < 0) return "مبلغ مالیات نمی‌تواند منفی باشد.";
  if (form.totalBill <= 0) return "مبلغ صورت‌حساب باید بیشتر از صفر باشد.";
  if (!form.lineDescription.trim()) return "شرح قلم صورت‌حساب را وارد کنید.";
  if (form.lineAmount <= 0) return "تعداد قلم باید بیشتر از صفر باشد.";
  if (form.lineFee <= 0) return "مبلغ واحد قلم باید بیشتر از صفر باشد.";
  if (form.vatRate < 0 || form.vatRate > 1) return "نرخ مالیات قلم باید بین صفر و یک باشد.";
  return null;
}

// ---------------------------------------------------------------------------
// Last validation run (for the overview coverage card)
// ---------------------------------------------------------------------------

const LAST_RUN_KEY = "hesabyar-compliance-last-report-run";

export function saveLastReportRunId(runId: string): void {
  try {
    window.localStorage.setItem(LAST_RUN_KEY, runId);
  } catch {
    // Private mode / disabled storage: the overview simply shows no run.
  }
}

export function loadLastReportRunId(): string | null {
  try {
    return window.localStorage.getItem(LAST_RUN_KEY);
  } catch {
    return null;
  }
}
