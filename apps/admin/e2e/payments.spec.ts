import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { CUSTOMER_NAME, DEV_KEY, PAYMENT_ID, PAYMENT_REFERENCE, login } from "./auth";

/**
 * Payments pages against the real backend + Tryton (US-304).
 * Requires HESABYAR_DEV_API_KEY and the US304_* seed ids (see e2e/auth.ts).
 * Payments are read-only in this UI (no create/approve in the API yet).
 */

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  test.skip(!PAYMENT_ID, "US304 seed ids not set — run /tmp/seed_us304.py");
  await login(page);
});

test("payments list shows the seeded payment and filters by kind and state", async ({ page }) => {
  await page.goto("/payments");
  await expect(page.getByRole("heading", { name: "پرداخت‌ها" })).toBeVisible();
  await expect(page.getByText("شرکت: HesabYar Adapter Test")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("دریافتی").first()).toBeVisible();
  await expect(page.getByText("۵۰٬۰۰۰ تومان").first()).toBeVisible();

  // Kind filter: payable hides the receivable payment.
  await page.getByLabel("نوع").click();
  await page.getByRole("option", { name: "پرداختی" }).click();
  await expect(page.getByText("هنوز پرداختی ثبت نشده است.")).toBeVisible({ timeout: 15_000 });

  // Back to all kinds + draft state: the payment is visible again.
  await page.getByLabel("نوع").click();
  await page.getByRole("option", { name: "همه", exact: true }).click();
  await page.getByLabel("وضعیت").click();
  await page.getByRole("option", { name: "پیش‌نویس" }).click();
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });

  // Party search narrows to the seeded customer.
  await page.getByLabel("جستجو").fill(PAYMENT_REFERENCE);
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });
});

test("payment detail shows party, amount, state and journal", async ({ page }) => {
  await page.goto(`/payments/${PAYMENT_ID}`);
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });
  // MoneyDisplay renders the number and the unit in separate spans, so
  // match the number alone (it appears exactly once on this page).
  await expect(page.getByText("۵۰٬۰۰۰")).toBeVisible();
  await expect(page.getByText("دریافتی")).toBeVisible();
  await expect(page.getByText("پیش‌نویس").first()).toBeVisible();
  await expect(page.getByText("HesabYar US-304 Payments")).toBeVisible();
  await expect(page.getByText(PAYMENT_REFERENCE)).toBeVisible();
  // Read-only: no create/approve action exists on this page.
  await expect(page.getByRole("button", { name: /تأیید|ثبت پرداخت/ })).toHaveCount(0);
});

test("payment CSV export downloads normalized values", async ({ page }) => {
  await page.goto("/payments");
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "خروجی CSV" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^payments-\d{4}-\d{2}-\d{2}\.csv$/);
  const path = await download.path();
  const text = path ? await readFile(path, "utf-8") : "";
  expect(text).toContain("receivable");
  expect(text).toContain("draft");
  expect(text).toContain("500000");
  expect(text).not.toMatch(/[۰-۹]/);
});

test("unknown payment id shows the not-found state", async ({ page }) => {
  await page.goto("/payments/999999999");
  await expect(page.getByText("پرداخت موردنظر پیدا نشد.")).toBeVisible({ timeout: 15_000 });
});
