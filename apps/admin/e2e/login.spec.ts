import { expect, test } from "@playwright/test";

/**
 * Login -> session cookie -> BFF data call, against the real backend.
 * Requires the FastAPI backend on :8001 and HESABYAR_DEV_API_KEY
 * (see apps/admin/README.md). The authenticated tests skip cleanly when
 * the key is absent; the redirect + invalid-key tests always run.
 */

const DEV_KEY = process.env.HESABYAR_DEV_API_KEY ?? "";

// Cold dev-server compiles plus real backend round-trips can exceed
// Playwright's 30s default on a loaded VM.
test.setTimeout(120_000);

test("unauthenticated dashboard visit redirects to /login", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByText("ورود به حسابیار")).toBeVisible();
  await expect(page.getByLabel("کلید دسترسی")).toBeVisible();
});

test("invalid key shows a Persian error and stays on /login", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("کلید دسترسی").fill("hya_live_thiskeydoesnotexist0000000000");
  await page.getByRole("button", { name: "ورود", exact: true }).click();
  // #login-error, not getByRole("alert"): Next's route announcer also has role=alert.
  await expect(page.locator("#login-error")).toContainText("این کلید معتبر نیست");
  await expect(page).toHaveURL(/\/login$/);
});

test("valid key logs in, sets an httpOnly session cookie, and loads companies via the BFF", async ({
  page,
  context,
}) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");

  await page.goto("/login");
  await page.getByLabel("کلید دسترسی").fill(DEV_KEY);
  await page.getByRole("button", { name: "ورود", exact: true }).click();

  // Dashboard renders with live data (Adapter Test company in the top bar).
  // Generous timeout: the first dev-server compile of "/" + the cold
  // backend probe can take a while.
  // Any port: the suite runs against :3000/:3001/:3002 depending on the story.
  await expect(page).toHaveURL(/\/$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "داشبورد", exact: true })).toBeVisible({ timeout: 15_000 });
  // The company name appears in several widgets; the first is the top-bar selector.
  await expect(page.getByText("HesabYar Adapter Test").first()).toBeVisible({ timeout: 15_000 });

  // The session cookie exists, is httpOnly, and the key is nowhere in the browser.
  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === "hesabyar_session");
  expect(session).toBeDefined();
  expect(session!.httpOnly).toBe(true);
  const storage = await page.evaluate(() => ({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
    cookieVisibleToJs: document.cookie,
  }));
  expect(JSON.stringify(storage)).not.toContain("hya_live_");
  expect(storage.cookieVisibleToJs).not.toContain("hesabyar_session");

  // The BFF route answers directly with the session cookie attached.
  const res = await page.request.get("/api/bff/companies?limit=1");
  expect(res.ok()).toBe(true);
  const body = (await res.json()) as { items: { name: string }[] };
  expect(body.items[0]?.name).toBe("HesabYar Adapter Test");
});

test("logout returns to /login and the dashboard is gated again", async ({ page }) => {
  test.skip(!DEV_KEY, "HESABYAR_DEV_API_KEY not set — mint one per apps/admin/README.md");

  await page.goto("/login");
  await page.getByLabel("کلید دسترسی").fill(DEV_KEY);
  await page.getByRole("button", { name: "ورود", exact: true }).click();
  await expect(page.getByRole("heading", { name: "داشبورد", exact: true })).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "خروج" }).click();
  await expect(page).toHaveURL(/\/login$/, { timeout: 30_000 });

  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/, { timeout: 30_000 });

  // The BFF now refuses without a session.
  const res = await page.request.get("/api/bff/companies?limit=1");
  expect(res.status()).toBe(401);
});
