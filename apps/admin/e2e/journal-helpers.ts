import { expect, type Page } from "@playwright/test";

/**
 * Shared journal helpers for the US-305 journey/responsive specs
 * (login → journal cycle → reports → logout, keyboard walkthrough).
 * Mirrors the US-303 journals.spec helpers; kept separate so the
 * sibling spec stays untouched.
 */

export const TEST_MARK = "تست رابط کاربری";

const TRYTON_BASE_URL = process.env.TRYTON_BASE_URL ?? "http://127.0.0.1:8000";
const TRYTON_DATABASE = process.env.TRYTON_DATABASE ?? "hesabyar_tryton_test";
const TRYTON_API_KEY = process.env.TRYTON_API_KEY ?? "hesabyar-dev-rest-key";
const TEST_JOURNAL_NAME = "HesabYar Adapter Test General Journal";

export async function resolveTestJournalId(): Promise<number> {
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

export interface TestAccount {
  id: number;
  code: string;
  name: string;
}

/** Two postable accounts from the live chart, via the BFF. */
export async function pickAccounts(page: Page): Promise<[TestAccount, TestAccount]> {
  const companies = await page.request.get("/api/bff/companies?limit=10");
  expect(companies.ok()).toBe(true);
  const companyId = ((await companies.json()) as { items: { id: string }[] }).items[0].id;
  const res = await page.request.get(`/api/bff/accounting/accounts?company=${companyId}&limit=100`);
  expect(res.ok()).toBe(true);
  const accounts = (
    (await res.json()) as { items: (TestAccount & { postable: boolean; active: boolean; closed: boolean })[] }
  ).items.filter((a) => a.postable && a.active && !a.closed);
  expect(accounts.length).toBeGreaterThanOrEqual(2);
  return [accounts[0], accounts[1]];
}

/** First company id, via the BFF. */
export async function firstCompanyId(page: Page): Promise<string> {
  const companies = await page.request.get("/api/bff/companies?limit=10");
  expect(companies.ok()).toBe(true);
  return ((await companies.json()) as { items: { id: string }[] }).items[0].id;
}

/**
 * The state badge in the VISIBLE detail header. Scoped through the
 * heading (an ARIA query, which ignores hidden trees): Next 16 keeps
 * the previous route mounted — hidden — for instant back-navigation,
 * so a page-wide text lookup can match the stale tree too.
 */
export function stateBadge(page: Page, text: string) {
  return page
    .getByRole("heading", { name: /^سند / })
    .locator("xpath=..")
    .getByText(text, { exact: true });
}

/** Select an account in one grid row's picker by typing its code. */
export async function selectAccount(page: Page, rowLabel: string, code: string) {
  await page.getByRole("combobox", { name: rowLabel }).click();
  await page.getByPlaceholder("جست‌وجوی حساب با کد یا نام…").fill(code);
  await page.getByRole("option", { name: new RegExp(code.replace(/\./g, "\\.")) }).first().click();
}

/**
 * Create a draft journal through the BFF (no UI) and return its id.
 * The UI create flow is covered by the journey spec; this seeds drafts
 * for tests that only need one (e.g. the dialog focus-trap test).
 */
export async function createDraftViaBff(
  page: Page,
  opts: { description: string; journalId: number; accounts: [TestAccount, TestAccount]; amount?: number },
): Promise<number> {
  const companyId = await firstCompanyId(page);
  const amount = opts.amount ?? 100000;
  const created = await page.request.post("/api/bff/accounting/journal-drafts", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      company_id: companyId,
      date: new Date().toISOString().slice(0, 10),
      description: opts.description,
      journal_id: opts.journalId,
      lines: [
        { account_id: opts.accounts[0].id, debit: amount, credit: 0 },
        { account_id: opts.accounts[1].id, debit: 0, credit: amount },
      ],
    },
  });
  expect(created.ok(), await created.text()).toBe(true);
  return ((await created.json()) as { id: number }).id;
}
