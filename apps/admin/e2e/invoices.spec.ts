import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { CUSTOMER_NAME, DEV_KEY, PARTY_ID, REVENUE_ACCOUNT_ID, login } from "./auth";

/**
 * Invoices pages against the real backend + Tryton (US-304).
 * Requires HESABYAR_DEV_API_KEY and the US304_* seed ids (see e2e/auth.ts).
 *
 * The create test makes a REAL invoice draft in the dev DB, with a
 * clearly-marked test description ("فاکتور آزمایشی US-304") so the test
 * data is identifiable. Drafts are deletable from the engine if needed.
 */

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("after logout, /invoices redirects to /login", async ({ page }) => {
  await page.getByRole("button", { name: "خروج" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/invoices");
  await expect(page).toHaveURL(/\/login$/);
});

test("invoices list loads from the API and filters by state", async ({ page }) => {
  await page.goto("/invoices");
  await expect(page.getByRole("heading", { name: "فاکتورها" })).toBeVisible();
  await expect(page.getByText("شرکت: HesabYar Adapter Test")).toBeVisible({ timeout: 15_000 });

  // The seeded customer has at least the drafts created by earlier runs;
  // filter to drafts and search by party name.
  await page.getByLabel("وضعیت").click();
  await page.getByRole("option", { name: "پیش‌نویس" }).click();
  await page.getByLabel("جستجو").fill(CUSTOMER_NAME);
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });

  // A search that matches nothing shows the empty state.
  await page.getByLabel("جستجو").fill("مشتری ناموجود ۹۹۹۹");
  await expect(page.getByText("فاکتوری با این جستجو پیدا نشد.")).toBeVisible();
});

test("invoice draft creation form creates a draft and validate confirms it", async ({ page }) => {
  test.skip(!PARTY_ID || !REVENUE_ACCOUNT_ID, "US304 seed ids not set — run /tmp/seed_us304.py");

  await page.goto("/invoices/new");
  await expect(page.getByRole("heading", { name: "فاکتور جدید" })).toBeVisible();

  // Type stays "out" (sale). Pick the receivable account 1.2.1.
  await page.getByLabel("حساب دریافتنی").click();
  await page.getByRole("option", { name: /1\.2\.1/ }).click();

  await page.getByLabel("شناسهٔ طرف حساب").fill(PARTY_ID);
  await page.getByLabel("مرجع").fill("US-304-E2E");
  await page.getByLabel("شرح", { exact: true }).fill("فاکتور آزمایشی US-304");

  // One line: revenue account 4.1.1, 500,000 Toman, qty 1.
  await page.getByLabel("حساب", { exact: true }).click();
  await page.getByRole("option", { name: /4\.1\.1/ }).click();
  await page.getByLabel("قیمت واحد").fill("500000");
  await page.getByLabel("شرح ردیف").fill("کالای آزمایشی");

  await expect(page.getByText("۵۰۰٬۰۰۰ تومان").first()).toBeVisible();

  await page.getByRole("button", { name: "ساخت پیش‌نویس" }).click();

  // Lands on the new invoice's detail page.
  await expect(page).toHaveURL(/\/invoices\/\d+$/, { timeout: 30_000 });
  await expect(page.getByText("فاکتور آزمایشی US-304")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible();
  await expect(page.getByText("پیش‌نویس").first()).toBeVisible();
  await expect(page.getByText("کالای آزمایشی")).toBeVisible();
  await expect(page.getByText("۵۰۰٬۰۰۰ تومان").first()).toBeVisible();

  // Validate is a read-only check: the draft is valid.
  await page.getByRole("button", { name: "بررسی پیش‌نویس" }).click();
  await expect(page.getByText("پیش‌نویس معتبر است و آمادهٔ مرحلهٔ بعد است.")).toBeVisible({ timeout: 15_000 });

  // The new draft appears in the list under the party search.
  await page.goto("/invoices");
  await page.getByLabel("جستجو").fill(CUSTOMER_NAME);
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });
});

test("invoice CSV export downloads normalized values", async ({ page }) => {
  await page.goto("/invoices");
  await expect(page.getByText("شرکت: HesabYar Adapter Test")).toBeVisible({ timeout: 15_000 });
  await page.getByLabel("جستجو").fill(CUSTOMER_NAME);
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 15_000 });

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "خروجی CSV" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^invoices-\d{4}-\d{2}-\d{2}\.csv$/);
  const path = await download.path();
  const text = path ? await readFile(path, "utf-8") : "";
  // Normalized values: integer IRR, ISO dates, English state codes, ASCII ids.
  expect(text).toContain("out");
  expect(text).toContain("draft");
  expect(text).toContain("2026-11-17");
  expect(text).not.toMatch(/[۰-۹]/);
});

test("unknown invoice id shows the not-found state", async ({ page }) => {
  await page.goto("/invoices/999999999");
  await expect(page.getByText("فاکتور موردنظر پیدا نشد.")).toBeVisible({ timeout: 15_000 });
});
