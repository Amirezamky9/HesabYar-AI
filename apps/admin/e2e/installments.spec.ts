import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";
import { pickJalaliDate } from "./settlement-helpers";

/**
 * Installments center e2e (US-545) — plan creation through the UI
 * form, receipt → leg paid, the overdue report (seeded via the BFF
 * API so the legs are overdue on any run date), the reports page,
 * and the plan-change flow (partial settlement → re-amortisation →
 * amendment history).
 *
 * Serial: the seed tests create two plans (unique invoice ids per
 * run) and the later tests read them. Pages against remote Supabase
 * can take ~40s to load data — heading/data waits are 90s, never
 * the 30s default.
 *
 * Money is integer Toman in the forms; the backend converts (×10)
 * to Rial. Journal move ids are recorded, not verified, by the
 * backend (docs/installments.md) — 4242/4243 are the e2e fixtures.
 */

test.setTimeout(900_000);
test.describe.configure({ mode: "serial" });

const RUN = Date.now().toString().slice(-8);
const INVOICE_UI = `INV-545-UI-${RUN}`;
const INVOICE_OVERDUE = `INV-545-OD-${RUN}`;
const CUSTOMER_REF = `CUST-545-${RUN}`;
const JOURNAL_RECEIPT = "4242";
const JOURNAL_SETTLEMENT = "4243";

let planId = "";
let overduePlanId = "";

async function firstCompanyId(page: Page): Promise<string> {
  const res = await page.request.get("/api/bff/companies?limit=50");
  expect(res.ok()).toBeTruthy();
  const body = (await res.json()) as { items: { id: string }[] };
  expect(body.items.length).toBeGreaterThan(0);
  return body.items[0].id;
}

/**
 * Seeds an overdue plan straight through the BFF (two legs due
 * 1405-07-01 / 1405-07-15 — overdue on any run date). Returns the
 * created plan's id.
 */
async function seedOverduePlanViaApi(page: Page): Promise<string> {
  const companyId = await firstCompanyId(page);
  const res = await page.request.post("/api/bff/installments/plans", {
    data: {
      company_id: companyId,
      invoice_id: INVOICE_OVERDUE,
      customer_ref: CUSTOMER_REF,
      legs: [
        { due_date: "1405-07-01", principal_toman: 1_000_000 },
        { due_date: "1405-07-15", principal_toman: 1_000_000 },
      ],
    },
  });
  expect(res.status(), await res.text()).toBe(201);
  const body = (await res.json()) as { id: string };
  return body.id;
}

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("installments center renders and the nav entry exists", async ({ page }) => {
  await page.goto("/installments");
  await expect(page.getByRole("heading", { name: "مرکز اقساط" })).toBeVisible({ timeout: 90_000 });
  await expect(
    page.locator("nav[aria-label='ناوبری اصلی']").getByRole("button", { name: "مرکز اقساط" }),
  ).toBeVisible({ timeout: 30_000 });
});

test("creates a plan through the UI form", async ({ page }) => {
  await page.goto("/installments/new");
  await expect(page.getByRole("heading", { name: "برنامهٔ قسط جدید" })).toBeVisible({ timeout: 90_000 });
  await page.getByLabel("شناسهٔ فاکتور فروش").fill(INVOICE_UI);
  await page.getByLabel("مرجع مشتری").fill(CUSTOMER_REF);
  await page.getByLabel("اصل کل (تومان)").fill("3000000");
  await page.getByLabel("تعداد اقساط").fill("3");
  await page.getByLabel("فاصلهٔ اقساط (روز)").fill("30");
  // First leg due today: the leg is "due" (due ON its due date),
  // so the next test's receipt pays it.
  await pickJalaliDate(page, "installment-first-due-date", new Date().toISOString().slice(0, 10));
  await page.getByRole("button", { name: "ساخت برنامه", exact: true }).click();
  await expect(page).toHaveURL(/\/installments\/[0-9a-f-]+$/, { timeout: 90_000 });
  planId = page.url().split("/installments/")[1];
  expect(planId).toMatch(/^[0-9a-f-]+$/);
  // Detail: the invoice id is the heading; the schedule shows 3
  // legs. The first leg is due today ("سررسیدشده") — or overdue if
  // the date rolled over mid-run, so the test asserts the leg count
  // here and the paid state after the receipt, not the due badge.
  await expect(page.getByRole("heading", { name: INVOICE_UI })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "جدول سررسید" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("۳ قسط").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "اقدام‌ها" })).toBeVisible({ timeout: 30_000 });
});

test("records a receipt and the first leg becomes paid", async ({ page }) => {
  await page.goto(`/installments/${planId}`);
  await expect(page.getByRole("heading", { name: INVOICE_UI })).toBeVisible({ timeout: 90_000 });
  await page.getByRole("button", { name: "دریافت جدید" }).click();
  await page.getByLabel("مبلغ دریافت (تومان)").fill("1000000");
  await page.getByLabel("شناسهٔ سند حسابداری (روزنامه)").first().fill(JOURNAL_RECEIPT);
  await page.getByRole("button", { name: "ثبت دریافت", exact: true }).click();
  await expect(page.getByText("دریافت ثبت شد.")).toBeVisible({ timeout: 90_000 });
  // The first leg (1,000,000 Toman, due today) is paid; the other
  // two are still expected.
  await expect(page.getByText("پرداخت‌شده").first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("در انتظار سررسید").first()).toBeVisible({ timeout: 30_000 });
});

test("center list shows the seeded plan", async ({ page }) => {
  await page.goto("/installments");
  await expect(page.getByRole("heading", { name: "مرکز اقساط" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(INVOICE_UI)).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(CUSTOMER_REF).first()).toBeVisible({ timeout: 30_000 });
});

test("overdue report shows the seeded overdue legs", async ({ page }) => {
  overduePlanId = await seedOverduePlanViaApi(page);
  expect(overduePlanId).toMatch(/^[0-9a-f-]+$/);
  await page.goto("/installments/reports?report=overdue");
  await expect(page.getByRole("heading", { name: "گزارش‌های اقساط" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("heading", { name: "گزارش ۱ — اقساط معوق" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText(INVOICE_OVERDUE)).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("button", { name: "خروجی CSV" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "اجرای پیگیری" })).toBeVisible({ timeout: 30_000 });
});

test("reports page lists all 11 reports", async ({ page }) => {
  await page.goto("/installments/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌های اقساط" })).toBeVisible({ timeout: 90_000 });
  for (const title of [
    "۱. اقساط معوق",
    "۲. پیش‌بینی وصول",
    "۳. سود تأمین مالی تحقق‌یافته",
    "۴. مراحل پیگیری (دونینگ)",
    "۵. BNPL در برابر داخلی",
    "۶. صورت وضعیت مشتری",
    "۷. برنامهٔ اقساط هر قرارداد",
    "۸. تسویه‌های زودهنگام",
    "۹. اقساط با چک برگشتی",
    "۱۰. لاگ یادآوری پیامکی",
    "۱۱. قدمت اقساط (aging)",
  ]) {
    await expect(page.getByRole("button", { name: title, exact: true })).toBeVisible({ timeout: 30_000 });
  }
  // Switching reports swaps the header + source line.
  await page.getByRole("button", { name: "۵. BNPL در برابر داخلی", exact: true }).click();
  await expect(page.getByRole("heading", { name: "گزارش ۵ — BNPL در برابر داخلی" })).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByText("دو دنیا جدا نشان داده می‌شوند", { exact: false })).toBeVisible({
    timeout: 30_000,
  });
});

test("plan change with versioning: partial settlement records an amendment", async ({ page }) => {
  await page.goto(`/installments/${planId}`);
  await expect(page.getByRole("heading", { name: INVOICE_UI })).toBeVisible({ timeout: 90_000 });
  await page.getByRole("button", { name: "تسویهٔ زودهنگام" }).click();
  await page.locator("#settlement-type").click();
  await page.getByRole("option", { name: "جزئی — بازتوزیع اقساط باقی‌مانده" }).click();
  // Legs 2–3 (1,000,000 Toman each, future) are untouched by the
  // allocation rule — the whole 1,500,000 becomes advance and
  // re-amortises the remaining 2,000,000 down to 500,000 over the
  // two open legs (250,000 each).
  await page.getByLabel("مبلغ تسویه (تومان)").fill("1500000");
  await page.getByLabel("شناسهٔ سند حسابداری (روزنامه)").first().fill(JOURNAL_SETTLEMENT);
  await page.getByRole("button", { name: "ثبت تسویه", exact: true }).click();
  await expect(page.getByText("تسویهٔ جزئی انجام شد")).toBeVisible({ timeout: 90_000 });
  // The amendment history records the change with before/after principals.
  await expect(page.getByRole("heading", { name: "تاریخچهٔ تغییرات برنامه" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("تغییر ۱ — بازتوزیع اقساط (تسویهٔ جزئی زودهنگام)")).toBeVisible({
    timeout: 90_000,
  });
});
