import { expect, test } from "@playwright/test";
import { login, DEV_KEY } from "./auth";

/**
 * Rules management UI (US-562, Phase 5-7 ۵خ W2), against the real
 * backend: one rules journey — create (disabled) → test-before-save
 * (dry-run-definition) → dry-run the stored rule → enable → live
 * evaluate → see the executions in history — then delete the rule.
 *
 * The rule carries a unique stamp per run (`US-562 e2e <stamp>`) so
 * the test finds its own rows in the shared CI/dev DB; no figure
 * assertion touches pre-existing data (the live-evaluate result is
 * asserted only as visible — with first_match, an earlier run's rule
 * may be the one that runs). The notify action is side-effect-safe
 * (in_app is recorded, email has no SMTP backend, sms is
 * unsupported in v1), so the live step sends nothing anywhere.
 *
 * The rule starts DISABLED: a disabled rule can still be dry-run
 * (test before activation, §6.4), and the journey's enable step is
 * then real instead of a no-op.
 */

// Generous budget: cold compiles + BFF round-trips; the 90s expect
// timeouts below match the established dashboard-batch pattern.
test.setTimeout(360_000);

const EXPECT_TIMEOUT = 90_000;

test("rules journey: create → dry-run → enable → execution in history", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const stamp = `${Date.now()}-${Math.floor(Math.random() * 10000)}`;
  const ruleName = `US-562 e2e ${stamp}`;

  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // Same known pre-existing dashboard issue as the review-queue
    // spec: the dashboard home's setup-status fetch can 502/503
    // against the remote dev DB while trytond restarts — not a
    // rules defect, and the login lands on "/" before this spec
    // navigates to the workflows page.
    if (
      page.url().endsWith("/") &&
      (text.includes("502 (Bad Gateway)") || text.includes("503 (Service Unavailable)"))
    ) {
      return;
    }
    errors.push(`${page.url()}: ${text}`);
  });
  page.on("pageerror", (err) => errors.push(`${page.url()}: pageerror: ${err.message}`));

  await login(page);
  await page.goto("/integrations/workflows");
  await expect(page.getByRole("heading", { name: "گردش‌کارها" })).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });
  await expect(page.getByTestId("rules-page")).toBeVisible({ timeout: EXPECT_TIMEOUT });

  // -- create (disabled) -------------------------------------------------
  await page.getByTestId("rules-new").click();
  const dialog = page.getByTestId("rule-form-dialog");
  await expect(dialog).toBeVisible({ timeout: EXPECT_TIMEOUT });

  await page.getByTestId("rule-form-name").fill(ruleName);
  // Event stays the default (تکمیل سفارش فروش) and the action stays
  // the default (اطلاع‌رسانی).
  await page.getByTestId("rule-form-add-condition").click();
  const conditionRow = page.getByTestId("rule-form-condition-row-0");
  await expect(conditionRow).toBeVisible();
  // Field stays amount_irr, operator stays >=; the threshold is
  // ۱٬۰۰۰٬۰۰۰ تومان (the sample below is ۱٬۵۰۰٬۰۰۰, so it matches).
  await page.getByTestId("rule-form-condition-value-0").fill("1000000");
  await page.getByTestId("rule-form-recipients").fill("ops");
  await page.getByTestId("rule-form-subject").fill("سفارش بزرگ");
  await page.getByTestId("rule-form-message").fill("سفارشی بزرگ ثبت شد.");

  // Test-before-save: dry-run the unsaved definition.
  await page.getByTestId("rule-form-amount").fill("1500000");
  await page.getByTestId("rule-form-channel").fill("zarinpal");
  await page.getByTestId("rule-form-run-dry").click();
  const definitionResult = page.getByTestId("rule-form-dry-result");
  await expect(definitionResult).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(definitionResult).toContainText("اجرا می‌شد");

  // Start disabled: the journey's enable step must be real.
  await page.getByTestId("rule-form-enabled").click();

  await page.getByTestId("rule-form-save").click();
  await expect(dialog).toBeHidden({ timeout: EXPECT_TIMEOUT });

  const card = page.getByTestId("rule-card").filter({ hasText: ruleName });
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(card).toContainText("غیرفعال");
  await expect(card).toContainText("تعریف کاربر");
  await expect(card).toContainText("هنوز اجرا نشده است");

  // -- dry-run the stored (disabled) rule --------------------------------
  await card.getByTestId("rule-card-open").click();
  const detail = page.getByTestId("rule-detail-dialog");
  await expect(detail).toBeVisible({ timeout: EXPECT_TIMEOUT });

  await page.getByTestId("rule-detail-amount").fill("1500000");
  await page.getByTestId("rule-detail-channel").fill("zarinpal");
  await page.getByTestId("rule-detail-run-dry").click();
  const dryResult = page.getByTestId("rule-detail-dry-result");
  await expect(dryResult).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(dryResult).toContainText("اجرا می‌شد");
  await expect(dryResult).toContainText("اطلاع‌رسانی به ops");

  // -- enable -------------------------------------------------------------
  await page.getByTestId("rule-detail-toggle").click();
  await expect(page.getByTestId("rule-detail-toggle")).toContainText("غیرفعال کردن", {
    timeout: EXPECT_TIMEOUT,
  });

  // -- live evaluate (asserted only as visible — first_match may run
  // an earlier run's rule instead of this one) ----------------------------
  await page.getByTestId("rule-detail-run-live").click();
  await expect(page.getByTestId("rule-detail-live-result")).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });

  // -- the dry-run execution is in this rule's history --------------------
  const historyRows = page.getByTestId("rule-execution-row");
  await expect(historyRows.first()).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(page.getByText("اجرا می‌شد (آزمایشی)").first()).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });

  // -- and in the page-level history tab, filtered to this rule -----------
  await page.getByRole("button", { name: "بستن", exact: true }).click();
  await expect(detail).toBeHidden({ timeout: EXPECT_TIMEOUT });
  await page.getByRole("tab", { name: "تاریخچهٔ اجراها" }).click();
  await page.getByTestId("rules-history-filter").click();
  await page.getByRole("option", { name: ruleName }).click();
  await expect(page.getByTestId("rule-execution-row").first()).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });

  // -- cleanup: delete the rule (its executions stay in the audit log) ----
  await page.getByRole("tab", { name: "قوانین", exact: true }).click();
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await card.getByTestId("rule-card-delete").click();
  const deleteDialog = page.getByTestId("rule-delete-dialog");
  await expect(deleteDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(deleteDialog).toContainText(ruleName);
  await page.getByTestId("rule-delete-confirm").click();
  await expect(card).toHaveCount(0, { timeout: EXPECT_TIMEOUT });

  expect(errors).toEqual([]);
});
