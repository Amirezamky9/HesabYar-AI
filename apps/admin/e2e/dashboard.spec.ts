import { expect, test, type Page } from "@playwright/test";

/**
 * US-302: dashboard home, navigation, chart of accounts and fiscal views
 * — live in the browser against the real backend + Tryton.
 *
 * Requires the FastAPI backend on :8001 and HESABYAR_DEV_API_KEY
 * (see apps/admin/README.md). Tests skip cleanly when the key is absent.
 * The dev company ("HesabYar Adapter Test") has 135 accounts, 1 open
 * fiscal year (FY 2026, 12 open periods); drafts/invoices/payments
 * vary because the database is shared with the sibling stories, so the
 * dashboard assertions accept either the empty or the populated state.
 */

const DEV_KEY = process.env.HESABYAR_DEV_API_KEY ?? "";
const COMPANY = "HesabYar Adapter Test";

// Cold dev-server compiles (first "/" + first route per page) plus the
// real backend round-trips can exceed Playwright's 30s default on a
// loaded VM; the login helper below already waits up to 30s for the
// dashboard heading alone.
test.setTimeout(120_000);

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

test("dashboard shows live widgets with company, period and unit on every tile", async ({ page }) => {
  // Company context: the top-bar selector shows the live company name.
  await expect(page.getByLabel("شرکت فعال")).toContainText(COMPANY, { timeout: 15_000 });

  // NOTE: the dev database is shared with the sibling stories, whose live
  // verification creates drafts/invoices/payments. The widgets therefore
  // assert EITHER the empty state OR the populated state — never a fixed
  // count. What must always hold: every widget renders live data with
  // company + period + unit, and no widget shows a fabricated number.
  const drafts = page.getByTestId("widget-drafts");
  await expect(drafts.getByText("پیش‌نویس‌های ثبت‌نشده", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(
    drafts.getByText("پیش‌نویس ثبت‌نشده‌ای نیست").or(drafts.getByText(/^[۰-۹٬]+ پیش‌نویس$/)),
  ).toBeVisible({ timeout: 15_000 });

  // Receivables / payables: only validated/posted invoices count, so draft
  // invoices from sibling runs do not change these — but assert flexibly
  // anyway in case a sibling validates one.
  const receivables = page.getByTestId("widget-receivables");
  await expect(
    receivables
      .getByText("صورت‌حساب فروش باز و پرداخت‌نشده‌ای نیست")
      .or(receivables.getByText("صورت‌حساب باز (تأییدشده یا ثبت‌شده)")),
  ).toBeVisible();
  const payables = page.getByTestId("widget-payables");
  await expect(
    payables
      .getByText("صورت‌حساب خرید باز و پرداخت‌نشده‌ای نیست")
      .or(payables.getByText("صورت‌حساب باز (تأییدشده یا ثبت‌شده)")),
  ).toBeVisible();

  // Recent lists: empty state or live rows.
  const recentInvoices = page.getByTestId("widget-recent-invoices");
  await expect(
    recentInvoices.getByText("هنوز صورت‌حسابی ثبت نشده است.").or(recentInvoices.locator("li").first()),
  ).toBeVisible();
  const recentPayments = page.getByTestId("widget-recent-payments");
  await expect(
    recentPayments.getByText("هنوز پرداختی ثبت نشده است.").or(recentPayments.locator("li").first()),
  ).toBeVisible();

  // Setup tile: live counts from setup-status (135 accounts, FY 2026, 12 open periods).
  const setup = page.getByTestId("widget-setup");
  await expect(setup.getByText("۱۳۵ حساب نصب‌شده")).toBeVisible();
  await expect(setup.getByText("۱۲ دوره")).toBeVisible();

  // Every live tile states company + period + unit.
  const metas = page.locator("p", { hasText: `شرکت: ${COMPANY}` });
  expect(await metas.count()).toBeGreaterThanOrEqual(6);
  await expect(page.locator("p", { hasText: "دوره: سال مالی FY 2026" }).first()).toBeVisible();
  await expect(page.locator("p", { hasText: "واحد: تومان" }).first()).toBeVisible();

  // Coming-soon tiles: no fake numbers.
  await expect(page.getByTestId("widget-coming-soon-cash").getByText("موجودی نقد و بانک")).toBeVisible();
  await expect(page.getByTestId("widget-coming-soon-reports").getByText("تراز آزمایشی و صورت‌های مالی")).toBeVisible();
  await expect(page.getByTestId("widget-coming-soon-tax").getByText("وضعیت مالیات و مودیان")).toBeVisible();
  expect(await page.getByText("به‌زودی", { exact: true }).count()).toBeGreaterThanOrEqual(3);

  // Widget links point at the underlying pages.
  await expect(page.getByRole("link", { name: "مشاهدهٔ روزنامه‌ها" })).toHaveAttribute(
    "href",
    "/accounting/journals",
  );
  await expect(page.getByRole("link", { name: "مشاهدهٔ صورت‌حساب‌های فروش" })).toHaveAttribute(
    "href",
    "/invoices?type=out",
  );
});

test("sidebar lists all 13 §18 modules; later-phase modules are disabled coming-soon", async ({
  page,
}) => {
  const nav = page.locator("nav[aria-label='ناوبری اصلی']");
  const live = ["داشبورد", "حسابداری", "فروش و خرید", "دریافت و پرداخت", "بانک و صندوق", "گزارش‌ها"];
  for (const label of live) {
    await expect(nav.getByText(label, { exact: true }).first()).toBeVisible();
  }
  const comingSoon = [
    "دارایی ثابت",
    "استانداردها و انطباق",
    "یکپارچه‌سازی‌ها",
    "هوش مصنوعی و MCP",
    "مدیریت API",
    "کاربران و دسترسی‌ها",
    "تنظیمات",
  ];
  for (const label of comingSoon) {
    const button = nav.getByRole("button", { name: new RegExp(label) });
    await expect(button).toBeVisible();
    await expect(button).toBeDisabled();
  }
  // No duplicate CRM module, no Tryton navigation.
  await expect(nav.getByText("CRM")).toHaveCount(0);
  await expect(nav.getByText("Tryton")).toHaveCount(0);

  // Accounting group expands to its three children with the exact Phase 3 paths.
  await nav.getByRole("button", { name: "حسابداری" }).click();
  await expect(nav.getByRole("link", { name: "سر فصل حساب‌ها" })).toHaveAttribute(
    "href",
    "/accounting/accounts",
  );
  await expect(nav.getByRole("link", { name: "روزنامه‌ها" })).toHaveAttribute(
    "href",
    "/accounting/journals",
  );
  await expect(nav.getByRole("link", { name: "سال‌های مالی" })).toHaveAttribute(
    "href",
    "/accounting/fiscal-years",
  );
});

test("sidebar collapses to icons and expands again", async ({ page }) => {
  const nav = page.locator("nav[aria-label='ناوبری اصلی']");
  await page.getByRole("button", { name: "بستن نوار کناری" }).click();
  // Labels disappear in collapsed mode (icons + tooltips remain).
  await expect(nav.getByText("فروش و خرید", { exact: true })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "فروش و خرید" })).toBeVisible();
  await expect(page.getByRole("button", { name: "باز کردن نوار کناری" })).toBeVisible();

  await page.getByRole("button", { name: "باز کردن نوار کناری" }).click();
  await expect(nav.getByText("فروش و خرید", { exact: true })).toBeVisible();
});

test("accounts page: tree, search, type filter, detail panel, breadcrumbs", async ({ page }) => {
  const nav = page.locator("nav[aria-label='ناوبری اصلی']");
  // The accounting group starts collapsed on the dashboard home.
  await nav.getByRole("button", { name: "حسابداری" }).click();
  await nav.getByRole("link", { name: "سر فصل حساب‌ها" }).click();
  await expect(page).toHaveURL(/\/accounting\/accounts$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "سر فصل حساب‌ها" })).toBeVisible();

  // Breadcrumbs: داشبورد / حسابداری / سر فصل حساب‌ها.
  const crumbs = page.locator("nav[aria-label='مسیر صفحه']");
  await expect(crumbs.getByRole("link", { name: "حسابداری" })).toHaveAttribute("href", "/accounting");
  await expect(crumbs.locator("li[aria-current='page']")).toHaveText("سر فصل حساب‌ها");

  // Live counts: 135 accounts, 99 postable.
  await expect(page.getByText("۱۳۵ حساب · ۹۹ حساب قابل‌ثبت")).toBeVisible({ timeout: 15_000 });

  // Tree: the root + the six top-level classes are expanded by default.
  await expect(page.getByText("Universal Chart of Accounts")).toBeVisible();
  await expect(page.getByText("Assets", { exact: true })).toBeVisible();
  await expect(page.getByText("1.0.0", { exact: true }).first()).toBeVisible();

  // Expand a depth-2 node and open a leaf's detail panel. (The root and
  // the six top-level classes, including Assets, are expanded by default.)
  await page.getByRole("button", { name: "باز کردن زیرمجموعه‌های Cash And Financial Assets" }).click();
  await page.getByRole("button", { name: /Cash and Cash Equivalents/ }).first().click();
  const detail = page.locator("aside").last();
  await expect(detail.getByText("نقد و معادل‌های نقد")).toBeVisible();
  await expect(detail.getByText("قابل‌ثبت", { exact: true })).toBeVisible();
  await expect(detail.getByText("1.1.1", { exact: true })).toBeVisible();

  // Search by code (Persian digits normalize to ASCII).
  await page.getByLabel("جستجو در حساب‌ها").fill("۲٫۱٫۱");
  await expect(page.getByText("Trade Payables")).toBeVisible();
  await expect(page.getByText("۱ حساب از ۱۳۵ حساب")).toBeVisible();

  // Search by name.
  await page.getByLabel("جستجو در حساب‌ها").fill("Receivable");
  await expect(page.getByText("Accounts, Notes And Loans Receivable")).toBeVisible();
  await expect(page.getByText("Nontrade And Other Receivables")).toBeVisible();

  // Type filter: «حساب‌های دریافتنی» shows only receivable accounts.
  await page.getByLabel("جستجو در حساب‌ها").fill("");
  await page.getByLabel("نوع حساب").click();
  await page.getByRole("option", { name: /حساب‌های دریافتنی/ }).click();
  await expect(page.getByText("۳ حساب از ۱۳۵ حساب")).toBeVisible();
  await expect(page.getByText("Accounts, Notes And Loans Receivable")).toBeVisible();
  await expect(page.getByText("Trade Payables")).toHaveCount(0);

  // Clear filters returns to the tree.
  await page.getByRole("button", { name: "پاک کردن فیلترها" }).click();
  await expect(page.getByText("Universal Chart of Accounts")).toBeVisible();
});

test("accounts search with no match shows an empty state", async ({ page }) => {
  await page.goto("/accounting/accounts");
  await expect(page.getByText("۱۳۵ حساب · ۹۹ حساب قابل‌ثبت")).toBeVisible({ timeout: 15_000 });
  await page.getByLabel("جستجو در حساب‌ها").fill("zzz-no-such-account");
  await expect(page.getByText("حسابی با این مشخصات پیدا نشد.")).toBeVisible();
});

test("fiscal years page: years, periods and setup status from the live API", async ({ page }) => {
  const nav = page.locator("nav[aria-label='ناوبری اصلی']");
  // The accounting group starts collapsed on the dashboard home.
  await nav.getByRole("button", { name: "حسابداری" }).click();
  await nav.getByRole("link", { name: "سال‌های مالی" }).click();
  await expect(page).toHaveURL(/\/accounting\/fiscal-years$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "سال‌های مالی" })).toBeVisible();

  // Years tab: FY 2026 open, Jalali dates.
  await expect(page.getByText("FY 2026").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("۱۱ دی ۱۴۰۴").first()).toBeVisible();
  await expect(page.getByText("۱۰ دی ۱۴۰۵").first()).toBeVisible();
  await expect(page.getByText("باز", { exact: true }).first()).toBeVisible();

  // Periods tab: 12 open standard periods for FY 2026.
  await page.getByRole("tab", { name: "دوره‌های مالی" }).click();
  await expect(page.getByText("2026-01", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("2026-12", { exact: true })).toBeVisible();
  await expect(page.getByText("استاندارد").first()).toBeVisible();

  // Setup tab: chart installed, 135 accounts, ready.
  await page.getByRole("tab", { name: "وضعیت راه‌اندازی" }).click();
  await expect(page.getByText("نصب‌شده · ۱۳۵ حساب")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("۱۲ دورهٔ باز")).toBeVisible();
  await expect(page.getByText("آمادهٔ ثبت اسناد")).toBeVisible();
});

test("keyboard: sidebar link focuses and activates with Enter", async ({ page }) => {
  const link = page
    .locator("nav[aria-label='ناوبری اصلی']")
    .getByRole("link", { name: "سر فصل حساب‌ها" });
  // The accounting group is collapsed on the dashboard home; expand it first.
  await page.locator("nav[aria-label='ناوبری اصلی']").getByRole("button", { name: "حسابداری" }).click();
  await link.focus();
  await expect(link).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/accounting\/accounts$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "سر فصل حساب‌ها" })).toBeVisible();
});

test.describe("mobile layout (390px)", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("no horizontal overflow on the dashboard; hamburger opens the nav sheet", async ({ page }) => {
    await expect(page.getByTestId("widget-drafts")).toBeVisible({ timeout: 15_000 });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBe(0);

    // The desktop sidebar is hidden; navigation goes through the sheet.
    await expect(page.getByRole("button", { name: "بستن نوار کناری" })).toHaveCount(0);
    await page.getByRole("button", { name: "باز کردن ناوبری" }).click();
    const sheet = page.getByRole("dialog", { name: "حسابیار" });
    await expect(sheet).toBeVisible();
    const sheetNav = sheet.locator("nav[aria-label='ناوبری اصلی']");
    await sheetNav.getByRole("button", { name: "حسابداری" }).click();
    await sheetNav.getByRole("link", { name: "سال‌های مالی" }).click();
    await expect(page).toHaveURL(/\/accounting\/fiscal-years$/, { timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "سال‌های مالی" })).toBeVisible();
  });

  test("accounts detail opens in a sheet on mobile", async ({ page }) => {
    await page.goto("/accounting/accounts");
    await expect(page.getByText("۱۳۵ حساب · ۹۹ حساب قابل‌ثبت")).toBeVisible({ timeout: 15_000 });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBe(0);

    await page.getByRole("button", { name: "باز کردن زیرمجموعه‌های Cash And Financial Assets" }).click();
    await page.getByRole("button", { name: /Cash and Cash Equivalents/ }).first().click();
    const sheet = page.getByRole("dialog", { name: "جزئیات حساب" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByText("1.1.1", { exact: true })).toBeVisible();
    await expect(sheet.getByText("نقد و معادل‌های نقد")).toBeVisible();
  });
});

test.describe("mobile layout (320px)", () => {
  test.use({ viewport: { width: 320, height: 700 } });

  test("no horizontal overflow on the dashboard at 320px", async ({ page }) => {
    await expect(page.getByTestId("widget-drafts")).toBeVisible({ timeout: 15_000 });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBe(0);
  });
});
