import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY, STATEMENT_ID } from "./auth";
import { firstCompanyId } from "./journal-helpers";

/**
 * Unified-panel journeys (US-555, Phase 5-6 ۵ح) — CROSS-SECTION flows
 * only. The per-feature specs own the details and must keep passing
 * (dashboard.spec.ts: the six sections; cash-forecast.spec.ts: the
 * calendar; import-batches.spec.ts: the settlement batch flow;
 * review-queue.spec.ts: the queue, filters, actions). This spec
 * exercises the journeys BETWEEN them:
 *
 * - dashboard → cash calendar → back to the dashboard;
 * - dashboard attention widget → review queue → one action per
 *   type-GROUP through the dialog → dashboard counter drops;
 * - dashboard imports section → bank statement import → confirm →
 *   the batch shows in the dashboard imports widget;
 * - the cash calendar at 360–390px (the calendar spec has no
 *   responsive test; dashboard/queue responsive checks live in
 *   their own specs).
 *
 * Serial: the tests mutate shared state (review items, import
 * batches) on one CI database. Assertions are resilient to
 * non-empty dev data: counts are read as a baseline and asserted
 * as DELTAS, never as exact figures, except for data this run
 * created itself (found by its unique stamp/filename).
 *
 * The unified dashboard fires ~16 API calls; the dashboard batch
 * took ~105s against remote Supabase in ۵ث, so the file timeout
 * follows the dashboard/cheques pattern (180s). The two longest
 * journeys are marked slow (180s × 3 = 540s each). CI's Tryton is
 * local and fast, but do not lower these.
 */

test.setTimeout(180_000);
test.describe.configure({ mode: "serial" });

const JOURNAL_NAME = "HesabYar US-304 Bank";
const RUN_ID = Date.now().toString(36);

const FA_DIGITS: Record<string, string> = {
  "۰": "0",
  "۱": "1",
  "۲": "2",
  "۳": "3",
  "۴": "4",
  "۵": "5",
  "۶": "6",
  "۷": "7",
  "۸": "8",
  "۹": "9",
};

/** Parse a Persian-digit number ("۲٬۳۰۰" → 2300). */
function parseFaNumber(text: string): number {
  return Number(text.replace(/[۰-۹]/g, (d) => FA_DIGITS[d]).replace(/٬/g, ""));
}

function csvFixture(refPrefix: string): string {
  return (
    "تاریخ,شرح,برداشت,واریز,مانده,شماره سند\n" +
    `1405/08/26,واریز مشتری ${refPrefix},,۲٬۰۰۰٬۰۰۰,۷٬۰۰۰٬۰۰۰,${refPrefix}-1\n` +
    `1405/08/26,پرداخت تأمین‌کننده ${refPrefix},۵۰۰٬۰۰۰,,۶٬۵۰۰٬۰۰۰,${refPrefix}-2\n`
  );
}

/**
 * Seed one review item via the BFF (same shape as
 * review-queue.spec.ts's seedItem — specs cannot import from each
 * other, so the helper is local). `companyId` MUST be the dashboard's
 * active company: the attention widget is company-scoped, and items
 * filed without a company never appear in its counter.
 */
async function seedReviewItem(
  page: Page,
  companyId: string,
  stamp: string,
  seed: { type: string; severity: string; channel: string | null; amountIrr: number | null },
): Promise<string> {
  const created = await page.request.post("/api/bff/review-queue/items", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      item_type: seed.type,
      severity: seed.severity,
      reason: `US-555 e2e ${stamp} ${seed.type}`,
      suggested_action: "اقدام آزمایشی",
      company_id: companyId,
      channel: seed.channel,
      amount_irr: seed.amountIrr,
    },
  });
  expect(created.ok(), await created.text()).toBe(true);
  return ((await created.json()) as { id: string }).id;
}

/**
 * The attention widget's waiting count (open + in-review), read from
 * the rendered widget: 0 for the empty state, otherwise the leading
 * Persian number of "N آیتم در انتظار بررسی". Call only after the
 * widget is visible — the dashboard renders a skeleton until the
 * summary fetch lands, so a visible widget always holds fresh data.
 */
async function readWaitingCount(page: Page): Promise<number> {
  const widget = page.getByTestId("widget-review-queue");
  const empty = await widget
    .getByText("آیتم در انتظار بررسی نیست؛ صف خالی است.")
    .count();
  if (empty > 0) return 0;
  const text = await widget.getByText(/آیتم در انتظار بررسی/).first().textContent();
  const match = text?.match(/[۰-۹٬]+/);
  expect(match, `no waiting count in widget text: ${text ?? "(null)"}`).not.toBeNull();
  return parseFaNumber(match![0]);
}

async function dashboardWaitingCount(page: Page): Promise<number> {
  await expect(page.getByTestId("widget-review-queue")).toBeVisible({ timeout: 120_000 });
  return readWaitingCount(page);
}

/**
 * Load every queue page until the given reasons are all visible (or
 * the list is exhausted) — the shared DB accumulates items from
 * earlier runs, and this run's seeds sort last. Copied from
 * review-queue.spec.ts (same reason as seedReviewItem).
 */
async function loadAllQueuedItems(page: Page, reasons: string[]): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const allVisible = await page.evaluate(
      (wanted) => wanted.every((r) => document.body.innerText.includes(r)),
      reasons,
    );
    const moreCount = await page.getByRole("button", { name: "بارگذاری بیشتر" }).count();
    if (allVisible) return;
    const more = page.getByRole("button", { name: "بارگذاری بیشتر" });
    if (moreCount > 0) {
      const before = await page.getByTestId("review-item-card").count();
      await more.click();
      await expect
        .poll(() => page.getByTestId("review-item-card").count(), { timeout: 30_000 })
        .toBeGreaterThan(before);
      continue;
    }
    await page.waitForTimeout(1000);
  }
}

/**
 * Perform one action on the seeded item through the dialog: open its
 * card, optionally switch the action select away from the suggested
 * action, perform, and wait for the expected status chip.
 */
async function performDialogAction(
  page: Page,
  reason: string,
  expectedStatus: "حل‌شده" | "در حال بررسی",
  actionLabel?: string,
): Promise<void> {
  await loadAllQueuedItems(page, [reason]);
  const card = page.locator('[data-testid="review-item-card"]', { hasText: reason });
  await card.getByRole("button", { name: "جزئیات و اقدام" }).click();
  const dialog = page.getByTestId("review-item-dialog");
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  if (actionLabel !== undefined) {
    await dialog.locator("#review-action").click();
    await page.getByRole("option", { name: actionLabel }).click();
  }
  await dialog.getByTestId("perform-action").click();
  await expect(dialog.getByText(expectedStatus, { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });
  await dialog.getByRole("button", { name: "بستن" }).click();
}

test.beforeEach(async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
});

test("dashboard → cash calendar → back to the dashboard", async ({ page }) => {
  // The cash-forecast section + card on the dashboard home.
  await expect(page.getByTestId("dashboard-section-cash-forecast")).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByTestId("widget-cash-forecast")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("پیش‌بینی نقد ۳۰ روز آینده")).toBeVisible({ timeout: 90_000 });

  // Follow the card's link to the calendar page.
  await page.getByRole("link", { name: "مشاهدهٔ تقویم پیش‌بینی" }).click();
  await expect(page).toHaveURL(/\/reports\/cash-forecast$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "پیش‌بینی جریان نقد" })).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByRole("grid")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("پیش‌بینی ۳۰ تا ۱۲۰ روزه")).toBeVisible({ timeout: 90_000 });

  // Back through the breadcrumb: the dashboard home renders again
  // with its sections (the calendar spec never takes the return leg).
  // (The sidebar's dashboard group is collapsed on this page —
  // expansion is derived from the pathname — so the breadcrumb's
  // «داشبورد» link is the reliable way home.)
  await page
    .locator("nav[aria-label='مسیر صفحه']")
    .getByRole("link", { name: "داشبورد" })
    .click();
  await expect(page).toHaveURL(/\/$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "داشبورد", exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByTestId("dashboard-section-cash-forecast")).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByTestId("widget-cash-forecast")).toBeVisible({ timeout: 90_000 });
});

test("attention widget → queue → one action per type-group → counter drops", async ({
  page,
}) => {
  // Three dashboard loads + four dialog actions: mark the test slow
  // (180s × 3 = 540s), comfortably above the review-queue spec's 360s.
  test.slow();
  const stamp = String(Date.now());
  const reasonFor = (type: string) => `US-555 e2e ${stamp} ${type}`;

  // Baseline first: the shared DB may already hold waiting items.
  await page.goto("/");
  const baseline = await dashboardWaitingCount(page);

  // One representative item per type-GROUP (not all ten types —
  // the US-552 spec performs one action per type via the BFF):
  // mapping, settlement, cheque, import. All company-scoped so the
  // attention widget counts them.
  const companyId = await firstCompanyId(page);
  const seeds = [
    { type: "mapping", severity: "warning", channel: "zarinpal", amountIrr: 1_500_000 },
    { type: "settlement_variance", severity: "warning", channel: "snapppay", amountIrr: 2_300_000 },
    { type: "cheque_exception", severity: "critical", channel: null, amountIrr: 8_000_000 },
    { type: "manual_import_review", severity: "info", channel: "sadad", amountIrr: null },
  ] as const;
  for (const seed of seeds) {
    await seedReviewItem(page, companyId, stamp, seed);
  }

  // The widget counter rises by exactly the four seeded items.
  await page.goto("/");
  expect(await dashboardWaitingCount(page)).toBe(baseline + 4);

  // Follow the widget's link into the queue.
  await page.getByTestId("widget-review-queue").getByRole("link", { name: "مشاهدهٔ صف بازبینی" }).click();
  await expect(page).toHaveURL(/\/review-queue$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "صف بازبینی" })).toBeVisible({ timeout: 30_000 });

  // One dialog action per group. The mapping item's suggested action
  // is complete_mapping_retry (a learning form the US-552 spec
  // covers), so this journey takes its dismiss instead; the other
  // three take their pre-selected suggested actions.
  await performDialogAction(page, reasonFor("mapping"), "حل‌شده", "رد کردن");
  await performDialogAction(page, reasonFor("settlement_variance"), "حل‌شده");
  await performDialogAction(page, reasonFor("manual_import_review"), "حل‌شده");
  // contact_customer is a NOTE: the item moves to in_review and
  // still counts as waiting — the counter drops by 3, not 4.
  await performDialogAction(page, reasonFor("cheque_exception"), "در حال بررسی");

  // Back on the dashboard, the counter dropped by the three
  // resolved items; the in-review cheque item still waits.
  await page.goto("/");
  expect(await dashboardWaitingCount(page)).toBe(baseline + 1);
});

test("dashboard imports section → statement import → confirm → widget shows the batch", async ({
  page,
}) => {
  // The import POST + confirm each take up to 120s against remote
  // Tryton (bank-import.spec.ts), plus two dashboard loads: mark the
  // test slow (180s × 3 = 540s).
  test.slow();
  test.skip(!STATEMENT_ID, "US304 seed ids not set — run /tmp/seed_us304.py");
  const refPrefix = `E2E-555-${RUN_ID}`;
  const filename = `unified-555-${RUN_ID}.csv`;

  // Start from the dashboard imports section and follow its link.
  await expect(page.getByTestId("dashboard-section-imports")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByTestId("widget-manual-imports")).toBeVisible({ timeout: 90_000 });
  await page.getByTestId("widget-manual-imports").getByRole("link", { name: "ایمپورت صورت‌حساب جدید" }).click();
  await expect(page).toHaveURL(/\/banking\/statements\/import$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "ایمپورت صورت‌حساب بانکی" })).toBeVisible({
    timeout: 30_000,
  });

  // The wizard itself is bank-import.spec.ts's; this journey drives
  // it once to produce the batch (fresh refs so re-runs never hit
  // the dedup layer).
  await page.getByLabel("دفتر صورت‌حساب").click();
  await page.getByRole("option", { name: JOURNAL_NAME }).click();
  await page.getByLabel("حساب پیش‌فرض ردیف‌های بدون کد").click();
  const firstAccount = page.getByRole("option").first();
  await expect(firstAccount).toBeVisible({ timeout: 90_000 });
  await firstAccount.click();
  await page.locator("#import-file").setInputFiles({
    name: filename,
    mimeType: "text/csv",
    buffer: Buffer.from(csvFixture(refPrefix), "utf-8"),
  });
  await expect(page.getByText("ستون‌های شناسایی‌شده")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "ایمپورت و پیش‌نمایش" }).click();
  await expect(page.getByText("پیش‌نمایش ایمپورت")).toBeVisible({ timeout: 120_000 });
  await page.getByRole("button", { name: "تأیید و ثبت صورت‌حساب" }).click();
  await expect(page.getByText("صورت‌حساب ثبت شد")).toBeVisible({ timeout: 120_000 });

  // Back on the dashboard, the imports widget lists the new batch
  // (newest first, title = filename) — the round trip the
  // bank-import spec never takes.
  await page.goto("/");
  const imports = page.getByTestId("widget-manual-imports");
  await expect(imports).toBeVisible({ timeout: 120_000 });
  await expect(imports.getByText(filename)).toBeVisible({ timeout: 120_000 });
  await expect(imports.getByText("صورت‌حساب بانکی").first()).toBeVisible({ timeout: 30_000 });
});

for (const width of [360, 390]) {
  test(`cash calendar: no horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/reports/cash-forecast");
    await expect(page.getByRole("heading", { name: "پیش‌بینی جریان نقد" })).toBeVisible({
      timeout: 90_000,
    });
    await expect(page.getByRole("grid")).toBeVisible({ timeout: 90_000 });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, `horizontal overflow on /reports/cash-forecast at ${width}px`).toBe(0);
  });
}
