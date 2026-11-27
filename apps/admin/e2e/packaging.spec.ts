import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";
import { firstCompanyId } from "./journal-helpers";

/**
 * Packaging & shipping (US-515, Phase 5 wave 2), against the real
 * backend (Supabase) and the real Tryton dev DB. Full flow: login →
 * save packaging settings (engine ids from setup_packaging_us515.py;
 * locations resolved via the BFF, category/root from env with dev
 * defaults) → create a material via the UI → set a reorder point →
 * record a consumption → price calculator → open a count → order-cost
 * lookup. Every figure comes from the live API.
 *
 * Created materials carry a unique stamp per run (`US-515 e2e
 * <stamp>`) so the test finds its own rows even when earlier runs
 * left materials in the shared dev DB. The consumption goes negative
 * by design (no stock was received) — that is the warning signal, not
 * an error.
 *
 * Env (dev defaults match setup_packaging_us515.py on the dev DB):
 *   US515_CATEGORY_ID        (default 7)
 *   US515_ANALYTIC_ROOT_ID   (default 2)
 * The spec skips cleanly when HESABYAR_DEV_API_KEY is absent.
 */

// Cold server compiles on this VM exceed Playwright's 30s default,
// and every BFF round-trip hits Supabase.
test.setTimeout(360_000);

const CATEGORY_ID = Number(process.env.US515_CATEGORY_ID ?? 7);
const ANALYTIC_ROOT_ID = Number(process.env.US515_ANALYTIC_ROOT_ID ?? 2);

function stamp(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

/** Collect console errors for one test; asserted at the end. */
function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    errors.push(`${page.url()}: ${msg.text()}`);
  });
  page.on("pageerror", (err) => errors.push(`${page.url()}: pageerror: ${err.message}`));
  return errors;
}

async function seedSettings(page: Page, companyId: string): Promise<void> {
  const locations = await page.request.get(
    `/api/bff/packaging/locations?company_id=${companyId}`,
  );
  expect(locations.ok()).toBe(true);
  const rows = (await locations.json()) as { engine_id: number; code: string | null }[];
  const byCode = new Map(rows.map((row) => [row.code, row.engine_id]));
  const pack = byCode.get("PACK");
  const produce = byCode.get("PACK-CONSUME");
  const warehouse = byCode.get("WH");
  expect(pack, "PACK location missing — run setup_packaging_us515.py").toBeDefined();
  expect(produce, "PACK-CONSUME location missing — run setup_packaging_us515.py").toBeDefined();
  expect(warehouse, "WH warehouse missing").toBeDefined();
  const saved = await page.request.put("/api/bff/packaging/settings", {
    data: {
      company_id: companyId,
      packaging_location_id: pack,
      production_location_id: produce,
      warehouse_id: warehouse,
      packaging_category_id: CATEGORY_ID,
      analytic_root_id: ANALYTIC_ROOT_ID,
      days_of_cover_window_days: 30,
      low_stock_alert_days: 7,
    },
  });
  expect(saved.ok(), `settings save failed: ${await saved.text()}`).toBe(true);
}

async function createMaterial(page: Page, name: string): Promise<void> {
  await page.getByTestId("create-material").click();
  await page.getByTestId("material-name").fill(name);
  await page.getByTestId("material-submit").click();
  await expect(page.getByRole("row", { name }).first()).toBeVisible({ timeout: 60_000 });
}

test.describe("packaging (US-515)", () => {
  test.beforeEach(async () => {
    test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY is not set (mint with scripts/mint-dev-key.py)");
  });

  test("materials flow: create, reorder point, consumption", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    const runStamp = stamp();
    const materialName = `US-515 e2e ${runStamp} کارتن`;
    const orderRef = `ORD-515-${runStamp}`;

    await login(page);
    const companyId = await firstCompanyId(page);
    await seedSettings(page, companyId);

    await page.goto("/packaging");
    await expect(page.getByRole("heading", { name: "انبار و بسته‌بندی" })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByTestId("packaging-setup-notice")).toHaveCount(0);

    await createMaterial(page, materialName);
    const row = page.getByRole("row", { name: materialName }).first();

    // Reorder point: min 10, target 50.
    await row.getByRole("button", { name: "نقطهٔ سفارش" }).click();
    await page.getByTestId("reorder-min").fill("10");
    await page.getByTestId("reorder-target").fill("50");
    await page.getByTestId("reorder-submit").click();
    await expect(row).toContainText("کمینه ۱۰", { timeout: 60_000 });

    // Consumption: 2 units for the order.
    await row.getByRole("button", { name: "ثبت مصرف" }).click();
    await page.getByTestId("consume-order").fill(orderRef);
    await page.getByTestId("consume-quantity").fill("2");
    await page.getByTestId("consume-submit").click();
    // The table refreshes; the stock goes to -2 (negative by design).
    await expect(row).toContainText("منفی", { timeout: 60_000 });

    // Supply check runs without error.
    await page.getByTestId("supply-check").click();
    await expect(page.getByTestId("supply-report")).toBeVisible({ timeout: 120_000 });

    expect(errors, `console errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("price calculator computes an approximate total", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    const runStamp = stamp();
    const materialName = `US-515 e2e ${runStamp} چسب`;

    await login(page);
    const companyId = await firstCompanyId(page);
    await seedSettings(page, companyId);

    await page.goto("/packaging");
    await expect(page.getByRole("heading", { name: "انبار و بسته‌بندی" })).toBeVisible({
      timeout: 60_000,
    });
    await createMaterial(page, materialName);

    await page.goto("/packaging/calculator");
    await expect(page.getByRole("heading", { name: "محاسبه‌گر قیمت" })).toBeVisible({
      timeout: 60_000,
    });
    await page.getByTestId("quote-goods").fill("500000");
    await page.getByTestId("quote-material-0").click();
    await page.getByRole("option", { name: materialName }).click();
    await page.getByTestId("quote-quantity-0").fill("2");
    // Explicit shipment (no carrier rate seeded) + 10% margin.
    await page.getByTestId("quote-shipment-mode").click();
    await page.getByRole("option", { name: "مبلغ دستی" }).click();
    await page.getByTestId("quote-shipment").fill("7500");
    await page.getByTestId("quote-compute").click();

    const result = page.getByTestId("quote-result");
    await expect(result).toContainText("تقریبی", { timeout: 60_000 });
    // goods 500000 + packaging 0 (fresh material, no cost) + shipment 7500 = 507500; +10% = 558250.
    // MoneyDisplay renders the number and the unit in separate spans,
    // so raw textContent has no space between them — match with a
    // spacing-tolerant regex instead of a literal "۵۵۸٬۲۵۰ تومان".
    await expect(result.getByText(/۵۵۸٬۲۵۰\s*تومان/)).toBeVisible({ timeout: 60_000 });

    expect(errors, `console errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("counts: open a count and see it listed", async ({ page }) => {
    const errors = collectConsoleErrors(page);

    await login(page);
    const companyId = await firstCompanyId(page);
    await seedSettings(page, companyId);

    await page.goto("/packaging/counts");
    await expect(page.getByRole("heading", { name: "شمارش دوره‌ای مواد" })).toBeVisible({
      timeout: 60_000,
    });
    await page.getByTestId("create-count").click();
    await page.getByTestId("count-by").fill("انباردار آزمایشی");
    await page.getByTestId("count-submit").click();
    // The new count opens in the detail dialog. Creating it round-trips
    // Tryton (create inventory + complete lines ≈ 20s over Supabase),
    // so the assertion needs the long timeout — the create dialog shows
    // «در حال باز کردن…» until the API responds.
    await expect(page.getByRole("dialog")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByRole("dialog")).toContainText("انباردار آزمایشی", {
      timeout: 120_000,
    });
    await page.keyboard.press("Escape");

    expect(errors, `console errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("order cost: lookup shows the report", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    const runStamp = stamp();
    const materialName = `US-515 e2e ${runStamp} نایلون`;
    const orderRef = `ORD-515-${runStamp}`;

    await login(page);
    const companyId = await firstCompanyId(page);
    await seedSettings(page, companyId);

    await page.goto("/packaging");
    await expect(page.getByRole("heading", { name: "انبار و بسته‌بندی" })).toBeVisible({
      timeout: 60_000,
    });
    await createMaterial(page, materialName);
    const row = page.getByRole("row", { name: materialName }).first();
    await row.getByRole("button", { name: "ثبت مصرف" }).click();
    await page.getByTestId("consume-order").fill(orderRef);
    await page.getByTestId("consume-quantity").fill("1");
    await page.getByTestId("consume-submit").click();
    await expect(row).toContainText("منفی", { timeout: 60_000 });

    await page.goto("/packaging/order-cost");
    await expect(page.getByRole("heading", { name: "هزینهٔ کامل سفارش" })).toBeVisible({
      timeout: 60_000,
    });
    await page.getByTestId("order-ref").fill(orderRef);
    await page.getByTestId("order-search").click();
    const report = page.getByTestId("order-cost-report");
    await expect(report).toBeVisible({ timeout: 60_000 });
    // No cost centre yet — the page offers to create one.
    await expect(report).toContainText("مرکز هزینه", { timeout: 60_000 });
    await page.getByTestId("order-create-center").click();
    await expect(report).toContainText("بسته‌بندی", { timeout: 60_000 });
    await expect(report).toContainText("جمع هزینه", { timeout: 60_000 });

    expect(errors, `console errors:\n${errors.join("\n")}`).toEqual([]);
  });

  test("materials page has no horizontal overflow at 360px", async ({ page }) => {
    await login(page);
    const companyId = await firstCompanyId(page);
    await seedSettings(page, companyId);
    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto("/packaging");
    await expect(page.getByRole("heading", { name: "انبار و بسته‌بندی" })).toBeVisible({
      timeout: 60_000,
    });
    const overflow = await page.evaluate(() => {
      const el = document.scrollingElement ?? document.documentElement;
      return el.scrollWidth - el.clientWidth;
    });
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
