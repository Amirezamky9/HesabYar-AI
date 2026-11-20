import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { DEV_KEY, STATEMENT_ID, STATEMENT_NAME, login } from "./auth";

// The remote Tryton backend answers in ~7s per call; a list/detail flow
// needs well over the 30s default test timeout.
test.setTimeout(240_000);

/**
 * Bank statements pages against the real backend + Tryton (US-304).
 * Requires HESABYAR_DEV_API_KEY and the US304_* seed ids (see e2e/auth.ts).
 */

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  test.skip(!STATEMENT_ID, "US304 seed ids not set — run /tmp/seed_us304.py");
  await login(page);
});

test("statements list shows the seeded statement and the import-status summary", async ({ page }) => {
  await page.goto("/banking/statements");
  await expect(page.getByRole("heading", { name: "صورت‌حساب‌های بانکی" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("شرکت: HesabYar Adapter Test")).toBeVisible({ timeout: 30_000 });

  // Import-status summary: 1 statement, 1 draft, 2 lines, 0 matched, 2 unmatched.
  await expect(page.getByText("وضعیت ورود صورت‌حساب‌ها")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("ردیف‌های تطبیق‌نیافته")).toBeVisible();

  await expect(page.getByText(STATEMENT_NAME).first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("HesabYar US-304 Bank").first()).toBeVisible();
  await expect(page.getByText("۷۵٬۰۰۰ تومان").first()).toBeVisible();

  // State filter: posted hides the draft statement.
  await page.getByLabel("وضعیت").click();
  await page.getByRole("option", { name: "ثبت‌شده" }).click();
  await expect(page.getByText("هنوز صورت‌حساب بانکی ثبت نشده است.")).toBeVisible({ timeout: 60_000 });
});

test("statement detail reconciles: no candidates match the seed lines", async ({ page }) => {
  await page.goto(`/banking/statements/${STATEMENT_ID}`);
  await expect(page.getByRole("heading", { name: STATEMENT_NAME })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Customer receipt")).toBeVisible();
  await expect(page.getByText("Supplier payment")).toBeVisible();
  await expect(page.getByText("۱۰۰٬۰۰۰ تومان")).toBeVisible();
  await expect(page.getByText("۲۵٬۰۰۰ تومان")).toBeVisible();
  await expect(page.getByText("ماندهٔ اول")).toBeVisible();
  await expect(page.getByText("ماندهٔ آخر")).toBeVisible();
  await expect(page.getByText("۷۵٬۰۰۰ تومان").first()).toBeVisible();

  // Reconcile is live (US-514): the seed has no posted invoice and no
  // payment matching these amounts, so both lines come back unmatched
  // and the manual-match form appears. Reconciling never posts anything.
  const reconcile = page.getByRole("button", { name: "تطبیق صورت‌حساب" });
  await expect(reconcile).toBeEnabled();
  await reconcile.click();
  await expect(page.getByText("تطبیق دستی یک ردیف")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("تطبیق‌نیافته").first()).toBeVisible();
});

test("statement CSV export downloads normalized values", async ({ page }) => {
  await page.goto("/banking/statements");
  await expect(page.getByText(STATEMENT_NAME).first()).toBeVisible({ timeout: 60_000 });

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "خروجی CSV" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^statements-\d{4}-\d{2}-\d{2}\.csv$/);
  const path = await download.path();
  const text = path ? await readFile(path, "utf-8") : "";
  expect(text).toContain("US-304 e2e statement");
  expect(text).toContain("draft");
  expect(text).toContain("750000");
  expect(text).not.toMatch(/[۰-۹]/);
});

test("unknown statement id shows the not-found state", async ({ page }) => {
  await page.goto("/banking/statements/999999999");
  await expect(page.getByText("صورت‌حساب موردنظر پیدا نشد.")).toBeVisible({ timeout: 30_000 });
});
