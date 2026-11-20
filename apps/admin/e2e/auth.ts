import { expect, type Page } from "@playwright/test";

/**
 * Shared login helper for the US-304 specs (real backend on :8001).
 * The dev key comes from HESABYAR_DEV_API_KEY (.env.local, gitignored).
 * Seeded US-304 ids (party, accounts, payment, statement) come from the
 * US304_* vars the seed script writes to the same file — specs skip
 * cleanly when they are absent.
 */

export const DEV_KEY = process.env.HESABYAR_DEV_API_KEY ?? "";
export const PARTY_ID = process.env.US304_PARTY_ID ?? "";
export const REVENUE_ACCOUNT_ID = process.env.US304_REVENUE_ACCOUNT_ID ?? "";
export const PAYMENT_ID = process.env.US304_PAYMENT_ID ?? "";
export const STATEMENT_ID = process.env.US304_STATEMENT_ID ?? "";

export const CUSTOMER_NAME = "HesabYar US-304 Test Customer";
export const PAYMENT_REFERENCE = "US-304 e2e payment";
export const STATEMENT_NAME = "US-304 e2e statement";

export async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("کلید دسترسی").fill(DEV_KEY);
  await page.getByRole("button", { name: "ورود", exact: true }).click();
  // The login page redirects to "/" on success. Wait for the URL, not
  // for the dashboard home's content — US-302 owns that page.
  await expect(page).toHaveURL(/\/$/, { timeout: 30_000 });
}
