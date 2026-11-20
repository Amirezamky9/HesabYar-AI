import dotenv from "dotenv";
import { defineConfig, devices } from "@playwright/test";

// Dev key + US-304 seed ids for the specs live in .env.local
// (gitignored, never committed) — written by scripts/ci/setup-playwright.sh.
dotenv.config({ path: ".env.local" });

/**
 * Playwright config for CI (GitHub Actions job `playwright`) and for
 * local runs against a CI-style stack (scripts/ci/setup-playwright.sh).
 *
 * Same specs, same reporters and same timeouts as the dev config
 * (playwright.config.ts) — with two deliberate differences:
 *
 * 1. NO `webServer` block. The setup script starts `pnpm start`
 *    (production server) explicitly and waits for it. The dev config's
 *    `pnpm dev` server does not hydrate in this environment, and
 *    auto-starting it here would hide a failed production server
 *    behind a dev-server fallback. If the server is not up, the specs
 *    fail fast with a connection error instead.
 * 2. `baseURL` comes from PLAYWRIGHT_BASE_URL (default :3000), so a
 *    local run can point at a non-default Next port without editing
 *    any config.
 *
 * Run it with:
 *   pnpm exec playwright test --config playwright.ci.config.ts
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3000",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
