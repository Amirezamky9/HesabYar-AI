import { expect, test, type Page } from "@playwright/test";

/**
 * US-704: the operator monitoring panel — live in the browser against
 * the real backend.
 *
 * Requires the FastAPI backend on :8001 and HESABYAR_DEV_API_KEY
 * (see apps/admin/README.md). Tests skip cleanly when the key is
 * absent. The dev key needs monitoring:read + monitoring:manage
 * (apps/admin/scripts/mint-dev-key.py).
 *
 * The dev database is shared with the sibling stories: alert counts
 * and queue depths are never asserted as fixed numbers. What must
 * always hold: every section renders, the backup cards never show a
 * fabricated success (unknown until US-703 lands), and the channels
 * + tokens tabs load without error.
 */

// The monitoring overview fires 2 API calls, but the login + page
// load against remote Supabase is slow (the dashboard batch took
// ~105s in ۵ث); the timeout matches the dashboard spec (180s).
test.setTimeout(180_000);

const DEV_KEY = process.env.HESABYAR_DEV_API_KEY ?? "";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("کلید دسترسی").fill(DEV_KEY);
  await page.getByRole("button", { name: "ورود", exact: true }).click();
  await expect(page.getByRole("heading", { name: "داشبورد", exact: true })).toBeVisible({
    timeout: 30_000,
  });
}

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("monitoring overview shows all five sections", async ({ page }) => {
  await page.goto("/monitoring");
  const root = page.getByTestId("monitoring-overview");
  await expect(root).toBeVisible({ timeout: 30_000 });
  await expect(root.getByRole("heading", { name: "پایش سیستم", exact: true })).toBeVisible();

  const sections = [
    ["monitoring-processes", "سلامت فرایندها"],
    ["monitoring-queues", "صف‌ها و تأخیر"],
    ["monitoring-alerts", "هشدارهای اخیر"],
    ["monitoring-tokens", "انقضای توکن‌ها"],
    ["monitoring-backups", "وضعیت پشتیبان‌گیری"],
  ] as const;
  for (const [testId, heading] of sections) {
    const section = page.getByTestId(testId);
    await expect(section).toBeVisible({ timeout: 30_000 });
    await expect(section.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  }

  // Processes: all five rows render (API, database, Tryton, worker, MCP).
  const processes = page.getByTestId("monitoring-processes");
  for (const label of ["رابط برنامه‌نویسی", "پایگاه داده", "ترایتون", "کارگر", "سرور MCP"]) {
    await expect(processes.getByText(label, { exact: true })).toBeVisible();
  }

  // Backups: until US-703 lands, both databases report unknown —
  // never a fabricated success.
  const backups = page.getByTestId("monitoring-backups");
  await expect(backups.getByText("حسابیار", { exact: true })).toBeVisible();
  await expect(backups.getByText("ترایتون", { exact: true })).toBeVisible();
  await expect(backups.getByText("نامشخص").first()).toBeVisible();
});

test("evaluate now runs and reports", async ({ page }) => {
  await page.goto("/monitoring");
  const root = page.getByTestId("monitoring-overview");
  await expect(root).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "ارزیابی اکنون" }).click();
  // Either a fresh alert count or the "no new alert" note — the
  // evaluation ran and reported back.
  await expect(
    page.getByText(/هشدار جدید ثبت شد|هشدار جدیدی نبود/),
  ).toBeVisible({ timeout: 30_000 });
});

test("channels and tokens tabs load", async ({ page }) => {
  await page.goto("/monitoring");
  const root = page.getByTestId("monitoring-overview");
  await expect(root).toBeVisible({ timeout: 30_000 });

  await page.getByRole("tab", { name: "کانال‌های هشدار" }).click();
  await expect(page.getByRole("button", { name: "کانال جدید" })).toBeVisible({
    timeout: 30_000,
  });

  await page.getByRole("tab", { name: "توکن‌ها" }).click();
  await expect(page.getByRole("button", { name: "توکن جدید" })).toBeVisible({
    timeout: 30_000,
  });
});

test("monitoring page has no horizontal overflow at 360px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/monitoring");
  const root = page.getByTestId("monitoring-overview");
  await expect(root).toBeVisible({ timeout: 30_000 });
  const overflow = await page.evaluate(() => {
    return document.documentElement.scrollWidth - document.documentElement.clientWidth;
  });
  expect(overflow).toBe(0);
});
