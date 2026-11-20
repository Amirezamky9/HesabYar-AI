import { expect, test } from "@playwright/test";
import { login, DEV_KEY } from "./auth";
import {
  buildSettlementCsv,
  faDigits,
  jalaliPeriodOfToday,
  loadAllSchedulePages,
  nextContractValidFrom,
  pickJalaliDate,
  seedSettlementWiring,
  seedSnappPaySale,
  SNAPPAY_NET_IRR,
  trancheRowsFor,
} from "./settlement-helpers";

/**
 * Settlement center e2e (US-525) — the SnappPay 4-tranche scenario
 * end to end (payment → import → deposit → tie-out → settled), the
 * import wizard's duplicate detection, contract versioning, and the
 * review-queue deep link.
 *
 * Serial: the scenario test seeds one payment (unique transaction id
 * per run) and the later tests reuse its import + payment. Pages
 * against remote Supabase can take ~40s to load data — heading/data
 * waits are 90s, never the 30s default.
 */

// 2700s: the scenario test legitimately takes ~30 min against the
// remote dev DB — wiring (~1 min) + the Tryton sale seed (invoice
// create/validate/post + fee, ~3 min of engine round-trips) +
// schedule/import pages (~40s loads each) + the deposit's inline
// tie-out, measured under ~23 min for the full 4-tranche chain
// (Tryton chain ~13 min + post-Tryton flush/commit tail under
// 10 min, runs 9/12; the BFF gives this path 30 min). The
// per-assertion waits (90s) are unchanged except the deposit's
// first toast (below).
test.setTimeout(2_700_000);
test.describe.configure({ mode: "serial" });

const RUN_ID = Date.now().toString(36);
const TXN_ID = `E2E-525-${RUN_ID}`;
const TRACKING_REF = `TRK525${RUN_ID}`;
const CSV_TEXT = buildSettlementCsv(TXN_ID, TRACKING_REF);

let paymentId = "";
let salesPeriod = jalaliPeriodOfToday();

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("settlement report renders", async ({ page }) => {
  await page.goto("/settlements");
  await expect(page.getByRole("heading", { name: "مرکز تسویه" })).toBeVisible({ timeout: 90_000 });
  // The nav module exists (a collapsible group button, like the other modules).
  await expect(
    page.locator("nav[aria-label='ناوبری اصلی']").getByRole("button", { name: "مرکز تسویه" }),
  ).toBeVisible({ timeout: 30_000 });
});

test("SnappPay 4-tranche payment settles end to end", async ({ page }) => {
  await seedSettlementWiring(page, TXN_ID);
  // The sale first: posted Tryton invoice (tranche payment term +
  // gateway payee) → payment linked to it → sale fee (policy A).
  // The invoice + fee are what the tie-out's clearing pre-check
  // reconciles the tranches against.
  const recorded = await seedSnappPaySale(page, TXN_ID);
  paymentId = recorded.paymentId;
  // The sales period comes from the recorded payment (the backend derives
  // it from the confirmation timestamp) — never assumed from the clock.
  salesPeriod = recorded.salesPeriod;

  // 1. The schedule shows 4 expected tranches for the payment.
  await page.goto(`/settlements/schedule?channel=snapppay&period=${encodeURIComponent(salesPeriod)}`);
  await expect(page.getByRole("heading", { name: "برنامهٔ قسط‌های تسویه" })).toBeVisible({ timeout: 90_000 });
  await loadAllSchedulePages(page);
  const expectedRows = trancheRowsFor(page, TXN_ID);
  await expect(expectedRows).toHaveCount(4, { timeout: 90_000 });
  await expect(expectedRows.filter({ hasText: "موردانتظار" })).toHaveCount(4);

  // 2. The import wizard: upload the gateway CSV, preview, approve.
  await page.goto("/settlements/import");
  await expect(page.getByRole("heading", { name: "ایمپورت فایل تسویه" })).toBeVisible({ timeout: 90_000 });
  await page.locator("#settlement-channel").click();
  await page.getByRole("option", { name: "اسنپ‌پی" }).click();
  await page.locator("#settlement-import-file").setInputFiles({
    name: `snapppay-settlement-${RUN_ID}.csv`,
    mimeType: "text/csv",
    buffer: Buffer.from(CSV_TEXT, "utf-8"),
  });
  await page.getByRole("button", { name: "آپلود و پیش‌نمایش" }).click();
  await expect(page.getByText("پیش‌نمایش ایمپورت")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("۴ ردیف منطبق")).toBeVisible({ timeout: 90_000 });
  await page.getByRole("button", { name: "تأیید و ثبت تسویه" }).click();
  await expect(page.getByText("تسویه ثبت شد")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("۴ قسط تأیید شد")).toBeVisible();

  // 3. The deposit: same tracking ref as the file → C5 exact-reference
  // auto-match; the company is pre-selected, so the tie-out runs inline
  // and posts the bank move.
  await page.getByRole("link", { name: "مشاهدهٔ ایمپورت و tie-out" }).click();
  await expect(page.getByRole("heading", { name: "ایمپورت فایل تسویه" })).toBeVisible({ timeout: 90_000 });
  await page.getByLabel("مبلغ واریز").fill(String(SNAPPAY_NET_IRR / 10));
  await page.getByLabel("شماره پیگیری بانکی").fill(TRACKING_REF);
  await page.getByRole("button", { name: "ثبت واریز بانکی" }).click();
  // 30 min: the deposit chains the match + inline tie-out + gate +
  // wiring + the per-tranche clearing transfers and bank receipts
  // (8 Tryton moves, dozens of round-trips) plus a post-Tryton
  // flush/commit tail — measured under ~23 min end to end against
  // the remote dev DB, runs 9/12 (the BFF gives this path 30 min).
  // The two toasts below render with the same response, so they
  // keep the 180s wait.
  await expect(page.getByText(/واریز ثبت شد/)).toBeVisible({ timeout: 1_800_000 });
  await expect(page.getByText("۴ قسط دریافت شد")).toBeVisible({ timeout: 180_000 });
  await expect(page.getByText(/سند بانکی ثبت شد/)).toBeVisible({ timeout: 180_000 });

  // 4. The schedule shows all 4 tranches settled.
  await page.goto(
    `/settlements/schedule?channel=snapppay&period=${encodeURIComponent(salesPeriod)}&state=settled`,
  );
  await expect(page.getByRole("heading", { name: "برنامهٔ قسط‌های تسویه" })).toBeVisible({ timeout: 90_000 });
  await loadAllSchedulePages(page);
  const settledRows = trancheRowsFor(page, TXN_ID);
  await expect(settledRows).toHaveCount(4, { timeout: 90_000 });
  await expect(settledRows.filter({ hasText: "تسویه‌شده" })).toHaveCount(4);
});

test("re-uploading the same file is detected as a duplicate", async ({ page }) => {
  await page.goto("/settlements/import");
  await expect(page.getByRole("heading", { name: "ایمپورت فایل تسویه" })).toBeVisible({ timeout: 90_000 });
  await page.locator("#settlement-channel").click();
  await page.getByRole("option", { name: "اسنپ‌پی" }).click();
  await page.locator("#settlement-import-file").setInputFiles({
    name: `snapppay-settlement-${RUN_ID}.csv`,
    mimeType: "text/csv",
    buffer: Buffer.from(CSV_TEXT, "utf-8"),
  });
  await page.getByRole("button", { name: "آپلود و پیش‌نمایش" }).click();
  await expect(page.getByText("این فایل قبلاً ایمپورت شده است")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("link", { name: "مشاهدهٔ ایمپورت قبلی" })).toBeVisible();
});

test("payment detail shows the settled payment", async ({ page }) => {
  expect(paymentId, "the scenario test must run first (serial)").not.toBe("");
  await page.goto(`/settlements/payments/${paymentId}`);
  await expect(page.getByRole("heading", { name: new RegExp(TXN_ID) })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("تسویه‌شده").first()).toBeVisible({ timeout: 30_000 });
});

test("contracts page creates a new version on zarinpal", async ({ page }) => {
  await page.goto("/settlements/contracts");
  await expect(page.getByRole("heading", { name: "قراردادهای درگاه" })).toBeVisible({ timeout: 90_000 });
  await page.locator("#contract-channel").click();
  await page.getByRole("option", { name: "زرین‌پال" }).click();
  // The new version's valid_from must be strictly after the active
  // version's (backend rule) — the form defaults to today, which 422s
  // whenever the active version is already effective today (run 16).
  const validFrom = await nextContractValidFrom(page, "zarinpal");
  await page.getByRole("button", { name: "ایجاد نسخهٔ جدید" }).click();
  await pickJalaliDate(page, "contract-valid-from", validFrom);
  await page.getByLabel("درصد کارمزد (٪) — دستی").fill("5");
  await page.getByLabel("نرخ مالیات کارمزد (٪)").fill("10");
  await page.locator("#contract-model").click();
  await page.getByRole("option", { name: "یکجا ماهانه" }).click();
  await page.getByRole("button", { name: "ایجاد نسخهٔ جدید" }).click();
  await expect(page.getByText(/قرارداد ساخته شد/)).toBeVisible({ timeout: 90_000 });
  // The new version is the active one: fee ۵٪ on the card.
  await expect(page.getByText(`${faDigits(5)}٪`).first()).toBeVisible({ timeout: 30_000 });
});

test("review-queue deep link pre-filters settlement_variance", async ({ page }) => {
  await page.goto("/review-queue?type=settlement_variance");
  await expect(page.getByRole("heading", { name: "صف بازبینی" })).toBeVisible({ timeout: 90_000 });
  await expect(page.locator("#filter-type")).toContainText("مغایرت تسویه", { timeout: 30_000 });
});
