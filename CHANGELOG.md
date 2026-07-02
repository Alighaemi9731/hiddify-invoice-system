# Changelog

All user-visible changes, important fixes, migrations, and operational notes are
recorded here from `v1.37.35` onward. Older detailed history remains available in
`CLAUDE.md` and Git commit/tag history.

## Unreleased

No changes yet.

## 1.53.3 - 2026-07-03

Batch U01 of the UI/UX program (`docs/UIUX_PLAN.md`).

### Fixed

- **Dark-mode "haze" that made card text hard to read.** In dark mode the cards were a 7%-white
  translucent surface under a heavy `blur(40px)`, and the mobile row-cards stacked a *second*
  translucency on top — ~3.4% white over black plus blur, i.e. a washed-out gray. Dark cards are
  now a near-opaque Apple-style surface (`#1c1c1e` @ 90%) with a lighter `blur(20px)` edge that
  keeps the glass identity, the secondary text color is lifted (`#a1a1a6`, ≈6.6:1 contrast), and
  the mobile Invoices/Payments/Resellers cards use a solid nested surface instead of the stacked
  alpha. The StatCard icon's double-blur "halo" is removed. Table hover/stripe/divider contrast
  nudged up to match. **Light mode is unchanged** (the only light-mode edit is the no-op StatCard
  blur removal); the frosted glass is deliberately kept on the sidebar, AppBar, and dialogs.

## 1.53.2 - 2026-07-02

### Fixed

- **Dunning reminders no longer fire a day early for late-night invoices.** Owner-reported: with
  `reminder1_day=2` and invoices sent at Tehran 03:00, the first reminder arrived on day 2 instead
  of day 3. The day counter extracted the **UTC** calendar date from `sent_at`/now — for any
  instant in the Tehran 00:00–03:29 window that's the *previous* day, pulling the anchor back and
  firing every reminder/warning/enforcement threshold one day ahead. New `periods.to_local_date()`
  converts stored instants to the Tehran calendar day; `run_dunning` now uses it for both the
  anchor and "today" (a whole-codebase sweep confirmed dunning was the only defect site — all
  other deadline checks already used the Tehran-local helper).

Batch I12 of the improvement program (`docs/IMPROVEMENT_PLAN.md`) — the final batch; the
2026-07-02 improvement program (I01–I12) is complete.

### Fixed

- **On-demand sub-reseller/interim PDFs for a billed month now come from the locked invoice.**
  v1.49.3 fixed this for DELIVERED invoices, but the on-demand paths (the bot's per-sub «📄 فاکتور
  <ماه>» PDFs and the own/sub usage PDFs) still recomputed from live snapshots — and the daily
  snapshot pruning silently shrank historic months (a sub's May PDF could lose users deleted in
  June). For any period locked in a persisted non-draft invoice (of the node or its root), the
  lines are now sourced from the stored `InvoiceLine` rows — including the metered-extra lines —
  filtered to the sub's subtree; the open/unbilled month keeps the live recompute by design.

Batch I11 of the improvement program (`docs/IMPROVEMENT_PLAN.md`).

### Added

- **Real storefront stats for shop admins.** The «📊 آمار» view in each reseller's shop bot showed
  only three counters (customers / plans / pending top-ups); it's now a business dashboard:
  customers (with active-in-30-days), enabled/total plans, live services (with a near-expiry
  count reusing the I10 math), **this-month sales in Toman** (purchases minus refunds from the
  wallet ledger, with purchase count), this-month confirmed top-ups, pending top-ups, and the
  total customer wallet balance (the reseller's liability).

Batch I10 of the improvement program (`docs/IMPROVEMENT_PLAN.md`).

### Added

- **Storefront customers now get near-expiry reminders.** A customer's config used to expire
  silently — the single most renewal-losing gap in the shop bots. A new daily job (11:15 Tehran)
  scans every provisioned storefront order and messages the customer through their shop's own bot
  when the config is `storefront_expiry_notify_days` (default **3**, `0` = off, editable in
  Settings → زمان‌بندی) or fewer days from expiring — «⏳ سرویس شما تا N روز دیگر منقضی می‌شود» with
  the existing **«🔄 تمدید سرویس»** button attached. Days-left comes from the panel snapshot
  (`start_date + package_days`), falling back to the order duration. Each order is reminded once
  per service period (new `expiry_alerted_at` column, migration `a8c5d7e2f4b6`); renewing re-arms
  the reminder; a customer who blocked the bot is stamped, not retried daily; already-expired
  configs are not spammed.

Batch I09 of the improvement program (`docs/IMPROVEMENT_PLAN.md`).

### Added

- **CSV export for invoices and payments.** A new «خروجی CSV» button next to the filters on both
  pages exports the FULL filtered dataset (current period/status + the search box, up to the
  2000-row API cap) as an Excel-friendly UTF-8-BOM CSV with Persian headers and the same status
  labels/Jalali dates the tables show. The financial-history page's existing export now shares the
  same `src/csv.ts` util (byte-identical output).
- **Mobile card views for Invoices and Payments.** Below 900 px both pages now render stacked
  cards (like the Resellers page) instead of a squeezed table — same status chips, Telegram links,
  amounts, and the complete per-row action set (detail/edit/PDF/send/pay actions on invoices;
  chain-check/confirm/reject/delete on payments). Desktop is pixel-unchanged.

Batch I08 of the improvement program (`docs/IMPROVEMENT_PLAN.md`) — pure frontend refactor,
zero behavior or visual change.

### Changed

- **Shared dialog/mutation plumbing.** Every money page re-implemented the same
  mutation-with-toast and dialog-open/close boilerplate; new `useToastMutation` +
  `useDialogState` hooks centralize it (adopted by Invoices, Payments, Panels, and the
  Resellers page), so a future fix to error toasts or cache invalidation lands once. Query
  keys and invalidation moments are unchanged everywhere; Persian strings byte-identical.
- **The 1,109-line Resellers page is now 9 focused files** under `pages/resellers/` (page
  shell, desktop table, mobile card, identity/status chips, action buttons, edit +
  capacity-bump dialogs, absent-resellers view, and a `useResellerTree` hook for the tree
  logic). Same route, same lazy chunk, same UI.

Batch I07 of the improvement program (`docs/IMPROVEMENT_PLAN.md`) — pure refactor, zero
behavior change.

### Changed

- **The bot's 3,434-line `handlers.py` is now a 15-module package** (`app/bot/handlers/`):
  common (router/middlewares/states/helpers), commands, broadcast, menus, support,
  reseller callbacks, subs management, user-create, storefront setup, owner views, misc,
  the free-text fallback, plus two helper-only modules. All modules register on the SAME
  router in the exact original order (aiogram dispatches first-match, so order is
  semantics); a new order-sensitive router-inventory test (34 message + 53 callback
  handlers + 2 outer middlewares, snapshot fixture generated from the pre-split file)
  proves registration is byte-identical. Every previously importable name is re-exported
  from the package, so all external imports keep working unchanged.

Batch I06 of the improvement program (`docs/IMPROVEMENT_PLAN.md`) — the isolated money batch.

### Changed

- **Payment invoice-sets got a real, indexed table.** The set of invoices a payment covers was
  stored only as a comma-joined string (`payments.settled_invoice_ids`), which is not queryable —
  the duplicate-pending block and the "don't un-pay an invoice another payment still settles"
  protection loaded **every** payment into Python and parsed strings on each submission. New
  `payment_settlements(payment_id, invoice_id)` join table (migration `f7a3b5d9c2e4`,
  automatically backfilled from the comma column with dangling ids skipped and logged); the three
  hot lookups are now single indexed queries. **Dual-write:** every writer keeps the comma column
  byte-equal, so rolling back to v1.50.5 is completely safe (old code reads the comma column and
  simply ignores the new table). A legacy payment confirmed after the migration (e.g. restored
  from an old backup) self-heals its rows at confirm time. Money behavior is unchanged —
  regression-tested: duplicate-pending block, multi-invoice revert overlap protection, legacy
  confirm, delete cleanup, and the migration backfill itself; validated up/down on PostgreSQL 16.

### Fixed

- **A malformed storefront bot token no longer crashes the setup flow.** Caught live by the new
  in-app error tracker (v1.50.0) within hours of deployment: a reseller pasted a token that passed
  the cheap pre-check but failed aiogram's constructor validation (`TokenValidationError`), which
  escaped the handler — the user got silence instead of the «توکن نامعتبر است» reply. Both the
  setup handler and the storefront manager (for a malformed *stored* token, which is now marked
  errored instead of re-raising every reconcile) construct the probe `Bot` inside a guard.

Batch I05 of the improvement program (`docs/IMPROVEMENT_PLAN.md`).

### Changed

- **Invoice status queries got real indexes.** Dunning, the reseller portal, and enforcement
  constantly filter invoices on `status` (usually together with `reseller_id`), but neither
  combination was indexed — those queries scanned the whole invoices table as it grows. Added
  `ix_invoices_status` and the compound `ix_invoices_reseller_status` via Alembic migration
  `e6d4a2c8b9f1` (additive DDL only; applied automatically on boot; verified up/down on
  PostgreSQL 16 and SQLite).

Batch I04 of the improvement program (`docs/IMPROVEMENT_PLAN.md`).

### Fixed

- **Every outgoing bot message is now bidi-safe.** The central send helpers already passed text
  through `rtl()`, but ~200 direct handler replies didn't — any message mixing Persian with
  Latin/digits (amounts, TXIDs, panel keys) could render jumbled in Telegram. A new client-session
  request middleware applies `rtl()` to the text/caption of every send/edit API call on all three
  bots (main, notifier, storefront), ending this class of bug instead of patching call sites one by
  one. `rtl()` is idempotent, so already-formatted messages pass through byte-identical; callback
  toast alerts are deliberately excluded for now.
- **Blocked-user replies show a clean message.** Replying to a support message from a reseller who
  blocked the bot (or deleted their account) told the owner the raw English API error; it now says
  «این کاربر ربات را مسدود کرده…» (and «گفتگویی با این کاربر پیدا نشد» for a never-started chat).
- **A revoked storefront-bot token stops burning retries.** If a reseller revoked their shop bot's
  token while it was polling, the poll loop retried forever (log spam every few seconds) and the
  ~30s reconcile kept re-validating the dead token. After 3 consecutive 401s the bot is now marked
  `errored` and excluded from polling; re-running the storefront setup wizard re-arms it.

Batch I03 of the improvement program (`docs/IMPROVEMENT_PLAN.md`).

### Fixed

- **PDF rendering no longer stalls the whole backend.** Every invoice-PDF render (reportlab,
  fully synchronous) used to run on the async event loop — during monthly generation the backend
  renders N+1 PDFs per reseller and every concurrent API/bot request waited behind them. All
  renders now run in worker threads (`asyncio.to_thread` inside `invoice_pdf._build_pdf`), with
  the shared reportlab font registration serialized behind a lock. A regression test renders
  three invoices concurrently.
- **Stale data purged on forced logout.** When the panel session expires (401 → auto-logout),
  the react-query cache is now cleared, so logging back in always refetches instead of briefly
  showing pre-logout invoice/payment data. The shared `QueryClient` moved to
  `src/api/queryClient.ts`.

Batch I02 of the improvement program (`docs/IMPROVEMENT_PLAN.md`).

### Changed

- **Bounded container logs + memory caps.** Docker's default json-file logging has no size cap, so
  container logs could slowly fill the VPS disk; every service now rotates at 10 MiB × 3 files. Each
  service also gets a memory ceiling sized against real production usage on the ~2 GiB host
  (db 512 MiB, backend 640 MiB, bot 512 MiB, frontend 128 MiB, caddy 192 MiB — normal total is
  ~0.5 GiB) so one runaway container gets OOM-killed and self-heals via `restart: unless-stopped`
  instead of taking down dockerd or Postgres with it.
- **Bot + frontend healthchecks.** The bot container (no HTTP server) now touches a container-local
  heartbeat file every 30 s from its event loop and the Compose healthcheck probes the file's age —
  a crashed/hung bot finally shows as `unhealthy` instead of blissfully "Up". The nginx frontend
  gets a `wget` self-probe. `deploy/smoke.sh` already fails on any unhealthy container, so these
  strengthen the post-deploy check automatically.
- **CI now tests against real PostgreSQL 16.** A new `backend-postgres` job applies every Alembic
  migration to a `postgres:16` service, runs `alembic check`, and executes new env-gated smoke
  tests (`tests/test_postgres_smoke.py`: settings roundtrip incl. an encrypted secret, core money
  rows + the production status-filtered query, and a CHECK-constraint rejection). SQLite/Postgres
  dialect differences have caused two production incidents before; they now fail in CI instead.

No schema or application-behavior change; the deploy requires one stack recreation to apply the
compose-level limits.

Batch I01 of the improvement program (`docs/IMPROVEMENT_PLAN.md`) — the successor to
the completed B00–B10 remediation plan.

### Added

- **Scheduler liveness heartbeat.** A silently-dead scheduler previously looked healthy from the
  outside while invoicing, dunning, and backups quietly never ran. A new fixed-cadence
  `scheduler_heartbeat` job stamps `scheduler_last_heartbeat` every ~2 minutes (plus once at boot),
  and `/health` now reports `"scheduler":"ok"|"stale"` with `"status":"degraded"` when the stamp is
  older than 10 minutes. Degraded stays HTTP **200** with `"database":"ok"` intact, so the Compose
  healthcheck and `deploy/smoke.sh` contracts are unchanged; only a real database outage is a 503.
- **Lightweight in-app error tracking (no external service).** Every `ERROR`/exception in the
  backend AND the bot is fingerprinted (logger + exception type + in-app frame + message template)
  and appended to small per-process rotating JSONL files (`data/logs/errors-backend.jsonl` /
  `errors-bot.jsonl`, ~2 MB × 3). `/health` gains an `errors_24h` counter (cached ~60 s), and the
  owner's daily digest appends a «خطاهای ثبت‌شده» section listing new error groups since the last
  delivered digest (cursor `error_digest_last_ts` advances only after a successful send, so an
  undelivered day is never lost). Every tracking path swallows its own failures. New internal
  read-only settings: `scheduler_last_heartbeat`, `error_digest_last_ts`. No migration.

### Fixed

- Two pre-existing lint findings that failed the release gate (an ambiguous loop-variable name in
  `invoice_pdf.py` and an unsorted import block in `tests/test_avax.py`). No behavior change.

## 1.49.3 - 2026-07-02

### Fixed

- **Invoice PDF/breakdown now matches the invoice text (complete GB) in every send path.** The
  customer-facing per-node PDFs (own users + each sub-reseller) and the text breakdown were
  re-computed from **current panel snapshots**, so any end-user deleted or changed on the panel
  *after* an invoice was issued silently shrank the PDF — while the invoice text used the locked
  `usage_gb`. Example: an invoice reading 85 GB in text arrived as a 50 GB PDF once some of that
  month's users were removed. Now, for a **persisted invoice** (real invoice, direct send, edit +
  resend, or tapping «فاکتورهای پرداخت‌نشده»), the PDFs and breakdown are rendered straight from the
  invoice's stored line items — so they always sum to the locked total (incl. the «مصرف اضافه/تمدید»
  extra lines) regardless of later panel changes. The «فاکتور علی‌الحساب» (interim, current-month
  preview) still reflects live usage by design, since it has no locked invoice yet.

## 1.49.2 - 2026-07-01

### Fixed

- **Manually recording a payment now notifies the reseller.** Marking an invoice paid by hand via the
  panel «ثبت پرداخت» button (`POST /api/invoices/{id}/mark-paid`) previously updated the ledger and
  lifted enforcement silently — the reseller got no confirmation. It now sends them the same
  «پرداخت … تأیید شد» acknowledgement as confirming a submitted payment (a safe no-op if they aren't on
  the bot). Sent after commit, so a delivery failure never rolls back the paid status.

## 1.49.1 - 2026-07-01

### Fixed

- **Chain-check toast RTL.** The «بررسی واریزی روی زنجیره» success toast mixed Persian, Latin
  (AVAX/GRAM/USDT) and digits in one flat line, so the bidi layout jumbled it. Each mixed value run is
  now wrapped in a First-Strong Isolate (U+2068…U+2069) with RTL-friendly separators, so it reads
  cleanly (e.g. «واریزی: ⁨1.4262 AVAX⁩ ≈ ⁨1,651,685 تومان⁩ — فاکتور ⁨1,659,208 تومان⁩ ✓ مطابق فاکتور»).
  The confirm-dialog display was already correct (each value is a `dir="ltr"` span).

## 1.49.0 - 2026-07-01

### Added

- **AVAX on-chain deposit check.** The panel «بررسی واریزی روی زنجیره» button now works for AVAX
  (previously disabled): it reads the actual native AVAX transfer for the tx hash from a free public
  Avalanche C-Chain RPC (`avalanche_rpc_url`), converts at the derived AVAX→Toman rate, and shows it
  against the invoice within `avax_amount_tolerance_pct` (default 5%). Display-only — AVAX is still
  **manually** confirmed, never auto-confirmed, and an AVAX hash is never looked up on the BSC RPC.
- **Storefront plans: edit + reorder.** Each plan in the shop-bot admin now has ✏️ ویرایش (change
  GB/days/price in place) and ⬆️/⬇️ move buttons, so a new plan needn't land at the bottom and be
  re-created to fix the order (`storefront.update_plan`/`move_plan`).
- **More `/` commands.** Owner adds `/search` + `/cancel`; reseller adds `/storefront`, `/register`,
  `/cancel` — the typed-command list now mirrors every menu action.

### Changed

- **Owner payment-review message reflects the decision.** After tapping تأیید/رد on the Telegram
  payment notification, the original message is edited in place («✅ این پرداخت تأیید شد» / «❌ … رد شد»)
  and its تأیید/رد buttons are removed — so it's obvious which payment was already handled and it can't
  be double-tapped.
- **Menu always at hand.** The bot re-sends the main menu as the last message after each completed
  action (viewing invoices/panels/subs, a payment, a search, etc.), so it never scrolls out of reach.
  It is NOT re-sent mid-flow (while an FSM prompt is awaiting input).

No schema/migration change (AVAX RPC settings are runtime, auto-seeded on boot). Tests in
`tests/test_avax.py`, `tests/test_storefront.py`, `tests/test_bot_ux.py`.

## 1.48.0 - 2026-06-28

### Added — AVAX (Avalanche) payment method

- AVAX is now a first-class owner↔reseller payment method alongside USDT, گرام/TON and card-to-card —
  turn it on in «تنظیمات → روش‌های پرداخت» and set the AVAX (C-Chain) wallet address. The customer sees an
  «❄️ اوالانچ (AVAX)» block with a tap-to-copy address, the live AVAX amount, and a «فقط شبکهٔ Avalanche
  C-Chain» warning — in the bot pay flow AND the reseller web portal pay dialog.
- **Live AVAX→Toman rate (derived).** No Iranian market quotes AVAX, so the rate is AVAX→USD from CoinGecko
  (free, no key) × the existing live USDT→Toman rate, with a manual fallback (`avax_rate_mode` manual|auto,
  `avax_toman_manual`). Refreshed hourly, on the «به‌روزرسانی نرخ» button, and best-effort before billing.
- **Manual confirmation** (like TON): the reseller submits a TXID (or a Snowtrace link); the owner verifies
  via the clickable `snowtrace.io` link in the Payments panel and confirms. No on-chain auto-verify.
- An Avalanche tx hash shares BSC's `0x`+64hex format, so when **both** USDT(BSC) and AVAX are enabled the
  bot asks which network for a bare hash (a pasted `snowtrace`/`bscscan` link is resolved automatically).
- No schema/migration change (the method is a VARCHAR enum value; config is runtime settings auto-seeded on
  boot). Tests in `tests/test_avax.py` + an AVAX case in `tests/test_portal.py`.

## 1.47.0 - 2026-06-28

### Changed — Storefront «مشتری‌ها» tab is now paginated + searchable

- The admin customers tab used to send **one Telegram message per customer** (capped at 30, silently
  dropping the rest) — a flood for resellers with many customers. It's now a single tidy message: a page
  of customers as inline buttons («{name} — {balance} ت») with «‹ قبلی»/«بعدی ›» navigation (8/page) and
  a «🔍 جستجو» button. Tapping a customer opens a detail view (balance + the manual wallet ±/services
  actions + a back button). Search matches by name substring or numeric Telegram id. New paginated/counted
  queries (`storefront.list_customers_page`, `count_customers`); the stats tab no longer loads every
  customer just to count them. No schema change.

## 1.46.0 - 2026-06-27

### Added — Storefront forced channel-join

- Each storefront admin (reseller) can now require their **customers to join the reseller's Telegram
  channel** before using the shop bot. New admin screen «🔒 عضویت اجباری»: the admin sets the channel by
  forwarding a post from it (or sending `@username` / a `-100…` id), and the bot **verifies it is an admin
  of that channel first** — if not, it replies «ابتدا ربات را در کانالِ خود ادمین کنید» and doesn't save.
  Once set, the admin can toggle forced-join on/off (enabling re-checks that the bot is still a channel
  admin) or clear the channel.
- Customers who aren't members are blocked on every action with a join prompt (channel link + «بررسی
  عضویت» button); after joining, the check passes and the shop opens. Admins are never gated. Reuses the
  main bot's membership primitives (`_is_member`, per-user one-time invite links). Uses the existing
  `StorefrontBot.channel_id/channel_link/channel_required` columns — no migration. Tests in
  `tests/test_storefront.py`.

## 1.45.1 - 2026-06-27

### Fixed — storefront double messages + one bot per panel

- **Most messages were sent twice.** With many storefront bots, the manager polled them through a single
  shared `start_polling(*bots)` and **restarted the whole fleet whenever any bot was added/removed** — the
  old and new pollers overlapped (Telegram «terminated by other getUpdates» 409s), so every update was
  delivered and handled **twice**. The manager now runs **one independent, cancellable poll loop per bot**
  (`bot.get_updates` → `dp.feed_update`) and reconciles **incrementally**: adding or removing a bot touches
  only that bot, never restarting the others. No more overlap, no more doubled messages.
- **One storefront bot PER PANEL (not per person).** v1.45.0 over-restricted setup to one bot per
  Telegram person, so an admin registered on two panels couldn't run a bot for each. Setup is back to one
  bot per registered (top-level) panel: «🏪 راه‌اندازی ربات فروشگاهی» shows a per-panel picker annotated
  with each panel's current bot («ربات فعلی: @x» / «بدون ربات»); picking a panel without a bot creates a
  new one (needs its own BotFather token), and re-picking a panel that has one replaces its token in place
  (all data preserved). Sub-resellers are still blocked (top-level gate, with defensive re-checks).
- **«سرویس‌های من» detail is one message.** Tapping a service now sends a single QR photo carrying the
  status + subscription link + the renew/pause/delete buttons, instead of a status message followed by a
  redundant «✅ سرویس آماده شد» config message.

## 1.45.0 - 2026-06-27

### Changed — Storefront enablement: on-by-default, free, one bot per person

- **The storefront permission is now ON by default for every reseller** (and all existing resellers are
  backfilled to enabled by migration `d5b8e3f2a017`) — the owner no longer toggles «اجازهٔ راه‌اندازی»
  one-by-one. The per-reseller toggle + fee field remain, so the owner can still disable or charge a
  specific reseller. It stays **free** by default (the monthly fee already defaults to 0).
- **Only first-tier admins can run a storefront.** Only TOP-LEVEL resellers (the ones registered in the
  bot — direct children of the panel owner) can set one up; their sub-resellers cannot. The setup gate
  enforced this already; with default-on it's now the sole gate, so a defensive top-level re-check was
  added at the panel-pick and token steps.
- **One storefront bot per person.** A reseller-admin who is top-level on several panels can no longer
  spin up multiple bots — they get exactly one. Re-running setup recognises their existing bot and offers
  to **replace its token** instead of creating a second (`storefront.get_bot_for_chat`).
- **New token migrates everything.** Sending a new BotFather token repoints the same storefront record, so
  all plans, customers, services and wallet balances carry over to the new token and the **old bot stops
  working** — the bot confirms this on success.

## 1.44.0 - 2026-06-27

### Added / Changed — Storefront reliability, subscription management & retention

A professional pass over the per-reseller storefront bots:

- **Free trial on by default for everyone.** New storefronts default to the trial enabled, and existing
  ones are backfilled to enabled (admins keep the on/off + volume/days controls). Claiming is now
  concurrency-safe (compare-and-set under a row lock + a per-customer in-process lock) so a double-tap
  can't mint two trials.
- **Configs reliably reference the real panel user.** Each order stores `(panel_id, panel_user_uuid)`
  (denormalized — joinable to `end_user_snapshots`, no fragile FK), and the uuid is pre-generated and
  recorded **before** provisioning.
- **Atomic, crash-safe purchase.** Buying now commits the order + wallet debit together in one short
  transaction before any network call, provisions outside the DB session, then finalizes — so money is
  never debited without either a config or a refund. A new **pending-order reaper** (scheduler job,
  `storefront_pending_order_reaper_minutes`, default 15m) reconciles any purchase orphaned by a mid-buy
  crash: it completes the ones whose config exists on the panel and refunds the rest (idempotent).
- **Full subscription control.** Customers can **renew** (same config/link, fresh GB+days, charged at the
  **current** plan price), **pause/resume**, and **delete** their services; the reseller-admin can manage
  any customer's subscriptions (free renew, pause/resume, delete) from «👥 مشتری‌ها → 📦 سرویس‌ها».
  Renew resets the config in place via `PATCH /user/{uuid}` (Hiddify); delete removes it from the panel.
- **Banned enforced everywhere.** A router middleware blocks a banned customer on every callback and
  message (not just `/start`), failing open on transient errors so a DB blip can't lock everyone out.
- **Automatic data retention.** The daily maintenance job purges storefront tire-kickers — customers with
  no financial footprint (zero balance, no live service, no confirmed top-up) inactive for
  `storefront_stale_customer_days` (default 90; 0 = off) — plus failed/deleted orders, rejected top-ups,
  and their proof files. Confirmed top-ups and provisioned services are never pruned. The owner↔reseller
  billing ledger is a separate subsystem and is untouched.
- **Scale & robustness.** Storefront-bot token validation now runs in bounded parallel (fast cold start
  with many bots), a duplicate/invalid token can no longer abort the whole fleet's polling, the bot
  process supervises its two loops independently, the DB pool is enlarged for Postgres, the welcome-text
  is now editable, pending top-ups are capped per customer (anti-spam), a proof file-handle leak is
  fixed, and a stray customer message re-shows the menu instead of being ignored.

Migration `c7a2f4e9d1b6` (order `panel_id`/`last_renewed_at` + index, wallet-txn `order_id`, customer
`last_seen_at`, free-trial backfill, partial-unique `bot_telegram_id`). Tests in `tests/test_storefront.py`.

## 1.43.2 - 2026-06-27

### Fixed — group membership gate rejected `restricted` members

- The forced-membership gate (`_is_member`) treated a supergroup member whose status is `restricted`
  (any restriction applied) as a non-member and showed «هنوز عضو گروه نیستید», even though Telegram
  reports such a user as still in the group (`is_member=True`). It now counts `restricted` +
  `is_member=True` as a member — matching `channel_guard`, which already did. (Channels never report
  `restricted`, so only the group gate was affected.) Also dropped the dead `"owner"` status (Telegram
  sends `"creator"`). Regression test in `tests/test_bot_identity_safety.py`.

## 1.43.1 - 2026-06-27

### Fixed — only the first storefront bot was actually running

- When a second/third reseller set up a storefront bot, setup reported success but the bot stayed dead
  (`/start` did nothing). Root cause: the manager called aiogram's `Dispatcher.start_polling` **once per
  bot** on a single shared Dispatcher, but `start_polling` holds `_running_lock` for its entire lifetime —
  so the first bot acquired the lock and every later bot's `start_polling` blocked forever. The manager
  now polls **all** active bots through a **single** `start_polling(*bots)` call and restarts that one
  poller only when the active set changes (bot added/removed or token rotated), skipping a bot whose token
  is invalid without churning the others, and self-heals if the poller dies. Regression test in
  `tests/test_storefront.py`.

## 1.43.0 - 2026-06-27

### Added — Storefront one-time free trial

- Each customer can claim **one** free trial config (admin opt-in, default **1 GB · 1 day** — at/under
  the owner's free-config threshold, so it's free for the reseller too). The customer menu shows a
  «🎁 تست رایگان» button only while it's enabled and unused; claiming provisions a config named
  «تست رایگان» + QR and sets `storefront_customers.free_trial_used` (only on success, so a failed
  provision can be retried). The reseller-admin enables/disables it and edits the volume/duration from a
  new «🎁 تنظیماتِ تست رایگان» admin screen. New columns `storefront_bots.free_trial_enabled/gb/days` +
  `storefront_customers.free_trial_used` (migration `b4e1d2f7a9c3`).

### Fixed — RTL/bidi

- **@usernames are clickable again.** `rtl()` was isolating mentions as `@⁨name⁩` (the `@` left
  *outside* the bidi isolate), which both mis-positioned it and stopped Telegram from detecting the
  mention. It now isolates `⁨@name⁩` (the `@` inside), so the admin-menu bot username, the BotFather
  guide, and the support-id prompt render correctly and are tappable. Emails stay one unit.
- **«سرویس‌های من» buttons no longer scramble.** Storefront inline-button labels (services, plans) are
  now `rtl()`-wrapped, so a service whose name mixes Persian and English (e.g. «phone») keeps its
  volume/duration in the right order.

## 1.42.0 - 2026-06-27

### Changed — Storefront bot UX redesign (customer + setup)

A full pass over the per-reseller storefront flows after the owner's review:

- **Plans show GB · days · price, no titles.** `buy_plans_kb`/`plans_manage_kb` now render
  «N گیگ · M روزه — P تومان» via a shared `plan_label()`; the add-plan wizard no longer asks for a
  title (goes straight to volume → days → price). The `StorefrontPlan.title` column is kept (unused) for
  backward-compat.
- **Each purchase gets a customer-chosen name.** Buying is now plan → **name** → confirm → charge →
  provision: the customer sends a name (1–40 chars) for the service, a confirmation card shows the plan +
  name + post-purchase balance, and the config is created on the panel with **exactly that name** (it's
  also the sub-link label), so a customer who buys several can tell them apart. The name is stored on the
  order (`storefront_orders.label`, migration `a3f5c9e1b7d2`) and passed through
  `storefront_provision.provision(label=…)`.
- **«سرویس‌های من» shows live usage + expiry.** Each provisioned service is an inline button; tapping it
  reads the config **live** from the panel (used GB / limit GB, remaining days — new
  `AdminApiClient.get_user` + `storefront_provision.live_status`, best-effort with a graceful fallback)
  and re-sends the subscription link + QR.
- **Wallet shows balance first.** Tapping «کیف پول» now shows the balance (and any pending top-ups) with
  an «➕ افزایش موجودی» button; the amount is only asked after that button (it no longer jumps straight
  into the amount prompt). The amount → method → proof flow is unchanged.
- **Setup names the target panel/account.** The storefront setup always states «… برای حسابِ X روی
  پنلِ Y راه‌اندازی می‌شود» before asking for the BotFather token (and still shows the picker when more
  than one of the admin's accounts has the storefront enabled). To offer a choice, the owner enables
  «فروشگاه» on each account in the web Resellers page.

## 1.41.2 - 2026-06-27

### Fixed — main bot menu reverted to inline; storefront setup no longer fails on token

- **Main bot menu is inline again.** v1.40.0 had moved the owner/reseller main menu to a persistent
  docked **reply keyboard**; the buttons didn't fit on smaller screens. The menu now renders as the
  previous **inline** keyboard (`owner_menu_keyboard` / `reseller_menu_keyboard`), and `_send_menu`
  first sends a `ReplyKeyboardRemove` so any lingering docked keyboard is cleared. The storefront-setup
  entry is now an inline button (`🏪 راه‌اندازی ربات فروشگاهی`, `menu:storefront`) shown only to
  eligible resellers. The reply-keyboard label handlers remain as a harmless compatibility shim.
- **Storefront setup no longer does nothing when a BotFather token is sent.** The storefront Telegram-id
  columns (`storefront_bots.bot_telegram_id`, `storefront_customers.telegram_id`) were declared as
  `Integer` (int32), but Telegram bot/user ids exceed int32 — Postgres raised
  `DataError: value out of int32 range` in `get_bot_by_telegram_id`, which aborted the token handler with
  no reply. Both columns are now `BigInteger` (mirroring `Reseller.bot_chat_id`); migration
  `f2b9c7a1d3e8` widens them (the storefront tables are empty in production, so the type change is
  trivially safe). SQLite has no int32 cap, which is why tests passed locally — a metadata regression
  test now asserts both columns are `BigInteger`.

## 1.41.1 - 2026-06-27

### Fixed — storefront migration boolean default on Postgres

- The storefront migration (`557ce30f0d9c`) added `resellers.storefront_enabled` with
  `server_default=text("0")`, which SQLite accepts but **Postgres rejects** for a BOOLEAN column
  (`DatatypeMismatchError`), so the v1.41.0 deploy's startup migration failed. Switched to
  `server_default=sa.false()`, which renders `false` on Postgres and `0` on SQLite. Verified the
  compiled DDL on both dialects. (v1.41.0 rolled back cleanly — transactional DDL — so no data was
  affected; production was restored to v1.40.1 in the interim.)

## 1.41.0 - 2026-06-27

### Added — Per-reseller VPN storefront bots (Phase 1)

- When the owner enables it, a top-level reseller can set up their OWN Telegram storefront bot from the
  main bot («🏪 راه‌اندازی ربات فروشگاهی»): pick a panel → BotFather guidance → send the token → it's
  validated, saved (token encrypted), and brought online immediately. All storefront bots run inside the
  existing bot container via a second dispatcher + a manager that reconciles against the DB (~30s),
  resolving the tenant by `bot.id`.
- The storefront bot has an **admin side** (the reseller) and a **customer side**. Admin: define plans
  (GB/days/price), set payment methods (card / USDT-BEP20 / GRAM-TON), review & confirm/reject top-ups,
  manage customers + manually adjust wallets, broadcast, stats, support contact, customer preview.
  Customer: buy from a wallet balance → the bot **auto-creates the config on the reseller's panel** and
  sends the sub link + QR; wallet top-up → admin confirms and **sets the credited Toman manually** (fully
  manual, no rates/API — even for crypto) → balance credited; «سرویس‌های من».
- Owner controls per reseller (panel → Resellers → edit): enable the feature + an optional monthly fee
  (global default in Settings). The fee is billed on the reseller's monthly invoice **only for months
  they run an active storefront bot**. Sold configs are created under the reseller's admin, so they count
  toward the reseller's own usage that the owner bills (the reseller keeps the retail markup).
- RTL-correct Persian throughout. New tables (`storefront_bots/plans/customers/wallet_txns/orders`),
  migration `557ce30f0d9c`; wallet-ledger + monthly-fee regressions in `tests/test_storefront.py`.

Later phases: customer forced-join gate, richer dashboards, expiry notifications, and more.

## 1.40.1 - 2026-06-26

### Fixed — security & correctness (external review)

- **Critical:** BSC tx hashes are canonicalized to lowercase before dedup/storage, so the same transfer
  submitted as `0xABC…` and `0xabc…` can no longer bypass the unique index and settle multiple invoices.
- **High:** portal login links are now strictly one-time — each carries a `jti` consumed atomically on
  exchange (new `portal_login_nonce` table), so a leaked link can't be replayed for fresh sessions within
  its 15-minute window. The login page also strips the token from the URL immediately.
- **High:** when automatic backup is enabled without a passphrase, the owner's backup message + logs warn
  that the (unencrypted) archive contains the system key and recommend setting a passphrase. The key stays
  in the archive so cross-server restore keeps working (owner's choice).
- **Medium:** the portal `/pay/txid` endpoint now enforces the same chain-specific tx-hash format as the
  bot (shared validation in `submit_reseller_payment`), rejecting malformed/overlong values instead of
  500ing on Postgres or storing junk review rows.
- **Medium:** if a portal screenshot proof fails to save to disk, the owner is notified text-only (no
  broken image) and the reseller is asked to resend, instead of a false success.
- **Low:** decimal excluded package sizes (e.g. 1.5 GB) are matched exactly instead of being truncated to 1.

Auto-applied migration `c8d2f1a4e6b9` adds `portal_login_nonce`. Regressions in `test_invoice_state.py`
and `test_security_fixes.py`; full gate green (235 backend tests).

## 1.40.0 - 2026-06-26

### Changed — Main menu is now a persistent docked keyboard (reply keyboard)

- The reseller and owner **main menus** are now a **persistent reply keyboard** docked at the bottom of
  the chat (always visible above the text box), instead of inline buttons attached to one message. The
  top-level actions are always one tap away and never scroll out of view.
- Tapping a docked main-menu button is handled by a **high-priority** handler registered before every
  FSM text handler, so it works **from anywhere** — it clears any in-progress flow and navigates, acting
  as a universal escape (complements the per-prompt «انصراف» buttons from v1.39.0). Sub-screens (pay,
  sub-management, etc.) stay inline since they need dynamic buttons.
- Labels route to the same actions as before via shared dispatchers (`_do_reseller_menu` / `_do_owner_menu`
  reusing the existing `_send_*` / `_dispatch_owner` helpers), so behavior is unchanged — only the menu's
  presentation. No schema/API change. Contract test in `tests/test_bot_ux.py`.

## 1.39.0 - 2026-06-26

### Changed — Bulletproof bot UX (no dead-ends, no mistypes, always a tappable exit)

- Every prompt that puts a user into an FSM state now carries a **visible exit button** («✖️ انصراف» →
  the new global `cancel` callback, or «« بازگشت»), and invalid input **re-shows the prompt with that
  exit** — so the user can always tap their way out and is never forced to remember `/cancel`. This fixes
  the locked **pay flow** trap (a malformed TXID / stray text used to keep the user stuck) and the
  GB-cap / capacity-bump / new-user-name / reseller-search prompts that previously errored with no button.
- **Presets over typing:** setting a sub-reseller's monthly GB cap and approving a capacity-increase
  request are now preset-button taps (`setcap:` / `capok:`) with a «مقدار دلخواه» fallback, mirroring the
  existing user-creation pickers — far less room to mistype.
- The Telegram **menu button** (the input-bar grid icon) is explicitly set to the curated `/` command
  list (`MenuButtonCommands`), giving a clean, always-available entry point.
- No schema change, no API change. New regressions in `tests/test_bot_ux.py`; full gate green (229 tests).

## 1.38.0 - 2026-06-25

### Added — Sub-reseller «🚫 توقف ساخت کاربر» (limits-only freeze)

- A reseller can now **freeze** a sub-reseller from the bot («مدیریت زیرمجموعه‌ها») AND the reseller web
  portal (زیرمجموعه‌ها): it zeros the sub-subtree's **`max_users`** (they can't create new users or expand)
  **without disabling existing users** — current customers stay online. This sits alongside the full
  **مسدودسازی** (which also disables users). Reversible with «رفع توقف ساخت کاربر» (== restore). A frozen
  sub can be escalated to a full suspend, and either is restored back to active.
- Built on the proven enforcement **queue** (the same per-panel-parallel, per-target, resumable worker as
  suspend/restore): a new `freeze` action runs only the admin-limits phase with `max_active_users` **kept**;
  unfreeze reuses the restore path; escalation recovers the real pre-freeze limits via `max_users_snapshot`.
  New `EnforcementState.frozen` + `EnforcementActionType.freeze` are VARCHAR enum values, so there is **no
  database migration**. The GB-cap-reached alert to the parent now offers freeze as an option. Regression
  tests in `tests/test_freeze.py`.

### Verified

- The bot/portal sub-reseller suspend & restore already route through the optimized queued enforcement
  (`enforce_reseller` / `queue_restore` → `process_enforcement_queue`, per-panel-parallel + per-target id
  resolution). No legacy synchronous suspend path remained — no change needed there.

### Changed — versioning policy & repository hygiene

- Added **`docs/VERSIONING.md`** — the rule for when to bump MAJOR / MINOR / PATCH — referenced from
  `docs/RELEASE_PROCESS.md` so it's read before every release. This release applies it: a new
  backward-compatible feature ⇒ **MINOR** ⇒ `1.38.0` (ending the long `1.37.x` patch run).
- `.github/dependabot.yml`: removed the `/deploy` docker ecosystem (no Dockerfile there → that
  "Dependabot Updates" run failed), **grouped** updates into one PR per ecosystem, lowered the open-PR
  limits, and moved to monthly — keeping the repo near a single `main` while still surfacing security
  updates.

## 1.37.109 - 2026-06-25

### Fixed

- **Changing the owner's Telegram id in Settings now reaches the bot.** `owner_chat_id` was pinned once
  and `_is_owner_user` ignored the editable `owner_telegram` afterwards, so a changed id never took effect.
  Now a **numeric** `owner_telegram` is authoritative — it re-pins `owner_chat_id` and the new account
  becomes owner immediately (old one loses access), no restart; an **@username** unpins so that account's
  next `/start` re-binds. The owner command menu is moved to the new chat (best-effort). Channel/group ids
  were checked — they read fresh and are editable, so they had no such staleness bug; their Settings help
  now notes a direct edit applies immediately.

### Added — Tools › «حذف ادمین از پنل هیدیفای» (queued cascade deletion)

- One owner action deletes an admin (reseller) **plus its whole sub-tree** — all sub-resellers and every
  user they created — from the **Hiddify panel** AND our DB, instead of the manual bottom-up grind. It runs
  through the existing enforcement **queue**: per-panel, **chunked**, **resumable**, panel-paced and
  apply-aware (users are removed via Hiddify's native bulk **delete** action — one `quick_apply` per batch —
  so a large admin doesn't overwhelm the panel), then the admin is deleted (Hiddify cascades its sub-admins),
  then the sub-tree is purged from our DB. The durable **financial ledger is kept**. New
  `EnforcementActionType.delete_admin` (VARCHAR enum → no migration), `AdminApiClient.bulk_delete_users` +
  `delete_admin`, `enforcement.queue_admin_deletion` + `_process_delete_action`, `reseller_purge.purge_subtree`,
  and Tools endpoints `GET /api/tools/admin/{id}/delete-preview` + `POST /api/tools/admin/{id}/delete`. The
  panel owner / super-admin is refused. A confirmation dialog shows the sub-reseller + user counts and warns
  it's irreversible. Tests in `tests/test_admin_delete.py` + `test_admin_api_bulk.py`. 219 backend tests
  pass; ruff + mypy + pip clean; frontend tsc + build clean.

## 1.37.108 - 2026-06-24

### Added — pay several invoices with one transfer («پرداخت همهٔ بدهی»)

A customer can now settle **one OR many** invoices with a single payment (TXID or receipt), instead of
one-per-transfer. Reviewed end-to-end (payment↔invoice attribution is the project's most sensitive logic)
and covered by new tests.

- **Bot**: «💳 پرداخت فاکتور» now shows a **«پرداخت همهٔ بدهی»** button (when ≥2 payable) alongside the
  per-invoice buttons; the locked pay flow holds an invoice-id **set** (`pay_invoice_ids`) and the
  TXID/receipt is attributed to exactly that set.
- **Reseller portal**: a «پرداخت همهٔ بدهی» button + a `PayDialog` that loads the payable set
  (`GET /api/portal/pay/options-all`), shows the summed amount, and submits `invoice_ids`.
- **Owner panel (Payments)**: the «فاکتور (دوره)» cell lists every covered invoice (with a per-invoice
  breakdown tooltip), «مبلغ» shows the total, and confirm/reject act on the whole set.
- **Shared safety rules** (`payments.submit_reseller_payment`, used by bot + portal): EVERY chosen invoice
  is re-validated under a row lock (owned, OWED, not future-deferred) — if any isn't payable the **whole
  batch is rejected atomically** (never silently pay a subset); **one pending payment per invoice** (an
  invoice already in a pending set blocks the submission); the set is stored in `settled_invoice_ids` with
  the payment amount = the **SUM**. On-chain `verify_payment` requires the deposit to cover the set's total;
  `confirm_manually`/`reject_payment`/`delete_payment` settle/revert the **whole set** (reject keeps an
  invoice paid if another confirmed payment still covers it); auto-restore lifts enforcement only when no
  due invoice **outside** the set remains; the dunning pending-payment hold covers the whole set. Capped at
  25 invoices per payment (fits `settled_invoice_ids`); legacy single-`invoice_id` rows keep working.
- New `tests/test_invoice_state.py` cases (multi-invoice submit/confirm/verify-floor/reject-with-overlap/
  pending-dedup/atomic-stale-reject/back-compat). 215 backend tests pass; ruff + mypy + pip clean; frontend
  tsc + build clean. Help page + CLAUDE.md updated.

## 1.37.107 - 2026-06-23

### Added — owner «ابزارها» (Tools) page for rare/special operations

- **Manually remove a mistaken end-user from billing.** Search end-users by **name or UUID**, see their
  panel/reseller/sold-quota/consumption/«روی پنل موجود است؟», and remove one with a confirmation dialog.
  Removal deletes the user's `EndUserSnapshot` **and** its `usage_meters` rows, so it drops out of BOTH the
  sold-quota base and the metering (overage/consumption) extras — fixing the case where a wrongly-created
  huge-quota user keeps inflating the invoice. DB-only: the Hiddify panel, sent invoices, and the financial
  ledger are untouched (regenerate the draft / «بازمحاسبه» a sent invoice to apply). New owner endpoints
  `GET /api/tools/end-users` + `POST /api/tools/end-users/{id}/remove`.
- **Relocated «قطع اتصالِ تلگرام»** (reseller Telegram-unbind) from the Resellers list to this Tools page
  (with its own reseller search) — both are rare actions, so they no longer clutter the main list.

### Added — DB retention

- `usage_meters` now has bounded retention: the daily maintenance prunes meter rows for periods older than
  `meter_retention_months` (default 6; 0 disables) — old periods are already locked into invoices + the
  financial ledger. Closes the one slow-growth vector (the live DB is ~39 MB; all other log/snapshot tables
  were already pruned daily).

Tests: `tests/test_tools.py` (search by name/uuid, remove deletes snapshot+meters, 404),
`tests/test_meter_retention.py`. 208 backend tests pass; ruff + mypy + pip clean; frontend tsc + build clean.
No schema migration.

## 1.37.106 - 2026-06-21

### Added

- **Owner can disconnect a reseller's Telegram account** (Resellers page). When a reseller registers
  their panel to a Telegram account and later loses access to it, they were stuck — bot registration
  refuses to re-bind a row already tied to a different account, and `/removelink` needs the lost account.
  A new owner-only **«قطع اتصالِ تلگرام»** action (a `LinkOff` icon, shown only on connected resellers,
  behind a **confirmation dialog** that names the reseller + the currently-bound account) releases the
  binding so the reseller can re-register from any account. It clears exactly what the bot's `/removelink`
  clears — `bot_chat_id`, `link_tag`, `registered_at` — for that one reseller row; it does **not** touch
  the Hiddify panel, users, or invoices. New `POST /api/resellers/{id}/unbind-telegram` (owner JWT,
  idempotent). No schema change. Tests in `tests/test_unbind_telegram.py`. 205 backend tests pass; ruff +
  mypy + pip clean; frontend tsc + build clean.

## 1.37.105 - 2026-06-21

### Changed (enforcement is now much lighter on the panels)

- **Suspend/restore no longer downloads the entire panel user list.** Previously every enforcement
  action called `GET /api/v2/admin/user/` (the WHOLE panel — 10k+ users, hundreds of KB) just to map the
  reseller's target uuids → the numeric ids the bulk action needs; on a large/busy panel that is slow and
  **503s**, and it repeated per-action during a suspension burst (it helped overwhelm a panel). Enforcement
  now resolves ids for **only the reseller's target users** via the single-user endpoint
  `GET /api/v2/admin/user/{uuid}/` (its `UserSchema` includes the numeric `id`), with bounded concurrency
  to stay gentle, and **caches** each id on `EndUserSnapshot.panel_user_id` so subsequent suspensions do
  **zero** id lookups. A user absent on the panel (404) is skipped (we only act on users still present).
  The bulk enable/disable + single `quick_apply_users`-per-batch is unchanged. New
  `AdminApiClient.get_user_id`; `get_user_ids` (whole-list) stays only as an ad-hoc/fallback helper.
- New nullable column `end_user_snapshots.panel_user_id` (Alembic migration `b7f1c0a9d2e4`, runs on boot;
  registered in the post-baseline column allowlist). No data backfill — populated lazily on first use.
- Tests: `tests/test_enforcement_lighten.py` (per-target resolution, id caching → zero lookups, 404
  skipped, whole-list never called) + `get_user_id` unit test; existing enforcement tests switched to mock
  the per-user lookup. 203 backend tests pass; ruff + mypy + pip clean; frontend tsc + build clean.

## 1.37.104 - 2026-06-19

### Fixed

- **Interim invoice («فاکتور علی‌الحساب») PDF now matches its text + the real invoice.** The interim
  TEXT already included the abuse-metered extra (overage + renew-by-edit), but the PDF rendered base
  snapshot lines only, so its total disagreed. A shared helper
  (`reseller_report.node_invoice_pdf_lines`) now composes base lines + the per-user metering-extra lines
  (labelled «… — مصرف اضافه/تمدید», and removed-from-panel users labelled «… — مصرف حذف‌شده از پنل»),
  exactly mirroring how the real end-of-month invoice persists its lines. The own/sub/interim PDFs
  (`render_own_usage_pdf`, `render_node_usage_pdf`, `render_sub_invoice_pdf`) render from it, so the PDF
  total == the interim text == the eventual real invoice.

### Changed

- **Enforcement queue runs one lane per panel, in parallel.** The worker previously processed a single
  global action per ~5-minute tick, serializing suspensions/restores across panels. It now groups pending
  actions by panel and processes panels concurrently (bounded by the new `enforcement_panel_concurrency`,
  default 6), each panel on its own session and still sequential within the panel (so a single panel is
  never hammered). `enforcement_action_batch_limit` is now **per panel** per tick (default raised 1→3).
  Per-action chunking/resumability is unchanged, so large resellers still resume across ticks — now
  without blocking other panels. No DB schema change; in-flight queued actions resume normally.
- Tests in `tests/test_invoice_enforcement_fixes.py` (PDF↔text parity with metering; multi-panel
  processed in one tick; setting range). 200 backend tests pass; ruff + mypy + pip clean; frontend
  tsc + build clean.

## 1.37.103 - 2026-06-19

### Changed (Toncoin → Gram rebrand)

The TON token was renamed **Toncoin (TON) → Gram (GRAM)** on 2026-06-15 (name + ticker + logo only; the
blockchain stays "The Open Network"/TON, no token swap, 1 TON = 1 GRAM).

- **Fix (functional): the auto rate was broken.** Wallex relisted the Toman market as **`GRAMTMN`** and
  removed `TONTMN`, so `rates.fetch_ton_toman` (querying `TONTMN`) returned nothing. It now reads
  `GRAMTMN` (with a `TONTMN` fallback for safety). Rate semantics, plausibility band, and the
  manual/auto split are unchanged.
- **Display rebrand → «گرام (GRAM)».** The coin shown to resellers/customers is now «گرام (GRAM)» and the
  amount unit is `GRAM`, across the bot pay instructions, owner payment review, the reseller portal pay
  dialog, the Settings page (toggle/section/wallet/tolerance/rate-mode labels + the live-rate chip), the
  header live-rate widget, and the Payments deposit-check display. The **deposit warning stays «فقط روی
  شبکهٔ TON واریز شود»** because deposits still happen on the TON network.
- **Unchanged (internal/blockchain):** `chain="ton"`, `PaymentMethod.ton_txid`, all setting keys
  (`pay_ton_enabled`, `ton_wallet_address`, `ton_rate_mode`, `ton_toman_*`, `toncenter_api_key`,
  `ton_amount_tolerance_pct`), API field names, the toncenter on-chain check, tonscan/tonviewer explorer
  links, and wallet addresses — so existing settings/payments keep working with **no DB migration**.
- Tests: `tests/test_rates_gram.py` (GRAMTMN + TONTMN-fallback + absent), and updated GRAM display
  assertions in `test_payment_review.py`. 197 backend tests pass; ruff + mypy clean; frontend tsc + build
  clean.

## 1.37.102 - 2026-06-19

### Fixed (bot-created user: correct subscription link + cleaner message)

- **The customer subscription link was wrong.** It used the panel's **admin** secret path; Hiddify v12
  serves end-users on a **separate client path**, so the link (and QR) the bot sent didn't match the
  panel's actual share link. Now the bot produces exactly the panel's link:
  `https://<host>/<client_proxy_path>/<uuid>/#<name-slug>` (and the QR encodes the same). We capture the
  client path (`proxy_path_client`) from the panel backup's `hconfigs` during every sync and cache it on
  the panel (`Panel.client_proxy_path`, new nullable encrypted column + Alembic migration); the first
  create after deploy fetches it on-demand if a panel hasn't re-synced yet. The `#<name>` fragment is
  slugified like Hiddify's `unicode_slug` (spaces→hyphens, unicode preserved). `Panel.user_sub_link`
  rebuilt accordingly (drops the `/auto/` form; falls back to the admin path only if the client path
  isn't known yet).
- **Dropped the extra “و بلافاصله فعال است”** from the create success message → just “✅ N کاربر ساخته شد.”
- Tests: `parse_backup` client-path extraction, `Panel.user_sub_link` (client path + slug + fallback),
  and the create flow's link. 194 backend tests pass; ruff + mypy + pip clean; the migration applies on a
  fresh and an adopted pre-Alembic DB.

## 1.37.101 - 2026-06-19

### Added (Bot: top-level resellers create Hiddify end-users — single + bulk)

A top-level reseller can now create end-users from the Telegram bot («➕ ساخت کاربر» / `/newuser`)
instead of going into the Hiddify panel — and the created user **works immediately** (no manual apply).

- **Safe create + auto-apply.** Reading the Hiddify **v12 `hiddifypanel` source**, the v2 admin
  `POST /api/v2/admin/user/` runs `user_driver.add_client()` + `hiddify.quick_apply_users()`, so the
  user is pushed to the proxy core at creation. New `AdminApiClient.create_user(...)` authenticates
  **as the reseller** (`Hiddify-API-Key: <admin_uuid>`), so the panel sets the user's `added_by_uuid`
  to that reseller automatically (correctly billed/owned). We supply a client-side `uuid` so the
  sub-link is known instantly.
- **Flow.** Single/bulk → (bulk: count) → volume (GB) → duration (days) → name → a **confirm summary**
  → create. Each created user gets a **QR image + the `auto` subscription link** (clickable +
  tap-to-copy `<code>`) so the reseller can hand it to the customer. Bulk creates `<base>1..<base>N`
  and sends one QR per user (paced for Telegram limits).
- **Bounded + owner-editable.** Volume/day/bulk-count options come from owner-editable settings
  (Settings → «ساخت کاربر»: `user_create_gb_options` 20/30/50/100, `user_create_day_options` 30/60,
  `user_create_bulk_counts` 5/10/20, + a master `user_create_enabled` toggle). Every choice is
  re-validated server-side against those lists.
- **Capacity guard.** Creating beyond the reseller's `max_users` is blocked up-front with a pointer to
  the capacity-increase request; the panel's own server-side `max_users` is the hard backstop (a
  mid-bulk rejection stops and reports "K of N created"). Auto sub-link:
  `https://<host>/<proxy_path>/<uuid>/auto/` via new `Panel.user_sub_link`. Owner gets a ping per batch.
- **Gating:** offered only to **top-level resellers**; `start_date` is left to Hiddify's default
  (starts on first connect). Server-side QR via the already-bundled `qrcode`/`Pillow`. Regression
  tests in `backend/tests/test_usercreate.py` (create POST shape/headers/limit-error, sub-link,
  settings list validation, capacity guard, bulk loop incl. mid-bulk limit). 193 backend tests pass;
  ruff + mypy clean; frontend tsc + build clean. No DB schema change.

## 1.37.100 - 2026-06-19

### Changed / Added (portal follow-ups)

- **Sub-card sale trend → daily (current month).** Replaced the per-sub 6-month sparkline with a
  **daily** bar chart for the current month — the same look as the owner dashboard's «روند فروش
  روزانه» — so a reseller sees roughly how much each sub sold each day. New
  `GET /api/portal/subs/{id}/sales-by-day?period=` (reuses `node_invoice` + the same daily bucketing
  as `/summary` and `reports.sales_by_day`, `_owns_sub`-guarded). The dashboard's chart option was
  extracted into a shared `dailyTrendOption` helper used by both, so they stay identical (the existing
  daily chart is unchanged). Fetched lazily per card (cached).
- **Capacity-increase request is now actionable for the owner.** A reseller's «درخواستِ افزایشِ
  ظرفیت» no longer just asks the owner to free-text reply — the owner's Telegram message now carries
  **«✅ تأیید (+N) / ✏️ مبلغِ دیگر / ❌ رد»** buttons. Approving applies the bump immediately via the
  existing `admin_capacity.bump_limits`, «مبلغِ دیگر» prompts the owner for a custom amount (1–5000),
  and either way the requesting reseller is notified of the outcome (`notifier.send_to_reseller`);
  rejecting notifies a decline. Buttons are owner-only and removed after the action (no double-tap).
  New `keyboards.capacity_request_keyboard` + bot handlers `capok`/`capmore`/`capno` +
  `OwnerCapBumpState`. No DB schema change. Tests in `backend/tests/test_portal.py` (sales-by-day
  ownership/shape/bad-period, request attaches the action keyboard, keyboard variants). 185 backend
  tests pass; ruff + mypy clean; frontend tsc + build clean.

## 1.37.99 - 2026-06-19

### Added (Reseller portal — feature batch v2) + Subs ratio fix

Eight reseller-portal enhancements (all scoped to `/api/portal/*` under `get_current_reseller`,
sub actions gated by `_owns_sub`; no DB schema change), plus a fix to the sub-reseller user ratio.

- **Fix — Subs «کاربران» ratio.** The sub card showed `enabled/total` (e.g. "۱/۱") which was
  meaningless; it now shows **created-users (whole subtree) / max_users** via the same `CapacityBar`
  the owner panel uses (`/subs` now returns `max_users`, `can_add_admin`, and recent `months`).
- **#1 Payment QR codes** — the pay dialog renders a scannable QR of the USDT/TON wallet address
  next to the tap-to-copy row (`qrcode.react`).
- **#2 Payment timeline + receipt** — the Payments page shows a 3-step review timeline
  (ثبت → بررسی → نتیجه) and a «مشاهدهٔ رسید» button for screenshot payments
  (`GET /api/portal/payments/{id}/proof`, scoped + path-traversal-guarded; `has_proof`/`verified_at` added).
- **#6 Per-sub PDF** — download a sub-reseller's GB-only invoice PDF for any recent month from the
  portal (`GET /api/portal/subs/{id}/pdf?period=`, reusing `invoice_pdf.render_sub_invoice_pdf`).
- **#7 Manage sub capacity** — increase a sub's `max_users`/`max_active_users` (+۵۰/۱۰۰/۲۰۰/۵۰۰ or
  custom, capped at 5000) and toggle «اجازهٔ ساختِ زیرمجموعه», reusing `admin_capacity.bump_limits` /
  `set_can_add_admin` (`POST /api/portal/subs/{id}/bump-limits` / `…/can-add-admin`).
- **#8 «ظرفیتِ من»** — a capacity meter per reseller on the «پنل‌ها و ظرفیت» page + a «درخواستِ
  افزایشِ ظرفیت» button that pings the owner on Telegram (`GET /api/portal/capacity`,
  `POST /api/portal/capacity/request`).
- **#9 Sub monthly-trend** — a compact 6-month sale sparkline on each sub card (inline, no chart lib
  per card).
- **#11 In-portal notifications** — a bell in the portal header with a popover of recent events
  (invoice issued, payment confirmed/rejected/pending, sub at GB cap, reseller blocked), derived live
  from existing data (`GET /api/portal/notifications`, no new table; unread tracked client-side).
- **#15 Onboarding/help** — a Persian «راهنما» page describing each portal section + a one-time
  dismissible welcome banner on the portal dashboard.

**Shared payment core unchanged** — the bot and portal still both call `payments.submit_reseller_payment`.
Regression coverage extended in `backend/tests/test_portal.py` (capacity/can-add-admin/sub-PDF
ownership + validation, `/capacity` scoping, capacity-request relay, proof ownership, notifications
scoping). 182 backend tests pass; ruff + mypy clean; frontend tsc + build clean (bundle budget OK).

## 1.37.98 - 2026-06-19

### Added (Reseller web portal — standalone site, Telegram one-time login)

Resellers can now log into a full standalone website (besides the Telegram bot) and see/do
everything that concerns them. Login is via Telegram but **not** a Mini App: the bot hands a
one-time link that opens the real site already signed in (no passwords for ~400 resellers).

- **Auth** — a new reseller-bot button/command «🌐 ورود به پنلِ تحتِ وب» (`/portal`) mints a
  short-lived (15-min) **signed** login token and replies with `https://<server_domain>/portal/login?t=…`.
  The site exchanges it (`POST /api/portal/auth/exchange`) for a ~7-day reseller session token
  (`role="reseller"`). Stateless (bot + backend share `SECRET_KEY`) — no new table/migration.
  **Strict role isolation:** reseller tokens reach ONLY `/api/portal/*` (owner's `get_current_subject`
  rejects them); owner tokens are rejected by the new `get_current_reseller` dependency. Every
  request is scoped to the caller's own reseller rows — a reseller can never see another reseller's
  or the owner's data; client-supplied ids are never trusted.
- **View** — Dashboard (month-to-date sale estimate, daily-sale bar chart, per-reseller breakdown,
  outstanding debt), Invoices (+PDF, owed flagged), Payments history, Sub-resellers (usage/GB-cap/
  status), and Panel links (tap-to-copy). New read endpoints under `/api/portal`.
- **Actions** — pay an owed invoice by **TXID** (USDT/BEP-20 or TON) or by uploading a **receipt
  image**; set/clear a sub-reseller's monthly **GB cap**; **suspend/restore** a sub-reseller; and
  **message support** (relayed to the owner's Telegram with a reply button). The owner receives the
  same rich review + confirm/reject buttons as for bot-submitted payments.
- **Shared payment core (no behavior drift):** extracted the bot's payment-submission rules into
  `payments.submit_reseller_payment` — re-validate the chosen invoice under a row lock (owned + owed
  + not future-deferred), block duplicate tx hashes (confirmed/pending) and re-open a rejected one
  only for its owner, enforce one pending payment per invoice, and **never auto-confirm**. Both the
  bot (`_handle_txid`/`_handle_payment_proof`) and the portal call it, so the safety rules are
  identical on both surfaces.
- **Frontend** — a self-contained `/portal` route group in the existing SPA: own axios instance
  (`portal_token`), own auth context + layout, fully independent of the owner app and its first-run
  setup gate, reusing the existing RTL glass theme + components.
- Regression tests in `backend/tests/test_portal.py` (login-token roundtrip/expiry, owner/reseller
  role isolation, per-reseller scoping of invoices/payments/PDF, the shared submit rules incl.
  duplicate/reopen/one-pending/not-payable, sub-cap + ownership guards, support relay). NOTE: the
  portal login link is shown only when the `server_domain` setting is configured (it is in
  production). No database schema change.

## 1.37.97 - 2026-06-19

### Added (Dashboard — daily sale trend chart)

- A new full-width **«روند فروش روزانه»** bar chart under «۱۰ نماینده برتر»: one bar per day of the
  selected month, height = that day's share of the sale (Σ of each service's `usage_gb × the
  bundle root's price`, bucketed by the service's creation date). Computed live via the invoice
  engine (present-filtered) so it matches what's billed and works for the in-progress month.
  Styled to match the other dashboard charts (theme accent vertical gradient, rounded bars, axis
  tooltip, dark/light aware). New `GET /api/reports/sales-by-day?period=YYYY-MM`. NOTE: per-bundle
  floor (min-sale) and metering overage are bundle-level, so this is the faithful BASE-sale trend
  (Σ days ≈ the month's base sale).

## 1.37.96 - 2026-06-18

### Added (DB retention — stale removed-user snapshots)

- The daily maintenance sweep now also prunes **end-user snapshots of users removed from Hiddify**
  whose creation month is older than the **previous** billing month, plus any **orphaned
  `usage_meters`** left behind (that table has no foreign key, so meters from a deleted snapshot or
  a deleted panel were never cleaned). The current + previous month are always preserved, so
  mid-month-deletion billing (which reads the lingering snapshot) is never affected; financial
  tables are never touched. This caps the slow growth of removed-user rows.
- Audit: no other table currently orphans on Hiddify removal — `end_user_snapshots`/`invoices`/
  `payments` cascade on panel/reseller delete, `delivery_log`/`sync_runs` null out, and
  `enforcement_actions` cascade. The only gap was `usage_meters` (no FK), now swept here.

## 1.37.95 - 2026-06-18

### Fixed (high-volume finder — exclude removed users billed on consumption)

- The «هشدارِ کاربرانِ پرحجم» list wrongly included end-users that were DELETED from Hiddify,
  showing them by their (now irrelevant) sold quota — e.g. a removed test config with a 1-billion-GB
  quota that used 0.8 GB appeared with an absurd would-be amount, even though a deleted user is billed on
  CONSUMPTION (and dropped entirely when consumption ≤ the free threshold). The finder now computes
  the ACTUAL billed quota (mirroring the invoice engine): present users by sold quota, removed
  users only when they consumed ≥ the deleted-full-quota cutoff (then full quota). Removed users
  billed on small consumption — or dropped — no longer appear. Such rows that DO appear are marked
  «حذف‌شده» in the list and warning. No data was orphaned by a bug: removed-user snapshots are
  retained ON PURPOSE so mid-month deletions are billed fairly.

## 1.37.94 - 2026-06-18

### Added (high-volume users — catch the 1000 GB mistake before billing)

- A new **«⚠️ هشدارِ کاربرانِ پرحجم»** card on the broadcast page lists end-users created with a very
  large sold quota (the Hiddify 1000 GB default, often left by mistake) and the **responsible
  top-level reseller** the invoice lands on — even when a deep sub-reseller created the user (the
  real creator is shown as a «↳ ساختهٔ زیرمجموعه» note). Filter by threshold (default from the new
  `high_volume_gb_threshold` setting), panel, and «فقط ماهِ جاری» (default on). Each row shows the
  would-be amount (GB × the root's price) and a tap-through Telegram deep-link to the admin.
- **Warn action**: DM the responsible admin one aggregated message listing all their high-volume
  users (per-row «هشدار به این ادمین» or «به همهٔ ادمین‌های جدول»), reusing the background broadcast
  worker (throttle + concurrency + 429 retry + owner summary). Only bot-registered admins are
  messaged; the rest are counted in the owner summary. Nothing is persisted to the DB.
- The creator→root mapping, present-filter, and current-month rule mirror the invoice engine, so
  the list matches exactly what will be billed. `GET/POST /api/reports/high-volume-users[/warn]`.

## 1.37.93 - 2026-06-18

### Fixed (Resellers — «پُری ظرفیت» now counts the whole subtree)

- The capacity column counted only the admin's OWN users, not their sub-resellers' — so e.g. an
  admin with 108 users across their subtree showed 22. `_usage_counts` now returns **subtree**
  totals (the admin itself + all descendants, per panel, cycle-safe, memoized O(n)), matching what
  Hiddify shows. Both the list and the tree tab use this single source, so they agree. The quota
  denominator stays the admin's own `max_users` (so a full subtree correctly reads >100% / red).
  UUID matching is case-insensitive, and the total counts ALL users (not just active). Frontend
  unchanged (`users_count` is now the subtree total).

## 1.37.92 - 2026-06-18

### Changed (bot panel messages — clearer & bidi-clean)

- **«🖥 پنل‌های من»**: now shows each reseller ONLY their current panel link (the «آدرسِ قبلی» line
  was removed) with a blank line between panels — much tidier, no duplicate-link clutter.
- **«اعلامِ آدرسِ جدیدِ پنل»** message: reworded to be shorter and clearer, with every Persian label
  on its own line and each URL alone on the next line (in tap-to-copy `<code>`), so no line mixes
  Persian + English and the layout stays clean.
- **Panel-migration preview** on the broadcast page: the sample links are each on their own
  left-aligned monospace line below their label (no more Persian-label-plus-URL wrapping mess).

## 1.37.91 - 2026-06-18

### Added (panel domain migration — keep old reseller links working)

- **Panel host aliases**: a panel can now carry old/alternate hosts (new `panels.host_aliases`
  column + Alembic migration). They are **matcher-only** — a reseller's stale pasted link with an
  old host still registers in the bot — and **never** touch billing, backup, or the Admin API
  (those always derive from the current `host`). Changing aliases alone does **not** trigger a
  re-sync; only host/proxy/owner/api-key changes do (unchanged). The registration matcher keeps
  its fail-closed «unique match or None» rule.
- **Panels page**: the edit dialog gained a «هاست‌های قبلی/مستعار» field and a one-step
  «مهاجرتِ دامنه» action (moves the current host into the aliases and sets the new host).
- **«اعلامِ آدرسِ جدیدِ پنل»** (new section on the broadcast page + bot path): sends each registered
  reseller on a panel a **personalized** message with their OWN new + previous admin link, reusing
  the background broadcast worker (throttle + bounded concurrency + 429 retry + owner summary).
  Only registered resellers are messaged; nothing is persisted to the DB.
- **«🖥 پنل‌های من»** in the bot now shows each reseller their own tap-to-copy panel link (built from
  the panel's current host, so it auto-updates after a move) plus an «آدرسِ قبلی» line when the
  panel has an old host.

## 1.37.90 - 2026-06-18

### Changed (broadcast — proper background send model)

- The owner broadcast used to send to every recipient inline within the one HTTP request (a plain
  `for` loop, no concurrency or throttle) — slow for large audiences, tripping the 120s request
  timeout, and the report was lost when it did. Rebuilt as a standard background broadcast:
  - The request **resolves recipients fast and returns immediately** (`{started, total,
    unregistered}`); the actual send runs in the background.
  - Sending uses **bounded concurrency (20) + a global rate limit (~25 msg/s)** with per-recipient
    error handling: `429` → wait `retry_after` and retry (up to 3×), blocked → counted, others →
    failed. A **summary is pushed to the owner's Telegram** at the end («📣 پیام همگانی تمام شد …»).
  - The panel shows an **immediate "started" message + live progress** (polled from an in-memory
    snapshot — `GET /api/ops/broadcast/status`). The bot owner-broadcast path got the same
    background treatment.
  - Still **nothing persisted to the DB** — no table, no per-recipient rows; only the in-memory
    progress snapshot + server log. The recipient preview is unchanged.

## 1.37.89 - 2026-06-17

### Fixed (health report — «آخرین پشتیبان» showed «—»)

- The system-health message read the last-backup time from the `data/backups` disk folder, but the
  normal auto-backup streams straight to the owner's Telegram and never writes a zip there — so it
  always showed «—». Now every SUCCESSFUL backup stamps an internal `last_backup_at` timestamp
  (auto-backup all three branches, and the manual panel download), and the health label is read
  from that (formatted Tehran-local). A failed backup never stamps (the scheduler still alerts the
  owner). No DB+key zips pile up on the server. Existing installs show «—» until the next
  successful backup, then it fills in.

## 1.37.88 - 2026-06-17

### Changed (absent-reseller delete — full branch cleanup)

- Deleting an absent reseller now also removes the **absent sub-resellers beneath it** and the
  **end-user snapshots (+ usage meters)** those removed admins created — a complete cleanup of a
  branch that's gone from the panel. PRESENT sub-resellers are left untouched (the next sync would
  just recreate them). Invoices/lines/payments of the deleted rows go too; the durable financial
  ledger is still kept. The confirm dialog and result message reflect this (counts of resellers +
  users removed). Server still refuses to delete a reseller that is currently present.

## 1.37.87 - 2026-06-17

### Fixed (UI — tree tab now identical to the main list)

- Removed the **last row-background tint** from the tree tab (even the faint violet wash on root
  rows). Over the translucent glass surface any tint read as a foggy/matte film; tree rows now
  render with the exact same transparent background as the crisp main-list rows.
- The subtree tree now **starts collapsed** instead of auto-expanding every root branch, so its
  default height and scrolling match the main list (a compact page of root rows). Branches expand
  on demand via the chevrons or all at once with «باز کردن شاخه‌ها».

## 1.37.86 - 2026-06-17

### Fixed (UI — Resellers tree tab looked foggy)

- The «درخت زیرمجموعه‌ها» table rows were tinted with `alpha(background.paper, …)`. Under the
  glass theme `background.paper` is already translucent (`rgba(255,255,255,0.07)`), so nested rows
  ended up with a heavy ~22% white film — the "something fell on it / matte" look. Tree rows are
  now as crisp as the main list (hierarchy is conveyed by indentation, connectors, chevrons, and
  bold root names; root branches keep only a whisper-faint violet wash).

## 1.37.85 - 2026-06-17

### Added (absent resellers — see & safely delete removed admins)

- New **«نماینده‌های غایب (حذف‌شده از پنل)»** view in the Resellers page (third tab beside
  list/tree): admins removed from the Hiddify panel whose DB row still lingers. Each shows name,
  panel, last seen (Jalali), user count, and remaining sub-reseller count, with a guarded delete.
- Backend `GET /api/resellers/absent` (inverse of the presence filter, restricted to panels with a
  good latest sync so a failed sync never marks everyone absent) and `DELETE
  /api/resellers/{id}/absent`. The delete **re-checks absence server-side** and refuses (409) a
  reseller that is currently present — this path is only for removed admins.
- Deletion cascades the reseller's invoices, invoice lines, and payments, but the **durable
  financial ledger (`financial_records`) is intentionally kept** (financial history is permanent).
  Sub-resellers are not cascaded (their `parent_admin_uuid` is a plain string, not an FK); the
  delete dialog warns when the row has delivered/paid invoices, payments, or remaining
  sub-resellers. Owner-only; nothing extra is persisted.

## 1.37.84 - 2026-06-17

### Fixed (broadcast — correct audience) & Added (professional filters + report)

- **Bug fixed:** every broadcast audience was selecting chat ids from *all* resellers
  (sub-resellers and billing-exempt included, across every panel), so a targeted send went to
  the wrong people. All audiences now start from ONE base set — the **top-level resellers in the
  «نمایندگان» main list that are NOT exempt from billing and are present on an active panel** —
  exactly the set the user expects. Sub-resellers, exempt resellers, the owner, and removed admins
  are never included. Recipients are de-duplicated by Telegram id (one person on two panels gets
  one message).
- **Configurable filters** (each on top of the base set, combinable with an optional single-panel
  restriction): همه نمایندگان · بدهکاران (فاکتور پرداخت‌نشده) · فروش صفرِ این ماه · **کم‌تر از N
  کاربرِ فعال** (N editable) · **فاکتورِ این ماه زیرِ مبلغ** (amount editable).
- **Preview + full report:** a «پیش‌نمایشِ گیرندگان» button shows exactly who matches before
  sending; after sending, a per-recipient report lists who received / was blocked / failed, plus a
  count of matched-but-unregistered resellers. The bot broadcast report now also names who it
  couldn't reach. None of this is written to the database (no log/DB bloat).

## 1.37.83 - 2026-06-16

### Added / Fixed (USDT on-chain read — now free & working, like TON)

- The USDT (BEP-20) on-chain check was broken: the old `api.bscscan.com` endpoint is a
  **deprecated V1 API** (returns an error), and Etherscan's V2 API **excludes BSC from the free
  tier**. Replaced it with a **free, key-less read straight from a public BSC JSON-RPC node**
  (`eth_getTransactionReceipt` → parse the ERC-20 Transfer logs for our wallet), mirroring how TON
  is read via toncenter. New `bsc_rpc_url` setting (default a public node; verified against a real
  BSC-USDT transfer).
- The on-chain read is now **unified for both chains**: one «بررسی واریزی روی زنجیره» action and
  one confirm-dialog block in the panel handle TON and USDT, and the owner bot's payment summary
  shows the «وضعیت شبکه» line for both (received amount vs invoice, match/mismatch within
  tolerance; USDT also shows confirmations). Display-only — confirmation stays **manual** for
  every method; no auto-confirm.

## 1.37.82 - 2026-06-16

### Fixed (TON on-chain read — now finds the deposit)

- The TON deposit reader said «از زنجیره خوانده نشد» even for a real, confirmed transaction. Root
  cause: the hash a customer copies from their wallet is the **sender-side** transaction — its
  `in_msg` is the external trigger (no TON value) and the actual credit to our wallet is in the
  transaction's **`out_msgs`**, which the reader didn't inspect. It now scans both `in_msg` and
  `out_msgs` and counts any message whose destination is our wallet, so a normal TON payment is
  read correctly (verified against the real transaction: 17.35 TON → our wallet). Still
  best-effort and display-only — confirmation stays manual. (toncenter v3, free; the optional
  `toncenter_api_key` setting only raises the rate limit, not required for this volume.)

## 1.37.81 - 2026-06-16

### Changed (payment review — professional, complete, actionable)

- **Owner bot — the payment notification and the «پرداخت‌های در انتظار» detail are now one rich,
  complete summary** with approve/reject buttons attached directly under it (no more «به پنل
  بروید»). Each shows: the tracking number, a **clickable reseller name** that opens their
  Telegram profile, the method, the **exact invoice amount** with its paid-currency equivalent
  (TON/USDT), a **clickable explorer link** to the transaction, and — for TON — a best-effort
  **on-chain status** line («✅ واریزی یافت شد: X TON ≈ Y تومان — مطابق فاکتور (±5٪)» / «⚠️ مغایر»
  / «⚪️ از زنجیره خوانده نشد»). The screenshot-receipt forward to the owner carries the same
  summary and buttons.
- The pending-payments list no longer shows a bare «—» for the amount: it falls back to the
  invoice amount when the payment row has none (typical for TON/screenshot).

### Fixed (panel — TON on-chain check)

- The Payments «بررسی زنجیره» action no longer errors for a TON payment. For TON it now reads the
  **actual TON deposited** (toncenter) and reports «واریزی: X TON ≈ Y | فاکتور: Z» with a
  match/mismatch verdict; USDT still runs the BscScan verify. Confirmation stays manual.

## 1.37.80 - 2026-06-16

### Fixed (bot — pay flow locked, no duplicate payment details)

- Tapping an invoice's «💳 پرداخت فاکتور» button repeatedly used to re-send the full
  payment-details message every time, piling up duplicates. Now, once a reseller is in
  the pay flow for an invoice, tapping any invoice button again is rejected with a single
  alert («شما در حال پرداخت یک فاکتور هستید … برای انتخاب فاکتور دیگر ابتدا /cancel را بزنید»)
  — no new message is sent. The flow stays locked on the chosen invoice until the customer
  sends a proof/TXID or cancels. To switch invoices they re-open the pay list (/pay or
  /menu, which clear the flow) or send /cancel.

## 1.37.79 - 2026-06-16

### Added (Invoices tab — Telegram link + search)

- The Invoices list now has a **تلگرام** column: for a reseller whose Telegram is
  connected (started the bot), it shows a clickable Telegram icon that opens their
  private chat (`https://t.me/<username>`, or `tg://user?id=…` when no @username),
  so the owner can message them in one tap. The same column was added to the
  «نمایندگان با فاکتور صفر» tab.
- The Invoices list got a **search box** (like other tabs): filter by reseller name
  or the 8-digit invoice number, with Persian/Arabic digit normalization. The result
  count, total, pagination, and empty state all respect the active search.
- The Telegram deep-link logic is now a single shared `TelegramLink` component reused
  by the Resellers and Invoices pages (one source of truth for the href rules).

## 1.37.78 - 2026-06-16

### Changed (sent invoice text — slimmer, no stale payment details)

- The invoice text delivered by the bot now shows only the **invoice number, period,
  usage (GB), and payable amount**, followed by a call to action to tap the
  «💳 پرداخت فاکتور» button (or the «🧾 فاکتورهای پرداخت‌نشده» menu) to choose the
  method and pay. The embedded card/USDT/TON instructions — and especially the TON
  amount, which could be stale by the time the customer reads the message — were
  removed from the invoice body. The live amounts are shown on the pay screen, which
  recomputes them at tap time. PDFs and the pay/unpaid-invoices flows are unchanged.
- `tpl_invoice` migrated for un-customized installs (the prior `{payment_instructions}`
  default → the new `{pay_cta}` form); customized templates are never overwritten.

### Added (TON deposit reader — decision aid for manual confirmation)

- The panel Payments confirm dialog now shows, for a TON payment, the **actual TON
  deposited** for the txid (read best-effort from the public toncenter API), its Toman
  equivalent at the live TON rate, and the invoice amount, with a green/red badge when
  the received value is within / outside the new `ton_amount_tolerance_pct` setting
  (default **5%**). This is purely a decision aid — confirmation stays **manual**; no
  TON auto-confirm was added. Optional `toncenter_api_key` setting raises the read rate
  limit. The reader never blocks billing or the event loop and fails silently when the
  API is unavailable.

## 1.37.77 - 2026-06-16

### Changed (billing — multiple legitimate renewals in one month)

- **Several proper (day+volume) renewals of the same config in one month are now billed on real
  usage, not just once.** Before, the normal rule counted only the last package, so 3× renewing a
  10 GB config billed 10 GB (your loss). Now each closed cycle's **actual consumption** (capped at
  that cycle's sold quota) is banked and billed, while the final cycle is billed on sold quota like
  any present user. So fully-used renewals bill ~3×, but renewals the customer didn't use aren't
  over-charged — neither your loss nor the reseller's. A cycle that started a prior month isn't
  re-billed (already invoiced then). Tracked in a new `usage_meters.renew_used_gb` column
  (Alembic migration `b2d5e8f1a673`); applied to the real invoice AND the interim/report/GB-cap;
  it's normal usage, so it never triggers the abuse warning.

## 1.37.76 - 2026-06-16

### Changed (billing — closes a revenue loophole)

- **A deleted config that consumed real traffic is now billed its full sold quota.** Previously a
  user removed from the panel was always billed only on consumption, so a reseller could sell a
  50 GB config, let the customer use 30 GB, then delete it and be charged for just 30 GB. Now: if
  a deleted config's consumption is **at/above `deleted_full_quota_over_gb`** (Settings →
  قیمت‌گذاری, default **5 GB**), it's billed the full sold quota (e.g. 50 GB); below the cutoff it's
  still billed on consumption (small leftover / renew-by-delete); negligible usage (≤ free
  threshold) is still ignored. `0` disables (consumption-only, the old behaviour). Applied
  consistently to the real invoice AND the interim/sub-report/GB-cap so they stay in sync.

## 1.37.75 - 2026-06-16

### Fixed (critical — backup restore)

- **Restore now works onto a database that already has the schema** (i.e. always, since the
  backend creates the schema on boot). Previously `psql` aborted with «cannot drop constraint
  app_users_pkey … webauthn_credentials_user_id_fkey depends on it» — the dump's `--clean`
  drop-ordering failed on the passkey→owner foreign key, so the whole restore rolled back and
  did nothing. The restore now resets the `public` schema to a clean slate first (inside the
  same single transaction, so it's still all-or-nothing), then imports the dump. Verified by a
  real end-to-end test: a from-scratch isolated install was restored from a live backup and came
  up healthy with the **owner account, password, passkey, TOTP, all encrypted secrets, and all
  data preserved** (every row count + the owner-row hash + the SECRET_KEY matched production).

## 1.37.74 - 2026-06-16

### Changed

- **Payment tracking number («شمارهٔ پیگیری») is now an 8-digit non-sequential code** too, so
  customers can't infer how many payments the business has processed. Applied everywhere it's
  shown — the customer's payment ack/reject messages, the owner bot's pending-payment review,
  and the Payments panel (list + search). Search matches the new code; a different multiplier
  from the invoice code means a payment and an invoice with the same internal id get different
  numbers. Bijective (no collisions); covered by tests. No database change.

## 1.37.73 - 2026-06-16

### Added

- **8-digit invoice number** that hides the business volume. Instead of the raw sequential id
  (1, 2, 3…), every invoice now has a stable, unique, **non-sequential** 8-digit number — so a
  reseller can't tell how many invoices have ever been issued. It's shown on the invoice PDF, in
  the bot invoice message (tap-to-copy), and in the panel (a «شماره» column + the detail title).
  Pure derivation from the id (a bijection coprime to the 8-digit span → no collisions); no
  database change.

## 1.37.72 - 2026-06-16

### Changed

- **Live rates moved off the Dashboard and onto the Invoices page**, now showing BOTH the
  Tether (USDT) and TON applied rates with a refresh button that updates both live.
- **Configurable rate source** — a new «منبعِ نرخِ آنلاین» setting (Wallex / Tetherland);
  default is now **Wallex** for USDT (the other is the fallback). TON is Wallex-only.
- **TON gets its own manual/auto toggle** (`ton_rate_mode`) and a manual TON rate
  (`ton_toman_manual`), mirroring the USDT setting — and the TON online rate now follows it
  everywhere it's shown (invoice/payment displays). Settings shows a live TON-rate chip too.

## 1.37.71 - 2026-06-15

### Added

- **Live exchange-rate display.** A «نرخِ زنده» card on the Dashboard shows the current
  USDT→Toman (and TON→Toman when TON payment is on) rate with the last-update time, an
  auto/manual/stale badge, and a refresh button. The Invoices header also shows a compact
  «نرخ اعمالی» chip — the exact rate invoice generation will apply. Backed by a new read-only
  `GET /api/ops/rates` (no network I/O — reads the cached values). Rates come from Tetherland
  (USDT, primary) / Wallex (USDT fallback + TON), refreshed hourly, on demand, and before
  billing.

## 1.37.70 - 2026-06-15

Whole-codebase review fixes (no behavior change to the core money/enforcement logic — the
review confirmed those are correct).

### Fixed

- **Daily dunning report now lists the newly-enforced resellers.** `enforced_links` was built
  and returned but never populated (dead since enforcement became queued), so the "click to
  message" list was always empty. It's now appended whenever a real suspension is queued/done.
- **Payment deadline check uses Tehran time.** `_reseller_has_other_due` compared
  `deferred_until` against UTC `date.today()` while enforcement/dunning use Tehran — a
  near-midnight edge. Now uses the shared Tehran-local date.

### Changed (dead-code cleanup)

- Removed the unused `enforcement.restore_reseller` wrapper (callers use `queue_restore`).
- Slimmed `build_invoice_pdf` to its real (volume-only) parameters — dropped 10 long-ignored
  money/wallet args and the now-dead amount computations at the call sites.
- Removed dead frontend exports `CHART_COLORS` and `fmtCompact`, an unused `Divider` import,
  and the unused `visible` parameter on `crypto.mask`.

## 1.37.69 - 2026-06-15

### Added — admin bot overhaul (a real management tool from your phone)

- **Professional period-aware stats** (`/stats`): a KPI dashboard with a «این ماه / ماه قبل /
  دو ماه قبل» switch — total billed (vs last month), collected + collection-rate %, period
  outstanding, invoice/paid/debtor counts, billed services, and total debt — plus a **per-panel
  breakdown** with each panel's sales, invoice count, and sync health.
- **System health** (`/health`): panel sync status, in-flight enforcement queue, failed actions,
  pending payments, and last-backup time at a glance.
- **Pending-payment review from the bot** (`/payments`): list pending payments, tap one to see
  the receipt image + details, and **approve/reject** right there — the reseller is notified
  automatically. No need to open the web panel.
- **Reseller search + card + quick actions**: send a name → the reseller's card (panel, this
  month's sales, debt, capacity, status) with one-tap **suspend/restore, capacity bump
  (+100/200/500), recent invoices, and a direct Telegram chat** link.
- **Daily digest to the owner** (`daily_digest_*` settings, default 09:00 Tehran): a once-a-day
  summary of this month's KPIs, today's confirmed payments, total debt, and any health warnings.
- **Telegram column in the Resellers main list** — a clickable Telegram icon (before «پنل»)
  opens each registered main reseller's private chat in one tap (also on mobile cards). The
  reseller API now returns the Telegram `username`.

All numbers come from the new `owner_report` service (shared by the bot + digest) and match
the web panel; no data-model change.

## 1.37.68 - 2026-06-15

### Added

- **Owner alert on a stuck enforcement.** If a queued suspension/restore fails after its
  retries (e.g. a wrong panel API key), the owner is now notified on Telegram instead of it
  being only logged — so debt is never silently left uncollected nor a paid reseller left
  suspended.
- **Automatic log retention** so the database can't bloat over time. A new daily
  maintenance job (`daily_maintenance`, 04:30 local) prunes the three append-only
  log/audit tables — `sync_runs`, `delivery_log`, and terminal `enforcement_actions`
  (suspension/restore rows with their large JSON snapshots) — older than
  `log_retention_days` (Settings → زمان‌بندی, default **90**, min 7; `0` disables).
  It never touches the financial ledger or invoices, and preserves operationally-live
  rows: an owed invoice's reminder logs (dunning de-dup) and in-flight enforcement
  queue work are kept regardless of age. Replaces the previous enforcement-only
  30-day prune that ran on the queue worker (and never cleared dry-run rows).
- **`docs/DATABASE.md`** — a complete schema reference (every table, when rows are
  written, growth/retention, and the meaning of each column).

### Fixed (invoice correctness audit)

- **Interim («علی‌الحساب») now matches the real invoice.** The bot's interim breakdown,
  sub-reseller report, and GB-cap math previously summed only the snapshot quota rule and
  omitted the abuse-metered extra (overage + renew-by-edit) that the end-of-month invoice
  adds — so they under-reported whenever a reseller renewed by edit or reset usage. They now
  include the same metered extra, so the interim equals the final invoice for the period.
- **Concurrent invoice generation is serialized.** `generate_invoices` / `recompute_invoice`
  now take a Postgres transaction-level advisory lock, so the monthly scheduler run, a manual
  «صدور فاکتورهای دوره», and double-clicks can't race on the same `(reseller, period)`
  (previously the loser could abort the whole run with an integrity error). No-op on SQLite.
- **Guard against billing an incomplete month.** Generating invoices for the current or a
  future month (the period isn't over yet) now asks for confirmation, since the normal action
  is to issue the previous, completed month.

### Changed

- **Settings «متفرقه» eliminated.** Seven tuning settings that previously fell into the
  catch-all section with raw English keys now have proper Persian labels + help in their right
  category: log retention, online-rate max age, and the five enforcement/pending-hold knobs.
- **Help page completed/updated** for the newest features: log retention & auto-deletion,
  backup contents (DB + settings + key) / optional passphrase / atomic restore + auto-restart /
  what's not in the backup, the stuck-enforcement owner alert, the incomplete-month guard, and
  the interim-equals-final note.
- Removed the obsolete `e2e/` Playwright scaffolding (not part of CI).

## 1.37.67 - 2026-06-14

### Fixed

- **Favicon stayed purple after update** — the browser caches favicons by filename
  almost indefinitely, so the old purple icon persisted even though the files were
  already blue. Added a `?v=2` cache-bust to the favicon/apple-touch/manifest icon
  URLs so browsers fetch the new blue icon.
- **`theme-color` was still purple** (`#6d5efc`) in `index.html` and
  `site.webmanifest` — tinted the browser/PWA chrome purple. Changed to Apple blue
  (`#0071e3`); manifest `background_color` set to `#000000`.

## 1.37.66 - 2026-06-14

### Fixed

- Favicon/tab icon regenerated from the new Apple-blue SVG — browser tab now shows
  the blue icon instead of the old purple one.
- Resellers page: removed "نماینده اصلی" / "X زیرمجموعه" / "زیرمجموعه سطح X"
  sub-labels under each reseller name in both the flat list and the tree view.

## 1.37.65 - 2026-06-14

Apple-theme UI overhaul: liquid glass, unified pill controls, blue accent.

### Changed

- **Apple design system**: primary accent changed to Apple blue (`#0071e3` light /
  `#2997ff` dark); background `#f5f5f7` light / `#000000` dark; text `#1d1d1f` /
  `#f5f5f7`; Apple system success/error/warning colours throughout.
- **Liquid glass**: two-tier glass system — content surfaces at 7–36% opacity with
  `blur(40px) saturate(180%)`; floating overlays (dialogs, menus) at 78–84% opacity
  matching Apple nav-bar values extracted from apple.com. Dark mode removes the white
  inner gradient from `glassBgImage` eliminating the "foggy" tint on pure-black.
- **Unified pill controls**: `MuiOutlinedInput`, `MuiSelect`, `SegmentedTabs`
  container and tabs all set to `borderRadius: 50px` — same pill language as buttons
  (`980`) and chips. Multiline textareas keep `14px` for correct proportions.
- **Background**: replaced multi-colour ambient blobs with a single brand-accent glow
  from the top + dot grid. Dot grid later removed; single radial glow remains.
- **Double-glass fix**: `MuiTableContainer` override removed glass surface — the
  wrapping `Card` already provides it; double application caused the brownish fog on
  the Resellers table in dark mode.
- **Hardcoded purples removed**: `StatCard` default, `Layout` nav/logo gradient,
  `Dashboard` stat card and rank colours, `Login` icon — all updated to Apple blue /
  Apple system palette.
- **New logo**: `favicon.svg` and `icon-square.svg` redesigned — invoice document +
  blue checkmark badge on Apple-blue gradient with top gloss reflection.
- **Search placeholders**: colon and hash-tag prefixes removed; all search fields use
  clean `جستجوی X...` pattern with `SearchIcon` adornment.
- **SegmentedTabs**: new shared `SegmentedTabs` component used on Invoices, Logs,
  Resellers; replaces ad-hoc tab implementations for consistency.
- **Select/dropdown**: replaced `TextField select` with `Select displayEmpty renderValue`
  across Invoices, Payments, FinancialHistory, Broadcast — eliminates floating labels.
- **Panels page**: removed incorrect "(حداکثر ۱۰)" text.
- **AccountBackup**: split into three cards, placeholder text shortened to fit.

## 1.37.64 - 2026-06-14

Enforcement chunk-size fix, snapshot trimming, and old-row pruning.

### Fixed

- **Chunk size was always 100, not 500**: `settings_service.py` had the default for
  `enforcement_user_chunk_size` at 100, so the `or 500` fallback in `enforcement.py`
  never fired. The DB default is now 500, and a migration bumps existing installs from
  100 → 500 (only if the value was still at the old default).

### Changed

- **Snapshot trimmed on completion**: `panel_user_ids` (UUID→Hiddify-ID mapping cached
  for retry) is removed from the snapshot JSON when an action reaches `done`. This is a
  pure performance cache with no audit value; removing it saves ~100 KB per completed row.
- **Automatic pruning of old rows**: at the start of each enforcement worker tick, rows
  with status `done` or `reverted` that are older than 30 days are deleted. This keeps
  the `enforcement_actions` table from growing unbounded while preserving a month of
  audit history.

## 1.37.63 - 2026-06-14

Optimal enforcement: parallel admin limits + full chunk loop in one worker tick.

### Changed

- **Admin limit patching is now parallel**: all admins in a suspension or restore are
  patched concurrently via `asyncio.gather` with a configurable semaphore
  (`enforcement_admin_chunk_size`, default 10 concurrent). Previously each admin
  required a separate worker tick, so 20 sub-admins took 20 ticks × the worker interval.
- **All user chunks complete in one worker invocation**: the worker loops over every
  remaining chunk in a single call rather than returning after each chunk. Progress is
  committed after each successful chunk so a crash resumes from exactly the last
  committed point. A *failed* chunk still returns partial, deferring the retry to the
  next worker tick — the retry logic and `_MAX_RETRIES` guard are unchanged.
- **Default `enforcement_user_chunk_size` raised from 100 to 500**: fewer
  `quick_apply_users` calls per enforcement reduces load on the Hiddify panel.
- `enforcement_admin_chunk_size` now controls parallel concurrency (semaphore size)
  rather than a per-tick batch count.
- Tests updated to reflect one-tick completion; the partial-restore safety invariant
  (reseller stays `enforced` until all users are re-enabled) is preserved and tested.

## 1.37.62 - 2026-06-14

Hide removed resellers from UI counts and lists.

### Changed

- Resellers deleted from a Hiddify panel (absent from the latest successful sync) are no
  longer shown in the reseller list, hierarchy tree, or any admin count. Their DB rows
  are kept for billing history but are invisible in the UI.
- The panel card's reseller count now correctly excludes both the owner row and removed
  admins — it reflects exactly the live admins on the Hiddify panel.
- Dashboard and bot stats (`آمار کلی`) count only active (present) top-level resellers.

## 1.37.61 - 2026-06-14

Panel owner UUID migration fix.

### Fixed

- When a Hiddify panel backup is restored on a new server (changing the super-admin UUID),
  the next panel sync now deletes the stale owner reseller row instead of leaving an orphan
  `is_owner=True` entry in the database. The new owner row is created and all sub-resellers'
  `parent_admin_uuid` is updated automatically from the backup data.

## 1.37.60 - 2026-06-14

CAPTCHA contrast improvement and enforcement restore invoice status fix.

### Changed

- CAPTCHA image uses darker text with stroke rendering, lighter background, and reduced
  noise so characters are clearly legible without weakening bot resistance.
- Login page CAPTCHA container CSS forces full-opacity rendering regardless of the
  browser's forced-color or dark-mode filter.

### Fixed

- After a full enforcement restore completes, any invoice still in `enforced` status is
  now moved back to `overdue` so the dunning cycle can resume reminders normally.

## 1.37.59 - 2026-06-12

Queued, resumable enforcement restore.

### Changed

- Payment confirmation, invoice defer, panel restore, and bot restore now enqueue a
  durable restore action instead of holding the request until every panel write finishes.
- Restore runs in two safe phases: admin limits top-down in bounded batches, then users
  through Hiddify's native bulk Enable action.
- Added `enforcement_admin_chunk_size` (default 10). Manual suspension now also uses the
  durable queue.

### Fixed

- Paying or deferring during a partial suspension cancels that suspension and restores
  only users and limits that were actually changed.
- Queue workers re-check current debt before continuing an invoice-linked suspension.
- Restore progress survives restarts, retries transient failures up to five times, records
  missing users, and clears saved limits for the full reseller subtree.

### Verification

- Confirmed from Hiddify-Panel source that user bulk actions perform one SQL update and one
  `quick_apply_users`; admin limits have no bulk endpoint and use bounded PATCH batches.
- Live test-panel cycle: 2 users and limits 100/100 changed to disabled and 0/0, then
  returned to enabled and 100/100 with final reseller state `active`.
- Backend tests, Ruff, Mypy, frontend build/bundle budget, deploy-script syntax, and
  whitespace checks pass.

## 1.37.58 - 2026-06-11

Native Hiddify bulk enforcement.

### Changed

- Replaced one REST PATCH per end user with Hiddify's native Flask-Admin bulk
  Enable/Disable action. Each enforcement chunk now performs one bulk POST and triggers
  Hiddify's `quick_apply_users` only once for the whole batch.
- Added Hiddify user UUID-to-internal-ID discovery and persisted only the relevant mapping
  in the resumable enforcement snapshot.
- Manual enforcement, queued enforcement, and payment restore now share the same bounded
  bulk-user path. They never silently fall back to the high-load per-user PATCH path.
- New installations default to 100 users per enforcement batch; existing runtime values
  remain configurable and unchanged.

### Verification

- Inspected the official Hiddify-Manager/Hiddify-Panel source and confirmed that the
  public v2 REST API only supports single-user PATCH while the panel UI exposes a native
  multi-row action at `/admin/user/action/`.
- Live test-panel batches of 5 and 100 users succeeded; the 100-user batch completed in
  about 11 seconds with one POST.
- Drained the complete test queue: 2,105 existing users were verified disabled, one
  snapshot user had already been deleted from Hiddify, and all 21 affected admins were
  verified with zero user limits.
- Confirmed a manual payment through the real payment service and verified bulk restore
  end to end: the invoice became paid, the reseller became active, its limits returned to
  100/100, and all 56 snapshotted users were enabled again.
- Full backend tests, adapter regressions, lint/typecheck, frontend build, and whitespace
  checks pass.

## 1.37.57 - 2026-06-11

Queued enforcement worker for high-volume dunning.

### Changed

- Dunning no longer performs live suspension writes inline. When enforcement is enabled,
  unpaid invoices now create durable queued enforcement actions and return quickly.
- Added a scheduled enforcement worker that processes queued actions in bounded,
  resumable chunks, disabling users first and then zeroing reseller/admin limits.
- Added runtime settings for worker interval, queued action batch size, and user chunk size.
- Exposed queue progress in enforcement reports/logs and added an operations endpoint for
  manually running one worker pass.

### Fixed

- A prior dry-run enforcement record no longer blocks the first real live enforcement queue
  item after enforcement is enabled.

### Verification

- Full backend pytest, backend lint/typecheck, frontend typecheck/build, dependency audit,
  and diff whitespace checks pass.
- Local test panel verified a live queued enforcement from planned to partial chunks and
  final enforced state without running the full action inline.

## 1.37.56 - 2026-06-10

Reseller hierarchy sorting.

### Fixed

- Enabled all eight operational sort headers in the reseller hierarchy view.
- Sorted root resellers and each sibling group recursively so descendants remain attached
  to their parents instead of flattening or corrupting the hierarchy.
- Reset root pagination whenever the sort column or direction changes.

### Verification

- All backend, frontend, dependency, release-tool, and diff checks pass.
- A browser regression verifies sortable headers in both reseller views, and a populated
  hierarchy check confirms sorting changes sibling order without separating descendants.

## 1.37.55 - 2026-06-10

Reseller list sorting restoration.

### Fixed

- Restored sortable headers for panel, price per GB, capacity fill, sub-reseller
  permission, bot connection, enforcement status, and billing inclusion in the main
  reseller list.
- Reset pagination to the first page whenever the sort column or direction changes.
- Kept hierarchy rows in parent/child order because global sorting would break the tree.

### Verification

- Frontend typecheck, production build, dependency audit, and bundle checks pass.
- A browser regression verifies all eight operational headers are sortable and clicking
  the panel header changes the actual row order.

## 1.37.54 - 2026-06-10

Reseller hierarchy production follow-up.

### Changed

- Paginated the hierarchy by main reseller, rendering at most 25 root branches and their
  visible descendants per page instead of mounting the entire production tree at once.
- Removed admin UUIDs from visible reseller rows and mobile cards while preserving
  name-or-UUID search behavior in the API and search field.

### Verification

- Verified UUID search against production data without exposing the UUID in the UI.
- Frontend typecheck, production build, dependency audit, and populated hierarchy renders
  pass without React, console, page, or horizontal-overflow errors.

## 1.37.53 - 2026-06-10

Reseller list and hierarchy redesign.

### Changed

- Reorganized the Resellers page with a concise heading and current-result count,
  integrated name/UUID search, panel filtering, and segmented main-list/tree views.
- Rebuilt the main reseller table with compact panel, price, capacity, sub-reseller
  permission, bot connection, enforcement, billing, and action states.
- Replaced the old indented tree table with collapsible branches, visible hierarchy
  connectors, depth labels, descendant counts, cycle warnings, and expand/collapse controls.
- Added dedicated responsive reseller cards for mobile instead of relying on a collapsed
  desktop table.
- Preserved all existing edit, capacity, billing exclusion, sub-admin permission,
  enforcement, and restore operations. No manual “add reseller” action was introduced.

### Verification

- All backend tests, Ruff, mypy, and dependency checks pass.
- Frontend typecheck, production build, PWA generation, bundle checks, and dependency
  audit pass.
- Populated desktop list/tree and mobile tree renders have no React, console, page, or
  horizontal-overflow errors.
- Added a read-only Playwright scenario for switching between the two reseller views.

## 1.37.52 - 2026-06-10

Dashboard information and visualization redesign.

### Added

- Added active and healthy panel counts, period invoice count, distinct debtor count,
  previous-period sales, and a real sales comparison to the Dashboard API.
- Added a focused regression test covering current/previous period totals, panel health,
  reseller connectivity, outstanding debt, and ranking output.

### Changed

- Reorganized the Dashboard around four concise operational metrics, clean panel sales
  progress bars, an invoice-status donut with collection rate, and a responsive top-ten
  reseller ranking.
- Preserved the existing light/dark glass theme while improving information density,
  spacing, labels, empty states, and mobile layout.
- Added typed frontend contracts for the Dashboard response.

### Verification

- All backend tests, Ruff, mypy, and dependency checks pass.
- Frontend typecheck, production build, PWA generation, and bundle checks pass.
- Playwright rendered populated desktop and mobile dashboards without React, console,
  page, or horizontal-overflow errors.

## 1.37.51 - 2026-06-10

Login redesign and Dashboard production fix.

### Added

- Rebuilt the Login page as a minimal RTL split layout with the invoice-system branding,
  password visibility control, CAPTCHA, passkey login, and the supplied financial SVG.
- Added a full-page pale-blue background glow that starts at the right edge and fades
  through the center while preserving a clean white form area.

### Fixed

- Fixed the remaining production Dashboard `Minified React error #130`. Rolldown's
  CommonJS interop rendered the `echarts-for-react/lib/core` default import as an object
  instead of a React component.
- Replaced `echarts-for-react` with a small native ECharts React adapter that owns chart
  initialization, option updates, responsive resizing, and disposal directly.
- Updated Login E2E selectors for the new placeholder-based fields and submit button.
- Added a build guard that rejects any future `echarts-for-react` runtime in generated
  JavaScript.

### Verification

- Backend pytest, Ruff, mypy, and dependency checks pass.
- Frontend audit, typecheck, production build, PWA generation, and bundle checks pass.
- Playwright rendered all three Dashboard charts from the production bundle with no
  console or page errors, and verified the final Login layout.

## 1.37.50 - 2026-06-10

Dashboard chart runtime hotfix.

### Fixed

- Fixed the Dashboard route failing with `Class extends value undefined is not a
  constructor or null` after the white-screen hotfix. The chart libraries
  (`echarts`, `zrender`, `echarts-for-react`) must stay in one vendor chunk; splitting
  them across multiple chunks breaks zrender class initialization in the production
  Rolldown build.
- Raised the JavaScript chunk budget to 600 KiB to allow the single safe chart vendor
  chunk while keeping all chunks bounded.

### Verification

- Verified the production build and a Playwright Dashboard smoke test against the built
  SPA: Dashboard renders, no console/page errors are emitted.

## 1.37.49 - 2026-06-10

Production frontend white-screen hotfix.

### Fixed

- Fixed the admin panel loading as a blank white page after `v1.37.48`. The production
  build was splitting MUI/Emotion into unsafe vendor chunks and loading CommonJS icon
  wrappers through Rolldown, causing frontend runtime crashes before React mounted.
- MUI icons now import from the package's ESM build, and the manual `vendor-ui` split was
  removed so MUI/Emotion initialize consistently.

### Verification

- Reproduced the blank page locally from the `v1.37.48` production build.
- Verified the fixed production build with Playwright: `#root` renders the login form and
  no console/page errors are emitted.

## 1.37.48 - 2026-06-10

Audit remediation B10 — cleanup and documentation.

### Fixed

- Removed dormant enum values that had no producing workflow and added an Alembic migration
  that normalizes any legacy rows before the reduced enums are loaded.
- Removed the obsolete cold-payment fallback. If an invoice changes state while its payment
  flow is open, the submitted proof is rejected instead of being attached to another debt.
- Removed the one-off deleted-invoice development script and remaining import-placement and
  lambda-style Ruff suppressions.
- Updated Help, README, architecture, remediation tracking, and contributor guidance to match
  the current manual-confirmation, exact-invoice payment and verified-release workflows.

### Verification

- Backend tests include enum-contract and stale-payment-attribution regressions.
- Full Ruff, mypy, pytest, Alembic, frontend build/audit, Compose, release-tool and secret
  tracking gates pass.

## 1.37.47 - 2026-06-10

Audit remediation B09 — scheduler, deployment, and supply-chain hardening.

### Fixed

- Replaced repeating `*/N` cron expressions with fixed-anchor interval triggers, preserving
  true spacing for values such as 7 hours or 17 minutes without restart starvation.
  `rate_refresh_hours` now live-applies like every other schedule setting.
- Replaced the root-level mutable `main/get.sh` update path with exact GitHub Release
  archives verified by SHA-256. Applied archives are cached and tracked-file manifests remove
  stale release files safely.
- Added a tested offline rollback command and release packaging/verification tooling.
- Made `/health` database-aware and added a backend Compose health check. Caddy now waits for
  API/database readiness, and every production install runs a post-deploy version, container,
  health, and migration smoke check.
- Added hash-locked Python production/development manifests and made Docker/CI install with
  `--require-hashes`. Frontend moved to Node 22 and Vite 8; `npm audit` is clean.
- Pinned GitHub Actions and container base images by digest/commit SHA and added weekly
  Dependabot coverage.

### Verification

- Backend gate: 87 tests, Ruff, mypy, `pip check`, fresh lockfile installation, and Alembic.
- Frontend type/build and bundle budget pass with zero npm audit vulnerabilities.
- Release tooling test applies two versions, removes a stale file, and rolls back offline.

## 1.37.46 - 2026-06-09

Audit remediation B08 — build, test, and frontend quality gate.

### Fixed

- Added a maintained mypy configuration and resolved the high-signal typing failures in
  billing, payment, delivery, enforcement, API relationship loading, and ORM models.
  Untyped third-party packages and aiogram callback narrowing are isolated explicitly
  instead of globally suppressing type errors.
- Expanded Ruff from undefined-name checks to import, syntax, modernization, whitespace,
  ambiguous-name, and exception-chaining rules; CI now runs the full configured baseline.
- Normalized naive/aware snapshot timestamps before billing freshness comparisons, avoiding
  a runtime `TypeError` on SQLite/legacy timestamp rows.
- Added an isolated staging Compose stack bound to localhost with separate volumes, no bot,
  and scheduler jobs disabled.
- Split React, MUI, ECharts/zrender, data, and animation dependencies into bounded frontend
  chunks. The production build now fails when any JavaScript chunk exceeds 500 KiB.

### Verification

- Added an integrated billing → manual payment → ledger → backup workflow regression test.
- Backend gate: 83 tests, Ruff, mypy over 92 source files, and Alembic drift checks.
- Frontend production build/typecheck passes with every JavaScript chunk below 500 KiB.
- CI validates both production and isolated staging Compose configurations.

## 1.37.45 - 2026-06-09

### Fixed

- Preserved application logging when Alembic migrations run inside backend/bot startup.
  The migration environment now configures console logging only for direct Alembic CLI use,
  preventing startup migration from disabling normal service logs.

### Verification

- Added a subprocess regression test that runs the programmatic migration path and verifies
  that an existing application logger, level, and handler remain unchanged.

## 1.37.44 - 2026-06-09

Audit remediation B07 — database evolution and input contracts.

### Fixed

- Replaced startup `create_all` / ad-hoc `ADD COLUMN` evolution with versioned Alembic
  migrations. Fresh databases run the complete baseline; existing pre-Alembic databases are
  stamped only after every expected table and column is validated, then upgraded to head.
- Serialized backend/bot migration startup with a PostgreSQL advisory lock, preventing both
  processes from racing to migrate the same database.
- Added database check constraints for non-negative invoice, ledger, payment, reseller
  pricing/cap, and usage-meter values.
- Added strict API validation for non-negative invoice/reseller edits and capacity bumps.
  Runtime settings now use a known-key allowlist, strict value types, safe ranges, finite
  numeric values, read-only internal keys, and atomic bulk validation before writes.
- Reseller-tree construction now uses case-insensitive panel-scoped UUID identities, detects
  cycles, and surfaces malformed cyclic components without recursion failure or hidden rows.
- Replaced mutable Pydantic list defaults with `Field(default_factory=list)`.

### Verification

- Added migration tests for fresh install, safe adoption of an existing schema, rejection of
  an incomplete schema, input contracts, atomic settings, and cyclic reseller trees.
- Rehearsed the migrations against a restored clone of the production PostgreSQL database:
  revision `6a9c7f21d4e0`, 23 non-negative constraints, then removed the temporary database.

## 1.37.43 - 2026-06-09

Audit remediation B06 — bot identity, membership, and input safety.

### Fixed

- Forced channel/group membership is now checked for every private bot message, including
  direct slash commands and payment-state text/photos, not only inline button callbacks.
  `/start` remains available for join links and `/cancel` remains available to exit a flow.
  Membership-check failures now fail closed instead of granting access.
- Panel-link registration now requires one unique normalized `host + proxy path + UUID`
  match. Incomplete, mismatched, or ambiguous links are rejected instead of falling back to
  the first reseller with the same UUID.
- User names and support-message text are HTML-escaped before Telegram HTML rendering.
  The legacy invoice-template wallet placeholder is escaped as well.
- Bot and invoice API payment/deadline eligibility checks now use the same Tehran-local date
  helper, avoiding different results around UTC/Tehran midnight.

### Verification

- Added router-middleware, matching ambiguity, HTML-injection, and Tehran-date regression
  coverage in `tests/test_bot_identity_safety.py` and expanded `tests/test_matching.py`.

## 1.37.42 - 2026-06-09

Audit remediation B05 — enforcement and reminder consistency.

### Fixed

- A partial restore (some users fail to re-enable) now keeps the reseller **enforced** so the
  next trigger retries, instead of flipping to active and leaving those users disabled forever.
  The restore snapshot is preserved for the retry; the reseller is marked active only when every
  user re-enable succeeds.
- A pending (under-review) payment now pauses dunning only on **its own invoice** — not on the
  customer's unrelated invoices or other panels — matching the per-invoice payment model. The
  hold also **expires** after `pending_payment_hold_days` (default 7), so a stale, never-reviewed
  proof can no longer shield a debt indefinitely.
- The daily dunning report now distinguishes **delivered** reminders from merely **attempted**
  ones (a reminder that was blocked/unmatched/errored is shown as such, not counted as sent).
- The per-sub GB-cap monthly alert flag is armed only after the alert **actually reaches every
  configured recipient**; a transient Telegram failure is retried on the next check instead of
  being suppressed for the rest of the month.

### Verification

- Added `tests/test_enforcement_dunning.py`: partial-restore-stays-enforced-then-succeeds,
  per-invoice hold with expiry plus attempted-vs-delivered counting, and the GB-cap
  flag-only-after-delivery behaviour.

## 1.37.41 - 2026-06-09

Audit remediation B04 — billing and synchronization correctness.

### Fixed

- Billing now excludes any panel whose latest sync failed or never ran, instead of
  invoicing last month's stale snapshots. Skipped panels are reported in the generate
  result and the monthly job notifies the owner so the shortfall is never silent.
- A leftover DRAFT invoice whose reseller drops to zero usage (or is removed from the
  panel) is reconciled away when the period is regenerated, so a stale positive draft
  can no longer be delivered.
- A reseller (admin) removed from the panel is no longer billed forever — billing skips
  resellers not present in the panel's latest sync.
- The backup fetch now requires BOTH the `admin_users` and `users` collections (as lists,
  with a non-empty admin set). A truncated/partial backup fails the sync — which then
  excludes that panel from billing — instead of silently looking like every user/admin
  was deleted.
- In auto mode the exchange rate falls back to the manual rate when the cached live rate
  is stale (older than the new `rate_max_age_hours`, default 48h), so billing never uses a
  days-old quote when the source has been down.
- The "zero sale" preview now folds in the abuse-metered extra, so a reseller billed only
  on metered overage no longer shows up as a zero sale.

### Deferred (within B04, documented)

- Rendering the per-sub usage-breakdown PDFs from persisted invoice lines rather than live
  snapshots: the payable amount is always authoritative (rendered from the locked invoice
  row in the text and the line-based invoice PDF), and the per-node PDFs are GB-only usage
  breakdowns; reworking that pipeline is a larger, higher-risk change tracked as a follow-up.

### Verification

- Added `tests/test_billing_sync.py`: panel-billable gate, removed-reseller exclusion,
  partial-backup rejection, stale-rate fallback, and an end-to-end generate that skips a
  failed-sync panel and reconciles a zeroed draft while leaving the failed panel's draft intact.

## 1.37.40 - 2026-06-09

Audit remediation B03 — payment and invoice state machine.

### Fixed

- Invoice status transitions are now enforced in one place (`app/services/invoice_state.py`).
  The panel can no longer cancel/defer/edit a paid invoice, mark a draft or canceled invoice
  paid, or defer a draft/paid/canceled invoice — each returns a clear `400` instead of
  silently corrupting state. The invoice action buttons are gated to match.
- Confirming a payment for one invoice no longer restores a suspended reseller while other
  due (non-deferred) invoices remain — enforcement lifts only when the reseller has no other
  current debt. The same guard applies to the manual «ثبت پرداخت».
- Rejecting or deleting a payment no longer un-pays an invoice that another confirmed payment
  still settles.
- When a confirmed payment is reversed (reject/delete) or an invoice is un-marked paid, the
  invoice gets a fresh dunning window (reminder/warning marks cleared, `sent_at` re-anchored)
  instead of jumping straight back to overdue/enforcement on the next run.
- Reverting an invoice to unpaid clears the stale settling TXID from the durable financial
  ledger, so the ledger never shows a transaction hash against an unpaid invoice.
- A submitted TXID/receipt re-validates the chosen invoice under lock (still owned, still owed,
  not deferred to the future); a stale selection falls back to the oldest payable invoice
  instead of mis-attributing the payment.

### Verification

- Added regression tests (`tests/test_invoice_state.py`) for the transition matrix and
  operation guards, the multi-payment "don't un-pay an invoice settled elsewhere" rule, ledger
  TXID clearing + dunning reset on reversal, the restore-only-when-no-other-debt invariant, and
  the under-lock proof re-validation.

## 1.37.39 - 2026-06-09

Audit remediation B02 — backup, restore, and operational recovery.

### Fixed

- A backup is now refused (with a clear error) when no usable database image can be
  produced: a failed/empty `pg_dump` or an invalid SQLite file raises instead of shipping
  a dump-less archive that was previously reported as a successful backup.
- The scheduled backup job now notifies the owner on Telegram when an automatic backup
  fails, instead of failing silently.
- Postgres restore is now atomic: the import runs in a single transaction
  (`--single-transaction`, `ON_ERROR_STOP`), so a mid-restore failure rolls back and the
  live database is left exactly as it was — never half-dropped. A pre-restore safety dump
  of the current database is kept on disk before each restore.
- A restored `SECRET_KEY` is written to `.env` only after the database restore succeeds.
  A failed restore no longer leaves a new key against an unchanged database.
- Uploaded backups are validated before anything is read: archive size cap, member
  allowlist, per-member and total decompressed-size limits, compression-ratio (zip-bomb)
  guard, and `meta.json` shape.
- Blocking `pg_dump`/`psql` work runs off the request event loop (panel and bot restore).
- After a successful restore both the backend and the bot self-restart (via a shared
  restart marker) so neither keeps a stale `SECRET_KEY` or a handle to the pre-restore DB.

### Added

- Optional password-protected backups: set a `backup_passphrase` (Settings → زمان‌بندی) to
  encrypt every archive (PBKDF2 → Fernet). Restore then requires the same passphrase,
  entered on the panel restore form or read from the configured setting. Off by default —
  unencrypted self-sufficient cross-server restore is unchanged when no passphrase is set.

### Verification

- Added regression tests for dump/SQLite validation, passphrase encryption round-trip and
  wrong/missing passphrase, archive guards (stray member, oversize, zip bomb), the
  persist-key-only-after-success invariant on both failure and success paths, refusal to
  build a dump-less backup, encrypted-restore-without-passphrase, and the loop-free
  cross-process restart signal.

## 1.37.38 - 2026-06-09

### Security

- JWT authentication now fails closed with `503` when live account validation cannot
  reach the database; a signed token is never trusted by itself.
- Protected APIs require an active owner account plus matching mandatory `role` and
  `epoch` claims. Legacy or role-mismatched tokens are rejected.
- Password and passkey login reject non-owner accounts.
- New passwords enforce bcrypt's 72-byte UTF-8 limit with a controlled validation error.
- First-run setup is serialized with an in-process lock and a PostgreSQL row lock so
  concurrent requests cannot create multiple owners.

### Fixed

- Starting TOTP setup no longer overwrites an active authenticator secret. A replacement
  secret is stored separately and becomes active only after a valid confirmation code.
- Disabling TOTP clears both active and pending secrets.
- Existing databases receive the nullable pending-secret column through the current
  additive schema synchronization path.

### Verification

- Added regression coverage for DB failure, missing/mismatched JWT claims, inactive and
  non-owner accounts, bcrypt byte limits, TOTP replacement rollback/confirmation,
  concurrent setup, and legacy-schema column addition.

## 1.37.37 - 2026-06-09

### Fixed

- Made the Compose validation job self-contained by generating a temporary CI-only
  `.env` with the required dummy PostgreSQL password.

### Verification

- The preceding `v1.37.36` CI run proved backend and frontend jobs green and exposed
  the missing Compose interpolation input. This patch corrects that infrastructure-only
  failure without changing application behavior.

## 1.37.36 - 2026-06-09

### Fixed

- Restored a clean TypeScript build by typing invoice list responses and importing
  MUI's `PaletteMode` from its supported public entry point.
- Corrected the Playwright CAPTCHA locator to match the actual Persian image label.

### Changed

- Production frontend builds now run TypeScript checking before Vite.
- Docker frontend builds use the committed lockfile with `npm ci`.
- E2E tests require an explicit target and refuse production unless the operator
  deliberately enables a read-only production run.
- Added GitHub Actions checks for backend tests, Ruff, frontend type/build, deploy
  script syntax, and Compose configuration.

### Documentation

- Added a staged remediation tracker for the 2026-06-09 whole-codebase audit.
- Added a repeatable release, production deploy, smoke-check, and rollback process.
- Added local-only production operator metadata and Claude commands for batch fixes
  and releases.
- Corrected stale deployment and manual-payment documentation.

### Verification

- Backend: 36 tests passed; Ruff `F` checks passed.
- Frontend: TypeScript and Vite production build passed.
- Playwright: all 6 tests collected with an explicit non-production target.
- Deploy scripts passed `bash -n`; production Compose config validated.

## 1.37.35 - 2026-06-09

### Fixed

- Treated metering overage tolerance as a threshold: overage at or below the
  tolerance is ignored, while overage above it is billed in full.

### Verification

- Backend tests: 36 passed.
- Frontend Vite production build completed successfully.
- The later whole-codebase audit identified TypeScript and broader typing/lint debt;
  remediation starts with batch B00 in `docs/REMEDIATION_PLAN.md`.
