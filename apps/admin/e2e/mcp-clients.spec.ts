import { expect, test, type Page } from "@playwright/test";
import { login, DEV_KEY } from "./auth";

/**
 * MCP client-management journey (US-606), against the real backend:
 * create a client → see it listed → rotate its key → revoke it →
 * the audit tab shows the revocation. Plus a responsive pass at
 * 360px / 390px (no horizontal overflow, actions reachable).
 *
 * The client carries a unique stamp per run (`US-606 e2e <stamp>`)
 * so the test finds its own card in the shared CI/dev DB. The
 * client is left revoked at the end of the journey — a revoked
 * client is inert (its key no longer authenticates) and stays
 * visible in the audit trail, which is the point of the exercise.
 *
 * The secret is read from the secret dialog's input and asserted
 * only as a `hya_live_` prefix — it is never logged.
 */

// Generous budget: cold compiles + BFF round-trips; the 90s expect
// timeouts below match the established dashboard-batch pattern.
test.setTimeout(360_000);

const EXPECT_TIMEOUT = 90_000;

/** True when the document has no horizontal overflow at the current viewport. */
async function hasNoHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

function captureErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // Same known pre-existing dashboard issue as the review-queue
    // spec: the dashboard home's setup-status fetch can 502/503
    // against the remote dev DB while trytond restarts — not an MCP
    // defect, and the login lands on "/" before this spec navigates
    // to the MCP page.
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

test("mcp journey: create → listed → rotate → revoke → audit shows the revocation", async ({
  page,
}) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
  const stamp = `${Date.now()}-${Math.floor(Math.random() * 10000)}`;
  const clientName = `US-606 e2e ${stamp}`;
  const errors = captureErrors(page);

  await login(page);
  await page.goto("/ai/mcp-clients");
  await expect(page.getByRole("heading", { name: "کلاینت‌های MCP" })).toBeVisible({
    timeout: EXPECT_TIMEOUT,
  });

  // -- create ------------------------------------------------------------
  await page.getByTestId("mcp-create-button").click();
  const formDialog = page.getByTestId("mcp-client-form-dialog");
  await expect(formDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await page.getByTestId("mcp-client-name-input").fill(clientName);
  await page.getByTestId("mcp-client-submit-button").click();

  // The secret dialog appears with the one-time secret.
  const secretDialog = page.getByTestId("mcp-client-secret-dialog");
  await expect(secretDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  const secretInput = page.getByTestId("mcp-client-secret-input");
  const firstSecret = await secretInput.inputValue();
  expect(firstSecret).toMatch(/^hya_live_/);
  await page.getByTestId("mcp-client-secret-close-button").click();
  await expect(secretDialog).toBeHidden({ timeout: EXPECT_TIMEOUT });

  // -- listed ------------------------------------------------------------
  const card = page.locator('[data-testid="mcp-client-card"]', { hasText: clientName });
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(card.getByText("فعال", { exact: true })).toBeVisible();

  // -- rotate ------------------------------------------------------------
  await card.getByTestId("mcp-rotate-button").click();
  const rotateDialog = page.getByTestId("mcp-rotate-confirm-dialog");
  await expect(rotateDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await page.getByTestId("mcp-rotate-confirm-button").click();
  await expect(secretDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  const secondSecret = await secretInput.inputValue();
  expect(secondSecret).toMatch(/^hya_live_/);
  expect(secondSecret).not.toBe(firstSecret);
  await page.getByTestId("mcp-client-secret-close-button").click();
  await expect(secretDialog).toBeHidden({ timeout: EXPECT_TIMEOUT });
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });

  // -- revoke ------------------------------------------------------------
  await card.getByTestId("mcp-revoke-button").click();
  const revokeDialog = page.getByTestId("mcp-revoke-dialog");
  await expect(revokeDialog).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await page.getByTestId("mcp-revoke-reason-input").fill("پایان تست US-606");
  await page.getByTestId("mcp-revoke-confirm-button").click();
  await expect(revokeDialog).toBeHidden({ timeout: EXPECT_TIMEOUT });

  // Revoked clients hide by default; show them and confirm the state.
  await expect(card).toBeHidden({ timeout: EXPECT_TIMEOUT });
  await page.getByTestId("mcp-show-revoked-switch").click();
  await expect(card).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(card.getByText("باطل‌شده", { exact: true })).toBeVisible();

  // -- audit shows the revocation ----------------------------------------
  await page.getByTestId("mcp-audit-tab").click();
  // Narrow to this client's revocation: the management-event filter
  // plus the client filter (the audit list may hold many rows).
  await page.getByTestId("mcp-audit-tool-filter").click();
  await page.getByRole("option", { name: "ابطال کلاینت" }).click();
  await page.getByTestId("mcp-audit-client-filter").click();
  await page.getByRole("option", { name: clientName }).click();
  const revokeRow = page
    .locator('[data-testid="mcp-audit-row"]', { hasText: "mcp.client.revoke" })
    .first();
  await expect(revokeRow).toBeVisible({ timeout: EXPECT_TIMEOUT });
  await expect(revokeRow.getByText("مدیریت", { exact: true })).toBeVisible();
  await expect(revokeRow.getByText("مجاز", { exact: true })).toBeVisible();

  expect(errors).toEqual([]);
});

for (const width of [360, 390]) {
  test(`mcp clients page: no horizontal overflow at ${width}px`, async ({ page }) => {
    test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");
    await page.setViewportSize({ width, height: 800 });
    const errors = captureErrors(page);

    await login(page);
    await page.goto("/ai/mcp-clients");
    await expect(page.getByRole("heading", { name: "کلاینت‌های MCP" })).toBeVisible({
      timeout: EXPECT_TIMEOUT,
    });
    await expect(page.getByTestId("mcp-create-button")).toBeVisible({
      timeout: EXPECT_TIMEOUT,
    });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(1000);
    let ok = await hasNoHorizontalOverflow(page);
    if (!ok) {
      await page.waitForTimeout(2000);
      ok = await hasNoHorizontalOverflow(page);
    }
    expect(ok, `horizontal overflow on /ai/mcp-clients at ${width}px`).toBe(true);

    // The audit tab fits too.
    await page.getByTestId("mcp-audit-tab").click();
    await page.waitForTimeout(1000);
    ok = await hasNoHorizontalOverflow(page);
    if (!ok) {
      await page.waitForTimeout(2000);
      ok = await hasNoHorizontalOverflow(page);
    }
    expect(ok, `horizontal overflow on the audit tab at ${width}px`).toBe(true);

    expect(errors).toEqual([]);
  });
}
