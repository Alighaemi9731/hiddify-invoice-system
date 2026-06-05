# E2E tests (Playwright)

Browser-driven end-to-end tests for the invoice panel. They log in like a real admin
(solving the image CAPTCHA with OCR) and verify key UI invariants — currently read-only
(no invoices are sent, no payments confirmed).

## Run

```bash
cd e2e
npm install
npx playwright install chromium   # first time only

# credentials + target (never commit these)
export E2E_USER='admin'
export E2E_PASS='your-password'
export E2E_BASE_URL='https://invoice.varzesh3.com.de'   # or a staging URL

npm test
```

## What it checks

- Login works (CAPTCHA solved automatically, retried on OCR miss).
- Dashboard loads and shows **Toman only** (no USDT figure).
- Payments / Invoices / Debts tables have **no USDT column**.
- The sidebar renders the app version.

## Notes

- The CAPTCHA is single-use and noisy, so the login helper retries up to 8 times with a
  fresh captcha each time. `deviceScaleFactor: 3` sharpens the screenshot for OCR.
- Point `E2E_BASE_URL` at a **staging** stack before adding destructive tests
  (generate/send/confirm). Against prod, keep tests read-only.
- Add new specs under `tests/`; reuse `helpers/login.ts`.
