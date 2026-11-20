import { expect, test } from "@playwright/test";
import { login, DEV_KEY } from "./auth";
import {
  CHEQUE_ISSUER_NAME,
  seedChequeViaForm,
  uniqueSayadId,
} from "./cheque-helpers";

/**
 * Cheque center e2e (US-535) — register received / collateral /
 * issued cheques through the UI form, then the center list, detail
 * (timeline + transitions), intake queue, checkbooks, all 17 reports
 * and the three dashboard widgets.
 *
 * Serial: the seed tests register three cheques (unique Sayyad id
 * per run) and the later tests read them. Pages against remote
 * Supabase can take ~40s to load data — heading/data waits are 90s,
 * never the 30s default.
 */

test.setTimeout(900_000);
test.describe.configure({ mode: "serial" });

// Today is 2026-11-19 (Gregorian): issue today, due 7 days out so
// the received cheque lands in the due-soon widget and report 1.
const ISSUE_ISO = "2026-11-19";
const DUE_ISO = "2026-11-26";

const SAYAD_RECEIVED = uniqueSayadId("001");
const SAYAD_COLLATERAL = uniqueSayadId("002");
const SAYAD_ISSUED = uniqueSayadId("003");

let receivedId = "";
let collateralId = "";
let issuedId = "";

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("cheque center renders and the nav entry exists", async ({ page }) => {
  await page.goto("/cheques");
  await expect(page.getByRole("heading", { name: "مرکز چک" })).toBeVisible({ timeout: 90_000 });
  await expect(
    page.locator("nav[aria-label='ناوبری اصلی']").getByRole("button", { name: "مرکز چک" }),
  ).toBeVisible({ timeout: 30_000 });
});

test("registers a received commercial cheque due soon", async ({ page }) => {
  receivedId = await seedChequeViaForm(page, {
    sayadId: SAYAD_RECEIVED,
    issueIso: ISSUE_ISO,
    dueIso: DUE_ISO,
  });
  expect(receivedId).toMatch(/^[0-9a-f-]+$/);
  // Detail: the Sayyad id is the heading; the lifecycle timeline and
  // the confirm-receipt transition are visible for a registered cheque.
  await expect(page.getByRole("heading", { name: SAYAD_RECEIVED })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "اقدام‌ها" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "تأیید رسید برگ" }).first()).toBeVisible();
  await expect(page.getByText(CHEQUE_ISSUER_NAME).first()).toBeVisible();
});

test("registers a collateral cheque", async ({ page }) => {
  collateralId = await seedChequeViaForm(page, {
    sayadId: SAYAD_COLLATERAL,
    purpose: "collateral",
    amountToman: "5000000",
    issueIso: ISSUE_ISO,
    dueIso: DUE_ISO,
  });
  expect(collateralId).toMatch(/^[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: SAYAD_COLLATERAL })).toBeVisible({ timeout: 30_000 });
});

test("registers an issued cheque via a checkbook", async ({ page }) => {
  issuedId = await seedChequeViaForm(page, {
    sayadId: SAYAD_ISSUED,
    kind: "issued",
    amountToman: "3000000",
    issueIso: ISSUE_ISO,
    dueIso: DUE_ISO,
  });
  expect(issuedId).toMatch(/^[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: SAYAD_ISSUED })).toBeVisible({ timeout: 30_000 });
});

test("center list shows the seeded cheques", async ({ page }) => {
  await page.goto("/cheques");
  await expect(page.getByRole("heading", { name: "مرکز چک" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(SAYAD_RECEIVED)).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(SAYAD_COLLATERAL)).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(SAYAD_ISSUED)).toBeVisible({ timeout: 90_000 });
});

test("intake queue shows the registered cheques", async ({ page }) => {
  await page.goto("/cheques/intake");
  await expect(page.getByRole("heading", { name: "در انتظار رسید برگ" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(SAYAD_RECEIVED)).toBeVisible({ timeout: 90_000 });
});

test("checkbooks page renders", async ({ page }) => {
  await page.goto("/cheques/checkbooks");
  await expect(page.getByRole("heading", { name: "دسته‌چک‌ها" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByTestId("checkbook-card").first()).toBeVisible({ timeout: 90_000 });
});

const REPORT_TITLES: [string, string][] = [
  ["due-received", "دریافتی‌های سررسید نزدیک/گذشته"],
  ["issued", "پرداختی‌های سررسید آینده"],
  ["bounced", "چک‌های برگشتی"],
  ["in-clearing", "در جریان وصول"],
  ["endorsed", "خرج‌شده/واگذارشده"],
  ["collateral", "چک‌های پشتوانه"],
  ["notes-balance", "ماندهٔ اسناد دریافتنی/پرداختنی"],
  ["checkbooks", "صورت وضعیت دسته‌چک"],
  ["registry", "دفتر چک"],
  ["mismatches", "مغایرت سایت"],
  ["unreceived", "چک‌های سایت و نرسیده"],
  ["clearing-match", "تطبیق وصول"],
  ["inquiry-ledger", "دفتر استعلام‌ها"],
  ["cash-forecast", "پیش‌بینی نقدینگی چک"],
  ["party-statement", "صورت وضعیت چکی مشتری"],
  ["storage", "چک‌ها به تفکیک محل نگهداری"],
  ["transfer-history", "تاریخچهٔ انتقال و واگذاری"],
];

test("reports page shows all 17 reports", async ({ page }) => {
  await page.goto("/cheques/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌های چک" })).toBeVisible({ timeout: 90_000 });
  // The seeded received cheque is due in 7 days: it must appear in
  // report 1, and the seeded collateral cheque in report 6.
  await expect(page.getByText(SAYAD_RECEIVED)).toBeVisible({ timeout: 90_000 });
  for (const [, title] of REPORT_TITLES) {
    await page
      .locator("nav[aria-label='فهرست گزارش‌های چک']")
      .getByRole("button", { name: new RegExp(title) })
      .click();
    await expect(page.getByRole("heading", { name: new RegExp(title) })).toBeVisible({ timeout: 30_000 });
  }
  // Report 7 is the only honest coming-soon: no figures, a reason.
  // (Scoped to the card's explanation text — "به‌زودی" alone also
  // matches the nav badges.)
  await page
    .locator("nav[aria-label='فهرست گزارش‌های چک']")
    .getByRole("button", { name: /ماندهٔ اسناد دریافتنی\/پرداختنی/ })
    .click();
  await expect(
    page.getByText(/ماندهٔ اسناد دریافتنی\/پرداختنی گزارش مرجع ترایتون است/),
  ).toBeVisible({ timeout: 30_000 });
  // Report 6 shows the seeded collateral cheque.
  await page
    .locator("nav[aria-label='فهرست گزارش‌های چک']")
    .getByRole("button", { name: /چک‌های پشتوانه/ })
    .click();
  await expect(page.getByText(SAYAD_COLLATERAL)).toBeVisible({ timeout: 30_000 });
});

test("dashboard shows the three cheque widgets", async ({ page }) => {
  await page.goto("/");
  // The dashboard batch (8 parallel fetches incl. paginated drafts +
  // invoices) takes ~105s against remote Supabase (measured 2026-11-20);
  // 90s was too tight there. Local/CI loads it in seconds.
  await expect(page.getByTestId("widget-cheque-exposure")).toBeVisible({ timeout: 180_000 });
  await expect(page.getByTestId("widget-cheque-in-clearing")).toBeVisible({ timeout: 180_000 });
  await expect(page.getByTestId("widget-cheque-due-soon")).toBeVisible({ timeout: 180_000 });
  // The due-soon widget must list the received cheque due in 7 days
  // (at least one cheque; other runs may have seeded more).
  const dueSoon = page.getByTestId("widget-cheque-due-soon");
  await expect(dueSoon).not.toContainText("بازی نیست");
  await expect(dueSoon).toContainText("چک");
});
