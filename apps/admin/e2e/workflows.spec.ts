import { expect, test, type APIResponse, type Page } from "@playwright/test";
import { DEV_KEY, login } from "./auth";
import { firstCompanyId, pickAccounts } from "./journal-helpers";

/**
 * Workflows e2e (US-565, Phase 5-7 ۵خ W3) against the real backend:
 * one rule journey (create → dry-run → enable → live execution),
 * one recurring template (create → preview, via the BFF — US-563
 * shipped no admin UI for recurring templates), and one bank rule
 * (suggest → 3 confirms → promote → auto-code → undo).
 *
 * Every created row carries a unique stamp per run (`US-565 e2e
 * <stamp>`) so the tests find their own rows in the shared CI/dev
 * DB; no figure assertion touches pre-existing data. The bank-rule
 * CSV fixtures MUST tie out (start + Σ lines = end balance) or the
 * tie-out guard degrades the auto-code to a suggestion and the
 * journey fails. Statements cannot be deleted (no product path) —
 * they stay as drafts; the rule and the template are deleted at
 * the end of their journeys.
 */

// Generous budget: cold compiles + BFF round-trips against remote
// Tryton; the 90s/180s timeouts match the established patterns
// (the dashboard batch took ~105s in ۵ث).
test.setTimeout(600_000);

const EXPECT_TIMEOUT = 90_000;
const LONG_TIMEOUT = 180_000;

function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // Same known pre-existing dashboard issue as the rules spec:
    // the dashboard home's setup-status fetch can 502/503 against
    // the remote dev DB while trytond restarts — not a workflows
    // defect, and the login lands on "/" before each spec navigates.
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

async function bffPost(page: Page, path: string, data?: unknown): Promise<APIResponse> {
  return page.request.post(path, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data,
    timeout: LONG_TIMEOUT,
  });
}

async function bffGet(page: Page, path: string): Promise<unknown> {
  const res = await page.request.get(path, { timeout: LONG_TIMEOUT });
  expect(res.ok(), `${path} -> ${res.status()}`).toBe(true);
  return res.json();
}

const FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

/** 100000 -> «۱۰۰٬۰۰۰» (thousands-separated Persian digits, like the fixtures). */
function faDigits(value: number): string {
  return value
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, "٬")
    .replace(/\d/g, (d) => FA_DIGITS[Number(d)]);
}

// -- the rule journey ------------------------------------------------------------

test("rule journey: create → dry-run → enable → live execution", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const errors = collectConsoleErrors(page);
  const stamp = `${Date.now()}-${Math.floor(Math.random() * 10000)}`;
  const ruleName = `US-565 e2e rule ${stamp}`;

  await login(page);
  await page.goto("/integrations/workflows");
  await expect(page.getByRole("heading", { name: "گردش‌کارها" })).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });
  await expect(page.getByTestId("rules-page")).toBeVisible({ timeout: EXPECT_TIMEOUT });

  // -- create (disabled, notify action, one amount condition) ------------
  await page.getByTestId("rules-new").click();
  const dialog = page.getByTestId("rule-form-dialog");
  await expect(dialog).toBeVisible({ timeout: EXPECT_TIMEOUT });

  await page.getByTestId("rule-form-name").fill(ruleName);
  await page.getByTestId("rule-form-add-condition").click();
  await expect(page.getByTestId("rule-form-condition-row-0")).toBeVisible();
  await page.getByTestId("rule-form-condition-value-0").fill("1000000");
  await page.getByTestId("rule-form-recipients").fill("ops");
  await page.getByTestId("rule-form-subject").fill("سفارش بزرگ");
  await page.getByTestId("rule-form-message").fill("سفارشی بزرگ ثبت شد.");
  await page.getByTestId("rule-form-enabled").click();
  await page.getByTestId("rule-form-save").click();
  await expect(dialog).toBeHidden({ timeout: EXPECT_TIMEOUT });

  const card = page.getByTestId("rule-card").filter({ hasText: ruleName });
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(card).toContainText("غیرفعال");

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
  await expect(page.getByTestId("rule-execution-row").first()).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });

  // -- cleanup ------------------------------------------------------------
  await page.getByRole("button", { name: "بستن", exact: true }).click();
  await expect(detail).toBeHidden({ timeout: EXPECT_TIMEOUT });
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await card.getByTestId("rule-card-delete").click();
  const deleteDialog = page.getByTestId("rule-delete-dialog");
  await expect(deleteDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await page.getByTestId("rule-delete-confirm").click();
  await expect(card).toHaveCount(0, { timeout: EXPECT_TIMEOUT });

  expect(errors).toEqual([]);
});

// -- the recurring journey (BFF: US-563 shipped no admin UI) --------------------

test("recurring journey: create → preview → pause → resume → delete (BFF)", async ({
  page,
}) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const errors = collectConsoleErrors(page);
  const stamp = `${Date.now()}-${Math.floor(Math.random() * 10000)}`;
  const templateName = `US-565 e2e recurring ${stamp}`;

  await login(page);
  const companyId = await firstCompanyId(page);
  const [debitAccount, creditAccount] = await pickAccounts(page);

  // A journal template: ۱۰۰٬۰۰۰ ریال debit = ۱۰۰٬۰۰۰ ریال credit,
  // monthly on Jalali day 15, starting today.
  const today = new Date().toISOString().slice(0, 10);
  const created = await bffPost(page, "/api/bff/recurring-templates", {
    company_id: companyId,
    kind: "journal",
    name: templateName,
    schedule: { kind: "monthly", jalali_day: 15, start_date: today },
    journal: {
      description: templateName,
      lines: [
        { account_id: debitAccount.id, debit: 100_000, credit: 0 },
        { account_id: creditAccount.id, debit: 0, credit: 100_000 },
      ],
    },
  });
  expect(created.status(), "create template").toBe(201);
  const template = (await created.json()) as { id: string; status: string };
  expect(template.status).toBe("active");

  // Preview: 3 occurrences, each ۱۰۰٬۰۰۰ ریال, all on Jalali day 15.
  // The exact dates depend on today — only the day and the total
  // are asserted.
  const preview = (await bffGet(
    page,
    `/api/bff/recurring-templates/${template.id}/preview?count=3`,
  )) as {
    occurrence_number: number;
    scheduled_date: string;
    jalali_date: string;
    total_amount_irr: number;
    requested_jalali_day: number;
  }[];
  expect(preview).toHaveLength(3);
  for (const [index, occurrence] of preview.entries()) {
    expect(occurrence.occurrence_number).toBe(index + 1);
    expect(occurrence.total_amount_irr).toBe(100_000);
    expect(occurrence.requested_jalali_day).toBe(15);
    expect(occurrence.jalali_date).toMatch(/^14\d\d-\d\d-15$/);
  }

  // Pause → resume → delete (a never-fired template can be deleted).
  const paused = await bffPost(page, `/api/bff/recurring-templates/${template.id}/pause`);
  expect(paused.status(), "pause template").toBe(200);
  expect(((await paused.json()) as { status: string }).status).toBe("paused");
  const resumed = await bffPost(page, `/api/bff/recurring-templates/${template.id}/resume`);
  expect(resumed.status(), "resume template").toBe(200);
  expect(((await resumed.json()) as { status: string }).status).toBe("active");
  const deleted = await page.request.delete(`/api/bff/recurring-templates/${template.id}`, {
    timeout: LONG_TIMEOUT,
  });
  expect(deleted.status(), "delete template").toBe(204);

  expect(errors).toEqual([]);
});

// -- the bank-rule journey ---------------------------------------------------------

const IMPORT_COLUMNS = {
  date: "تاریخ",
  description: "شرح",
  debit: "برداشت",
  credit: "واریز",
  balance: "مانده",
  bank_ref: "شماره سند",
};

/** One CSV withdrawal line (Persian digits, Jalali date, Rial amounts). */
function csvWithdrawal(
  description: string,
  amountIrr: number,
  balanceIrr: number,
  ref: string,
): string {
  return `1405/08/26,${description},${faDigits(amountIrr)},,${faDigits(balanceIrr)},${ref}`;
}

async function importStatementViaBff(
  page: Page,
  opts: {
    companyId: string;
    journalId: number;
    defaultAccountId: number;
    filename: string;
    csv: string;
  },
): Promise<number> {
  const created = await bffPost(page, "/api/bff/bank/statements/imports", {
    company: opts.companyId,
    bank_key: "melli",
    statement_journal_id: opts.journalId,
    filename: opts.filename,
    content_base64: Buffer.from(opts.csv, "utf-8").toString("base64"),
    currency_unit: "rial",
    default_account_id: opts.defaultAccountId,
    columns: IMPORT_COLUMNS,
    date_kind: "jalali",
    header_row: 0,
  });
  expect(created.status(), "import batch create").toBe(201);
  const batch = (await created.json()) as { id: string; new_rows: number };
  expect(batch.new_rows).toBeGreaterThan(0);
  const confirmed = await bffPost(
    page,
    `/api/bff/bank/statements/imports/${batch.id}/confirm`,
  );
  expect(confirmed.status(), "import batch confirm").toBe(200);
  const confirmedBatch = (await confirmed.json()) as { engine_statement_id: number | null };
  expect(confirmedBatch.engine_statement_id).not.toBeNull();
  return confirmedBatch.engine_statement_id as number;
}

async function reconcileViaBff(
  page: Page,
  statementId: number,
): Promise<{
  matched_count: number;
  suggested_count: number;
  lines: { line_id: number; outcome: string; target_type: string | null }[];
}> {
  const res = await bffPost(page, `/api/bff/bank/statements/${statementId}/reconcile`);
  expect(res.status(), "reconcile").toBe(200);
  return (await res.json()) as {
    matched_count: number;
    suggested_count: number;
    lines: { line_id: number; outcome: string; target_type: string | null }[];
  };
}

test("bank-rule journey: suggest → confirm ×3 → promote → auto-code → undo", async ({
  page,
}) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const errors = collectConsoleErrors(page);
  const stamp = `${Date.now()}-${Math.floor(Math.random() * 10000)}`;
  const ruleName = `US-565 e2e ${stamp}`;
  // The pattern fragment: unique per run, and it appears in every
  // fixture description below (the matcher normalises case/digits).
  const fragment = `قاعده565-${stamp}`;

  await login(page);
  const companyId = await firstCompanyId(page);
  const [targetAccount, correctedAccount] = await pickAccounts(page);
  const journals = (await bffGet(
    page,
    `/api/bff/bank/statement-journals?company=${companyId}`,
  )) as { id: number; name: string }[];
  expect(journals.length, "statement journals").toBeGreaterThan(0);
  const journalId =
    journals.find((j) => j.name === "HesabYar US-304 Bank")?.id ?? journals[0].id;

  // -- create the rule in the UI (SUGGEST, description-only pattern) ---
  await page.goto("/banking/bank-rules");
  await expect(page.getByRole("heading", { name: "قواعد تطبیق" })).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });
  await expect(page.getByTestId("bank-rules-page")).toBeVisible({ timeout: EXPECT_TIMEOUT });

  await page.getByTestId("bank-rules-new").click();
  const dialog = page.getByTestId("bank-rule-form-dialog");
  await expect(dialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await page.getByTestId("bank-rule-form-name").fill(ruleName);
  await page.getByTestId("bank-rule-form-description").fill(fragment);
  // Direction stays the default (هر دو); the amount range stays open.
  await page.getByTestId("bank-rule-form-target-account").click();
  const targetOption = page.getByRole("option", {
    name: new RegExp(targetAccount.code.replace(/\./g, "\\.")),
  });
  // The accounts list takes ~33s against remote Tryton.
  await expect(targetOption.first()).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await targetOption.first().click();
  await page.getByTestId("bank-rule-form-save").click();
  await expect(dialog).toBeHidden({ timeout: EXPECT_TIMEOUT });

  const card = page.getByTestId("bank-rule-card").filter({ hasText: ruleName });
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(card).toContainText("پیشنهاد");
  await expect(card).toContainText("۰ از ۳");

  // -- import 3 matching lines; reconcile → 3 suggestions --------------
  // Balances tie out: ۱٬۰۰۰٬۰۰۰ − (۱۰۰٬۰۰۰ + ۱۵۰٬۰۰۰ + ۲۰۰٬۰۰۰) = ۵۵۰٬۰۰۰.
  const csv1 =
    "تاریخ,شرح,برداشت,واریز,مانده,شماره سند\n" +
    csvWithdrawal(`پرداخت ${fragment} سفارش یک`, 100_000, 900_000, `REF-${stamp}-1`) +
    "\n" +
    csvWithdrawal(`پرداخت ${fragment} سفارش دو`, 150_000, 750_000, `REF-${stamp}-2`) +
    "\n" +
    csvWithdrawal(`پرداخت ${fragment} سفارش سه`, 200_000, 550_000, `REF-${stamp}-3`) +
    "\n";
  const statementId1 = await importStatementViaBff(page, {
    companyId,
    journalId,
    defaultAccountId: targetAccount.id,
    filename: `us565-${stamp}-1.csv`,
    csv: csv1,
  });
  const firstRun = await reconcileViaBff(page, statementId1);
  expect(firstRun.suggested_count).toBe(3);
  expect(firstRun.matched_count).toBe(0);

  // -- confirm all 3 suggestions in the UI ------------------------------
  await page.getByTestId("bank-rules-tab-suggestions").click();
  const ownCards = page
    .getByTestId("bank-rule-suggestion-card")
    .filter({ hasText: ruleName });
  await expect(ownCards).toHaveCount(3, { timeout: EXPECT_TIMEOUT });
  const confirmButtons = ownCards.getByTestId("bank-rule-suggestion-confirm");
  for (let remaining = 3; remaining > 0; remaining--) {
    await expect(confirmButtons).toHaveCount(remaining, { timeout: EXPECT_TIMEOUT });
    await confirmButtons.first().click();
    await expect(confirmButtons).toHaveCount(remaining - 1, { timeout: EXPECT_TIMEOUT });
  }

  // -- promote to AUTO ----------------------------------------------------
  await page.getByTestId("bank-rules-tab-rules").click();
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(card).toContainText("۳ از ۳");
  await expect(card).toContainText("آمادهٔ ارتقاست");
  await card.getByTestId("bank-rule-promote").click();
  await expect(card).toContainText("خودکار", { timeout: EXPECT_TIMEOUT });

  // -- import 1 more matching line; reconcile → auto-coded ---------------
  // Balances tie out: ۱٬۰۰۰٬۰۰۰ − ۱۲۰٬۰۰۰ = ۸۸۰٬۰۰۰.
  const csv2 =
    "تاریخ,شرح,برداشت,واریز,مانده,شماره سند\n" +
    csvWithdrawal(`پرداخت ${fragment} سفارش چهار`, 120_000, 880_000, `REF-${stamp}-4`) +
    "\n";
  const statementId2 = await importStatementViaBff(page, {
    companyId,
    journalId,
    defaultAccountId: targetAccount.id,
    filename: `us565-${stamp}-2.csv`,
    csv: csv2,
  });
  const secondRun = await reconcileViaBff(page, statementId2);
  expect(secondRun.matched_count).toBe(1);
  expect(secondRun.lines).toHaveLength(1);
  expect(secondRun.lines[0].target_type).toBe("rule");

  // -- undo the auto-code in the statement-detail UI ----------------------
  // A UI reconcile now reports the line as already_matched (the BFF
  // reconcile coded it); the undo form covers that outcome too.
  await page.goto(`/banking/statements/${statementId2}`);
  await expect(page.getByRole("button", { name: "تطبیق صورت‌حساب" })).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });
  await page.getByRole("button", { name: "تطبیق صورت‌حساب" }).click();
  const undoForm = page.getByTestId("undo-auto-code-form");
  await expect(undoForm).toBeVisible({ timeout: LONG_TIMEOUT });
  await page.getByTestId("undo-auto-code-line").click();
  await page.getByRole("option").first().click();
  await page.getByTestId("undo-auto-code-account").fill(String(correctedAccount.id));
  await page.getByTestId("undo-auto-code-submit").click();
  await expect(page.getByTestId("undo-auto-code-notice")).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });
  await expect(page.getByTestId("undo-auto-code-notice")).toContainText(
    "کدگذاری خودکار برگردانده و تصحیح شد",
  );

  // -- cleanup: delete the rule (its suggestions survive by design) -----
  await page.goto("/banking/bank-rules");
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await card.getByTestId("bank-rule-delete").click();
  const deleteDialog = page.getByRole("alertdialog");
  await expect(deleteDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(deleteDialog).toContainText(ruleName);
  await page.getByRole("button", { name: "حذف قاعده", exact: true }).click();
  await expect(card).toHaveCount(0, { timeout: EXPECT_TIMEOUT });

  expect(errors).toEqual([]);
});

// -- responsive checks ------------------------------------------------------------

for (const width of [360, 390]) {
  test(`bank-rules page has no horizontal overflow at ${width}px`, async ({ page }) => {
    test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
    const errors = collectConsoleErrors(page);
    await page.setViewportSize({ width, height: 800 });
    await login(page);
    await page.goto("/banking/bank-rules");
    await expect(page.getByRole("heading", { name: "قواعد تطبیق" })).toBeVisible({
      timeout: EXPECT_TIMEOUT,
    });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBe(0);
    expect(errors).toEqual([]);
  });
}
