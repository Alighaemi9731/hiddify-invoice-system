# CLAUDE.md

Guidance for working in this repository. Keep this file up to date as the project grows.

## Project goal

Automated **reseller management & invoicing** for a VPN business running on **Hiddify** panels (~10 panels, ~400 resellers). Each month the system pulls usage from every panel, computes each reseller's invoice, sends it via a Telegram bot, collects **USDT (BEP-20)**, and (optionally) runs reminders + suspension for unpaid invoices. Replaces a manual desktop tool.

- **Phase 1:** localhost MVP — admin web panel + Telegram bot + Postgres + invoicing/dunning flow. Everything dockerized. **Complete.**
- **Phase 2 (in progress):** server deploy scaffolding lives in `deploy/` — one-line installer (`deploy/install.sh`, Ubuntu 24.04/26.04), `deploy/docker-compose.prod.yml`, and a **Caddy** reverse proxy (`deploy/Caddyfile`) for one-domain + automatic HTTPS. See `deploy/README.md`.

Full background and the resolved business decisions: see `docs/ARCHITECTURE.md`. (The earlier `MY_UNDERSTANDING.md` and the three reference folders were removed once their content was absorbed.)

## Architecture (one-liner)

`Hiddify panels → sync (backup JSON) → Postgres snapshot → invoice engine → Telegram bot + web panel → USDT payment verify → dunning/enforcement (Hiddify Admin API)`.

```
backend/   FastAPI API + APScheduler (sync, invoicing, dunning)
  app/core        config, db (async SQLAlchemy), crypto (Fernet), security (JWT/bcrypt)
  app/models      ORM: panels, resellers, end_user_snapshots, invoices, invoice_lines,
                  payments, settings, delivery_log, enforcement_actions, sync_runs, app_users
  app/schemas     Pydantic request/response models
  app/api         routers: auth, (panels, resellers, invoices, payments, debts, sales,
                  settings, dashboard — added per milestone)
  app/services    panel_client (backup-JSON + admin-API adapters), sync, invoice_engine,
                  pricing, payments, dunning, pdf, settings_service, bootstrap
  app/bot         aiogram v3 Telegram bot (membership gate, link match, delivery, payments)
  app/scheduler   APScheduler jobs
frontend/  React + Vite + TypeScript SPA (RTL, Persian) — MUI + Recharts
docs/      ARCHITECTURE.md + Mermaid diagrams
```

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Pydantic v2, APScheduler.
- **DB:** PostgreSQL 16 (prod/docker). `sqlite+aiosqlite` is supported for quick local runs/tests via `DATABASE_URL`.
- **Bot:** aiogram v3.
- **PDF:** reportlab + arabic-reshaper + python-bidi (Persian/RTL). **Dates:** Gregorian for billing periods; Jalali (`jdatetime`) for display.
- **Crypto/payments:** BEP-20 USDT; MVP verifies a submitted TXID via the BscScan API. HD-wallet per-reseller addresses are a deferred drop-in module.
- **Frontend:** React + Vite + TS + MUI (RTL theme, Vazirmatn) + Recharts.

## Data model essentials

- **panels** — one per Hiddify panel. `host`, `proxy_path` (encrypted), `owner_uuid`, `admin_api_key` (encrypted, for write/enforcement). Derives `backup_url` and `admin_api_base`.
- **resellers** — a panel admin (`mode` agent/admin). Keyed by `(panel_id, admin_uuid)`. Has `parent_admin_uuid` (hierarchy), `bot_chat_id` (set when they register in the bot), `price_per_gb` (nullable → global default), `exclude_from_billing`, `enforcement_state`, and `max_users_snapshot`/`max_active_users_snapshot` for exact restore.
- **end_user_snapshots** — latest snapshot per `(panel_id, user_uuid)`: `added_by_uuid`, `usage_limit_gb`, `start_date`, `enable`, etc.
- **invoices** + **invoice_lines** — one invoice per `(reseller, period_start, period_end)`.
- **payments**, **settings** (runtime, panel-editable), **delivery_log**, **enforcement_actions**, **sync_runs**.

## The invoice formula (source of truth — confirmed with owner)

For each reseller **and its descendant sub-resellers** (bundle via `parent_admin_uuid`), sum **`usage_limit_GB` (quota SOLD, not consumed)** of every end-user where:
- `added_by_uuid` ∈ the reseller/descendants, AND
- `start_date` is within the billing **Gregorian month** (service created that month), AND
- `usage_limit_GB != 1` (1 GB = test config; **5 GB now counts as normal traffic and IS billed**).

`amount_toman = Σ usage_limit_GB × price_per_GB` (default 1000 T/GB; per-reseller override). **No** prior-unpaid carry-over. Convert to USDT via the configurable `toman_per_usdt` rate. The **Owner** (super_admin) and `exclude_from_billing` resellers are never billed.

## Dunning & enforcement (build now, dry-run by default)

Per unpaid invoice (timings + texts editable in settings): **D+2** reminder, **D+4** reminder, **D+5** hard warning + **enforcement** — via the Hiddify **Admin REST API**: disable the reseller's + sub-resellers' end-users and set the reseller's `max_active_users`/`max_users` to 0 (snapshot prior values first). `enforcement_enabled` defaults **False** (dry-run: logs intended actions). On **confirmed payment**, auto-restore the snapshot.

## Security conventions

- **Never commit secrets.** Bot token, wallet seed/xpub, panel proxy paths/UUIDs/API keys, card numbers → `.env` (gitignored) or DB `settings` (encrypted). The three reference folders + `MY_UNDERSTANDING.md` are gitignored.
- Secret DB columns/settings are encrypted with Fernet (`app/core/crypto.py`) keyed off `SECRET_KEY`. The settings API masks secrets on read.
- Bootstrap config in `.env`; runtime config in DB, editable from the panel.

## Coding conventions

- `from __future__ import annotations`; modern type hints; SQLAlchemy 2.0 `Mapped[...]` / `mapped_column`.
- Async throughout (FastAPI, SQLAlchemy, aiogram, APScheduler). Each scheduler job opens its own session and never crashes the loop.
- Pure invoice math lives in `app/services/invoice_engine.py` and is unit-testable without a DB.
- Panel access goes through the `PanelClient` interface so read (backup JSON) and write (Admin API) adapters are swappable.
- Money: Toman as integer/Numeric; USDT as Numeric(…,6).

## Local run

**Docker (full stack):**
```bash
cp .env.example .env        # then edit SECRET_KEY, ADMIN_PASSWORD, etc.
docker compose up --build
# API → http://localhost:8000/docs   SPA → http://localhost:5173
```

**Backend only (no Docker), quick SQLite run:**
```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="sqlite+aiosqlite:///./data/app.db" SECRET_KEY=dev ADMIN_PASSWORD=dev123
uvicorn app.main:app --reload
```

Seed demo data from the sample backup: `python -m app.cli.seed_sample` (added in M2).

## Milestone status

- [x] **M1** scaffold, models, auth, settings, docker, docs
- [x] **M2** panel sync + Panels CRUD + sample seed (`python -m app.cli.seed_sample`)
- [x] **M3** invoice engine + Toman→USDT + Persian PDF + invoices/resellers/reports(sales,debts,dashboard)/settings APIs
- [x] **M4** Telegram bot (aiogram): membership gate, link match, invoice delivery, TXID submit, delivery log
- [x] **M5** BEP-20 payment verification (BscScan TXID) + auto mark-paid + auto-restore hook
- [x] **M6** dunning + enforcement (Admin-API write adapter, reminders D+2/D+4/D+5, suspend/restore, dry-run default)
- [x] **M7** React RTL/Persian SPA (login, dashboard charts, panels, resellers, invoices, payments, debts, sales, logs, settings)
- [x] **M8** polish: unit tests (`backend/tests`), synthetic demo seed (`app.cli.seed_demo`), Makefile, run docs

**Phase 1 MVP is complete and runnable.** Phase 2 (installer/domain/SSL/systemd) is intentionally NOT built.

Backend API surface: auth, panels, resellers, invoices, payments, reports (sales/sales-by-panel/debts/dashboard/delivery-log/enforcement-actions), operations (dunning/run, run-monthly), settings. Scheduler jobs: monthly invoicing, daily dunning, periodic sync.
