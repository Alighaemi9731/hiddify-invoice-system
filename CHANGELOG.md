# Changelog

All user-visible changes, important fixes, migrations, and operational notes are
recorded here from `v1.37.35` onward. Older detailed history remains available in
`CLAUDE.md` and Git commit/tag history.

## Unreleased

No changes yet.

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
