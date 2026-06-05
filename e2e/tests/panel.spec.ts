import { test, expect } from "@playwright/test";
import { login, teardownOcr } from "../helpers/login";

// One login per file (the captcha is rate-limited), reused across tests.
test.beforeAll(async ({ browser }) => {
  const page = await browser.newPage();
  await login(page);
  await page.close();
});

test.afterAll(async () => {
  await teardownOcr();
});

// Each test logs in fresh (cookies/token aren't shared across contexts), but the
// OCR worker is warm so it's quick. These are READ-ONLY checks — no send/confirm.
test.beforeEach(async ({ page }) => {
  await login(page);
});

test("login works and the dashboard loads", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible();
  // Stat cards are present (panels / resellers / sales / debt).
  await expect(page.getByText("نمایندگان اصلی")).toBeVisible();
});

test("dashboard shows Toman only — no USDT figure", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "داشبورد" })).toBeVisible();
  // After the Toman-canonical cleanup, the dashboard must not render any USDT text.
  await expect(page.getByText("USDT", { exact: false })).toHaveCount(0);
});

test("payments table has a مبلغ column but no USDT column", async ({ page }) => {
  await page.goto("/payments");
  await expect(page.getByRole("columnheader", { name: "مبلغ" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "USDT" })).toHaveCount(0);
});

test("invoices table has no USDT column", async ({ page }) => {
  await page.goto("/invoices");
  await expect(page.getByRole("columnheader", { name: /مبلغ/ })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "USDT" })).toHaveCount(0);
});

test("debts table has no USDT column", async ({ page }) => {
  await page.goto("/debts");
  await expect(page.getByRole("columnheader", { name: "USDT" })).toHaveCount(0);
});

test("sidebar shows the app version", async ({ page }) => {
  await page.goto("/");
  // The version pinned in VERSION is rendered in the sidebar footer (e.g. v1.37.8).
  await expect(page.getByText(/^v\d+\.\d+\.\d+$/)).toBeVisible();
});
