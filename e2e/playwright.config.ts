import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config for the invoice panel. Target is configurable via E2E_BASE_URL
 * (default = prod). Point it at a staging stack once one exists so destructive
 * tests can be added safely.
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
    baseURL: process.env.E2E_BASE_URL || "https://invoice.varzesh3.com.de",
    headless: true,
    ignoreHTTPSErrors: true,
    // Higher DPI → sharper captcha screenshot → better OCR.
    deviceScaleFactor: 3,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
