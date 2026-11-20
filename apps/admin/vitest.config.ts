import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    include: ["lib/**/*.test.{ts,tsx}", "components/**/*.test.{ts,tsx}", "features/**/*.test.{ts,tsx}"],
    exclude: ["node_modules", "e2e/**"],
    // Deterministic timezone for date tests (panel timezone is Asia/Tehran).
    env: { TZ: "Asia/Tehran" },
  },
});
