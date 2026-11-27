import { expect, test, type Page } from "@playwright/test";

/**
 * Journal list + draft editor + validate/post/reverse, against the real
 * backend and Tryton (US-303). Requires the FastAPI backend on :8001 and
 * HESABYAR_DEV_API_KEY (see apps/admin/README.md).
 *
 * The e2e creates real Tryton moves in the dev DB — every test draft is
 * described «تست رابط کاربری …» so the data is identifiable.
 */

const DEV_KEY = process.env.HESABYAR_DEV_API_KEY ?? "";
const TEST_MARK = "تست رابط کاربری";

// Cold dev-server compiles on this VM exceed Playwright's 30s default.
test.setTimeout(120_000);

// The dev engine holds several general journals, so journal creation
// needs an explicit journal id. The test harness (Node, not the
// browser) resolves the test journal by name through the Tryton REST
// API, exactly like the Phase 2 Python e2e — same env vars/defaults.
const TRYTON_BASE_URL = process.env.TRYTON_BASE_URL ?? "http://127.0.0.1:8000";
const TRYTON_DATABASE = process.env.TRYTON_DATABASE ?? "hesabyar_tryton_test";
const TRYTON_API_KEY = process.env.TRYTON_API_KEY ?? "hesabyar-dev-rest-key";
const TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal";

async function resolveTestJournalId(): Promise<number> {
  // Tryton wants base64url WITH padding (Python urlsafe_b64encode keeps
  // the `=`; Node's "base64url" encoding strips it and Tryton 500s).
  const domain = Buffer.from(
    JSON.stringify([
      ["name", "=", TEST_JOURNAL_NAME],
      ["type", "=", "general"],
    ]),
  )
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
  const res = await fetch(
    `${TRYTON_BASE_URL}/${TRYTON_DATABASE}/rest/model/account.journal?d=${domain}&f=name`,
    { headers: { Authorization: `Bearer ${TRYTON_API_KEY}` } },
  );
  if (!res.ok) throw new Error(`Tryton journal lookup failed: ${res.status}`);
  const records = (await res.json()) as { id: number }[];
  if (records.length === 0) throw new Error(`Test journal ${TEST_JOURNAL_NAME} not found`);
  return records[0].id;
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("کلید دسترسی").fill(DEV_KEY);
  await page.getByRole("button", { name: "ورود", exact: true }).click();
  // Generous timeout: the first cold compile of / + the backend probe is slow.
  await expect(page).toHaveURL(/\/$/, { timeout: 60_000 });
}

interface TestAccount {
  id: number;
  code: string;
  name: string;
}

/** Two postable accounts from the live chart, via the BFF. */
async function pickAccounts(page: Page): Promise<[TestAccount, TestAccount]> {
  const companies = await page.request.get("/api/bff/companies?limit=10");
  expect(companies.ok()).toBe(true);
  const companyId = ((await companies.json()) as { items: { id: string }[] }).items[0].id;
  const res = await page.request.get(
    `/api/bff/accounting/accounts?company=${companyId}&limit=100`,
  );
  expect(res.ok()).toBe(true);
  const accounts = (
    (await res.json()) as { items: (TestAccount & { postable: boolean; active: boolean; closed: boolean })[] }
  ).items.filter((a) => a.postable && a.active && !a.closed);
  expect(accounts.length).toBeGreaterThanOrEqual(2);
  return [accounts[0], accounts[1]];
}

/**
 * The state badge in the VISIBLE detail header. Scoped through the
 * heading (an ARIA query, which ignores hidden trees): Next 16 keeps
 * the previous route mounted — hidden — for instant back-navigation,
 * so a page-wide text lookup can match the stale tree too.
 */
function stateBadge(page: Page, text: string) {
  return page
    .getByRole("heading", { name: /^سند / })
    .locator("xpath=..")
    .getByText(text, { exact: true });
}

/** Select an account in one grid row's picker by typing its code. */
async function selectAccount(page: Page, rowLabel: string, code: string) {
  await page.getByRole("combobox", { name: rowLabel }).click();
  await page.getByPlaceholder("جست‌وجوی حساب با کد یا نام…").fill(code);
  await page.getByRole("option", { name: new RegExp(code.replace(/\./g, "\\.")) }).first().click();
}

test("journal list loads drafts and filters by state", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);

  await page.goto("/accounting/journals");
  await expect(page.getByRole("heading", { name: "دفتر روزنامه" })).toBeVisible({ timeout: 30_000 });
  // The table finishes loading (data rows or the empty-state row).
  await expect(page.locator("table tbody tr").first()).toBeVisible({ timeout: 30_000 });

  // The "posted" filter is honest: the drafts endpoint never returns
  // posted journals, so the filter explains that instead of faking rows.
  await page.getByLabel("وضعیت").click();
  await page.getByRole("option", { name: "ثبت‌شده" }).click();
  await expect(page.getByText("سند ثبت‌شده‌ای در این فهرست نیست")).toBeVisible();

  await page.getByLabel("وضعیت").click();
  await page.getByRole("option", { name: "پیش‌نویس" }).click();
  await expect(page.getByText("سند ثبت‌شده‌ای در این فهرست نیست")).toBeHidden();
});

test("unbalanced draft cannot be saved", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  const [debitAccount, creditAccount] = await pickAccounts(page);

  await page.goto("/accounting/journals/new");
  await expect(page.getByText("سند جدید", { exact: true })).toBeVisible({ timeout: 30_000 });

  await page.getByLabel("شرح سند").fill(`${TEST_MARK} — نامتوازن`);
  await selectAccount(page, "حساب ردیف ۱", debitAccount.code);
  await selectAccount(page, "حساب ردیف ۲", creditAccount.code);
  await page.getByLabel("بدهکار").first().fill("1000000");
  await page.getByLabel("بستانکار").nth(1).fill("500000");

  // The live balance bar shows the mismatch and the save stays blocked.
  await expect(page.getByTestId("balance-bar")).toContainText("نامتوازن");
  await expect(page.getByRole("button", { name: "ذخیرهٔ پیش‌نویس" })).toBeDisabled();

  // Balancing the grid unblocks the save.
  await page.getByLabel("بستانکار").nth(1).fill("1000000");
  await expect(page.getByTestId("balance-bar")).toContainText("تراز است");
  await expect(page.getByRole("button", { name: "ذخیرهٔ پیش‌نویس" })).toBeEnabled();
});

test("create → validate → post → reverse end-to-end from the UI", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  const [debitAccount, creditAccount] = await pickAccounts(page);
  const journalId = await resolveTestJournalId();
  const description = `${TEST_MARK} — سند آزمایشی ${Date.now()}`;

  // --- create ---
  await page.goto("/accounting/journals/new");
  await expect(page.getByText("سند جدید", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByLabel("شرح سند").fill(description);
  await page.getByLabel("شناسهٔ دفتر روزنامه (اختیاری)").fill(String(journalId));
  await selectAccount(page, "حساب ردیف ۱", debitAccount.code);
  await selectAccount(page, "حساب ردیف ۲", creditAccount.code);
  await page.getByLabel("بدهکار").first().fill("1000000");
  await page.getByLabel("بستانکار").nth(1).fill("1000000");
  await expect(page.getByTestId("balance-bar")).toContainText("تراز است");

  await page.getByRole("button", { name: "ذخیرهٔ پیش‌نویس" }).click();
  await expect(page).toHaveURL(/\/accounting\/journals\/\d+$/, { timeout: 30_000 });
  const draftId = Number(page.url().match(/\/journals\/(\d+)$/)![1]);
  await expect(page.getByText(description)).toBeVisible();
  await expect(stateBadge(page, "پیش‌نویس")).toBeVisible();

  // --- validate ---
  await page.getByRole("button", { name: "بررسی اعتبار" }).click();
  await expect(page.getByText("سند معتبر است و آمادهٔ ثبت است.")).toBeVisible({ timeout: 30_000 });

  // --- post (with the confirm dialog) ---
  await page.getByRole("button", { name: "ثبت سند", exact: true }).click();
  await expect(page.getByText("سند ثبت شود؟")).toBeVisible();
  await page.getByRole("button", { name: "ثبت سند", exact: true }).last().click();
  await expect(page.getByText("سند ثبت شد.")).toBeVisible({ timeout: 30_000 });
  await expect(stateBadge(page, "ثبت‌شده")).toBeVisible({ timeout: 30_000 });

  // A posted journal has NO edit/delete path in the UI (§19 rule 8).
  await expect(page.getByRole("button", { name: "ویرایش" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /حذف/ })).toHaveCount(0);

  // The posted move is visible in Tryton (read back through the BFF).
  const posted = await page.request.get(`/api/bff/accounting/journal-drafts/${draftId}`);
  expect(posted.ok()).toBe(true);
  const postedBody = (await posted.json()) as { state: string; total_debit: number; total_credit: number };
  expect(postedBody.state).toBe("posted");
  expect(postedBody.total_debit).toBe(1_000_000);
  expect(postedBody.total_credit).toBe(1_000_000);

  // --- reverse (with the confirm dialog) ---
  await page.getByRole("button", { name: "برگشت سند" }).click();
  await expect(page.getByText("سند برگشت بخورد؟")).toBeVisible();
  await page.getByLabel("شرح برگشت (اختیاری)").fill("برگشت تست رابط کاربری");
  await page.getByRole("button", { name: "برگشت سند", exact: true }).last().click();
  await expect(page.getByText("سند برگشت خورد.")).toBeVisible({ timeout: 30_000 });
  await expect(page).toHaveURL(new RegExp(`/accounting/journals/\\d+\\?origin=${draftId}$`), {
    timeout: 30_000,
  });
  const reversalId = Number(page.url().match(/\/journals\/(\d+)\?/)![1]);
  expect(reversalId).not.toBe(draftId);
  // The reversal links back to the original journal.
  await expect(page.getByText("این سند، برگشت")).toBeVisible();
  await expect(page.locator(`a[href="/accounting/journals/${draftId}"]`)).toBeVisible();
  await expect(stateBadge(page, "ثبت‌شده")).toBeVisible({ timeout: 30_000 });

  // The reversal is a posted move in Tryton too.
  const reversal = await page.request.get(`/api/bff/accounting/journal-drafts/${reversalId}`);
  expect(reversal.ok()).toBe(true);
  const reversalBody = (await reversal.json()) as { state: string };
  expect(reversalBody.state).toBe("posted");
});

test("list search finds a draft by its description", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  await login(page);
  const [debitAccount, creditAccount] = await pickAccounts(page);
  const journalId = await resolveTestJournalId();
  const companies = await page.request.get("/api/bff/companies?limit=10");
  const companyId = ((await companies.json()) as { items: { id: string }[] }).items[0].id;
  const description = `${TEST_MARK} — جست‌وجو ${Date.now()}`;

  // Seed one draft through the BFF (the UI create flow is covered above).
  const created = await page.request.post("/api/bff/accounting/journal-drafts", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      company_id: companyId,
      date: new Date().toISOString().slice(0, 10),
      description,
      journal_id: journalId,
      lines: [
        { account_id: debitAccount.id, debit: 100000, credit: 0 },
        { account_id: creditAccount.id, debit: 0, credit: 100000 },
      ],
    },
  });
  expect(created.ok(), await created.text()).toBe(true);

  await page.goto("/accounting/journals");
  await expect(page.getByRole("heading", { name: "دفتر روزنامه" })).toBeVisible({ timeout: 30_000 });
  await page.getByLabel("جست‌وجو").fill(description);
  await expect(page.getByText(description)).toBeVisible({ timeout: 30_000 });

  // A search that matches nothing shows the empty state.
  await page.getByLabel("جست‌وجو").fill("رشته‌ای که هیچ سندی ندارد ۹۸۷۶۵۴");
  await expect(page.getByText("سندی با این فیلترها پیدا نشد.")).toBeVisible();
});
