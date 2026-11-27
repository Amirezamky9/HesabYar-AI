import { expect, test } from "@playwright/test";
import { CUSTOMER_NAME, login, DEV_KEY } from "./auth";

/**
 * Reports section (US-305, US-550), against the real backend and Tryton.
 * Requires the FastAPI backend on :8001 and HESABYAR_DEV_API_KEY.
 *
 * Live views come from the seeded US-304 rows (draft invoice 20 for
 * party «HesabYar US-304 Test Customer», payment 7); the ledger and
 * financial statements are live (US-550) over the posted moves in the
 * current Jalali year.
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

test("general ledger shows live summary and drills into one account", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "دفتر روزنامه" }).click();

  const ledger = page.getByTestId("report-general-ledger");
  await expect(ledger).toBeVisible();
  // Summary: totals + one row per account with posted activity.
  await expect(ledger.getByText(/جمع بدهکار/)).toBeVisible({ timeout: 30_000 });
  await expect(ledger.locator("table tbody tr").first()).toBeVisible({ timeout: 30_000 });
  // Source + freshness line (§11.1).
  await expect(ledger.getByText(/منبع: ترایتون/)).toBeVisible();
  await expect(ledger.getByText(/دادهٔ زنده/)).toBeVisible();

  // Row click drills into that account's lines with running balances.
  await ledger.locator("table tbody tr").first().click();
  await expect(ledger.getByText(/^دفتر کل — /)).toBeVisible({ timeout: 30_000 });
  await expect(ledger.getByText(/ماندهٔ ابتدای دوره/)).toBeVisible();
  await expect(ledger.locator("table tbody tr").first()).toBeVisible({ timeout: 30_000 });

  // Back to the summary.
  await ledger.getByRole("button", { name: "بازگشت به خلاصهٔ همهٔ حساب‌ها" }).click();
  await expect(ledger.getByText(/جمع بدهکار/)).toBeVisible({ timeout: 30_000 });
});

test("financial statements are live with comparisons and charts", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "صورت‌های مالی" }).click();

  // Trial balance (default inner tab): live figures + drill-down.
  const trialBalance = page.getByTestId("report-trial-balance");
  await expect(trialBalance).toBeVisible();
  await expect(trialBalance.getByText(/تراز متوازن است/)).toBeVisible({ timeout: 30_000 });
  await expect(trialBalance.getByText(/دادهٔ زنده/)).toBeVisible();
  await trialBalance.locator("table tbody tr").first().click();
  // Drill-down switches to the ledger tab with that account selected.
  await expect(page.getByRole("tab", { name: "دفتر روزنامه" })).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByTestId("report-general-ledger").getByText(/^دفتر کل — /),
  ).toBeVisible({ timeout: 30_000 });

  // Balance sheet: prior-period comparison column + balance check.
  await page.getByRole("tab", { name: "صورت‌های مالی" }).click();
  await page.getByRole("tab", { name: "ترازنامه", exact: true }).click();
  const balanceSheet = page.getByTestId("report-balance-sheet");
  await expect(balanceSheet).toBeVisible();
  await expect(balanceSheet.getByText(/ترازنامه متوازن است/)).toBeVisible({ timeout: 30_000 });
  await expect(balanceSheet.getByText(/مقایسه \(/)).toBeVisible();

  // P&L: monthly revenue/expense bar chart + net income.
  await page.getByRole("tab", { name: "صورت سود و زیان", exact: true }).click();
  const incomeStatement = page.getByTestId("report-income-statement");
  await expect(incomeStatement).toBeVisible();
  await expect(incomeStatement.getByText(/سود خالص/)).toBeVisible({ timeout: 30_000 });
  await expect(
    incomeStatement.getByRole("img", { name: /نمودار میله‌ای ماهانه/ }),
  ).toBeVisible({ timeout: 30_000 });

  // Cash flow: three sections + waterfall chart + tie-out.
  await page.getByRole("tab", { name: "صورت جریان وجوه نقد", exact: true }).click();
  const cashFlow = page.getByTestId("report-cash-flow");
  await expect(cashFlow).toBeVisible();
  await expect(cashFlow.getByText(/ماندهٔ پایان دوره/)).toBeVisible({ timeout: 30_000 });
  await expect(cashFlow.getByRole("img", { name: /نمودار آبشاری/ })).toBeVisible({ timeout: 30_000 });
  await expect(cashFlow.getByText(/فعالیت‌های عملیاتی/)).toBeVisible();
});

test("trial balance CSV export carries the applied filters in its header", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/reports");
  await page.getByRole("tab", { name: "صورت‌های مالی" }).click();
  const trialBalance = page.getByTestId("report-trial-balance");
  await expect(trialBalance.getByText(/تراز متوازن است/)).toBeVisible({ timeout: 30_000 });

  const downloadPromise = page.waitForEvent("download", { timeout: 30_000 });
  await trialBalance.getByRole("button", { name: "خروجی CSV" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^report-trial-balance-\d{4}-\d{2}-\d{2}\.csv$/);
  const path = await download.path();
  const text = await (await import("node:fs/promises")).readFile(path!, "utf-8");
  expect(text.startsWith("\uFEFF")).toBe(true);
  expect(text).toContain("# تراز آزمایشی");
  expect(text).toContain("# شرکت:");
  expect(text).toContain("# دوره:");
  expect(text).toContain("کد حساب,نام حساب,بدهکار (ریال),بستانکار (ریال),مانده (ریال)");
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
