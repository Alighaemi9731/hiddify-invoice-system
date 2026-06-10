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
export E2E_BASE_URL='https://invoice-staging.example.com'
export E2E_ALLOW_REMOTE=1

npm test
```

For the repository's isolated staging stack:

```bash
cp deploy/.env.staging.example .env.staging
# Replace every placeholder before starting.
docker compose -p invoice-staging --env-file .env.staging \
  -f deploy/docker-compose.staging.yml up -d --build
E2E_BASE_URL=http://127.0.0.1:18080 E2E_USER=staging E2E_PASS='...' npm test
```

The staging stack uses separate named volumes, binds only to localhost, disables the
scheduler, and does not start the Telegram bot.

`E2E_BASE_URL` is mandatory. The runner refuses every non-local target unless an intentional
read-only run also sets `E2E_ALLOW_REMOTE=1`; production coordinates never belong in Git.

## What it checks

- Login works (CAPTCHA solved automatically, retried on OCR miss).
- Dashboard loads and shows **Toman only** (no USDT figure).
- Payments / Invoices / Debts tables have **no USDT column**.
- The sidebar renders the app version.

## Notes

- The CAPTCHA is single-use and noisy, so the login helper retries up to 8 times with a
  fresh captcha each time. `deviceScaleFactor: 3` sharpens the screenshot for OCR.
- **Reliability caveat (important):** OCR can misread the captcha, and every wrong answer
  counts toward the login rate-limit — so running the full suite repeatedly against PROD
  can trip the lockout and make logins fail in a cascade. The app itself is unaffected.
  The robust fix is to run this against a **staging** stack with the captcha disabled
  (set `E2E_BASE_URL` there). Until then, treat a flaky run as an OCR/rate-limit artifact,
  not an app regression — re-run after the lockout window, or verify manually.
- Never add destructive tests (generate/send/confirm) to a suite that can target production.
- Add new specs under `tests/`; reuse `helpers/login.ts`.
