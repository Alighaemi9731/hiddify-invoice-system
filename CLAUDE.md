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
- `usage_limit_GB > free_under_gb` — configs at/under the free threshold (default **1 GB**, so both 0.5 GB and 1 GB) are free test configs; **anything above (1.5, 5, …) IS billed**. Comparison is exact (not rounded), so a real 1.3 GB package is never mistaken for a test config. (`free_under_gb` is editable in Settings/pricing.)

`amount_toman = Σ usage_limit_GB × price_per_GB` (default 1000 T/GB; per-reseller override). **No** prior-unpaid carry-over. Convert to USDT via the configurable `toman_per_usdt` rate. The **Owner** (super_admin) and `exclude_from_billing` resellers are never billed.

**Re-generation safety:** one invoice per `(reseller, period)` (unique constraint). Re-running «صدور فاکتورهای دوره» recomputes only DRAFTs; **paid** invoices are never recomputed (even with `force`), and other delivered (sent/overdue/enforced) are skipped unless `force`. So generating twice / regenerating a past month never duplicates or disturbs settled accounting. «حذف پیش‌نویس‌ها» (`POST /api/invoices/discard-drafts`) deletes only DRAFT invoices (a run you don't want to keep) — delivered ones are untouched. Drafts are NOT written to the financial ledger; an invoice enters the ledger only when sent/paid.

**Durable financial ledger:** every invoice's money facts (panel, reseller, month, GB, amount, paid/unpaid, txid) are mirrored into `financial_records` (denormalized, no FK). This ledger is **never** deleted by the "wipe data" reset and survives panel/reseller removal — viewable under «تاریخچهٔ مالی». Written by `app/services/financial_archive.py` on generate/pay/edit/cancel/defer.

## Dunning & enforcement (real Admin-API actions; auto-suspend OFF by default)

Per unpaid invoice (timings + texts editable in settings): **D+2** reminder, **D+4** reminder, **D+5** hard warning + **enforcement** — via the Hiddify **Admin REST API**: disable the reseller's + sub-resellers' end-users and set the reseller's `max_active_users`/`max_users` to 0 (snapshot prior values first). The day-count anchors on `sent_at`, UNLESS a **payment deadline** (`deferred_until`) is set on the invoice — then the whole cycle **restarts from the deadline date** (paused until then; the defer endpoint clears prior reminder marks and restores an already-suspended reseller for the new window). `enforcement_enabled` defaults **False** (dry-run logs intended actions); the live API path is fully implemented — flip it on in Settings. On **confirmed payment**, auto-restore the snapshot.

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

## Install & run (single production path — no separate local/dev version)

One command installs AND updates; re-running rebuilds to the latest release but
**preserves the database** (data is wiped only from the panel's «پاک‌سازی داده‌ها»):

```bash
curl -fsSL https://raw.githubusercontent.com/Alighaemi9731/hiddify-invoice-system/main/get.sh | sudo bash
```

It prints `http://<server-ip>`; the first visit shows the one-time **setup wizard**
(username / password / domain → auto-HTTPS). Stack = Caddy + SPA + FastAPI(+scheduler)
+ bot + Postgres, via `deploy/docker-compose.prod.yml`. The DB is Postgres only; the
backend image bundles `postgresql-client` so auto-backups capture a real `pg_dump` and
restores import via `psql`. Schema evolves on boot via `init_models()` +
`_sync_missing_columns` (dialect-aware `ADD COLUMN`). Running `backend/tests` (pytest,
`requirements-dev.txt`) is the only thing that touches SQLite — there is no local-run app variant.

## Milestone status

- [x] **M1** scaffold, models, auth, settings, docker, docs
- [x] **M2** panel sync + Panels CRUD + sample seed (`python -m app.cli.seed_sample`)
- [x] **M3** invoice engine + Toman→USDT + Persian PDF + invoices/resellers/reports(sales,debts,dashboard)/settings APIs
- [x] **M4** Telegram bot (aiogram): membership gate, link match, invoice delivery, TXID submit, delivery log
- [x] **M5** BEP-20 payment verification (BscScan TXID) + auto mark-paid + auto-restore hook
- [x] **M6** dunning + enforcement (Admin-API write adapter, reminders D+2/D+4/D+5, suspend/restore, dry-run default)
- [x] **M7** React RTL/Persian SPA (login, dashboard charts, panels, resellers, invoices, payments, debts, sales, logs, settings)
- [x] **M8** polish: unit tests (`backend/tests`), run docs
- [x] **M9** Phase 2 deploy: one-line installer/updater (latest release, DB preserved), Caddy auto-HTTPS, first-run setup wizard, security (captcha + rate-limit + TOTP 2FA), durable financial ledger. Single production-only code path (dev compose/Makefile/seeds removed).
- [x] **M18** Admin capacity & sub-admin control (Resellers tab). Both are real Hiddify Admin-API capabilities now surfaced in the panel. (1) **Capacity meter** — a new «پُری ظرفیت» column shows a colored bar of how full each admin's user quota is: users this admin CREATED (counted from `end_user_snapshots.added_by_uuid`, this admin only — the panel stores no live count) over its `max_users`; blue <70%, amber ≥70%, red ≥90% (`CapacityBar`). One grouped query (`_usage_counts`) feeds both list + tree. (2) **«افزایش ظرفیت» button** — a dialog with quick +50/100/200/500 chips + a free-entry field; `POST /api/resellers/{id}/bump-limits` reads the admin's CURRENT limits from the API (so repeat clicks compound), adds the amount to BOTH `max_users` and `max_active_users`, writes via the admin API, persists locally. (3) **«ساخت زیرمجموعه» toggle** — a per-row Switch bound to Hiddify's `can_add_admin` (now parsed in `base.PanelAdmin`, stored on `Reseller.can_add_admin`, synced each run); `POST /api/resellers/{id}/can-add-admin` flips it live. New `app/services/admin_capacity.py` + `AdminApiClient.get_admin`/`set_can_add_admin`/`_patch_admin` (shares the v12 500-bug verify-via-GET path with `set_admin_limits`). Verified live on the test panel (read limits/usage, bump +100, toggle, then restored to original).
- [x] **M17** Bot UX & owner activity feed. (1) **Rejection notice** — `reject_payment` now tells the customer their payment wasn't accepted (`tpl_payment_rejected`), but only on a real state CHANGE to rejected (re-reject is silent); confirm likewise notifies only on change to confirmed — so toggling/double-clicks don't spam. (2) **Mistyped input → menu** — `on_text` fallback (and any unknown `/command`) now shows the correct main menu via `_send_menu` (owner menu for the owner, reseller menu otherwise) instead of a dead-end hint. (3) **Richer owner bot menu** — added «🟡 فروش صفر این ماه» (`_owner_zerosale`), «🔄 همگام‌سازی پنل‌ها» (`owner:sync`), «🔔 اجرای یادآوری‌ها» (`owner:dunning`), «🗄 پشتیبان‌گیری اکنون» (`owner:backup`); heavy monthly-issue stays in the web panel. (4) **Broadcast to one panel** — `broadcast_audience_keyboard` gained «🖥 نمایندگان یک پنل» → panel picker (`bcaud:panel:<id>`, `broadcast_panel_keyboard`); the service already supported `audience="panel"`. (5) **Owner activity feed** — owner now gets a Telegram ping on the important events (like the panel's logs tab): a new TXID payment awaiting review or auto-confirmed on-chain (`_handle_txid`), a screenshot payment (photo forwarded, already), and a new reseller registering in the bot (`_handle_link`). Dunning/monthly/abuse owner pings already existed.
- [x] **M16** Multi-invoice settlement on manual confirm. The auto-verify path already settled the customer's due-now invoices oldest-first by on-chain amount (`_settle_due_now`); the MANUAL/screenshot confirm path previously paid only the single linked (oldest) invoice. Now `confirm_manually(session, payment_id, invoice_ids=...)` settles the EXACT set the owner picks. The panel confirm button opens a dialog (`GET /api/payments/{id}/due-invoices` → the customer's due-now invoices across all their reseller rows, oldest first) with checkboxes + running total + «تأیید همهٔ بدهی» / «تأیید فاکتورهای انتخاب‌شده» (and the proof image inline). Every confirm records `Payment.settled_invoice_ids` (comma list); `reject_payment` reverts EXACTLY those invoices (not just the primary), so a multi-invoice confirm is fully reversible. So one transfer can clear several invoices and the owner controls which — a customer paying for 2 invoices no longer leaves one stranded.
- [x] **M15** Payment UX & safety. (1) **Screenshot payments** — a reseller can pay by sending a deposit **photo** to the bot (`@router.message(F.photo)` → `_handle_payment_proof`), not just a TXID; the image is saved to `data/payment_proofs/payment_<id>.jpg`, a `Payment(method=screenshot, status=pending)` is linked to their oldest due invoice, and the photo is **forwarded to the owner's Telegram** + shown in the web panel (`GET /api/payments/{id}/proof`, `has_proof` flag, 🖼 «مشاهدهٔ رسید»). A reseller who sends the screenshot as a *file* is nudged to resend as a photo. (2) **Dunning hold while pending** — `run_dunning` skips ALL reminder/warning/enforcement steps for any invoice with a `pending` payment, and never auto-suspends a reseller who has any pending payment (`held_invoice_ids`/`held_reseller_ids`, `on_hold` count). So a customer awaiting the owner's confirmation is never auto-suspended; the hold lifts on confirm (→paid) or reject (→cycle resumes). (3) **Reversible decisions** — `payments.reject_payment` reverts a previously-confirmed invoice back to owed (un-pays it, updates the ledger), and `confirm_manually` works on a rejected payment (re-pays + restores) and only notifies the reseller on the FIRST confirm. Web-panel confirm/reject buttons stay enabled for every status (behind a confirm dialog) so a mis-click is always recoverable. Manual confirm does NOT auto-re-suspend on reject (dunning re-escalates on its timeline).
- [x] **M14** Per-sub-reseller invoices. A reseller bundled with its sub-resellers still gets ONE PDF for the whole subtree (the owner bills the top node — unchanged). NEW: in the bot's «مدیریت زیرمجموعه‌ها» view, each sub-reseller now shows per-month «📄 فاکتور <ماه>» buttons that build an on-demand PDF rooted at that sub (`reseller_report.node_invoice` → `invoice_pdf.render_sub_invoice_pdf`, `subinv:<sub_id>:<label>` handler) so a reseller can bill each of their sub-resellers separately. The sub PDF is base-only (sold-quota × the sub-node's `price_per_gb`), NOT persisted as an Invoice (the owner's invoice already covers the subtree), wallet address blank (reseller↔sub settlement is off-system), issuer = the requesting reseller's name. Returns "no sales" when the sub has zero billable usage that month.
- [x] **M13** Abuse-resistant metering (billing model "C", `app/services/metering.py`, `metering_enabled` default on). Runs inside the existing 6h sync (no extra cost): per-user reset-aware cumulative usage + provisioned-quota buffer on `EndUserSnapshot.meter_*`, and a monthly `usage_meters` row. Billing ADDS the abuse the snapshot rule misses, on top of the unchanged base: **overage** (consumption past the paid buffer → catches "reset usage daily so a small package never ends") + **edit_renewal** (quota topped up without a new `start_date` → catches renew-by-edit). On the first delivery of an affected invoice the reseller gets a per-user breakdown (which user, what, that it's billed) and the owner gets a heads-up (`notify_abuse_if_any`). Normal billing is untouched and stays correct even before metering has synced.
- [x] **M12** Invoice correction & payment robustness. (1) Per-invoice «بازمحاسبه از روی پنل» (`POST /api/invoices/{id}/recompute`, `invoicing.recompute_invoice`) — syncs the panel then refreshes that one invoice's figures from current data, keeping its status (paid invoices protected); the tool to fix an already-sent invoice after the reseller corrected the panel. (2) Payment settles the customer's DUE-NOW invoices oldest-first across all their reseller rows in ONE transfer (`payments._settle_due_now`); deferred-to-future invoices are excluded from both the bot «pay» total and settlement. (3) Resending an invoice deletes the previously delivered Telegram message (`delivery_log.tg_message_id`) so the reseller's chat shows only the latest version.
- [x] **M11** Backup now carries `secret_key` in meta.json (restore writes it to `.env` first) so a cross-server restore can decrypt the encrypted settings — restore is self-sufficient. (Note: a bulk "reset admin passwords" feature was considered but removed — Hiddify v12's REST API has no admin-password field; only the web form sets it.)
- [x] **M10** No-terminal ops: panel «راه‌اندازی مجدد سرویس» button + auto-restart after restore (`/api/ops/restart`, process exits → Docker `unless-stopped` restarts; pooled conns dropped via `engine.dispose()`/`pool_pre_ping`). Bot sub-reseller management: a reseller picks a panel → lists their sub-resellers → views a report (user count, per-month sold-quota sales via `reseller_report.node_report`) → **suspend/restore** that sub-reseller (reuses `enforcement.enforce_reseller`/`restore_reseller` with `dry_run=False`; ownership-gated by `_owns_sub`).

**Phase 1 MVP is complete and runnable.** Phase 2 (installer/domain/SSL/systemd) is intentionally NOT built.

Backend API surface: auth, panels, resellers, invoices, payments, reports (sales/sales-by-panel/debts/dashboard/delivery-log/enforcement-actions), operations (dunning/run, run-monthly), settings. Scheduler jobs: monthly invoicing, daily dunning, periodic sync.
