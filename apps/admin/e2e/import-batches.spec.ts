import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";
import {
  buildSettlementCsv,
  recordSnappPayPayment,
  seedSettlementWiring,
} from "./settlement-helpers";

/**
 * Unified manual-import batches e2e (US-554) — one full settlement
 * batch flow end to end (upload → mapping → preview → approve), the
 * batch-cycle section on the import detail page, the batch detail
 * page, and the reject → archive → re-run cycle.
 *
 * Serial: the second test reuses the wiring the first test seeds.
 * Pages against remote Supabase can take ~40s to load data — heading
 * waits are 90s, never the 30s default.
 *
 * No deposit / tie-out here: that chain is settlement.spec.ts's. The
 * batch approve runs with the auto-post checkbox UNCHECKED (the
 * default), so the batch ends `approved` (draft-hold) with no Tryton
 * moves — the sale seed is not needed, only the payment record.
 */

test.setTimeout(900_000);
test.describe.configure({ mode: "serial" });

const RUN_ID = Date.now().toString(36);
const TXN_ID = `E2E-554-${RUN_ID}`;
const REJECT_TXN_ID = `E2E-554-R-${RUN_ID}`;
const TRACKING_REF = `TRK554${RUN_ID}`;
const CSV_TEXT = buildSettlementCsv(TXN_ID, TRACKING_REF);
const REJECT_CSV_TEXT = buildSettlementCsv(REJECT_TXN_ID, TRACKING_REF);
const REJECT_FILENAME = `snapppay-settlement-reject-${RUN_ID}.csv`;
const REJECT_REASON = "مبلغ کارمزد اشتباه است";

/** Find a batch by its uploaded filename via the BFF list endpoint. */
async function findBatchByFilename(page: Page, filename: string): Promise<string> {
  const res = await page.request.fetch("/api/bff/imports/batches?kind=settlement");
  expect(res.ok(), `GET imports/batches → ${res.status()}: ${await res.text()}`).toBe(true);
  const batches = (await res.json()) as { id: string; filename: string }[];
  const found = batches.find((b) => b.filename === filename);
  expect(found, `no batch with filename ${filename}`).toBeDefined();
  return found!.id;
}

async function uploadSettlementCsv(page: Page, filename: string, csv: string): Promise<void> {
  await page.goto("/settlements/import");
  await expect(page.getByRole("heading", { name: "ایمپورت فایل تسویه" })).toBeVisible({
    timeout: 90_000,
  });
  await page.locator("#settlement-channel").click();
  await page.getByRole("option", { name: "اسنپ‌پی" }).click();
  await page.locator("#settlement-import-file").setInputFiles({
    name: filename,
    mimeType: "text/csv",
    buffer: Buffer.from(csv, "utf-8"),
  });
  await page.getByRole("button", { name: "آپلود و پیش‌نمایش" }).click();
  await expect(page.getByText("پیش‌نمایش ایمپورت")).toBeVisible({ timeout: 90_000 });
}

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("settlement batch flows upload → mapping → preview → approve", async ({ page }) => {
  await seedSettlementWiring(page, TXN_ID);
  // The payment first (no Tryton invoice needed — the batch approve
  // confirms tranches, it does not post): the 4 CSV rows then match.
  await recordSnappPayPayment(page, TXN_ID);

  await uploadSettlementCsv(page, `snapppay-settlement-${RUN_ID}.csv`, CSV_TEXT);
  await expect(page.getByText("۴ ردیف منطبق")).toBeVisible({ timeout: 90_000 });
  await page.getByRole("button", { name: "تأیید و ثبت تسویه" }).click();
  await expect(page.getByText("تسویه ثبت شد")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("۴ قسط تأیید شد")).toBeVisible();

  // The import detail page shows the batch-cycle section.
  await page.getByRole("link", { name: "مشاهدهٔ ایمپورت و tie-out" }).click();
  await expect(page.getByRole("heading", { name: "ایمپورت فایل تسویه" })).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByText("چرخهٔ بچ ایمپورت")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("تأییدشده").first()).toBeVisible();

  // The batch detail page: status, preview, stages, variance report.
  await page.getByRole("link", { name: "مشاهدهٔ کامل بچ" }).click();
  await expect(
    page.getByRole("heading", { name: "بچ ایمپورت تسویهٔ درگاه" }),
  ).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("پیش‌نمایش اثر حسابداری")).toBeVisible();
  await expect(page.getByText("مراحل بچ")).toBeVisible();
  await expect(page.getByText("گزارش مغایرت بچ")).toBeVisible();
});

test("a rejected batch is archived with its reason and re-runs", async ({ page }) => {
  // No payment seeded for this transaction id: the rows are new
  // payments, which is fine — the batch is rejected before approve.
  await uploadSettlementCsv(page, REJECT_FILENAME, REJECT_CSV_TEXT);
  await expect(page.getByText("۴ پرداخت جدید")).toBeVisible({ timeout: 90_000 });
  await page.locator("#settlement-reject-reason").fill(REJECT_REASON);
  await page.getByRole("button", { name: "رد ایمپورت" }).click();
  await expect(page.getByRole("heading", { name: "مرکز تسویه" })).toBeVisible({
    timeout: 90_000,
  });

  const batchId = await findBatchByFilename(page, REJECT_FILENAME);
  await page.goto(`/settlements/imports/batch/${batchId}`);
  await expect(
    page.getByRole("heading", { name: "بچ ایمپورت تسویهٔ درگاه" }),
  ).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("بایگانی‌شده").first()).toBeVisible();
  await expect(page.getByText(REJECT_REASON)).toBeVisible();

  await page.getByRole("button", { name: "اجرای دوبارهٔ بچ" }).click();
  await expect(page.getByText("در انتظار بازبینی").first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("تلاش ۲")).toBeVisible();
});
