# Database structure

Complete reference for the system's PostgreSQL schema (SQLite is used only for
tests). Every table, what it is for, **when rows are written**, **whether it grows**,
and **what each column means**.

Schema evolves on boot through versioned Alembic migrations (`backend/alembic`).
Money is stored as `Numeric`; secrets are Fernet-encrypted at rest (columns ending
`_enc`).

## Rolling back across a migration

**The database must never be left ahead of the code.** On boot the app upgrades to the
head revision its own build ships (`app/core/db.py::_upgrade_schema`). If
`alembic_version` names a revision that build does not contain, Alembic cannot resolve
the chain and raises `CommandError: Can't locate revision identified by '<rev>'`. That
happens *before* any DDL, so it blocks the boot even when the schema difference is
purely additive and the older code would have tolerated it. Nothing catches it, and
`restart: unless-stopped` turns it into an endless restart loop on **both** the backend
and the bot.

`deploy/rollback.sh` therefore downgrades the schema **before** swapping the code:

```bash
sudo ALLOW_DOWNGRADE=1 /opt/hiddify-invoice-system/deploy/rollback.sh v1.2.3
```

It compares the live revision against the target release's revision set, and when the
database is ahead it takes a `pg_dump` to `update/rollback-<tag>-<ts>.sql`, then runs
`alembic downgrade <target head>` **inside the current (newer) container** — which is
the only place the relevant `downgrade()` bodies still exist, because the installer
deletes files that the target release does not ship. Without `ALLOW_DOWNGRADE=1` it
refuses and changes nothing. Any failure aborts and leaves the system on the current
release, still running.

This works because **every migration is genuinely reversible**. Four revisions have a
deliberate no-op `downgrade()` (one-way data normalizations: obsolete enum labels,
lowercased uuids, lowercased TON txids, the storefront-enabled backfill) — rolling back
*past* them is still safe because they changed data, not structure.
`backend/tests/test_migration_downgrade_chain.py` enforces this: it walks the whole
chain down on SQLite and on real Postgres, and fails CI if a new migration ships a stub
`downgrade()` without being explicitly documented as one-way.

Every release that carries a migration records its `down_revision` in `CHANGELOG.md`, so
the rollback target is known without reading the code.

## Growth & retention at a glance

| Class | Tables | Growth | Cleanup |
|-------|--------|--------|---------|
| **Permanent records** | `panels`, `resellers`, `invoices`, `invoice_lines`, `payments`, `financial_records`, `reseller_crm_state`, `reseller_followups`, `app_users`, `webauthn_credentials`, `settings`, `bot_users` | Bounded by the business (panels, resellers, months billed, login accounts, follow-ups logged) | Never auto-deleted |
| **Operational state** | `end_user_snapshots`, `usage_meters` | One row per current end-user / per user-month | Upserted, not appended; see notes |
| **Logs / audit** | `sync_runs`, `delivery_log`, `enforcement_actions` | Append-only — would grow forever | **Pruned daily** by `log_retention_days` (default 60), see below |

### Log retention (the daily maintenance sweep)

`app/services/maintenance.py::prune_old_logs` runs daily (scheduler job
`daily_maintenance`, 04:30 local) and deletes rows older than **`log_retention_days`**
(default **90**, min 7; set in Settings → زمان‌بندی). It never touches the financial
ledger or invoices. It preserves rows that are still operationally live:

- **`sync_runs`** — deletes anything older than the window. Billing/freshness reads
  `panels.last_synced_at` (a column), never this table, so old rows have no value.
- **`delivery_log`** — deletes aged rows **except** those tied to an invoice that is
  still owed (`sent`/`overdue`/`enforced`): dunning reads them to avoid re-sending a
  reminder and to delete the prior message on a resend. Aged rows of paid/canceled
  invoices, and rows with no invoice (broadcasts, generic notices), are removed.
- **`enforcement_actions`** — deletes aged **terminal** rows (`done`, `reverted`,
  `dry_run`, `failed`) including their large JSON `snapshot`. In-flight queue work
  (`planned`, `running`, `partial`) is kept regardless of age.

Setting `log_retention_days = 0` disables pruning entirely.

> **Note — `end_user_snapshots`:** sync upserts the current users of each panel but
> does not delete the snapshot of a user removed on the panel; such a row simply stops
> being refreshed (its `last_synced_at` goes stale and billing/reporting treats the
> user as gone). This is a slow, bounded accumulation (a few hundred bytes per
> ever-seen user) and is intentionally kept — it is data, not a log.

---

## Core entities

### `panels` — a Hiddify panel the owner controls (~10)
Written when the owner adds/edits a panel in the Panels tab. `last_synced_at` /
`status` / `last_error` are updated on every sync.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `key` | short unique code, e.g. `fa1` |
| `name` | display name |
| `host` | panel hostname |
| `proxy_path_enc` | **encrypted** secret URL path |
| `owner_uuid` | panel super-admin UUID (also the backup basic-auth user) |
| `admin_api_key_enc` | **encrypted** admin API key (for write/enforcement) |
| `enabled` | included in sync/billing |
| `status` | `unknown` / `ok` / `error` / `disabled` |
| `source` | sync source (`backup_json`) |
| `last_synced_at` | timestamp of last successful sync — **the freshness signal billing uses** |
| `last_error` | last sync error text |

### `resellers` — a Hiddify admin (agent/admin) under a panel
Upserted on every panel sync from the backup's admin list. Bot/billing fields are set
later (bot registration, owner edits). Unique on `(panel_id, admin_uuid)`.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `panel_id` | FK → panels (cascade delete) |
| `admin_uuid` | identity on the panel |
| `name`, `comment` | from the panel |
| `parent_admin_uuid` | parent admin (hierarchy) |
| `mode` | `agent` / `admin` / `super_admin` |
| `is_owner` | true for the panel super-admin (never billed) |
| `panel_telegram_id` | Telegram id recorded on the panel |
| `bot_chat_id` | set when they register in the bot (drives delivery) |
| `link_tag` | the `#fragment` captured at registration |
| `registered_at` | when they registered in the bot |
| `price_per_gb` | Toman override; NULL → global default |
| `min_sale_toman` | per-reseller minimum-sale floor; NULL → global |
| `exclude_from_billing` | never invoiced when true |
| `panel_max_users` / `panel_max_active_users` | latest limits seen on the panel (refreshed each sync) |
| `can_add_admin` | Hiddify `can_add_admin` (may create sub-admins) |
| `gb_cap` | monthly SOLD-quota ceiling (GB) a parent sets on a sub; NULL/0 = none — alert only |
| `gb_cap_alerted_period` | `YYYY-MM` the over-cap alert last fired (warn once/month) |
| `enforcement_state` | `active` / `enforced` |
| `max_users_snapshot` / `max_active_users_snapshot` | limits captured before suspension, for exact restore |
| `last_seen_at` | last sync that still listed this admin (used to detect removal) |

### `end_user_snapshots` — latest snapshot of a Hiddify end-user (VPN service)
Upserted on every sync (freshest row per `(panel_id, user_uuid)`). The basis for
invoice math and enforcement. Unique on `(panel_id, user_uuid)`.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `panel_id` | FK → panels (cascade) |
| `user_uuid` | the service UUID |
| `name` | service name |
| `added_by_uuid` | which admin created it (billing attribution) |
| `usage_limit_gb` | **quota SOLD** (the billed quantity) |
| `current_usage_gb` | consumed GB at last sync |
| `start_date` | service creation date (billing-month test) |
| `package_days` | package length |
| `enable` | enabled on the panel (restore target) |
| `is_active`, `mode`, `last_online`, `comment` | panel state |
| `last_synced_at` | when this row was last refreshed |
| `meter_provisioned_gb` | lifetime GB ever sold/topped-up (metering) |
| `meter_consumed_gb` | true cumulative usage, reset-aware (metering) |
| `meter_init` | baseline-set guard so pre-existing users aren't re-billed when metering turns on |

### `usage_meters` — monthly metering bucket per end-user
One row per `(panel_id, user_uuid, period_label)`, written/updated during sync when
`metering_enabled`. Feeds abuse-resistant billing. Unique on those three.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `panel_id`, `user_uuid`, `period_label` | identity (`period_label` = `YYYY-MM`) |
| `added_by_uuid`, `name` | attribution / label |
| `quota_added_gb` | new + top-up quota this month |
| `consumed_gb` | consumption this month (reset-aware) |
| `overage_gb` | usage beyond the paid buffer (daily-reset trick) — billed |
| `edit_renewal_gb` | quota topped up without a new `start_date` — billed |
| `reset_count` | how many usage resets were detected |

---

## Billing & financial

### `invoices` — one monthly invoice per reseller
Created by `generate_invoices` (monthly job or manual «صدور فاکتورهای دوره»). Unique on
`(reseller_id, period_start, period_end)`. Paid invoices are never recomputed. All
money columns have `>= 0` CHECK constraints.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `reseller_id` | FK → resellers (cascade) |
| `panel_id` | FK → panels |
| `period_start` / `period_end` / `period_label` | the Gregorian billing month |
| `usage_gb` | total billed quota |
| `users_count` | number of billed services |
| `price_per_gb` | Toman applied |
| `amount_toman` | final amount (after floor) |
| `base_amount_toman` | amount before the minimum-sale floor |
| `min_sale_toman` / `floor_applied` | floor value + whether it was applied |
| `usdt_rate` | Toman-per-USDT used |
| `amount_usdt` | USDT equivalent |
| `status` | `draft`/`sent`/`paid`/`overdue`/`enforced`/`canceled` |
| `sent_at` / `paid_at` | delivery / payment timestamps |
| `deferred_until` / `defer_note` | payment grace deadline (pauses dunning while future) |
| `pdf_path` | rendered PDF path |

### `invoice_lines` — per-service line items of an invoice
Created with the invoice; cascade-deleted with it.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `invoice_id` | FK → invoices (cascade) |
| `end_user_uuid`, `name`, `start_date`, `usage_gb` | the service billed |
| `added_by_uuid` / `sub_reseller_name` | which sub-reseller created it (bundles) |

### `payments` — reseller payment attempts
Created when a reseller submits a TXID/screenshot in the bot, or the owner records one
manually. `txid` is unique (no reuse). Money columns `>= 0`.

| Column | Meaning |
|--------|---------|
| `id` | PK (the «شمارهٔ پیگیری» shown to the customer) |
| `reseller_id` | FK → resellers (cascade) |
| `invoice_id` | FK → invoices (the one invoice it pays; SET NULL if invoice removed) |
| `method` | `usdt_txid` / `manual` / `screenshot` / `ton_txid` |
| `status` | `pending` / `confirmed` / `rejected` |
| `chain` | `bsc` / `ton` |
| `txid` | unique on-chain hash (when present) |
| `from_address` / `to_address` / `confirmations` | on-chain detail |
| `amount_usdt` / `amount_toman` | amounts |
| `verified_at` | confirm timestamp |
| `note` / `raw_json` | free note / raw chain response |
| `proof_path` | screenshot path (method=screenshot) |
| `settled_invoice_ids` | comma list of invoices this payment settled (usually one) |

### `financial_records` — durable financial ledger
A denormalized, **FK-free** mirror of each invoice's money facts. Upserted by
`financial_archive` on generate/pay/edit/cancel/defer. **Survives** the "wipe data"
reset and panel/reseller deletion — the permanent «تاریخچهٔ مالی». Unique on
`invoice_id`.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `invoice_id` | soft reference (unique; no FK) |
| `panel_key` / `reseller_name` / `reseller_admin_uuid` | denormalized labels (kept after deletion) |
| `period_label` / `period_start` / `period_end` | billing month |
| `usage_gb` / `price_per_gb` / `amount_toman` / `amount_usdt` | money facts |
| `status` | invoice status snapshot |
| `paid_at` / `txid` | settlement facts (txid cleared if not paid) |

---

## Reseller follow-up («پیگیری»)

The owner's manual outreach memory for the churn board. Written **only** by
`/api/crm/*`; nothing here affects billing, dunning, or enforcement, and nothing here
sends a message. Segments themselves are computed live by `app/services/crm.py` and are
never stored — only the human decisions are.

### `reseller_crm_state` — current follow-up state (1:1, lazily created)
Created on a reseller's first follow-up. The board LEFT JOINs it, so "hide who I already
contacted" is one indexed join rather than a correlated MAX() over a growing log. Read
**fresh on every request** (never cached), so a logged follow-up drops the row off the
queue immediately.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `reseller_id` | FK → `resellers` (CASCADE), **unique** |
| `snoozed_until` | hidden from the default view while ≥ today; expires by date only |
| `muted` | permanent "never show me this one"; outranks `snoozed_until` |
| `last_touch_at` / `touch_count` | outreach stamps (clearing a snooze is not a touch) |
| `note` | the owner's pinned note — **not** `resellers.comment`, which sync overwrites |

### `reseller_followups` — append-only outreach log
Never updated, never pruned. Denormalized like `financial_records` so the history stays
readable after the reseller row is gone — hence `ON DELETE SET NULL`, not CASCADE.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `reseller_id` | FK → `resellers` (**SET NULL**) |
| `reseller_admin_uuid` / `reseller_name` / `panel_key` | denormalized labels (kept after deletion) |
| `segment` | the bucket at the moment of the touch (segments are recomputed live, so this is the only record of *why* they were contacted) |
| `note` | what happened |
| `snoozed_until` / `muted` | what was chosen at the time |
| `actor` | the web-panel username that logged it |
| `created_at` | timestamp (indexed for the paged log and the per-reseller timeline) |

---

## Logs / audit (pruned daily — see retention above)

### `sync_runs` — one row per panel sync attempt
Written at the start/end of every sync. Used by the Panels tab + freshness reporting.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `panel_id` | FK → panels (SET NULL) |
| `source` | `backup_json` |
| `status` | `running` / `success` / `failed` |
| `admin_count` / `user_count` | counts ingested |
| `error` | failure text |
| `started_at` / `finished_at` | timing |

### `delivery_log` — one row per Telegram delivery attempt
Written on every invoice send, reminder, warning, payment ack, abuse notice, broadcast.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `reseller_id` | FK → resellers (SET NULL) |
| `invoice_id` | FK → invoices (SET NULL); NULL for broadcasts/generic |
| `kind` | `invoice`/`reminder1`/`reminder2`/`warning`/`payment_ack`/`abuse_notice`/`generic` |
| `channel` | `telegram` |
| `status` | `sent`/`failed`/`blocked`/`unmatched` |
| `error` / `message_preview` | failure text / preview |
| `tg_message_id` / `tg_message_ids` | delivered message id(s), so a resend can delete the old pieces |
| `created_at` | timestamp (retention key) |

### `enforcement_actions` — one row per suspend/restore plan or attempt
Written by the enforcement queue (dunning, manual, payment restore). The largest rows
in the DB — the `snapshot` JSON holds the affected user-enable flags + prior admin
limits for exact restore.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `reseller_id` | FK → resellers (cascade) |
| `invoice_id` | FK → invoices (SET NULL) |
| `action` | `disable_users` / `restore` |
| `status` | `planned`/`running`/`partial`/`dry_run`/`done`/`failed`/`reverted` |
| `dry_run` | logged-only (no live writes) |
| `affected_count` | users in scope |
| `snapshot` | JSON: prior user-enable flags + admin limits + resumable progress |
| `error` | failure text |
| `created_at` | timestamp (retention key) |

---

## Auth, bot & config

### `app_users` — web-panel login accounts (owner / staff)
Owner row created by the first-run setup wizard.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `username` | unique login |
| `password_hash` | bcrypt |
| `role` | `owner` etc. |
| `is_active` | login allowed |
| `token_epoch` | bumped on password change → invalidates old JWTs |
| `totp_secret_enc` | **encrypted** active TOTP secret |
| `totp_pending_secret_enc` | **encrypted** pending TOTP secret (until confirmed) |
| `totp_enabled` | 2FA on |

### `webauthn_credentials` — passkeys (Face ID / Touch ID / security key)
Registered by an owner; cascade-deleted with the account.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `user_id` | FK → app_users (cascade) |
| `credential_id` | unique base64url credential id |
| `public_key` | COSE public key (base64) |
| `sign_count` | replay counter |
| `name` | label |

### `bot_users` — everyone who has interacted with the bot
Written/updated when a Telegram user messages the bot. Used by the channel/group guard.

| Column | Meaning |
|--------|---------|
| `id` | PK |
| `telegram_id` | unique Telegram id |
| `username` / `first_name` | profile |
| `last_seen_at` | last interaction |
| `last_kicked_at` | last guard removal |

### `settings` — runtime, panel-editable key/value config
Seeded at boot (`seed_defaults`) and edited from Settings. Secret values are encrypted
and masked on read.

| Column | Meaning |
|--------|---------|
| `key` | PK (e.g. `default_price_per_gb`, `log_retention_days`) |
| `value` | JSON value |
| `is_secret` | encrypted-at-rest + masked in the API |

---

*All tables also carry `created_at` / `updated_at` (via `TimestampMixin`) except the
three log tables, which keep only their own event timestamp (`created_at` /
`started_at`).*
