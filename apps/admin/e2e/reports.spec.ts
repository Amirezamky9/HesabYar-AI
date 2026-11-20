import { expect, test } from "@playwright/test";
import { CUSTOMER_NAME, login, DEV_KEY } from "./auth";

/**
 * Reports section (US-305), against the real backend and Tryton.
 * Requires the FastAPI backend on :8001 and HESABYAR_DEV_API_KEY.
 *
 * Live views come from the seeded US-304 rows (draft invoice 20 for
 * party «HesabYar US-304 Test Customer», payment 7); the coming-soon
 * views assert the honest backend-gap reasons and NO figures.
 */

// Cold dev-server compiles on this VM exceed Playwright's 30s default.
test.setTimeout(120_000);

test("invoice summaries show live data with company, period and unit", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });

  // The invoices tab is the default view.
  await expect(page.getByRole("tab", { name: "فاکتورها" })).toHaveAttribute("aria-selected", "true");

  // §11.3 provenance: every figure states company, period and unit.
  await expect(page.getByText(/شرکت:.*دوره:.*واحد: تومان/).first()).toBeVisible({ timeout: 30_000 });

  // The seeded draft invoice appears in the by-state summary (state
  // «پیش‌نویس») and in the by-party summary (its party name).
  await expect(page.getByText("پیش‌نویس").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible();

  // The recent-invoices table links to the seeded invoice's detail page.
  const recentRow = page.getByText(CUSTOMER_NAME).last();
  await expect(recentRow).toBeVisible();
  await recentRow.click();
  await expect(page).toHaveURL(/\/invoices\/\d+$/, { timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible();
});

test("invoice summary rows link to the filtered invoice list", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });

  // By-state row -> /invoices?state=draft, filtered to drafts.
  await page.locator('a[href="/invoices?state=draft"]').first().click();
  await expect(page).toHaveURL(/\/invoices\?state=draft/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "فاکتورها" })).toBeVisible();
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });

  // By-party row -> /invoices?q=<party>, filtered to that party.
  await page.goto("/reports");
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });
  await page.locator(`a[href^="/invoices?q="]`).first().click();
  await expect(page).toHaveURL(/\/invoices\?q=/, { timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });
});

test("payment summaries show live data and link to the filtered list", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "پرداخت‌ها" }).click();

  await expect(page.getByText(/شرکت:.*دوره:.*واحد: تومان/).first()).toBeVisible({ timeout: 30_000 });
  // The seeded payment is a draft receivable for the test party.
  await expect(page.getByText("دریافتی").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible();

  // By-kind row -> /payments?kind=receivable, filtered to receivables.
  await page.locator('a[href="/payments?kind=receivable"]').first().click();
  await expect(page).toHaveURL(/\/payments\?kind=receivable/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "پرداخت‌ها" })).toBeVisible();
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });
});

test("posted ledger and financial statements are honest coming-soon", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });

  // Posted-move ledger: the drafts endpoint never lists posted moves.
  await page.getByRole("tab", { name: "دفتر روزنامه" }).click();
  const ledger = page.getByTestId("report-coming-soon-ledger");
  await expect(ledger).toBeVisible();
  await expect(ledger.getByText("به‌زودی")).toBeVisible();
  await expect(ledger).toContainText("فقط پیش‌نویس‌ها را برمی‌گرداند");
  // No figure is shown: no money amount, no count.
  await expect(ledger).not.toContainText("تومان");
  // The card links to the closest live view (drafts list).
  await ledger.getByRole("link", { name: "مشاهدهٔ پیش‌نویس‌های سند" }).click();
  await expect(page).toHaveURL(/\/accounting\/journals$/, { timeout: 30_000 });

  // Financial statements: all four need the backend report endpoint.
  await page.goto("/reports");
  await page.getByRole("tab", { name: "صورت‌های مالی" }).click();
  for (const testId of [
    "report-coming-soon-trial-balance",
    "report-coming-soon-balance-sheet",
    "report-coming-soon-pl",
    "report-coming-soon-cash-flow",
  ]) {
    const card = page.getByTestId(testId);
    await expect(card).toBeVisible();
    await expect(card.getByText("به‌زودی")).toBeVisible();
    await expect(card).toContainText("نقطهٔ پایانی گزارش");
    await expect(card).not.toContainText("تومان");
  }
});

test("report CSV export downloads normalized values", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });

  const downloadPromise = page.waitForEvent("download", { timeout: 30_000 });
  // First export button on the invoices tab = by-state summary.
  await page.getByRole("button", { name: "خروجی CSV" }).first().click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^report-invoices-by-state-\d{4}-\d{2}-\d{2}\.csv$/);
  const path = await download.path();
  const content = (await import("node:fs/promises")).readFile(path!, "utf-8");
  const text = await content;
  // BOM + Persian headers + normalized (English code, integer IRR) rows.
  expect(text.startsWith("\uFEFF")).toBe(true);
  expect(text).toContain("وضعیت,تعداد فاکتور,مبلغ بدون مالیات (ریال),مالیات (ریال),مبلغ کل (ریال)");
  expect(text).toContain("draft,");
});

test("journal and account lists export CSV too", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  // Journals list (US-305 adds the export there).
  await page.goto("/accounting/journals");
  await expect(page.locator("table tbody tr").first()).toBeVisible({ timeout: 30_000 });
  const journalDownload = page.waitForEvent("download", { timeout: 30_000 });
  await page.getByRole("button", { name: "خروجی CSV" }).click();
  const journalFile = await journalDownload;
  expect(journalFile.suggestedFilename()).toMatch(/^journals-\d{4}-\d{2}-\d{2}\.csv$/);
  const journalPath = await journalFile.path();
  const journalText = await (await import("node:fs/promises")).readFile(journalPath!, "utf-8");
  expect(journalText).toContain("شناسه,تاریخ,شرح,دوره,وضعیت,بدهکار (ریال),بستانکار (ریال)");

  // Accounts page (US-305 adds the export there).
  await page.goto("/accounting/accounts");
  await expect(page.getByRole("heading", { name: "سر فصل حساب‌ها" })).toBeVisible({ timeout: 30_000 });
  const accountDownload = page.waitForEvent("download", { timeout: 30_000 });
  await page.getByRole("button", { name: "خروجی CSV" }).click();
  const accountFile = await accountDownload;
  expect(accountFile.suggestedFilename()).toMatch(/^accounts-\d{4}-\d{2}-\d{2}\.csv$/);
  const accountPath = await accountFile.path();
  const accountText = await (await import("node:fs/promises")).readFile(accountPath!, "utf-8");
  expect(accountText).toContain("شناسه,کد,نام,نوع,قابل‌ثبت,فعال,بسته");
});

test("print view hides the app chrome", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });

  // The print button exists (it calls window.print — not executed here).
  await expect(page.getByRole("button", { name: "چاپ گزارش" })).toBeVisible();

  // Under print media the sidebar, top bar and in-page controls hide
  // while the report content stays visible.
  await page.emulateMedia({ media: "print" });
  await expect(page.locator("aside")).toBeHidden();
  await expect(page.locator("header")).toBeHidden();
  await expect(page.getByRole("button", { name: "چاپ گزارش" })).toBeHidden();
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible();
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible();
});
