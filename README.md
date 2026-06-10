# Hiddify Reseller Invoicing System

Automated reseller management and monthly invoicing for a VPN business running on
**Hiddify** panels: sync usage from every panel → compute each reseller's invoice →
deliver it via a **Telegram bot** → collect supported manual/crypto payments → run
reminders & suspension for non-payers. Owner web panel is **Persian / RTL**.

- Architecture & diagrams: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Working guidance & conventions: [`CLAUDE.md`](CLAUDE.md)
- Changes and releases: [`CHANGELOG.md`](CHANGELOG.md)
- Audit remediation tracker: [`docs/REMEDIATION_PLAN.md`](docs/REMEDIATION_PLAN.md)

## Verified install (fresh Ubuntu server)

```bash
tmp="$(mktemp -d)" && cd "$tmp"
curl -fLO https://github.com/Alighaemi9731/hiddify-invoice-system/releases/latest/download/release-installer.sh
curl -fLO https://github.com/Alighaemi9731/hiddify-invoice-system/releases/latest/download/release-installer.sh.sha256
sha256sum -c release-installer.sh.sha256
sudo bash release-installer.sh
```

It asks **nothing**: it installs Docker, builds the whole stack behind Caddy, and
prints `http://<server-ip>`. Open that address — the **first-run setup wizard** asks
for a username, password, and (optionally) a domain. If you enter a domain it fetches
an SSL certificate automatically and the panel moves to `https://<domain>`.

The bootstrap resolves one release tag, downloads its immutable archive, verifies the
published SHA-256, and only then runs code as root. Updates from the panel use the same
verified path. Downloaded releases remain cached for `deploy/rollback.sh`; database
volumes are never wiped by install/update/rollback.

The stack: `caddy` (reverse proxy + auto-HTTPS), `frontend` (SPA), `backend`
(FastAPI + scheduler), `bot` (Telegram), `db` (PostgreSQL 16). Everything but the
secrets you enter in the panel is configured for you. See [`deploy/README.md`](deploy/README.md).

CI installs hash-locked Python dependencies, runs Ruff, mypy and workflow tests, checks
`pip` consistency, installs frontend packages with `npm ci`, requires a clean production
dependency audit, enforces the 500 KiB chunk budget, tests release rollback, and validates
both Compose stacks.

## Using the system

1. **Panels** → add each Hiddify panel (paste its admin link + API key). Hit **همگام‌سازی** (sync) to pull data.
2. **Invoices** → generate a selected period, review drafts, then send one invoice or the
   whole period. The scheduler performs the previous-month run automatically.
3. **Telegram bot**: a reseller `/start`s, joins the required chats, pastes their panel
   link, then selects the exact unpaid invoice before submitting a receipt or transaction hash.
4. **Payments** → the owner reviews and confirms submitted payments manually. USDT hashes
   can optionally be checked through BscScan; all hashes link to the appropriate explorer.
   Paid, previously-suspended resellers are restored when no other due debt remains.
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
