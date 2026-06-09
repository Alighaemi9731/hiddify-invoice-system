import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL;
if (!baseURL) {
  throw new Error("Set E2E_BASE_URL explicitly. E2E tests never default to production.");
}

const productionURL = "https://invoice.varzesh3.com.de";
if (
  baseURL.replace(/\/+$/, "") === productionURL &&
  process.env.E2E_ALLOW_PRODUCTION !== "1"
) {
  throw new Error(
    "Refusing to run E2E against production. Set E2E_ALLOW_PRODUCTION=1 only for an intentional read-only run.",
  );
}

/**
 * E2E config for the invoice panel. The target must be explicit so a local or CI
 * command cannot accidentally send repeated login attempts to production.
 */
export default defineConfig({
  testDir: "./tests",
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL,
    headless: true,
    ignoreHTTPSErrors: true,
    // Higher DPI → sharper captcha screenshot → better OCR.
    deviceScaleFactor: 3,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
