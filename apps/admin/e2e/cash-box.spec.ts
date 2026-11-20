import { expect, test, type Locator, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";
import { firstCompanyId } from "./journal-helpers";

/**
 * Cash box (US-516, Phase 5-2 K2), against the real backend
 * (Supabase). Full flow: login → cash-boxes page → start a counting
 * session in the UI → enter a short count → sign the minutes
 * (cashier + supervisor + manager) → approve the variance in the
 * review queue (via the BFF — the queue UI is US-504's territory)
 * → post the variance journal in the UI → report tab shows the
 * shortage. Every figure comes from the live API.
 *
 * The box is provisioned via the BFF on the «HesabYar US-516 Store
 * Cash Box» statement journal (looked up BY NAME): one box per
 * journal per tenant, so the spec reuses the existing box when a
 * previous run already registered it. Stale open sessions from a
 * crashed run are completed as balanced via the BFF first; a stale
 * session awaiting the manager blocks the run with a clear message
 * (it needs a human decision, not an automated one).
 *
 * The posted variance move accumulates in the shared test company
 * by design (like the US-202 e2e tests); the amount is small
 * (50,000 IRR) and balance assertions always read the live
 * balance, never a hardcoded one.
 */

// Cold dev-server compiles on this VM exceed Playwright's 30s default,
// and every BFF round-trip hits Supabase. 480s covers the clickUntil
// retries (manager sign + post, up to 3 × 60s each) on a flaky day.
test.setTimeout(480_000);

const STORE_JOURNAL_NAME = "HesabYar US-516 Store Cash Box";
const CASHIER = "صندوقدار آزمون";
const SUPERVISOR = "ناظر آزمون";
const MANAGER = "مدیر آزمون";
const MINUTES_TEXT = "صورتجلسهٔ شمارش آزمون: کسری ۵۰٬۰۰۰ ریال در صندوق مشاهده شد.";
const SHORTAGE_IRR = 50_000;

/**
 * Click a dialog button until an expected text appears, retrying the
 * click up to 3 times. The engine-touching actions (manager sign,
 * variance post) run long Tryton transactions; trytond answers 503
 * on a flaky Supabase connection (DatabaseOperationalError) and the
 * draft/session is untouched, so a retry is what an operator does —
 * the dialog keeps the button enabled after the error. Throws the
 * last expectation error when all attempts fail.
 */
async function clickUntil(dialog: Locator, buttonName: string, expectedText: string): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    await dialog.getByRole("button", { name: buttonName }).click();
    try {
      await expect(dialog.getByText(expectedText)).toBeVisible({ timeout: 60_000 });
      return;
    } catch (err: unknown) {
      lastError = err;
    }
  }
  throw lastError;
}

/** Collect console errors for the whole flow; asserted at the end. */
function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // "Failed to load resource" duplicates the response listener
    // below, which records the failing URL — skip it here.
    if (text.startsWith("Failed to load resource")) return;
    errors.push(`${page.url()}: ${text}`);
  });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    const url = response.url();
    // The dashboard's setup-status fetch exceeds the BFF's 30s upstream
    // timeout on Supabase and the BFF answers 502; the dashboard
    // degrades gracefully. Tryton-backed fetches can also answer 503
    // while trytond restarts. Both are known pre-existing backend
    // perf/env issues, not cash-box defects.
    if (
      page.url().endsWith("/") &&
      (response.status() === 502 || response.status() === 503)
    ) {
      return;
    }
    errors.push(`${page.url()}: ${response.request().method()} ${url} -> ${response.status()}`);
  });
  page.on("pageerror", (err) => errors.push(`${page.url()}: pageerror: ${err.message}`));
  return errors;
}

async function bffGet(page: Page, path: string): Promise<Record<string, unknown>> {
  // Reads are idempotent: retry a 502/503/504 (BFF upstream timeout or
  // a trytond DatabaseOperationalError on a flaky Supabase connection).
  let lastStatus = 0;
  for (let attempt = 0; attempt < 3; attempt++) {
    const response = await page.request.get(`/api/bff/${path}`);
    if (response.ok()) {
      return (await response.json()) as Record<string, unknown>;
    }
    lastStatus = response.status();
    if (lastStatus !== 502 && lastStatus !== 503 && lastStatus !== 504) break;
    await page.waitForTimeout(5_000);
  }
  expect(false, `GET /api/bff/${path} -> ${lastStatus} after 3 attempts`).toBe(true);
  throw new Error("unreachable");
}

async function bffPost(page: Page, path: string, data: unknown): Promise<Record<string, unknown>> {
  const response = await page.request.post(`/api/bff/${path}`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data,
  });
  expect(response.ok(), `POST /api/bff/${path} -> ${response.status()}: ${await response.text()}`).toBe(true);
  return (await response.json()) as Record<string, unknown>;
}

async function ensureBox(page: Page, companyId: string, stamp: string): Promise<string> {
  const journals = (await bffGet(
    page,
    `cash-boxes/statement-journals?company=${companyId}`,
  )) as unknown as { id: number; name: string }[];
  const store = journals.find((j) => j.name === STORE_JOURNAL_NAME);
  if (!store) {
    throw new Error(
      `Statement journal ${STORE_JOURNAL_NAME} not found — provision it with ` +
        `~/workspace/coord-logs/us516-provision-cashboxes.py.`,
    );
  }
  const boxes = await bffGet(page, `cash-boxes?company=${companyId}&limit=50`);
  const existing = (boxes.items as { id: string; statement_journal_id: number }[]).find(
    (b) => b.statement_journal_id === store.id,
  );
  if (existing) return existing.id;
  const created = await bffPost(page, "cash-boxes", {
    company_id: companyId,
    name_fa: `صندوق آزمون ${stamp}`,
    kind: "store",
    statement_journal_id: store.id,
  });
  return created.id as string;
}

async function clearStaleSessions(page: Page, companyId: string, boxId: string): Promise<void> {
  const sessions = await bffGet(
    page,
    `count-sessions?company=${companyId}&cash_box_id=${boxId}&limit=20`,
  );
  for (const item of sessions.items as {
    id: string;
    status: string;
    cashier: string;
    supervisor: string;
    book_balance_irr: number;
  }[]) {
    if (item.status === "archived") continue;
    if (item.status === "awaiting_manager") {
      throw new Error(
        `Stale session ${item.id} is awaiting the manager — resolve it by hand ` +
          `(approve + post, or recount) before re-running this spec.`,
      );
    }
    // in_progress / awaiting_supervisor: complete it as balanced.
    // (A session with signatures already recorded cannot be
    // corrected this way — the count call fails and the run stops
    // with a clear message instead of a cryptic 409.)
    try {
      await bffPost(page, `count-sessions/${item.id}/count`, {
        counted_amount_irr: item.book_balance_irr,
        actor: item.cashier,
      });
      await bffPost(page, `count-sessions/${item.id}/sign`, {
        role: "supervisor",
        actor: item.supervisor,
      });
    } catch {
      throw new Error(
        `Stale session ${item.id} (${item.status}) could not be completed as balanced ` +
          `— it probably has signatures already. Resolve it by hand before re-running this spec.`,
      );
    }
  }
}

test("cash-box counting: short count, minutes, approval, post, report", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY is not set in .env.local");
  const errors = collectConsoleErrors(page);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");

  await login(page);
  const companyId = await firstCompanyId(page);
  const boxId = await ensureBox(page, companyId, stamp);
  await clearStaleSessions(page, companyId, boxId);

  // -- Boxes page: the box card with its live balance -------------------
  await page.goto("/banking/cash-boxes");
  const card = page.getByTestId(`cash-box-card-${boxId}`);
  // The page's first list fetch can 502 on a flaky day (same transient
  // engine errors as above); the page offers a retry — use it.
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await expect(card).toBeVisible({ timeout: 60_000 });
      break;
    } catch (err: unknown) {
      const retry = page.getByRole("button", { name: "تلاش دوباره" });
      if (attempt === 2 || (await retry.count()) === 0) throw err;
      await retry.click();
    }
  }
  await expect(card.getByText("ماندهٔ دفتری")).toBeVisible();

  // -- Start a session in the UI ----------------------------------------
  await card.getByRole("button", { name: "شروع شمارش" }).click();
  const startDialog = page.getByTestId("cash-box-start-dialog");
  await expect(startDialog).toBeVisible({ timeout: 30_000 });
  await startDialog.getByLabel("صندوقدار").fill(CASHIER);
  await startDialog.getByLabel("ناظر").fill(SUPERVISOR);
  await startDialog.getByRole("button", { name: "شروع شمارش", exact: true }).click();

  // The session dialog opens on the new session.
  const dialog = page.getByTestId("cash-box-session-dialog");
  await expect(dialog).toBeVisible({ timeout: 60_000 });
  await expect(dialog.getByText("در جریان")).toBeVisible({ timeout: 30_000 });

  // The book balance comes from the live API (never hardcoded).
  const sessions = await bffGet(
    page,
    `count-sessions?company=${companyId}&cash_box_id=${boxId}&limit=5`,
  );
  const session = (sessions.items as { id: string; book_balance_irr: number }[])[0];
  expect(session).toBeDefined();
  const countedToman = session.book_balance_irr / 10 - SHORTAGE_IRR / 10;
  expect(countedToman).toBeGreaterThan(0);

  // -- Enter a short count ----------------------------------------------
  await dialog.getByLabel("مبلغ شمارش‌شده").fill(String(countedToman));
  await dialog.getByRole("button", { name: "ثبت مبلغ شمارش" }).click();
  await expect(dialog.getByText("کسری")).toBeVisible({ timeout: 60_000 });
  await expect(dialog.getByLabel("متن صورتجلسه")).toBeVisible({ timeout: 30_000 });

  // -- Sign the minutes: cashier + supervisor + manager -----------------
  await dialog.getByLabel("متن صورتجلسه").fill(MINUTES_TEXT);
  await dialog.getByRole("button", { name: "امضای صندوقدار" }).click();
  await expect(dialog.getByText("امضاشده").first()).toBeVisible({ timeout: 60_000 });
  await dialog.getByRole("button", { name: "امضای ناظر" }).click();
  await expect(dialog.getByText("حاضر شده")).toBeVisible({ timeout: 60_000 });
  await dialog.getByLabel("مدیر").fill(MANAGER);
  // The manager sign completes the session server-side (draft + review
  // item): a long Tryton sequence, retried like an operator would.
  await clickUntil(dialog, "امضای مدیر", "در انتظار تأیید مدیر");

  // The DRAFT journal and review item exist now.
  const completed = (await bffGet(page, `count-sessions/${session.id}`)) as {
    draft_journal_id: number | null;
    review_item_id: string | null;
  };
  expect(completed.draft_journal_id).not.toBeNull();
  expect(completed.review_item_id).not.toBeNull();

  // -- Approve the variance in the review queue (BFF) --------------------
  await bffPost(page, `review-queue/items/${completed.review_item_id}/actions`, {
    action: "approve_variance_entry",
    note: "صورتجلسه بررسی و تأیید شد.",
  });

  // -- Post the variance journal in the UI --------------------------------
  // Same retry: the post is a long Tryton transaction and trytond
  // answers 503 on a flaky Supabase connection (DatabaseOperationalError);
  // the draft is untouched, so a second click posts it.
  await clickUntil(dialog, "ثبت سند در دفتر", "بایگانی‌شده");
  await dialog.getByRole("button", { name: "بستن" }).click();

  // -- Report tab shows the shortage --------------------------------------
  await page.goto("/reports");
  await page.getByRole("tab", { name: "صندوق" }).click();
  const report = page.getByTestId("cash-box-report");
  await expect(report).toBeVisible({ timeout: 30_000 });
  await expect(report.getByText("مجموع کسری")).toBeVisible({ timeout: 60_000 });
  // The report's figures come from the live API: this box's line
  // carries at least this run's 50,000 IRR shortage (earlier runs
  // may have added more) and at least one session.
  const today = new Date().toISOString().slice(0, 10);
  const reportData = await bffGet(
    page,
    `cash-boxes/report?company=${companyId}&date_from=${today.slice(0, 8)}01&date_to=${today}`,
  );
  const line = (reportData.lines as { cash_box_id: string; shortage_irr: number; session_count: number }[]).find(
    (l) => l.cash_box_id === boxId,
  );
  expect(line).toBeDefined();
  expect(line!.shortage_irr).toBeGreaterThanOrEqual(SHORTAGE_IRR);
  expect(line!.session_count).toBeGreaterThanOrEqual(1);

  expect(errors, `console errors:\n${errors.join("\n")}`).toEqual([]);
});
