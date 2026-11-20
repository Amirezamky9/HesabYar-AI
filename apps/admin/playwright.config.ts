import dotenv from "dotenv";
import { defineConfig, devices } from "@playwright/test";

// Dev key for the login test lives in .env.local (gitignored, never committed).
dotenv.config({ path: ".env.local" });

/**
 * Browser e2e against the real backend. Requires:
 * - the FastAPI backend on :8001 with the Tryton dev DB (see README),
 * - a dev operator key in HESABYAR_DEV_API_KEY (mint with
 *   scripts/mint-dev-key.py; never commit it).
 * The login test skips cleanly when the key is absent.
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
  webServer: {
    command: "pnpm dev --port 3000 --hostname 127.0.0.1",
    url: "http://127.0.0.1:3000/login",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
