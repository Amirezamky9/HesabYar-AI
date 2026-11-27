import { expect, type Page } from "@playwright/test";
import { pickJalaliDate } from "./settlement-helpers";

/**
 * Cheque center e2e helpers (US-535) — seed cheques through the UI
 * registration form (the same path an operator uses), then drive the
 * center / intake / checkbook / report / dashboard pages.
 *
 * Money is integer Toman in the form; the backend converts (×10) to
 * Rial. Sayyad ids are 16 digits, unique per run.
 */

export const CHEQUE_ISSUER_NAME = "HesabYar US-535 E2E Issuer";
export const CHEQUE_PAYEE_NAME = "HesabYar US-535 E2E Payee";
export const CHEQUE_ISSUER_NATIONAL_ID = "1111111111";
export const CHEQUE_PAYEE_NATIONAL_ID = "2222222222";
export const CHEQUE_ISSUER_BANK = "056 سامان"; // backend requires the 3-digit bank code prefix

/** 16-digit Sayyad id, unique per run: "535" + last 10 digits of epoch ms + 3-digit suffix. */
export function uniqueSayadId(suffix: string): string {
  return `535${Date.now().toString().slice(-10)}${suffix}`;
}

export interface SeedChequeOptions {
  sayadId: string;
  kind?: "received" | "issued";
  purpose?: "payment" | "collateral";
  amountToman?: string;
  /** Gregorian YYYY-MM-DD. */
  issueIso?: string;
  /** Gregorian YYYY-MM-DD. */
  dueIso?: string;
  issuerName?: string;
  issuerBank?: string;
  securedObligation?: string;
}

/**
 * Registers a cheque through the UI form; returns the created
 * cheque's row id from the redirect URL (/cheques/<id>).
 * For an issued cheque the first available checkbook is picked.
 */
export async function seedChequeViaForm(page: Page, opts: SeedChequeOptions): Promise<string> {
  const {
    sayadId,
    kind = "received",
    purpose = "payment",
    amountToman = "1250000",
    issueIso,
    dueIso,
    issuerName = CHEQUE_ISSUER_NAME,
    issuerBank = CHEQUE_ISSUER_BANK,
    securedObligation,
  } = opts;
  await page.goto("/cheques/new");
  await expect(page.getByRole("heading", { name: "ثبت چک جدید" })).toBeVisible({ timeout: 90_000 });
  if (kind === "issued") {
    await page.locator("#cheque-kind").click();
    await page.getByRole("option", { name: "پرداختی (چک ما)" }).click();
  }
  if (purpose === "collateral") {
    await page.locator("#cheque-purpose").click();
    await page.getByRole("option", { name: /پشتوانه/ }).click();
  }
  await page.getByLabel("شناسهٔ ۱۶رقمی صیاد").fill(sayadId);
  await page.getByLabel("مبلغ (تومان)").fill(amountToman);
  if (issueIso) await pickJalaliDate(page, "cheque-issue-date", issueIso);
  if (dueIso) await pickJalaliDate(page, "cheque-due-date", dueIso);
  await page.getByLabel("نام صادرکننده").fill(issuerName);
  await page.getByLabel("کد ملی / شناسهٔ صادرکننده").fill(CHEQUE_ISSUER_NATIONAL_ID);
  // Exact match: "بانک صادرکننده" is a substring of the "شعبهٔ بانک
  // صادرکننده" label, so a loose getByLabel resolves to 2 elements.
  await page.getByLabel("بانک صادرکننده", { exact: true }).fill(issuerBank);
  await page.getByLabel("نام ذی‌نفع (ثبت‌شده در صیاد)").fill(CHEQUE_PAYEE_NAME);
  await page.getByLabel("کد ملی / شناسهٔ ذی‌نفع").fill(CHEQUE_PAYEE_NATIONAL_ID);
  await page.getByLabel("شناسهٔ سفارش").fill(`E2E-535-${sayadId.slice(-4)}`);
  await page.getByLabel("مرجع مشتری").fill(`CUST-535-${sayadId.slice(-4)}`);
  if (purpose === "collateral") {
    await page.getByLabel("تعهد تحت پوشش").fill(securedObligation ?? "قرارداد فروش اقساطی E2E-535");
  }
  if (kind === "issued") {
    await page.locator("#cheque-checkbook").click();
    await page.getByRole("option").first().click();
  }
  await page.getByRole("button", { name: "ثبت چک", exact: true }).click();
  await expect(page).toHaveURL(/\/cheques\/[0-9a-f-]+$/, { timeout: 90_000 });
  return page.url().split("/cheques/")[1];
}
