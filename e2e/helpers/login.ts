import { Page, expect } from "@playwright/test";
import { createWorker, Worker } from "tesseract.js";

let _worker: Worker | null = null;

async function ocrWorker(): Promise<Worker> {
  if (_worker) return _worker;
  const w = await createWorker("eng");
  await w.setParameters({
    tessedit_char_whitelist: "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    tessedit_pageseg_mode: "7" as any, // single text line
  });
  _worker = w;
  return w;
}

/**
 * Log into the panel, solving the image CAPTCHA with OCR. The captcha is single-use
 * and noisy, so on a miss we request a fresh one («کد جدید») and retry a few times —
 * each captcha is simple, so a handful of attempts converges reliably.
 *
 * Credentials come from env: E2E_USER, E2E_PASS (never hard-code them).
 */
export async function login(page: Page): Promise<void> {
  const user = process.env.E2E_USER;
  const pass = process.env.E2E_PASS;
  if (!user || !pass) throw new Error("Set E2E_USER and E2E_PASS environment variables.");

  const worker = await ocrWorker();
  await page.goto("/login");

  for (let attempt = 1; attempt <= 8; attempt++) {
    await page.getByRole("textbox", { name: "نام کاربری" }).fill(user);
    await page.getByRole("textbox", { name: "رمز عبور" }).fill(pass);

    const buf = await page.getByAltText("تصویر کد امنیتی").screenshot();
    const { data } = await worker.recognize(buf);
    const code = (data.text || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();

    const captchaBox = page.getByRole("textbox", { name: "کد امنیتی تصویر" });
    await captchaBox.fill(code);
    // exact:true so we don't also match the «ورود با Face ID / کلید عبور» passkey button
    await page.getByRole("button", { name: "ورود", exact: true }).click();

    // Success = we navigated off /login (the SPA replaces the route).
    try {
      await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 6000 });
      return;
    } catch {
      // OCR miss (or rate-limit blip) → grab a fresh captcha and try again.
      await page.getByRole("button", { name: "کد جدید" }).click().catch(() => {});
      await page.waitForTimeout(600);
    }
  }
  throw new Error("Login failed after 8 captcha attempts (OCR could not read the captcha).");
}

export async function teardownOcr(): Promise<void> {
  if (_worker) {
    await _worker.terminate();
    _worker = null;
  }
}
