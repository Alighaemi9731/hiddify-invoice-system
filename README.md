# Hiddify Reseller Invoicing System

Automated reseller management and monthly invoicing for a VPN business running on
**Hiddify** panels: sync usage from every panel → compute each reseller's invoice →
deliver it via a **Telegram bot** → collect **USDT (BEP-20)** → run reminders &
suspension for non-payers. Owner web panel is **Persian / RTL**.

- Architecture & diagrams: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Working guidance & conventions: [`CLAUDE.md`](CLAUDE.md)
- Changes and releases: [`CHANGELOG.md`](CHANGELOG.md)
- Audit remediation tracker: [`docs/REMEDIATION_PLAN.md`](docs/REMEDIATION_PLAN.md)

## Install (one line, on a fresh Ubuntu server)

```bash
curl -fsSL https://raw.githubusercontent.com/Alighaemi9731/hiddify-invoice-system/main/get.sh | sudo bash
```

It asks **nothing**: it installs Docker, builds the whole stack behind Caddy, and
prints `http://<server-ip>`. Open that address — the **first-run setup wizard** asks
for a username, password, and (optionally) a domain. If you enter a domain it fetches
an SSL certificate automatically and the panel moves to `https://<domain>`.

**Update / re-deploy:** run the exact same command again any time. It always rebuilds
to the latest release **but keeps your database** — data is never wiped by the
installer. To erase data and start a fresh panel, use **حساب و پشتیبان → پاک‌سازی کامل
داده‌ها** inside the panel.

The stack: `caddy` (reverse proxy + auto-HTTPS), `frontend` (SPA), `backend`
(FastAPI + scheduler), `bot` (Telegram), `db` (PostgreSQL 16). Everything but the
secrets you enter in the panel is configured for you. See [`deploy/README.md`](deploy/README.md).

CI runs backend Ruff, mypy and workflow tests, the frontend type/build plus a 500 KiB
chunk budget, shell syntax, and both production/staging Compose validation. The isolated
staging stack is documented in [`deploy/README.md`](deploy/README.md#isolated-staging).

## Using the system

1. **Panels** → add each Hiddify panel (paste its admin link + API key). Hit **همگام‌سازی** (sync) to pull data.
2. **Dashboard** → **صدور و ارسال ماهانه** generates the previous month's invoices and sends
   them, or use the **Invoices** tab to generate / preview / send per period (PDF per invoice).
3. **Telegram bot**: a reseller `/start`s, joins the required chats, pastes their panel
   link, then receives invoices and submits a receipt or supported transaction hash.
4. **Payments** → the owner reviews and confirms submitted payments manually. Transaction
   hashes link to the appropriate explorer; paid, previously-suspended resellers are restored.
5. **Dunning/enforcement**: unpaid invoices trigger reminders, a warning, then suspension.
   Setting a **payment deadline** on an invoice restarts that cycle from the new date.
   **Automatic suspension defaults to OFF** — enable it in **Settings** (`enforcement_enabled`)
   once your panel admin API keys are set.
6. **تاریخچهٔ مالی** → a permanent ledger of every reseller's monthly amount and paid/unpaid
   status; it survives data wipes and panel/reseller removal.

## Configuration & secrets

The installer generates `.env` (DB creds, `SECRET_KEY`) for you. Everything else — bot
token, wallet address, exchange rate, pricing, message texts, reminder schedule, the
enforcement on/off switch — is edited from the **Settings** tab (stored in the DB; secrets
encrypted at rest). **Never commit secrets**; `.env` is gitignored.
