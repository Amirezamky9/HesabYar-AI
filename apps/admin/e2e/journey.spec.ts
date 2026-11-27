import { expect, test } from "@playwright/test";
import { CUSTOMER_NAME, login, DEV_KEY, STATEMENT_NAME } from "./auth";
import {
  TEST_MARK,
  pickAccounts,
  resolveTestJournalId,
  selectAccount,
  stateBadge,
} from "./journal-helpers";

/**
 * Full Phase 3 journey (US-305) in a live browser against the real
 * backend + Tryton: login → dashboard → accounts → journal
 * create/validate/post/reverse → invoices → payments → statements →
 * reports → logout. Every page must be reachable, no console errors,
 * no dead buttons on the walked path.
 *
 * The journey creates real Tryton moves in the dev DB — every test
 * draft is described «تست رابط کاربری …» so the data is identifiable.
 */

// Cold dev-server compiles on this VM exceed Playwright's 30s default.
test.setTimeout(180_000);

test("full journey: login → dashboard → accounts → journal cycle → invoices → payments → statements → reports → logout", async ({
  page,
}) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");

  // Console errors and page errors anywhere in the journey fail the test.
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => pageErrors.push(String(err)));

  // --- login → dashboard ---
  await login(page);
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });
  // Dashboard widgets load (live tiles or honest coming-soon tiles).
  await expect(page.getByText("پیش‌نویس‌های ثبت‌نشده").first()).toBeVisible({ timeout: 30_000 });

  // --- accounts ---
  await page.goto("/accounting/accounts");
  await expect(page.getByRole("heading", { name: "سر فصل حساب‌ها" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/حساب قابل‌ثبت/)).toBeVisible({ timeout: 30_000 });
  // The CSV export button is live (enabled once the chart loads).
  await expect(page.getByRole("button", { name: "خروجی CSV" })).toBeEnabled({ timeout: 30_000 });

  // --- journal create → validate → post → reverse ---
  const [debitAccount, creditAccount] = await pickAccounts(page);
  const journalId = await resolveTestJournalId();
  const description = `${TEST_MARK} — سفر کامل ${Date.now()}`;

  await page.goto("/accounting/journals/new");
  await expect(page.getByText("سند جدید", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByLabel("شرح سند").fill(description);
  await page.getByLabel("شناسهٔ دفتر روزنامه (اختیاری)").fill(String(journalId));
  await selectAccount(page, "حساب ردیف ۱", debitAccount.code);
  await selectAccount(page, "حساب ردیف ۲", creditAccount.code);
  await page.getByLabel("بدهکار").first().fill("1000000");
  await page.getByLabel("بستانکار").nth(1).fill("1000000");
  await expect(page.getByTestId("balance-bar")).toContainText("تراز است");

  await page.getByRole("button", { name: "ذخیرهٔ پیش‌نویس" }).click();
  await expect(page).toHaveURL(/\/accounting\/journals\/\d+$/, { timeout: 30_000 });
  const draftId = Number(page.url().match(/\/journals\/(\d+)$/)![1]);
  await expect(page.getByText(description)).toBeVisible();
  await expect(stateBadge(page, "پیش‌نویس")).toBeVisible();

  await page.getByRole("button", { name: "بررسی اعتبار" }).click();
  await expect(page.getByText("سند معتبر است و آمادهٔ ثبت است.")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "ثبت سند", exact: true }).click();
  await expect(page.getByText("سند ثبت شود؟")).toBeVisible();
  await page.getByRole("button", { name: "ثبت سند", exact: true }).last().click();
  await expect(page.getByText("سند ثبت شد.")).toBeVisible({ timeout: 30_000 });
  await expect(stateBadge(page, "ثبت‌شده")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "برگشت سند" }).click();
  await expect(page.getByText("سند برگشت بخورد؟")).toBeVisible();
  await page.getByLabel("شرح برگشت (اختیاری)").fill("برگشت سفر کامل");
  await page.getByRole("button", { name: "برگشت سند", exact: true }).last().click();
  await expect(page.getByText("سند برگشت خورد.")).toBeVisible({ timeout: 30_000 });
  await expect(page).toHaveURL(new RegExp(`/accounting/journals/\\d+\\?origin=${draftId}$`), {
    timeout: 30_000,
  });

  // The journal list still loads and exports after the cycle.
  await page.goto("/accounting/journals");
  await expect(page.getByRole("heading", { name: "دفتر روزنامه" })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("table tbody tr").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "خروجی CSV" })).toBeEnabled();

  // --- invoices ---
  await page.goto("/invoices");
  await expect(page.getByRole("heading", { name: "فاکتورها" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "خروجی CSV" })).toBeEnabled();

  // --- payments ---
  await page.goto("/payments");
  await expect(page.getByRole("heading", { name: "پرداخت‌ها" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "خروجی CSV" })).toBeEnabled();

  // --- statements ---
  await page.goto("/banking/statements");
  await expect(page.getByRole("heading", { name: "صورت‌حساب‌های بانکی" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(STATEMENT_NAME).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "خروجی CSV" })).toBeEnabled();

  // --- reports ---
  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "پرداخت‌ها" }).click();
  await expect(page.getByText("دریافتی").first()).toBeVisible({ timeout: 30_000 });
  // US-550 wired the financial statements live (no more coming-soon cards).
  await page.getByRole("tab", { name: "صورت‌های مالی" }).click();
  const trialBalance = page.getByTestId("report-trial-balance");
  await expect(trialBalance).toBeVisible({ timeout: 30_000 });
  await expect(trialBalance.getByText(/تراز متوازن است/)).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "دفتر روزنامه" }).click();
  const ledger = page.getByTestId("report-general-ledger");
  await expect(ledger).toBeVisible({ timeout: 30_000 });
  await expect(ledger.getByText(/جمع بدهکار/)).toBeVisible({ timeout: 30_000 });

  // --- logout ---
  await page.getByRole("button", { name: "خروج" }).click();
  await expect(page).toHaveURL(/\/login$/, { timeout: 30_000 });
  await expect(page.getByLabel("کلید دسترسی")).toBeVisible();
  // The session is gone: the dashboard redirects back to /login.
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/, { timeout: 30_000 });

  // No console errors, no page errors anywhere in the journey.
  expect(consoleErrors, `console errors during the journey:\n${consoleErrors.join("\n")}`).toEqual([]);
  expect(pageErrors, `page errors during the journey:\n${pageErrors.join("\n")}`).toEqual([]);
});
