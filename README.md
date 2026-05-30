# Hiddify Reseller Invoicing System

Automated reseller management and monthly invoicing for a VPN business running on
**Hiddify** panels: sync usage from every panel → compute each reseller's invoice →
deliver it via a **Telegram bot** → collect **USDT (BEP-20)** → run reminders &
suspension for non-payers. Owner web panel is **Persian / RTL**.

This is the **Phase 1** localhost MVP. (Phase 2 — installer, domain/SSL, systemd — is not built yet.)

- Architecture & diagrams: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Working guidance & conventions: [`CLAUDE.md`](CLAUDE.md)

## Quick start (Docker)

```bash
cp .env.example .env
# Edit .env: set SECRET_KEY (long random), ADMIN_PASSWORD, and (optionally now,
# or later from the panel) TELEGRAM_BOT_TOKEN, payment + panel details.
docker compose up --build
```

- API + interactive docs → http://localhost:8000/docs
- Web panel (SPA) → http://localhost:5173
- Log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.

Services started: `db` (Postgres 16), `backend` (FastAPI + scheduler), `bot` (Telegram),
`frontend` (nginx + SPA).

## Quick start (backend only, no Docker)

Useful while developing. Uses a local SQLite file — no Postgres needed.

```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="sqlite+aiosqlite:///./data/app.db"
export SECRET_KEY="dev-secret-change-me" ADMIN_PASSWORD="dev123"
uvicorn app.main:app --reload
```

> Requires Python **3.12+**.

## Demo data & tests

```bash
# Synthetic demo data (no real panel needed) — then generate invoices from the panel:
cd backend && . .venv/bin/activate
DATABASE_URL="sqlite+aiosqlite:///./data/app.db" SECRET_KEY=dev python -m app.cli.seed_demo

# Or seed from a real Hiddify backup JSON:
python -m app.cli.seed_sample /path/to/backup.json

# Run unit tests (invoice formula, link matching, pricing):
python -m pytest
```

Or via the Makefile: `make venv`, `make seed-demo`, `make dev`, `make test`, `make frontend`.

## Using the system

1. **Panels** tab → add each Hiddify panel (host, secret proxy path, Owner UUID, and — for
   enforcement — the admin API key). Hit **همگام‌سازی** (sync) to pull data.
2. **Dashboard** → **صدور و ارسال ماهانه** generates the previous month's invoices and sends
   them, or use the **Invoices** tab to generate/preview/send per period (PDF per invoice).
3. **Telegram bot** (`bot` service): a reseller `/start`s, joins the announcement channel, pastes
   their panel link (matched by host+UUID), then receives invoices and submits a USDT **TXID**.
4. **Payments** tab → on-chain TXID verification (BscScan) auto-marks invoices paid; or confirm
   manually. Paid + previously-enforced resellers are auto-restored.
5. **Dunning/enforcement**: unpaid invoices trigger D+2/D+4 reminders, a D+5 warning, then
   suspension. **Enforcement defaults to dry-run** (logs only) — enable it in **Settings**
   (`enforcement_enabled`) once panel admin API keys are set.

## Configuration

`.env` holds only **bootstrap** values (DB, `SECRET_KEY`, initial owner login, optional
bot/payment bootstrap). Once running, the owner edits everything else — bot token,
wallet address, exchange rate, pricing, message texts, reminder schedule, the
enforcement on/off switch — from the **Settings** tab (stored in the DB; secrets encrypted).

**Never commit secrets.** `.env`, the three reference folders, and `MY_UNDERSTANDING.md`
are gitignored.

## Status

Built incrementally by milestone (see `CLAUDE.md`). **M1 (scaffold, models, auth,
settings, docker, docs)** is complete and the backend boots; panel sync, the invoice
engine, the bot, payments, dunning, and the SPA follow in M2–M8.
