import { expect, test } from "@playwright/test";
import { DEV_KEY, STATEMENT_ID, login } from "./auth";

// The remote Tryton backend answers in ~7s per call; a full import flow
// (login + companies + journals + detect + import + confirm + detail +
// reconcile) needs well over the 30s default test timeout.
test.setTimeout(240_000);

/**
 * Bank statement import + reconcile e2e (US-514, B1/B2) against the real
 * backend + Tryton. Requires HESABYAR_DEV_API_KEY and the US304_* seed
 * ids (see e2e/auth.ts). The import uses fresh bank refs per run so
 * re-runs never hit the dedup layer.
 */

const JOURNAL_NAME = "HesabYar US-304 Bank";

function csvFixture(refPrefix: string): string {
  return (
    "تاریخ,شرح,برداشت,واریز,مانده,شماره سند\n" +
    `1405/08/26,واریز مشتری ${refPrefix},,۲٬۰۰۰٬۰۰۰,۷٬۰۰۰٬۰۰۰,${refPrefix}-1\n` +
    `1405/08/26,پرداخت تأمین‌کننده ${refPrefix},۵۰۰٬۰۰۰,,۶٬۵۰۰٬۰۰۰,${refPrefix}-2\n`
  );
}

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  test.skip(!STATEMENT_ID, "US304 seed ids not set — run /tmp/seed_us304.py");
  await login(page);
});

test("import wizard: upload CSV, preview, confirm, and see the draft statement", async ({
  page,
}) => {
  const refPrefix = `E2E-${Date.now()}`;
  await page.goto("/banking/statements/import");
  await expect(
    page.getByRole("heading", { name: "ایمپورت صورت‌حساب بانکی" }),
  ).toBeVisible({ timeout: 30_000 });

  // Journals and accounts load from the API; pick the seeded journal and
  // the first postable account as the default (suspense) account.
  await page.getByLabel("دفتر صورت‌حساب").click();
  await page.getByRole("option", { name: JOURNAL_NAME }).click();
  await page.getByLabel("حساب پیش‌فرض ردیف‌های بدون کد").click();
  const firstAccount = page.getByRole("option").first();
  // The accounts list takes ~33s against remote Tryton.
  await expect(firstAccount).toBeVisible({ timeout: 90_000 });
  await firstAccount.click();

  // Upload the CSV fixture; detection fills the mapping editor.
  await page
    .locator("#import-file")
    .setInputFiles({
      name: "baam.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(csvFixture(refPrefix), "utf-8"),
    });
  await expect(page.getByText("ستون‌های شناسایی‌شده")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("قالب: CSV")).toBeVisible();

  // Import → preview: 2 new rows, 2,000,000 in, 500,000 out.
  await page.getByRole("button", { name: "ایمپورت و پیش‌نمایش" }).click();
  // The import POST measures ~36s (journal + full-chart validation
  // against remote Tryton); the BFF allows 120s for it.
  await expect(page.getByText("پیش‌نمایش ایمپورت")).toBeVisible({
    timeout: 120_000,
  });
  await expect(page.getByText("واریز مشتری")).toBeVisible();
  await expect(page.getByText("پرداخت تأمین‌کننده")).toBeVisible();
  await expect(page.getByText("۲۰۰٬۰۰۰ تومان").first()).toBeVisible();

  // Confirm → the draft statement exists; follow the link to it.
  await page.getByRole("button", { name: "تأیید و ثبت صورت‌حساب" }).click();
  await expect(page.getByText("صورت‌حساب ثبت شد")).toBeVisible({ timeout: 120_000 });
  await page.getByRole("link", { name: "مشاهدهٔ صورت‌حساب" }).click();
  await expect(page.getByText("واریز مشتری")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("۲۰۰٬۰۰۰ تومان").first()).toBeVisible();

  // Reconcile the new statement: no invoice matches these amounts, so
  // both lines are unmatched and the manual-match form appears.
  await page.getByRole("button", { name: "تطبیق صورت‌حساب" }).click();
  await expect(page.getByText("تطبیق دستی یک ردیف")).toBeVisible({ timeout: 60_000 });
});

test("reconciliation-status report tab shows the imported journal", async ({ page }) => {
  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({
    timeout: 30_000,
  });
  await page.getByRole("tab", { name: "تطبیق بانکی" }).click();
  await expect(page.getByText("وضعیت تطبیق بانکی")).toBeVisible({ timeout: 60_000 });
  // The seeded journal has lines in the current Jalali month (the US-304
  // seed statement is dated 2026-11-17).
  await expect(page.getByText(JOURNAL_NAME).first()).toBeVisible({ timeout: 60_000 });
});

test("import page is responsive at 360px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/banking/statements/import");
  await expect(
    page.getByRole("heading", { name: "ایمپورت صورت‌حساب بانکی" }),
  ).toBeVisible({ timeout: 30_000 });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBe(0);
});
