import { expect, test } from "@playwright/test";
import { login, DEV_KEY } from "./auth";

/**
 * Cash-forecast calendar e2e (US-553) — the monthly calendar page,
 * the reports tab, the dashboard cash card and cheque report 12
 * (monthly match-trend from stored history).
 *
 * Resilient to an empty AND a non-empty dev DB: no test seeds data
 * and none asserts a specific figure — only that the calendar,
 * both قطعی/پیش‌بینی columns, the 30/60/90/120-day horizon and the
 * trend render (or show their honest empty states). Pages against
 * remote Supabase can take ~40s to load data — heading/data waits
 * are 90s, never the 30s default.
 */

test.setTimeout(900_000);
test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("cash-forecast page renders the calendar with both columns", async ({ page }) => {
  await page.goto("/reports/cash-forecast");
  await expect(page.getByRole("heading", { name: "پیش‌بینی جریان نقد" })).toBeVisible({
    timeout: 90_000,
  });
  // Both buckets are visible as separate summary cards.
  await expect(page.getByText("ورودی قطعی این ماه")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("خروجی قطعی این ماه")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("ورودی پیش‌بینی این ماه")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("خروجی پیش‌بینی این ماه")).toBeVisible({ timeout: 90_000 });
  // The month grid renders (Saturday-first, 7 columns).
  await expect(page.getByRole("grid")).toBeVisible({ timeout: 90_000 });
  // The 30/60/90/120-day horizon table renders.
  await expect(page.getByText("پیش‌بینی ۳۰ تا ۱۲۰ روزه")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("۰ تا ۳۰ روز آینده")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("۹۱ تا ۱۲۰ روز آینده")).toBeVisible({ timeout: 90_000 });
  // The source breakdown renders (or its honest empty state).
  await expect(page.getByText("تفکیک بر اساس منبع")).toBeVisible({ timeout: 90_000 });
});

test("month navigation changes the displayed month", async ({ page }) => {
  await page.goto("/reports/cash-forecast");
  await expect(page.getByRole("grid")).toBeVisible({ timeout: 90_000 });
  const label = page.locator("span[aria-live='polite']");
  const before = await label.textContent();
  await page.getByRole("button", { name: "ماه بعد" }).click();
  await expect(label).not.toHaveText(before ?? "", { timeout: 30_000 });
  await page.getByRole("button", { name: "ماه قبل" }).click();
  await expect(label).toHaveText(before ?? "", { timeout: 30_000 });
});

test("clicking a day shows that day's items (or its empty state)", async ({ page }) => {
  await page.goto("/reports/cash-forecast");
  await expect(page.getByRole("grid")).toBeVisible({ timeout: 90_000 });
  const firstDay = page.getByRole("grid").getByRole("button").first();
  await firstDay.click();
  await expect(page.getByRole("heading", { name: /^اقلام / })).toBeVisible({ timeout: 30_000 });
});

test("reports page has a cash-forecast tab", async ({ page }) => {
  await page.goto("/reports");
  await expect(page.getByRole("tab", { name: "پیش‌بینی جریان نقد" })).toBeVisible({
    timeout: 90_000,
  });
  await page.getByRole("tab", { name: "پیش‌بینی جریان نقد" }).click();
  await expect(page.getByRole("grid")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("پیش‌بینی ۳۰ تا ۱۲۰ روزه")).toBeVisible({ timeout: 90_000 });
});

test("dashboard shows the cash card and links to the calendar", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "داشبورد", exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByText("پیش‌بینی نقد ۳۰ روز آینده")).toBeVisible({ timeout: 90_000 });
  await page.getByRole("link", { name: "مشاهدهٔ تقویم پیش‌بینی" }).click();
  await expect(page.getByRole("heading", { name: "پیش‌بینی جریان نقد" })).toBeVisible({
    timeout: 90_000,
  });
});

test("cheque report 12 renders the stored-history trend (or its empty state)", async ({ page }) => {
  await page.goto("/cheques/reports?report=clearing-match");
  await expect(page.getByRole("heading", { name: "تطبیق وصول" })).toBeVisible({ timeout: 90_000 });
  // Either the stored trend renders, or the honest empty state does.
  const empty = page.getByText("هنوز هیچ تطبیق وصلی اجرا نشده است");
  const trend = page.getByText("نرخ تطبیق ماهانه");
  await expect(empty.or(trend)).toBeVisible({ timeout: 90_000 });
  // The run-clearing form is still there.
  await expect(page.getByRole("heading", { name: "اجرای تطبیق وصول روی یک صورت‌حساب" })).toBeVisible({
    timeout: 90_000,
  });
});
