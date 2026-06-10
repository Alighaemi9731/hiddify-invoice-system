import { test, expect } from "@playwright/test";
import { login, teardownOcr } from "../helpers/login";

test.afterAll(async () => {
  await teardownOcr();
});

// Each test logs in fresh (cookies/token aren't shared across contexts). The captcha is
// solved by OCR with retries; no shared beforeAll login (it could exhaust the 90s hook
// timeout and fail the whole file). READ-ONLY checks — no send/confirm.
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

test("resellers page switches between the main list and hierarchy", async ({ page }) => {
  await page.goto("/resellers");
  await expect(page.getByRole("heading", { name: "نمایندگان" }).last()).toBeVisible();
  await expect(page.getByRole("tab", { name: /فهرست اصلی/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.getByRole("tab", { name: /درخت زیرمجموعه‌ها/ }).click();
  await expect(page.getByText(/شاخه اصلی/)).toBeVisible();
});

test("sidebar shows the app version", async ({ page }) => {
  await page.goto("/");
  // The version pinned in VERSION is rendered in the sidebar footer (e.g. v1.37.8).
  await expect(page.getByText(/^v\d+\.\d+\.\d+$/)).toBeVisible();
});
