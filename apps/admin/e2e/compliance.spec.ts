import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";

/**
 * Compliance center (US-405), against the real backend + Tryton
 * (Supabase). Full flow: login → compliance center → standards →
 * tax policy → run a validation → Moadian simulator submission →
 * logout. Every figure comes from the live API; the test asserts the
 * known live values (35 standards, Standard 15 blocked, 43/44
 * verified, 5 gated 1405 tax parameters, 36 rules on Standard 1).
 *
 * The Moadian leg creates its own test profile/document/submission
 * (unique slug + taxid per run) and drives it to «accepted» through
 * the simulator; nothing else in the suite depends on those rows.
 */

// Cold dev-server compiles on this VM exceed Playwright's 30s default,
// and every BFF round-trip hits Supabase — the full flow takes minutes.
test.setTimeout(360_000);

/** Collect console errors for the whole flow; asserted at the end. */
function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // The tax-policy step deliberately resolves a PRODUCTION decision on
    // a gated parameter: the backend correctly refuses with 422
    // TAX_POLICY_SOURCE_BLOCKED, and the test asserts the Persian refusal
    // on the page. Chrome logs that failed fetch as a console error; the
    // expected 422 is asserted product behavior, not a defect.
    const url = msg.location()?.url ?? page.url();
    if (url.includes("/compliance/tax-policy") && text.includes("422 (Unprocessable Entity)")) {
      return;
    }
    errors.push(`${page.url()}: ${text}`);
  });
  page.on("pageerror", (err) => errors.push(`${page.url()}: pageerror: ${err.message}`));
  return errors;
}

test("compliance center: standards, tax policy, validation run, Moadian simulator, logout", async ({
  page,
}) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const consoleErrors = collectConsoleErrors(page);
  const stamp = String(Date.now());
  const profileSlug = `us405-e2e-${stamp.slice(-8)}`;

  await login(page);

  // -- Overview: live coverage from the registry -------------------------
  await page.goto("/compliance");
  await expect(page.getByRole("heading", { name: "استانداردها و انطباق" })).toBeVisible({ timeout: 30_000 });
  const coverage = page.getByLabel("پوشش انطباق");
  await expect(coverage.getByText("استانداردهای ثبت‌شده")).toBeVisible({ timeout: 30_000 });
  await expect(coverage.getByText("۳۵", { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  await expect(coverage.getByText("قواعد پیاده‌شده")).toBeVisible({ timeout: 30_000 });
  await expect(coverage.getByText("۳۶", { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  // No run yet in this browser profile: executed shows the honest empty state.
  await expect(page.getByText("هنوز اعتبارسنجی‌ای اجرا نشده است.")).toBeVisible({ timeout: 30_000 });

  // -- Standards: blocked badge for 15, verified for 43/44 ---------------
  await page.goto("/compliance/standards");
  await expect(page.getByRole("heading", { name: "استانداردهای حسابداری" })).toBeVisible({ timeout: 30_000 });
  const blockedNotice = page.getByTestId("standards-blocked-notice");
  await expect(blockedNotice).toBeVisible({ timeout: 30_000 });
  await expect(blockedNotice).toContainText("استاندارد ۱۵");
  await expect(blockedNotice).toContainText("متن رسمی بازبینی‌شده");

  // Filter to blocked: only Standard 15 remains; open its detail.
  await page.locator("#verification-filter").click();
  await page.getByRole("option", { name: "مسدود — کمبود منبع" }).click();
  await expect(page.getByRole("link", { name: "۱۵", exact: true }).first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: "۱۵", exact: true }).first().click();
  await expect(page).toHaveURL(/\/compliance\/standards\/15$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: /استاندارد ۱۵/ })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("standard-verification")).toContainText("مسدود — کمبود منبع");
  await expect(page.getByTestId("standard-verification")).toContainText("متن رسمی بازبینی‌شده");

  // Standard 43 is VERIFIED_TEXT.
  await page.goto("/compliance/standards/43");
  await expect(page.getByRole("heading", { name: /استاندارد ۴۳/ })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("standard-verification")).toContainText("متن تأییدشده");

  // Standard 1 carries the 36 machine-readable rules.
  await page.goto("/compliance/standards/1");
  await expect(page.getByRole("heading", { name: /استاندارد ۱/ })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("قواعد ماشین‌خوان (۳۶)")).toBeVisible({ timeout: 30_000 });

  // -- Tax policy: all 5 parameters gated, decision resolves ------------
  await page.goto("/compliance/tax-policy");
  await expect(page.getByRole("heading", { name: "سیاست مالیاتی" })).toBeVisible({ timeout: 30_000 });
  const gatedNotice = page.getByTestId("tax-gated-notice");
  await expect(gatedNotice).toBeVisible({ timeout: 30_000 });
  await expect(gatedNotice).toContainText("۵ پارامتر از ۵ پارامتر");
  await expect(gatedNotice).toContainText("منبع رسمی آن هنوز پین نشده است");
  // The VAT rate is shown with its gated badge, never as verified truth.
  await expect(page.getByText("نرخ عمومی مالیات بر ارزش افزوده").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("تأیید با منبع مکمل").first()).toBeVisible({ timeout: 30_000 });

  // Reference decision for VAT today: resolves 10% with the gated label.
  await page.getByTestId("resolve-tax-decision").click();
  const decision = page.getByTestId("tax-decision-result");
  await expect(decision).toBeVisible({ timeout: 30_000 });
  await expect(decision).toContainText("۱۰٪");
  await expect(decision).toContainText("تأیید با منبع مکمل");

  // Production use of the same gated parameter is refused, in Persian.
  await page.locator("#usage-context").click();
  await page.getByRole("option", { name: "تولیدی" }).click();
  await page.getByTestId("resolve-tax-decision").click();
  await expect(page.getByText("برای استفادهٔ تولیدی مسدود است")).toBeVisible({ timeout: 30_000 });

  // -- Validation run over the live trial balance -------------------------
  await page.goto("/compliance/validations");
  await expect(page.getByRole("heading", { name: "اعتبارسنجی‌ها" })).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("run-validation").click();
  const result = page.getByTestId("validation-run-result");
  await expect(result).toBeVisible({ timeout: 60_000 });
  // The live trial balance has no posted moves, so every rule errors
  // honestly (never a fabricated pass); the backend note says so.
  await expect(result).toContainText("به ارقام صورت‌های مالی");
  await expect(result.getByText("قواعد پیاده‌شده", { exact: true })).toBeVisible();
  await expect(result.getByText("۳۶", { exact: true }).first()).toBeVisible();
  await expect(result.getByText("قواعد اجراشده")).toBeVisible();
  await expect(result.getByText("۳۵", { exact: true }).first()).toBeVisible();
  // Per-rule results are listed (BS001 is one of the 36 Standard 1 rules).
  await expect(result.getByText("BS001").first()).toBeVisible();
  await expect(result).toContainText("CF005");

  // The overview now shows the executed count from this run.
  await page.goto("/compliance");
  await expect(page.getByLabel("پوشش انطباق").getByText("۳۵", { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });

  // -- Moadian simulator: profile → document → submission → accepted -----
  await page.goto("/compliance/moadian");
  await expect(page.getByRole("heading", { name: "مودیان" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("شبیه‌ساز آزمایشی").first()).toBeVisible({ timeout: 30_000 });

  // Create a test profile (unique slug per run).
  await page.getByLabel("نام پروفایل").fill("پروفایل آزمایشی انطباق");
  await page.getByLabel("شناسهٔ یکتا (انگلیسی)").fill(profileSlug);
  await page.getByLabel("شناسهٔ حافظهٔ مالیاتی (۶ کاراکتر)").fill("ABC123");
  await page.getByLabel("کد اقتصادی (۱۰ یا ۱۴ رقم)").fill("32652362589632");
  await page.getByLabel("مرجع کلید (نام متغیر محیطی)").fill("MOADIAN_TEST_KEY");
  await page.getByTestId("create-moadian-profile").click();
  await expect(page.getByText(`پروفایل «پروفایل آزمایشی انطباق» ساخته شد.`)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(profileSlug).first()).toBeVisible({ timeout: 30_000 });

  // Create a draft document with a fresh unique tax id. The tax-id
  // input displays Persian digits (VibeFarsi input hook) while the
  // React state — and the notice — keep the Latin id, so read the id
  // from the notice, not from the input's displayed value.
  await page.getByRole("button", { name: "شناسهٔ تازه" }).click();
  await page.getByTestId("create-moadian-document").click();
  const notice = page.getByRole("status");
  await expect(notice).toContainText(/سند با شمارهٔ مالیاتی TS\d{20}/, { timeout: 30_000 });
  const noticeText = (await notice.textContent()) ?? "";
  const taxidMatch = noticeText.match(/TS\d{20}/);
  expect(taxidMatch, "notice carries the created tax id").not.toBeNull();
  const taxid = taxidMatch![0];
  await expect(page.getByText(taxid).first()).toBeVisible();

  // Create a draft submission (accept scenario) for that document.
  await page.locator("#sub-document").click();
  await page.getByRole("option", { name: taxid }).click();
  await page.getByTestId("create-moadian-submission").click();
  const detail = page.getByTestId("moadian-submission-detail");
  await expect(detail).toBeVisible({ timeout: 30_000 });
  await expect(detail.getByText("پیش‌نویس").first()).toBeVisible();

  // Recorded human approval (dialog traps focus; approve from keyboard).
  await page.getByTestId("approve-moadian-submission").click();
  const dialog = page.getByRole("dialog", { name: "تأیید ارسال مودیان" });
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  await page.getByLabel("یادداشت تأیید (اختیاری)").fill("تأیید آزمایشی end-to-end");
  await page.getByTestId("confirm-moadian-approve").click();
  await expect(detail.getByText("تأییدشده").first()).toBeVisible({ timeout: 30_000 });
  await expect(detail).toContainText("تأیید آزمایشی end-to-end");

  // Queue, then two reconcile passes: submitted (PENDING) → accepted (SUCCESS).
  await page.getByTestId("queue-moadian-submission").click();
  await expect(detail.getByText("در صف").first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("run-moadian-reconcile").click();
  await expect(detail.getByText("ارسال‌شده").first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("run-moadian-reconcile").click();
  await expect(detail.getByText("پذیرفته‌شده").first()).toBeVisible({ timeout: 30_000 });
  // Status evidence: receipt number, hashes, and the simulator events.
  await expect(detail.getByText("شمارهٔ رسید")).toBeVisible();
  await expect(detail.getByText("در انتظار پردازش").first()).toBeVisible({ timeout: 30_000 });
  await expect(detail.getByText("موفق").first()).toBeVisible({ timeout: 30_000 });

  // -- Logout ---------------------------------------------------------------
  await page.getByRole("button", { name: "خروج", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/, { timeout: 30_000 });
  await expect(page.getByLabel("کلید دسترسی")).toBeVisible();

  expect(consoleErrors, `console errors during the compliance flow:\n${consoleErrors.join("\n")}`).toEqual([]);
});

test("compliance center is reachable from the sidebar navigation", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "استانداردها و انطباق" }).click();
  await page.getByRole("link", { name: "مرکز انطباق" }).click();
  await expect(page).toHaveURL(/\/compliance$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "استانداردها و انطباق" })).toBeVisible({ timeout: 30_000 });

  await page.getByRole("link", { name: "مودیان", exact: true }).click();
  await expect(page).toHaveURL(/\/compliance\/moadian$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "مودیان" })).toBeVisible({ timeout: 30_000 });
});
