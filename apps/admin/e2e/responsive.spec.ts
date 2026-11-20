import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";
import {
  TEST_MARK,
  createDraftViaBff,
  pickAccounts,
  resolveTestJournalId,
} from "./journal-helpers";

/**
 * Responsive + accessibility pass (US-305), in a live browser against
 * the real backend + Tryton.
 *
 * - No horizontal overflow at 320px / 390px / desktop on every main route.
 * - Mobile navigation reaches every live section.
 * - Keyboard-only journal creation (no mouse): Tab through the editor,
 *   pick accounts with the keyboard, save with Enter.
 * - The post-confirm dialog traps focus.
 * - Reduced motion is respected; text contrast meets WCAG AA.
 */

// Cold dev-server compiles on this VM exceed Playwright's 30s default,
// and every BFF round-trip hits Supabase — 25 routes × slow fetches
// need a longer budget than the original 10-route suite. The overflow
// tests visit 25 routes each; /packaging alone takes ~39s against
// remote Supabase, so the file budget is 10 minutes (US-520).
test.setTimeout(600_000);

const ROUTES: { path: string; heading?: string; text?: string }[] = [
  { path: "/", heading: "داشبورد" },
  { path: "/review-queue", heading: "صف بازبینی" },
  { path: "/packaging", heading: "انبار و بسته‌بندی" },
  { path: "/packaging/calculator", heading: "محاسبه‌گر قیمت" },
  { path: "/packaging/counts", heading: "شمارش دوره‌ای مواد" },
  { path: "/packaging/order-cost", heading: "هزینهٔ کامل سفارش" },
  { path: "/accounting/accounts", heading: "سر فصل حساب‌ها" },
  { path: "/accounting/journals", heading: "دفتر روزنامه" },
  // «سند جدید» is plain text on the create page, not a heading.
  { path: "/accounting/journals/new", text: "سند جدید" },
  { path: "/accounting/fiscal-years", heading: "سال‌های مالی" },
  { path: "/invoices", heading: "فاکتورها" },
  { path: "/invoices/new", heading: "فاکتور جدید" },
  { path: "/payments", heading: "پرداخت‌ها" },
  { path: "/banking/statements", heading: "صورت‌حساب‌های بانکی" },
  { path: "/banking/cash-boxes", heading: "صندوق‌ها" },
  { path: "/settlements", heading: "مرکز تسویه" },
  { path: "/settlements/schedule", heading: "برنامهٔ قسط‌های تسویه" },
  { path: "/settlements/import", heading: "ایمپورت فایل تسویه" },
  { path: "/settlements/contracts", heading: "قراردادهای درگاه" },
  { path: "/reports", heading: "گزارش‌ها" },
  { path: "/compliance", heading: "استانداردها و انطباق" },
  { path: "/compliance/standards", heading: "استانداردهای حسابداری" },
  { path: "/compliance/tax-policy", heading: "سیاست مالیاتی" },
  { path: "/compliance/validations", heading: "اعتبارسنجی‌ها" },
  { path: "/compliance/moadian", heading: "مودیان" },
];

/** True when the document has no horizontal overflow at the current viewport. */
async function hasNoHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

for (const width of [320, 390, 1280]) {
  test(`no horizontal overflow at ${width}px on every main route`, async ({ page }) => {
    test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
    await page.setViewportSize({ width, height: 800 });

    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(`${page.url()}: ${msg.text()}`);
    });

    // The login page itself must fit too.
    await page.goto("/login");
    await expect(page.getByLabel("کلید دسترسی")).toBeVisible({ timeout: 30_000 });
    expect(await hasNoHorizontalOverflow(page), `horizontal overflow on /login at ${width}px`).toBe(true);

    await login(page);
    await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

    for (const route of ROUTES) {
      await page.goto(route.path);
      // 90s, not 30s: /packaging takes ~39s to load against remote
      // Supabase (proven by screenshot, US-520) and the other
      // data-heavy routes are slow for the same reason.
      if (route.heading) {
        await expect(page.getByRole("heading", { name: route.heading }).first()).toBeVisible({
          timeout: 90_000,
        });
      } else if (route.text) {
        await expect(page.getByText(route.text, { exact: true }).first()).toBeVisible({
          timeout: 90_000,
        });
      }
      // Let async content settle before measuring: wait for the web
      // font (fallback metrics can briefly overflow at 320px) and for
      // data to land.
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(1000);
      // A first failing measurement gets one re-check after more
      // settling: real overflow persists, load-timing flakes do not.
      let ok = await hasNoHorizontalOverflow(page);
      if (!ok) {
        await page.waitForTimeout(2000);
        ok = await hasNoHorizontalOverflow(page);
      }
      expect(ok, `horizontal overflow on ${route.path} at ${width}px`).toBe(true);
    }

    expect(consoleErrors, `console errors at ${width}px:\n${consoleErrors.join("\n")}`).toEqual([]);
  });
}

/**
 * Expand a collapsible nav group in the mobile sheet, if it is not
 * already open. Group expansion is derived from the current route
 * (an active group opens itself), so a blind click would toggle an
 * open group closed — check `aria-expanded` first (US-520).
 */
async function expandNavGroup(page: Page, name: string) {
  const button = page.getByRole("button", { name, exact: true });
  if ((await button.getAttribute("aria-expanded")) === "false") {
    await button.click();
  }
}

test("mobile navigation reaches every live section at 390px", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

  // Reports from the mobile nav sheet.
  await page.getByRole("button", { name: "باز کردن ناوبری" }).click();
  await page.getByRole("link", { name: "گزارش‌ها" }).click();
  await expect(page).toHaveURL(/\/reports$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "گزارش‌ها" })).toBeVisible({ timeout: 30_000 });

  // The accounting group expands to its children.
  await page.getByRole("button", { name: "باز کردن ناوبری" }).click();
  await page.getByRole("button", { name: "حسابداری" }).click();
  await page.getByRole("link", { name: "روزنامه‌ها" }).click();
  await expect(page).toHaveURL(/\/accounting\/journals$/, { timeout: 30_000 });

  // «فروش و خرید» and «بانک و صندوق» are collapsible groups
  // (buttons), not links: expand the group, then follow the child
  // link. Both groups have a «صورت‌حساب‌ها» child, so scope the link
  // to the group's list (`#nav-group-<id>`) to stay unambiguous.
  await page.getByRole("button", { name: "باز کردن ناوبری" }).click();
  await expandNavGroup(page, "فروش و خرید");
  await page.locator("#nav-group-sales").getByRole("link", { name: "صورت‌حساب‌ها" }).click();
  await expect(page).toHaveURL(/\/invoices$/, { timeout: 30_000 });

  // «دریافت و پرداخت» is a plain top-level link.
  await page.getByRole("button", { name: "باز کردن ناوبری" }).click();
  await page.getByRole("link", { name: "دریافت و پرداخت" }).click();
  await expect(page).toHaveURL(/\/payments$/, { timeout: 30_000 });

  await page.getByRole("button", { name: "باز کردن ناوبری" }).click();
  await expandNavGroup(page, "بانک و صندوق");
  await page.locator("#nav-group-banking").getByRole("link", { name: "صورت‌حساب‌ها" }).click();
  await expect(page).toHaveURL(/\/banking\/statements$/, { timeout: 30_000 });
});

/** Serializable description of the focused element, for Tab-walking. */
interface FocusedInfo {
  tag: string;
  id: string;
  role: string | null;
  ariaLabel: string | null;
  labelText: string | null;
  text: string;
}

async function focusedInfo(page: Page): Promise<FocusedInfo> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el) return { tag: "", id: "", role: null, ariaLabel: null, labelText: null, text: "" };
    const labelled = el as unknown as { labels?: NodeListOf<HTMLLabelElement> };
    return {
      tag: el.tagName,
      id: el.id,
      role: el.getAttribute("role"),
      ariaLabel: el.getAttribute("aria-label"),
      labelText: labelled.labels?.[0]?.textContent?.trim() ?? null,
      text: (el.textContent ?? "").trim().slice(0, 80),
    };
  });
}

/**
 * Press Tab until the focused element matches, or fail. `fromStart`
 * focuses the body first so the walk is deterministic. Each Tab
 * settles briefly first: focus traps redirect through the next
 * animation frame, and checking synchronously can catch the
 * focus-guard span (or Tab away before the redirect runs).
 */
async function tabUntil(
  page: Page,
  predicate: (info: FocusedInfo) => boolean,
  description: string,
  maxTabs = 60,
): Promise<FocusedInfo> {
  for (let i = 0; i < maxTabs; i += 1) {
    await page.keyboard.press("Tab");
    await page.waitForTimeout(120);
    const info = await focusedInfo(page);
    if (predicate(info)) return info;
  }
  throw new Error(`keyboard walk never reached: ${description}`);
}

test("keyboard-only journal creation: Tab, type, pick, save — no mouse", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  const [debitAccount, creditAccount] = await pickAccounts(page);
  const journalId = await resolveTestJournalId();
  const description = `${TEST_MARK} — صفحه‌کلید ${Date.now()}`;

  await page.goto("/accounting/journals/new");
  await expect(page.getByText("سند جدید", { exact: true })).toBeVisible({ timeout: 30_000 });
  // The chart must be loaded before the pickers offer accounts.
  // The picker is CORRECTLY disabled until then (US-520 diagnosis:
  // `AccountPicker` disables on `loading`, which clears only after
  // `fetchAllAccounts` resolves — two sequential BFF pages, because
  // the REST API caps pages at 100 and the chart has 137+ accounts).
  // Against remote Supabase that takes well over 30s, so wait 120s.
  await expect(page.getByRole("combobox", { name: "حساب ردیف ۱" })).toBeEnabled({ timeout: 120_000 });

  // Start the Tab walk from the top of the document.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur?.());
  await page.keyboard.press("Tab");

  // Description (Tab past the date picker and company select).
  await tabUntil(page, (info) => info.id === "journal-description", "شرح سند");
  await page.keyboard.type(description);

  // Journal id.
  await tabUntil(page, (info) => info.id === "journal-id", "شناسهٔ دفتر روزنامه");
  await page.keyboard.type(String(journalId));

  // Row 1 account: open the combobox with Enter, type the code, Enter.
  // The trigger is a <button role="combobox">; the open popover adds a
  // second combobox (the cmdk root), so scope to the button.
  const accountTrigger = (row: string) =>
    page.locator(`button[role="combobox"][aria-label="حساب ${row}"]`);
  await tabUntil(
    page,
    (info) => info.role === "combobox" && (info.ariaLabel ?? "").includes("ردیف ۱"),
    "حساب ردیف ۱",
  );
  await page.keyboard.press("Enter");
  await page.keyboard.type(debitAccount.code);
  await expect(
    page.getByRole("option", { name: new RegExp(debitAccount.code.replace(/\./g, "\\.")) }).first(),
  ).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press("Enter");
  await expect(accountTrigger("ردیف ۱")).toContainText(debitAccount.code, { timeout: 10_000 });

  // Row 1 debit.
  await tabUntil(page, (info) => info.labelText === "بدهکار", "بدهکار ردیف ۱");
  await page.keyboard.type("1000000");

  // Row 2 account.
  await tabUntil(
    page,
    (info) => info.role === "combobox" && (info.ariaLabel ?? "").includes("ردیف ۲"),
    "حساب ردیف ۲",
  );
  await page.keyboard.press("Enter");
  await page.keyboard.type(creditAccount.code);
  await expect(
    page.getByRole("option", { name: new RegExp(creditAccount.code.replace(/\./g, "\\.")) }).first(),
  ).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press("Enter");
  await expect(accountTrigger("ردیف ۲")).toContainText(creditAccount.code, { timeout: 10_000 });

  // Row 2 credit (the next بستانکار after the row-2 picker is row 2's).
  await tabUntil(page, (info) => info.labelText === "بستانکار", "بستانکار ردیف ۲");
  await page.keyboard.type("1000000");

  await expect(page.getByTestId("balance-bar")).toContainText("تراز است");

  // Save with the keyboard.
  await tabUntil(
    page,
    (info) => info.tag === "BUTTON" && info.text.includes("ذخیرهٔ پیش‌نویس"),
    "دکمهٔ ذخیرهٔ پیش‌نویس",
  );
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/accounting\/journals\/\d+$/, { timeout: 30_000 });
  // The detail page still has to fetch the draft over the BFF after
  // navigation — against remote Supabase that exceeds Playwright's
  // 5s default expect timeout, so wait 30s (US-520).
  await expect(page.getByText(description)).toBeVisible({ timeout: 30_000 });
});

test("post-confirm dialog traps focus and closes from the keyboard", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  const accounts = await pickAccounts(page);
  const journalId = await resolveTestJournalId();
  const draftId = await createDraftViaBff(page, {
    description: `${TEST_MARK} — تلهٔ فوکوس ${Date.now()}`,
    journalId,
    accounts,
  });

  await page.goto(`/accounting/journals/${draftId}`);
  await expect(page.getByRole("heading", { name: /^سند / })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "ثبت سند", exact: true }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible({ timeout: 10_000 });

  // Tab repeatedly: focus must stay inside the dialog. The trap's
  // guard redirect is deferred to the next animation frame, so poll
  // until focus settles inside after each Tab instead of asserting
  // synchronously (an immediate read can catch the focus-guard span).
  for (let i = 0; i < 10; i += 1) {
    await page.keyboard.press("Tab");
    await expect
      .poll(
        async () =>
          page.evaluate(() => {
            const el = document.activeElement;
            return el !== null && el.closest('[role="alertdialog"]') !== null;
          }),
        { timeout: 5000 },
      )
      .toBe(true);
  }

  // Close from the keyboard (Tab to «انصراف», Enter).
  await tabUntil(
    page,
    (info) => info.tag === "BUTTON" && info.text === "انصراف",
    "دکمهٔ انصراف در گفت‌وگو",
    10,
  );
  await page.keyboard.press("Enter");
  await expect(dialog).toBeHidden({ timeout: 10_000 });
  // The draft is untouched.
  await expect(page.getByRole("heading", { name: /^سند / })).toBeVisible();
});

test("reduced motion is respected", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await login(page);
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

  // The global reduced-motion rule collapses every transition/animation
  // to 0.01ms — check a button (transition-colors) and the sidebar
  // chevron (transition-transform). Chromium serializes 0.01ms as
  // "1e-05s" and may list one duration per transitioned property, so
  // parse every entry and require each to be effectively zero.
  const durations = await page.evaluate(() => {
    const button = document.querySelector("button");
    const chevron = document.querySelector("aside svg");
    const read = (el: Element | null) => {
      if (!el) return null;
      const style = getComputedStyle(el);
      return { transition: style.transitionDuration, animation: style.animationDuration };
    };
    return { button: read(button), chevron: read(chevron) };
  });
  const maxDurationMs = (value: string | null | undefined): number => {
    if (!value) return Number.NaN;
    return Math.max(
      ...value.split(",").map((entry) => {
        const match = entry.trim().match(/^([\d.e+-]+)(ms|s)$/);
        if (!match) return Number.NaN;
        return match[2] === "s" ? Number(match[1]) * 1000 : Number(match[1]);
      }),
    );
  };
  expect(maxDurationMs(durations.button?.transition)).toBeLessThanOrEqual(0.1);
  expect(maxDurationMs(durations.button?.animation)).toBeLessThanOrEqual(0.1);
  if (durations.chevron) expect(maxDurationMs(durations.chevron.transition)).toBeLessThanOrEqual(0.1);
});

/** WCAG relative luminance of an sRGB color. */
function luminance(r: number, g: number, b: number): number {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrastRatioFromLuminance(l1: number, l2: number): number {
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Relative luminance of a computed color. Chromium serializes the
 * oklch theme tokens as `lab(...)`, so both lab() and rgb() are
 * handled. For lab(), CIE L* converts back to Y (relative luminance)
 * exactly: Y = ((L* + 16) / 116)^3 above the linear segment, and
 * L* / 903.3 below it.
 */
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
  return contrastRatioFromLuminance(luminanceFromColor(fg), luminanceFromColor(bg));
}

test("text contrast meets WCAG AA on the dashboard", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

  const colors = await page.evaluate(() => {
    // Effective background: the nearest ancestor with an opaque color.
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
    return {
      bodyFg: bodyStyle.color,
      bodyBg: bodyStyle.backgroundColor,
      mutedFg: muted ? getComputedStyle(muted).color : null,
      mutedBg: effectiveBackground(muted),
    };
  });

  const bodyRatio = contrastRatio(colors.bodyFg, colors.bodyBg);
  expect(bodyRatio, `body text contrast ${bodyRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);

  if (colors.mutedFg) {
    const mutedRatio = contrastRatio(colors.mutedFg, colors.mutedBg);
    expect(mutedRatio, `muted text contrast ${mutedRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  }

  // Primary button text on the primary background (the «فاکتور جدید»
  // button on the invoices list is the default variant).
  await page.goto("/invoices");
  await expect(page.getByRole("heading", { name: "فاکتورها" })).toBeVisible({ timeout: 30_000 });
  const buttonColors = await page.evaluate(() => {
    const el = document.querySelector('a[href="/invoices/new"]');
    if (!el) return null;
    const style = getComputedStyle(el);
    return { fg: style.color, bg: style.backgroundColor };
  });
  expect(buttonColors, "primary button not found on /invoices").not.toBeNull();
  if (buttonColors) {
    const buttonRatio = contrastRatio(buttonColors.fg, buttonColors.bg);
    expect(buttonRatio, `primary button contrast ${buttonRatio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
  }
});
