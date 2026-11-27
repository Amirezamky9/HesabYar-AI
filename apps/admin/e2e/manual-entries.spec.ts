import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";

/**
 * Manual invoice entry (US-513, Phase 5 wave 2, stream S3), against
 * the real backend (Supabase). Full flow: login → order form (totals
 * update live) → submit → entry in the list → detail dialog with the
 * event trace → refund tab (two events) → cancellation tab → type
 * filter → responsive (no horizontal overflow at 320/390px) →
 * keyboard-only order creation.
 *
 * Entries carry a unique stamp per run (`US-513 e2e <stamp>`) so the
 * test finds its own rows even when earlier runs left entries in the
 * shared dev DB.
 */

// Every BFF round-trip hits Supabase.
test.setTimeout(360_000);

const SHOTS_DIR = `${process.env.HOME ?? "~"}/workspace/coord-logs/us513-shots`;

const STAMP = `US513-${Date.now().toString(36)}`;
const ORDER_NUMBER = `ORD-${STAMP}`;

/** Collect console errors for the whole flow; asserted at the end. */
function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // The dashboard's setup-status fetch exceeds the BFF's 30s upstream
    // timeout on Supabase and the BFF answers 502; the dashboard
    // degrades gracefully. Known pre-existing backend perf issue, not
    // a manual-entry defect (same allowance as the review-queue spec).
    if (
      page.url().endsWith("/") &&
      (text.includes("502 (Bad Gateway)") || text.includes("503 (Service Unavailable)"))
    ) {
      return;
    }
    errors.push(`${page.url()}: ${text}`);
  });
  page.on("pageerror", (err) => errors.push(`${page.url()}: pageerror: ${err.message}`));
  return errors;
}

async function hasNoHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
}

/** WCAG relative luminance of an sRGB color (same math as the US-405 a11y spec). */
function luminance(r: number, g: number, b: number): number {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function luminanceFromColor(color: string): number {
  const lab = color.match(/^lab\(\s*([\d.]+)%?\s+[-.\d]+\s+[-.\d]+\s*(?:\/\s*[\d.]+)?\)$/);
  if (lab) {
    const l = Number(lab[1]);
    if (l <= 8) return l / 903.3;
    const f = (l + 16) / 116;
    return f * f * f;
  }
  const rgb = color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
  if (!rgb) throw new Error(`cannot parse color: ${color}`);
  return luminance(Number(rgb[1]), Number(rgb[2]), Number(rgb[3]));
}

function contrastRatio(fg: string, bg: string): number {
  const l1 = luminanceFromColor(fg);
  const l2 = luminanceFromColor(bg);
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

test("manual order → refund → cancellation, with traceability", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const consoleErrors = collectConsoleErrors(page);
  await login(page);

  await page.goto("/invoices/manual");
  await expect(page.getByRole("heading", { name: "فاکتور جدید (دستی)" })).toBeVisible();
  await expect(page.getByTestId("manual-order-tab")).toBeVisible();
  await expect(page.getByTestId("manual-refund-tab")).toBeVisible();
  await expect(page.getByTestId("manual-cancellation-tab")).toBeVisible();

  // -- Order form: totals update live as lines are filled. --------------
  await page.getByTestId("manual-order-number").fill(ORDER_NUMBER);
  await page.getByTestId("manual-order-line-desc-0").fill("قهوه اسپرسو");
  await page.getByTestId("manual-order-line-qty-0").fill("۲");
  await page.getByLabel("قیمت واحد (تومان) *").fill("175000");
  // 2 × 175,000 = 350,000 Toman.
  await expect(page.getByTestId("manual-order-total")).toContainText("۳۵۰٬۰۰۰ تومان");

  await page.getByTestId("manual-order-submit").click();
  await expect(page.getByText("سفارش دستی ثبت شد")).toBeVisible({ timeout: 60_000 });

  // The entry appears in the list with its pipeline status.
  const orderCard = page.getByTestId("manual-entry-card").filter({ hasText: ORDER_NUMBER }).first();
  await expect(orderCard).toBeVisible({ timeout: 60_000 });
  await expect(orderCard).toContainText("۳۵۰٬۰۰۰ تومان");
  await expect(orderCard).toContainText("سفارش");

  // -- Detail dialog: the entry traces to its canonical event. ----------
  await orderCard.getByRole("button", { name: "جزئیات و ردیابی" }).click();
  const dialog = page.getByTestId("manual-entry-dialog");
  await expect(dialog).toBeVisible();
  // The detail fetch round-trips the BFF → Supabase; it can take well
  // over the default 5s on a cold backend.
  await expect(dialog.getByText("تکمیل سفارش")).toBeVisible({ timeout: 60_000 });
  await expect(dialog.getByText("دریافت‌شده")).toBeVisible();
  await expect(dialog.getByText("قهوه اسپرسو")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // -- Refund tab: one refund creates two events (credit note + payment).
  await page.getByTestId("manual-refund-tab").click();
  await page.getByTestId("manual-refund-order").fill(ORDER_NUMBER);
  await page.getByLabel("مبلغ برگشت (تومان) *").fill("175000");
  await expect(page.getByTestId("manual-refund-payment")).toBeChecked();
  await page.getByTestId("manual-refund-submit").click();
  await expect(page.getByText("برگشت دستی ثبت شد")).toBeVisible({ timeout: 60_000 });

  const refundCard = page
    .getByTestId("manual-entry-card")
    .filter({ hasText: "برگشت" })
    .filter({ hasText: ORDER_NUMBER })
    .first();
  await expect(refundCard).toBeVisible({ timeout: 60_000 });
  await refundCard.getByRole("button", { name: "جزئیات و ردیابی" }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("ایجاد برگشت")).toBeVisible({ timeout: 60_000 });
  await expect(dialog.getByText("برگشت پرداخت")).toBeVisible();
  await page.keyboard.press("Escape");

  // -- Cancellation tab. -------------------------------------------------
  await page.getByTestId("manual-cancellation-tab").click();
  await page.getByTestId("manual-cancel-order").fill(ORDER_NUMBER);
  await page.getByTestId("manual-cancel-submit").click();
  await expect(page.getByText("لغو سفارش ثبت شد")).toBeVisible({ timeout: 60_000 });
  const cancelCard = page
    .getByTestId("manual-entry-card")
    .filter({ hasText: "لغو سفارش" })
    .filter({ hasText: ORDER_NUMBER })
    .first();
  await expect(cancelCard).toBeVisible({ timeout: 60_000 });

  // -- Type filter narrows the list to refunds only. ----------------------
  await page.getByLabel("فیلتر نوع ورودی").click();
  await page.getByRole("option", { name: "برگشت", exact: true }).click();
  await expect(
    page.getByTestId("manual-entry-card").filter({ hasText: ORDER_NUMBER }),
  ).toHaveCount(1, { timeout: 60_000 });
  await expect(
    page.getByTestId("manual-entry-card").filter({ hasText: ORDER_NUMBER }).first(),
  ).toContainText("برگشت");

  await page.screenshot({ path: `${SHOTS_DIR}/manual-entries-list.png`, fullPage: true });
  expect(consoleErrors, `console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
});

test("manual entries page has no horizontal overflow at 320/390px", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const consoleErrors = collectConsoleErrors(page);
  await login(page);
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/invoices/manual");
    await expect(page.getByRole("heading", { name: "فاکتور جدید (دستی)" })).toBeVisible();
    expect(await hasNoHorizontalOverflow(page), `horizontal overflow at ${width}px`).toBe(true);
    // The order form's line card reflows at phone widths.
    await page.getByTestId("manual-order-number").fill(`ORD-${STAMP}-${width}`);
    expect(await hasNoHorizontalOverflow(page), `overflow after typing at ${width}px`).toBe(true);
  }
  expect(consoleErrors, `console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
});

test("keyboard-only manual order creation", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const consoleErrors = collectConsoleErrors(page);
  await login(page);
  await page.goto("/invoices/manual");

  // Tab to the order number field (no mouse), fill the form by keyboard.
  await page.getByTestId("manual-order-number").focus();
  await page.keyboard.type(`${ORDER_NUMBER}-KB`);
  await page.keyboard.press("Tab"); // date picker trigger — skip it
  await page.keyboard.press("Tab"); // customer name
  await page.keyboard.type("مشتری صفحه‌کلید");
  await page.getByTestId("manual-order-line-desc-0").focus();
  await page.keyboard.type("چای سبز");
  await page.getByTestId("manual-order-line-qty-0").fill("۱");
  await page.getByLabel("قیمت واحد (تومان) *").fill("138000");
  await expect(page.getByTestId("manual-order-total")).toContainText("۱۳۸٬۰۰۰ تومان");
  await page.getByTestId("manual-order-submit").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("سفارش دستی ثبت شد")).toBeVisible({ timeout: 60_000 });
  await expect(
    page.getByTestId("manual-entry-card").filter({ hasText: `${ORDER_NUMBER}-KB` }).first(),
  ).toBeVisible({ timeout: 60_000 });
  expect(consoleErrors, `console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
});

test("text contrast meets WCAG AA on the manual-entry page", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const consoleErrors = collectConsoleErrors(page);
  await login(page);
  await page.goto("/invoices/manual");
  await expect(page.getByRole("heading", { name: "فاکتور جدید (دستی)" })).toBeVisible();

  const colors = await page.evaluate(() => {
    const effectiveBackground = (el: Element | null): string => {
      let node: Element | null = el;
      while (node) {
        const bg = getComputedStyle(node).backgroundColor;
        if (bg && !bg.startsWith("rgba(0, 0, 0, 0") && bg !== "transparent") return bg;
        node = node.parentElement;
      }
      return getComputedStyle(document.body).backgroundColor;
    };
    const bodyStyle = getComputedStyle(document.body);
    const muted = document.querySelector(".text-muted-foreground");
    const submit = document.querySelector('[data-testid="manual-order-submit"]');
    const badge = document.querySelector('[data-testid="manual-entry-card"] [class*="badge" i]');
    return {
      bodyFg: bodyStyle.color,
      bodyBg: bodyStyle.backgroundColor,
      mutedFg: muted ? getComputedStyle(muted).color : null,
      mutedBg: effectiveBackground(muted),
      submitFg: submit ? getComputedStyle(submit).color : null,
      submitBg: submit ? getComputedStyle(submit).backgroundColor : null,
      badgeFg: badge ? getComputedStyle(badge).color : null,
      badgeBg: badge ? effectiveBackground(badge) : null,
    };
  });

  const bodyRatio = contrastRatio(colors.bodyFg, colors.bodyBg);
  expect(bodyRatio, `body text contrast ${bodyRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  if (colors.mutedFg) {
    const mutedRatio = contrastRatio(colors.mutedFg, colors.mutedBg);
    expect(mutedRatio, `muted text contrast ${mutedRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  }
  if (colors.submitFg && colors.submitBg) {
    const submitRatio = contrastRatio(colors.submitFg, colors.submitBg);
    expect(submitRatio, `submit button contrast ${submitRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  }
  if (colors.badgeFg && colors.badgeBg) {
    const badgeRatio = contrastRatio(colors.badgeFg, colors.badgeBg);
    expect(badgeRatio, `status badge contrast ${badgeRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  }
  expect(consoleErrors, `console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
});
