import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";

/**
 * Compliance center accessibility + responsive evidence (US-405).
 *
 * - Keyboard-only standards navigation: Tab to the verification
 *   filter, choose «مسدود» from the keyboard, Enter on the row.
 * - WCAG AA contrast on the compliance overview (same luminance math
 *   as the US-305 responsive spec).
 * - No horizontal overflow + screenshots at 320px / 390px / desktop
 *   for every compliance route (screenshots land in
 *   ~/workspace/coord-logs/us405-shots/).
 */

test.setTimeout(240_000);

const ROUTES: { path: string; heading: RegExp | string; shot: string }[] = [
  { path: "/compliance", heading: "استانداردها و انطباق", shot: "overview" },
  { path: "/compliance/standards", heading: "استانداردهای حسابداری", shot: "standards" },
  { path: "/compliance/tax-policy", heading: "سیاست مالیاتی", shot: "tax-policy" },
  { path: "/compliance/validations", heading: "اعتبارسنجی‌ها", shot: "validations" },
  { path: "/compliance/moadian", heading: "مودیان", shot: "moadian" },
];

async function hasNoHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
}

test("keyboard-only standards filter and detail navigation", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  await page.goto("/compliance/standards");
  await expect(page.getByRole("heading", { name: "استانداردهای حسابداری" })).toBeVisible({ timeout: 30_000 });

  // Tab from the top until the verification filter combobox is focused.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur?.());
  let focusedFilter = false;
  for (let i = 0; i < 40; i += 1) {
    await page.keyboard.press("Tab");
    await page.waitForTimeout(100);
    const info = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      return { id: el?.id ?? "", role: el?.getAttribute("role") ?? "", label: el?.getAttribute("aria-label") ?? "" };
    });
    if (info.id === "verification-filter" || info.label.includes("وضعیت تأیید")) {
      focusedFilter = true;
      break;
    }
  }
  expect(focusedFilter, "keyboard walk never reached the verification filter").toBe(true);

  // Open the filter with the keyboard and pick the blocked option
  // (fifth in the list: all, verified, corroborated, unverified, blocked).
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option", { name: "مسدود — کمبود منبع" })).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("link", { name: "۱۵", exact: true }).first()).toBeVisible({ timeout: 30_000 });

  // Tab to the Standard 15 row and open it with Enter (rows are focusable).
  await page.getByRole("link", { name: "۱۵", exact: true }).first().focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/compliance\/standards\/15$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: /استاندارد ۱۵/ })).toBeVisible({ timeout: 30_000 });
});

/** WCAG relative luminance of an sRGB color. */
function luminance(r: number, g: number, b: number): number {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function luminanceFromColor(color: string): number {
  const lab = color.match(/^lab\(\s*([\d.]+)%?\s+[-\d.]+\s+[-\d.]+\s*(?:\/\s*[\d.]+)?\)$/);
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

test("text contrast meets WCAG AA on the compliance center", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  await page.goto("/compliance");
  await expect(page.getByRole("heading", { name: "استانداردها و انطباق" })).toBeVisible({ timeout: 30_000 });

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
    const primaryButton = document.querySelector('[data-testid="compliance-page"] a');
    return {
      bodyFg: bodyStyle.color,
      bodyBg: bodyStyle.backgroundColor,
      mutedFg: muted ? getComputedStyle(muted).color : null,
      mutedBg: effectiveBackground(muted),
      linkFg: primaryButton ? getComputedStyle(primaryButton).color : null,
      linkBg: effectiveBackground(primaryButton),
    };
  });

  const bodyRatio = contrastRatio(colors.bodyFg, colors.bodyBg);
  expect(bodyRatio, `body text contrast ${bodyRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  if (colors.mutedFg) {
    const mutedRatio = contrastRatio(colors.mutedFg, colors.mutedBg);
    expect(mutedRatio, `muted text contrast ${mutedRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  }
  if (colors.linkFg) {
    const linkRatio = contrastRatio(colors.linkFg, colors.linkBg);
    expect(linkRatio, `link text contrast ${linkRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  }
});

for (const width of [320, 390, 1280]) {
  test(`screenshots and no horizontal overflow at ${width}px on compliance routes`, async ({ page }) => {
    test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
    await page.setViewportSize({ width, height: 800 });
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(`${page.url()}: ${msg.text()}`);
    });
    await login(page);
    await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

    for (const route of ROUTES) {
      await page.goto(route.path);
      await expect(page.getByRole("heading", { name: route.heading }).first()).toBeVisible({
        timeout: 30_000,
      });
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(1000);
      let ok = await hasNoHorizontalOverflow(page);
      if (!ok) {
        await page.waitForTimeout(2000);
        ok = await hasNoHorizontalOverflow(page);
      }
      expect(ok, `horizontal overflow on ${route.path} at ${width}px`).toBe(true);
      await page.screenshot({
        path: `/home/hatch/workspace/coord-logs/us405-shots/compliance-${route.shot}-${width}.png`,
        fullPage: true,
      });
    }
    expect(consoleErrors, `console errors at ${width}px:\n${consoleErrors.join("\n")}`).toEqual([]);
  });
}
