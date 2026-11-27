import { expect, test, type Page } from "@playwright/test";
import { CUSTOMER_NAME, DEV_KEY, PARTY_ID, REVENUE_ACCOUNT_ID, login } from "./auth";

/**
 * Fresh-install journey (US-706) in a live browser against the real
 * backend + Tryton: login → company setup state → first invoice →
 * first report → logout, plus 360px / 390px responsive checks on the
 * same route set.
 *
 * Honest mapping of the acceptance criterion onto what the product
 * has today:
 * - «signup» is the operator API-key login: there is no self-signup,
 *   keys are minted by the operator (apps/admin/scripts/mint-dev-key.py).
 * - «company setup» is the INSTALLED state the dashboard shows: the
 *   chart of accounts, the fiscal years, and the «وضعیت راه‌اندازی»
 *   tab (GET /accounting/setup-status — read-only; the wizard that
 *   ACTS on it is a later story, docs/setup-wizard.md). The
 *   provisioning itself is proven by the CI install scenario
 *   (tests/contract/test_fresh_install_us706.py); this spec proves
 *   the operator sees the installed company as ready in the UI.
 * - «first invoice» creates a real draft (marked as a UI test, like
 *   the other specs' residue); «first report» reads the trial balance.
 */

// Cold dev-server compiles on this VM exceed Playwright's 30s default.
test.setTimeout(180_000);

const INSTALL_MARK = "تست نصب تازه US-706";

/** True when the document has no horizontal overflow at the current viewport. */
async function hasNoHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

test("fresh-install journey: login → setup state → first invoice → first report → logout", async ({
  page,
}) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");

  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => pageErrors.push(String(err)));

  // --- login → dashboard ---
  await login(page);
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

  // --- company setup state: the installed chart is visible ---
  await page.goto("/accounting/accounts");
  await expect(page.getByRole("heading", { name: "سر فصل حساب‌ها" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/حساب قابل‌ثبت/)).toBeVisible({ timeout: 30_000 });

  // --- company setup state: fiscal years exist + setup reads ready ---
  await page.goto("/accounting/fiscal-years");
  await expect(page.getByRole("heading", { name: "سال‌های مالی" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/FY 202\d/).first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "وضعیت راه‌اندازی" }).click();
  await expect(page.getByText(/نصب‌شده · .* حساب/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("آمادهٔ ثبت اسناد")).toBeVisible({ timeout: 30_000 });

  // --- first invoice (needs the US-304 seed ids; skips cleanly without them) ---
  if (PARTY_ID && REVENUE_ACCOUNT_ID) {
    await page.goto("/invoices/new");
    await expect(page.getByRole("heading", { name: "فاکتور جدید" })).toBeVisible({ timeout: 30_000 });

    await page.getByLabel("حساب دریافتنی").click();
    await page.getByRole("option", { name: /1\.2\.1/ }).click();

    await page.getByLabel("شناسهٔ طرف حساب").fill(PARTY_ID);
    await page.getByLabel("مرجع").fill("US-706-E2E");
    await page.getByLabel("شرح", { exact: true }).fill(INSTALL_MARK);

    await page.getByLabel("حساب", { exact: true }).click();
    await page.getByRole("option", { name: /4\.1\.1/ }).click();
    await page.getByLabel("قیمت واحد").fill("500000");
    await page.getByLabel("شرح ردیف").fill("کالای آزمایشی نصب تازه");

    await expect(page.getByText("۵۰۰٬۰۰۰ تومان").first()).toBeVisible({ timeout: 30_000 });

    await page.getByRole("button", { name: "ساخت پیش‌نویس" }).click();

    await expect(page).toHaveURL(/\/invoices\/\d+$/, { timeout: 30_000 });
    await expect(page.getByText(INSTALL_MARK)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(CUSTOMER_NAME).first()).toBeVisible();
    await expect(page.getByText("پیش‌نویس").first()).toBeVisible();

    await page.getByRole("button", { name: "بررسی پیش‌نویس" }).click();
    await expect(page.getByText("پیش‌نویس معتبر است و آمادهٔ مرحلهٔ بعد است.")).toBeVisible({
      timeout: 30_000,
    });
  }

  // --- first report: the trial balance balances ---
  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "صورت‌های مالی" }).click();
  const trialBalance = page.getByTestId("report-trial-balance");
  await expect(trialBalance).toBeVisible({ timeout: 30_000 });
  await expect(trialBalance.getByText(/تراز متوازن است/)).toBeVisible({ timeout: 30_000 });

  // --- logout ---
  await page.getByRole("button", { name: "خروج" }).click();
  await expect(page).toHaveURL(/\/login$/, { timeout: 30_000 });
  await expect(page.getByLabel("کلید دسترسی")).toBeVisible();

  // No console errors, no page errors anywhere in the journey.
  expect(consoleErrors, `console errors during the journey:\n${consoleErrors.join("\n")}`).toEqual([]);
  expect(pageErrors, `page errors during the journey:\n${pageErrors.join("\n")}`).toEqual([]);
});

const INSTALL_ROUTES: { path: string; heading?: string; text?: string }[] = [
  { path: "/", heading: "داشبورد" },
  { path: "/accounting/accounts", heading: "سر فصل حساب‌ها" },
  { path: "/accounting/fiscal-years", heading: "سال‌های مالی" },
  { path: "/invoices/new", heading: "فاکتور جدید" },
  { path: "/reports", heading: "گزارش‌ها" },
];

for (const width of [360, 390]) {
  test(`fresh-install routes have no horizontal overflow at ${width}px`, async ({ page }) => {
    test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
    await page.setViewportSize({ width, height: 800 });

    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(`${page.url()}: ${msg.text()}`);
    });

    // The login page itself must fit too.
    await page.goto("/login");
    await expect(page.getByLabel("کلید دسترسی")).toBeVisible({ timeout: 30_000 });
    expect(await hasNoHorizontalOverflow(page), `horizontal overflow on /login at ${width}px`).toBe(true);

    await login(page);
    await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

    for (const route of INSTALL_ROUTES) {
      await page.goto(route.path);
      if (route.heading) {
        await expect(page.getByRole("heading", { name: route.heading })).toBeVisible({
          timeout: 30_000,
        });
      } else if (route.text) {
        await expect(page.getByText(route.text)).toBeVisible({ timeout: 30_000 });
      }
      expect(
        await hasNoHorizontalOverflow(page),
        `horizontal overflow on ${route.path} at ${width}px`,
      ).toBe(true);
    }

    expect(
      consoleErrors,
      `console errors during the ${width}px pass:\n${consoleErrors.join("\n")}`,
    ).toEqual([]);
  });
}
