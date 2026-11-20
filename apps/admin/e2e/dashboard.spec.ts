import { expect, test, type Page } from "@playwright/test";

/**
 * US-551: the unified dashboard (six live sections), navigation, chart
 * of accounts and fiscal views — live in the browser against the real
 * backend + Tryton.
 *
 * Requires the FastAPI backend on :8001 and HESABYAR_DEV_API_KEY
 * (see apps/admin/README.md). Tests skip cleanly when the key is absent.
 *
 * Current constants (verified against the code, not the old spec):
 * - The dev company ("HesabYar Adapter Test") has 153 accounts, 114
 *   postable: the universal chart (135 / 99) + the 18 HesabYar
 *   template accounts, 15 of which carry a type
 *   (hesabyar_setup/account_chart_en.xml). The receivable type filter
 *   matches 9 accounts (3 universal + 6 extension).
 * - 2 open fiscal years (FY 2026 + FY 2027), 24 open periods; the
 *   newest open year (FY 2027) is the default everywhere.
 * - Standards & compliance is a live module now (US-405); the live
 *   list also includes the cheque, installments, settlement and
 *   packaging centers.
 *
 * The dev database is shared with the sibling stories, whose live
 * verification creates drafts/invoices/payments/cheques. Dashboard
 * assertions therefore accept either the empty or the populated
 * state — never a fixed count. What must always hold: every section
 * renders with company + period + unit, every KPI links to its
 * operational page, and no widget shows a fabricated number.
 */

const DEV_KEY = process.env.HESABYAR_DEV_API_KEY ?? "";
const COMPANY = "HesabYar Adapter Test";

// The unified dashboard fires ~16 API calls; the dashboard batch took
// ~105s against remote Supabase in ۵ث, so the timeout matches the
// cheques spec's dashboard-test timeout (180s). CI's Tryton is local
// and fast, but do not lower this.
test.setTimeout(180_000);

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

test("unified dashboard shows all six live sections with company, period and unit", async ({
  page,
}) => {
  // Company context: the top-bar selector shows the live company name.
  await expect(page.getByLabel("شرکت فعال")).toContainText(COMPANY, { timeout: 15_000 });

  // All six live sections + the US-553 cash-forecast section (live card)
  // section + the overview section, in order.
  const sections = [
    ["dashboard-section-today", "پولِ امروز"],
    ["dashboard-section-on-the-way", "پولِ در راه"],
    ["dashboard-section-attention", "نیازمند توجه"],
    ["dashboard-section-treasury", "خزانه"],
    ["dashboard-section-cash-forecast", "پیش‌بینی نقدینگی"],
    ["dashboard-section-alerts", "هشدارها"],
    ["dashboard-section-imports", "ایمپورت‌های دستی"],
    ["dashboard-section-overview", "وضعیت کلی"],
  ] as const;
  for (const [testId, heading] of sections) {
    const section = page.getByTestId(testId);
    await expect(section).toBeVisible({ timeout: 30_000 });
    await expect(section.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  }

  // پولِ امروز: live widget, empty state or today's figures.
  const today = page.getByTestId("widget-today-money");
  await expect(today.getByText(/پولِ امروز · /)).toBeVisible();
  await expect(
    today
      .getByText("امروز دریافتی یا پرداختی موفقی ثبت نشده است.")
      .or(today.getByText("دریافتی امروز")),
  ).toBeVisible();

  // پولِ در راه: live widget, empty state or KPI rows.
  const onTheWay = page.getByTestId("widget-money-on-the-way");
  await expect(onTheWay.getByText("پولِ در راه", { exact: true })).toBeVisible();
  await expect(
    onTheWay
      .getByText("در این ماه پولی در راه نیست؛ نه قسط تسویه‌ای باز است، نه چکی در وصول، نه قسطی سررسید دارد.")
      .or(onTheWay.getByText("مطالبات درگاه")),
  ).toBeVisible();

  // نیازمند توجه: the review-queue widget, empty or with counters.
  const attention = page.getByTestId("widget-review-queue");
  await expect(attention.getByText("صف بازبینی", { exact: true })).toBeVisible();
  await expect(
    attention.getByText("آیتم در انتظار بررسی نیست؛ صف خالی است.").or(attention.locator("li").first()),
  ).toBeVisible();

  // خزانه: cashboxes + issued-due-this-week + the three cheque widgets,
  // all company-scoped; bank book balances are an honest coming-soon
  // row (no API source yet), never a zero.
  const treasury = page.getByTestId("widget-treasury");
  await expect(treasury.getByText("خزانه", { exact: true })).toBeVisible();
  await expect(
    treasury.getByText("صندوق فعالی ثبت نشده است.").or(treasury.getByText(/صندوق فعال/)),
  ).toBeVisible();
  await expect(
    treasury.getByText("در ۷ روز آینده (و سررسیدگذشته) چک پرداختی بازی نیست.").or(
      treasury.getByText(/چک · نزدیک‌ترین سررسید/),
    ),
  ).toBeVisible();
  await expect(
    treasury.getByText("ماندهٔ دفتری بانک‌ها و ماندهٔ قابل‌برداشت واقعی هنوز نقطهٔ پایانی در API ندارند"),
  ).toBeVisible();
  await expect(page.getByTestId("widget-cheque-exposure")).toBeVisible();
  await expect(page.getByTestId("widget-cheque-in-clearing")).toBeVisible();
  await expect(page.getByTestId("widget-cheque-due-soon")).toBeVisible();

  // پیش‌بینی نقدینگی: the live US-553 cash card — next-30-days
  // confirmed net, or the honest empty state when nothing is due.
  const forecast = page.getByTestId("widget-cash-forecast");
  await expect(forecast.getByText("پیش‌بینی نقد ۳۰ روز آینده", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(
    forecast
      .getByText("در ۳۰ روز آینده هیچ ورودی یا خروجی قطعی ثبت نشده است.")
      .or(forecast.getByText(/خالص قطعی:/)),
  ).toBeVisible({ timeout: 30_000 });

  // هشدارها: empty state or live alert rows.
  const alerts = page.getByTestId("widget-alerts");
  await expect(alerts.getByText("هشدارها", { exact: true })).toBeVisible();
  await expect(
    alerts.getByText("هشداری نیست؛ همه‌چیز در وضعیت عادی است.").or(alerts.locator("li").first()),
  ).toBeVisible();

  // ایمپورت‌های دستی: empty state or live import rows.
  const imports = page.getByTestId("widget-manual-imports");
  await expect(imports.getByText("ایمپورت‌های دستی", { exact: true })).toBeVisible();
  await expect(
    imports.getByText("هنوز ایمپورت دستی ثبت نشده است.").or(imports.locator("li").first()),
  ).toBeVisible();

  // Overview widgets (kept from US-302): drafts, receivables, payables,
  // recent lists — empty or populated, never a fixed count.
  const drafts = page.getByTestId("widget-drafts");
  await expect(drafts.getByText("پیش‌نویس‌های ثبت‌نشده", { exact: true })).toBeVisible();
  await expect(
    drafts.getByText("پیش‌نویس ثبت‌نشده‌ای نیست").or(drafts.getByText(/^[۰-۹٬]+ پیش‌نویس$/)),
  ).toBeVisible();
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
  const recentInvoices = page.getByTestId("widget-recent-invoices");
  await expect(
    recentInvoices.getByText("هنوز صورت‌حسابی ثبت نشده است.").or(recentInvoices.locator("li").first()),
  ).toBeVisible();
  const recentPayments = page.getByTestId("widget-recent-payments");
  await expect(
    recentPayments.getByText("هنوز پرداختی ثبت نشده است.").or(recentPayments.locator("li").first()),
  ).toBeVisible();

  // Setup tile: live counts from setup-status (153 accounts, FY 2027, 24 open periods).
  const setup = page.getByTestId("widget-setup");
  await expect(setup.getByText("۱۵۳ حساب نصب‌شده")).toBeVisible();
  await expect(setup.getByText("۲۴ دوره")).toBeVisible();
  await expect(setup.getByText("FY 2027", { exact: true })).toBeVisible();

  // Every live tile states company + period + unit.
  const metas = page.locator("p", { hasText: `شرکت: ${COMPANY}` });
  expect(await metas.count()).toBeGreaterThanOrEqual(6);
  await expect(page.locator("p", { hasText: "دوره: سال مالی FY 2027" }).first()).toBeVisible();
  await expect(page.locator("p", { hasText: "واحد: تومان" }).first()).toBeVisible();

  // Widget links point at the underlying pages.
  await expect(
    page.getByTestId("widget-money-on-the-way").getByRole("link", { name: "مشاهدهٔ زمان‌بندی تسویه" }),
  ).toHaveAttribute("href", "/settlements/schedule");
  await expect(
    page.getByTestId("widget-treasury").getByRole("link", { name: "مشاهدهٔ صندوق‌ها" }),
  ).toHaveAttribute("href", "/banking/cash-boxes");
  await expect(
    page.getByTestId("widget-alerts").getByRole("link", { name: "مشاهدهٔ صف بازبینی" }),
  ).toHaveAttribute("href", "/review-queue");
  await expect(
    page.getByTestId("widget-cash-forecast").getByRole("link", { name: "مشاهدهٔ تقویم پیش‌بینی" }),
  ).toHaveAttribute("href", "/reports/cash-forecast");
  await expect(page.getByRole("link", { name: "مشاهدهٔ روزنامه‌ها" })).toHaveAttribute(
    "href",
    "/accounting/journals",
  );
  await expect(page.getByRole("link", { name: "مشاهدهٔ صورت‌حساب‌های فروش" })).toHaveAttribute(
    "href",
    "/invoices?type=out",
  );
});

test("sidebar lists all §18 modules; later-phase modules are disabled coming-soon", async ({
  page,
}) => {
  const nav = page.locator("nav[aria-label='ناوبری اصلی']");
  const live = [
    "داشبورد",
    "حسابداری",
    "فروش و خرید",
    "دریافت و پرداخت",
    "بانک و صندوق",
    "مرکز چک",
    "مرکز اقساط",
    "مرکز تسویه",
    "انبار و بسته‌بندی",
    "گزارش‌ها",
    "استانداردها و انطباق",
  ];
  for (const label of live) {
    await expect(nav.getByText(label, { exact: true }).first()).toBeVisible();
  }
  const comingSoon = [
    "دارایی ثابت",
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

  // Cheque center group expands to its children (US-535).
  await nav.getByRole("button", { name: "مرکز چک" }).click();
  await expect(nav.getByRole("link", { name: "مرکز چک", exact: true })).toHaveAttribute(
    "href",
    "/cheques",
  );
  await expect(nav.getByRole("link", { name: "گزارش‌های چک" })).toHaveAttribute(
    "href",
    "/cheques/reports",
  );

  // Standards & compliance group is live now (US-405).
  await nav.getByRole("button", { name: "استانداردها و انطباق" }).click();
  await expect(nav.getByRole("link", { name: "مرکز انطباق" })).toHaveAttribute("href", "/compliance");
  await expect(nav.getByRole("link", { name: "استانداردها" })).toHaveAttribute(
    "href",
    "/compliance/standards",
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

  // Live counts: 153 accounts, 114 postable.
  await expect(page.getByText("۱۵۳ حساب · ۱۱۴ حساب قابل‌ثبت")).toBeVisible({ timeout: 15_000 });

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
  await expect(page.getByText("۱ حساب از ۱۵۳ حساب")).toBeVisible();

  // Search by name.
  await page.getByLabel("جستجو در حساب‌ها").fill("Receivable");
  await expect(page.getByText("Accounts, Notes And Loans Receivable")).toBeVisible();
  await expect(page.getByText("Nontrade And Other Receivables")).toBeVisible();

  // Type filter: «حساب‌های دریافتنی» shows only receivable accounts —
  // 9 now (3 universal + 6 from the HesabYar chart extension).
  await page.getByLabel("جستجو در حساب‌ها").fill("");
  await page.getByLabel("نوع حساب").click();
  await page.getByRole("option", { name: /حساب‌های دریافتنی/ }).click();
  await expect(page.getByText("۹ حساب از ۱۵۳ حساب")).toBeVisible();
  await expect(page.getByText("Accounts, Notes And Loans Receivable")).toBeVisible();
  await expect(page.getByText("Trade Payables")).toHaveCount(0);

  // Clear filters returns to the tree.
  await page.getByRole("button", { name: "پاک کردن فیلترها" }).click();
  await expect(page.getByText("Universal Chart of Accounts")).toBeVisible();
});

test("accounts search with no match shows an empty state", async ({ page }) => {
  await page.goto("/accounting/accounts");
  await expect(page.getByText("۱۵۳ حساب · ۱۱۴ حساب قابل‌ثبت")).toBeVisible({ timeout: 15_000 });
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

  // Years tab: both open years, newest first, Jalali dates.
  // FY 2027 = 2027-01-01..2027-12-31 = ۱۱ دی ۱۴۰۵ .. ۱۰ دی ۱۴۰۶.
  await expect(page.getByText("FY 2027").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("۱۱ دی ۱۴۰۵").first()).toBeVisible();
  await expect(page.getByText("۱۰ دی ۱۴۰۶").first()).toBeVisible();
  await expect(page.getByText("FY 2026").first()).toBeVisible();
  await expect(page.getByText("۱۱ دی ۱۴۰۴").first()).toBeVisible();
  await expect(page.getByText("۱۰ دی ۱۴۰۵").first()).toBeVisible();
  await expect(page.getByText("باز", { exact: true }).first()).toBeVisible();

  // Periods tab: defaults to the newest open year (FY 2027).
  await page.getByRole("tab", { name: "دوره‌های مالی" }).click();
  await expect(page.getByText("2027-01", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("2027-12", { exact: true })).toBeVisible();
  await expect(page.getByText("استاندارد").first()).toBeVisible();

  // Picking FY 2026 shows its 12 periods instead.
  await page.getByLabel("سال مالی").click();
  await page.getByRole("option", { name: /FY 2026/ }).click();
  await expect(page.getByText("2026-01", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("2026-12", { exact: true })).toBeVisible();

  // Setup tab: chart installed, 153 accounts, 24 open periods, ready.
  await page.getByRole("tab", { name: "وضعیت راه‌اندازی" }).click();
  await expect(page.getByText("نصب‌شده · ۱۵۳ حساب")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("۲۴ دورهٔ باز")).toBeVisible();
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
    await expect(page.getByText("۱۵۳ حساب · ۱۱۴ حساب قابل‌ثبت")).toBeVisible({ timeout: 15_000 });
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
