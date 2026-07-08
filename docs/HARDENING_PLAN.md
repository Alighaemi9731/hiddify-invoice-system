# Hardening plan

Execution tracker for the hardening program from the 2026-07-08 full-codebase review
(7 parallel review streams + deterministic tooling) against `v1.59.1` (successor to the
completed `docs/POLISH_PLAN.md`, P01–P05). ~60 verified findings: 6 must-fix
(money/availability), ~35 should-fix, plus nits and dead code. Bug fixes and
improvements only — no new features (owner decision). One batch per release; full gate
+ production smoke before the next. H05 and H06 are migration batches — released alone,
each MUST bump the `HEAD` pin in `backend/tests/test_migrations_contracts.py`.

Status values: `TODO`, `IN PROGRESS`, `DONE`, `DEFERRED`.

## Release gate for every batch

```bash
cd backend && .venv/bin/pytest -q && .venv/bin/ruff check app tests alembic && .venv/bin/mypy app && .venv/bin/pip check
cd ../frontend && npm ci && npm audit && npm run build   # tsc + Vite + budget
cd .. && bash -n deploy/*.sh get.sh && bash deploy/test-release-tools.sh
```

Every regression test added must FAIL on the pre-batch code (verify by stash/branch
check or by writing the test first). Payment/billing/migration batches (H01, H03, H05,
H06) additionally require the rollback + data-integrity review from
`.claude/commands/fix-batch.md`.

## H01 - Payment verification & submission integrity

Priority: P0 (money). Version: PATCH. Status: DONE in `v1.59.2`. No migration.

- `services/payments.py:846-852` (`verify_payment`): the `if not targets:` branch
  auto-confirms a payment whose invoice set has no owed member for ANY reason —
  including reverted-to-draft, canceled, or deleted invoices — burning the customer's
  unique txid (unresendable). Replace with a 3-way split: (a) any set member missing
  from DB or in `(draft, canceled)` → hold `pending` + note marker
  `"[needs manual review: invoice unpayable]"` (mirror the zero-amount guard at
  `:859-867`, same Persian wording style); (b) all members exist and all are `paid` →
  confirm (legitimately settled meanwhile); (c) any owed → existing flow.
- Cold reopen of a rejected txid (`:213-226`): currently flips rejected→pending with NO
  validation. Route the original coverage (`_settled_ids(existing)`) through the same
  per-invoice validation loop as the selection path (`:244-257`: ownership, `_OWED`,
  future-deferral) + the `_pending_invoice_ids_in_sets` one-pending check; on failure
  return `not_payable`, do not reopen.
- Lock the `existing` payment read at `:201` with `with_for_update()` and re-check
  `status == rejected` after acquiring (concurrent confirm-vs-resubmit race).
- Reopen must refresh `method` and `chain` from the current submission (`:274-284`) —
  a wrong-network resubmit currently keeps the old chain and the owner review keeps
  linking the wrong explorer.
- `mark_invoice_paid` (`services/payments.py:996-1005` + `api/invoices.py:200-225`)
  creates no Payment row, so `_settled_by_other_confirmed` can't protect the invoice
  and a later reject of an unrelated pending payment un-pays it. Fix: create a
  `Payment(method=PaymentMethod.manual, status=confirmed, settled_invoice_ids=…,
  amount_toman/usdt from the invoice, note="ثبت دستی از پنل")` + settlements rows.
  `PaymentMethod.manual` exists (`models/enums.py:41`, VARCHAR → no migration).
- Nits in the same files: lock invoices in `sorted(ids)` order in the `:244` loop
  (deadlock avoidance); `confirm_manually` must preserve the stored set order (don't
  rewrite `invoice_id`/set from DB-ordered `all_in_set`, `:1039-1058`); catch
  `IntegrityError` on the dup-txid insert race (`:304-306`) → map to `dup_pending`;
  don't overwrite `payment.amount_usdt` with the on-chain amount at `:815` before the
  decision is terminal.

Primary files: `backend/app/services/payments.py`, `backend/app/api/invoices.py`.
Tests: `backend/tests/test_usdt_deposit.py`, `test_payment_settlements.py` —
verify-holds-on-draft/canceled/deleted, confirms-when-all-paid, reopen-revalidates,
reopen-updates-chain-method, mark-paid-manual-row-protects-against-unpay,
concurrent-dup-txid-graceful.

## H02 - Enforcement mid-payment race + restore-source retention + queue lock

Priority: P0 (availability of paying customers). Version: PATCH. Status: DONE in `v1.59.3`. No migration.
(Implementation note: when the revert lands before ANY progress was committed, queue_restore
creates no restore — `_merge_into_pending_restore` therefore also CREATES the restore from
the source's final progress in that case, so post-copy chunks are always undone.)

- **Mid-payment race** (`services/enforcement.py:755, 848, 858-864, 981-990`): debt is
  checked once per tick; a payment confirmed mid-action makes `queue_restore` set
  `source.status = reverted`, but the worker's in-memory action object (sessions are
  `expire_on_commit=False`) finalizes `status = done` over it and stamps
  `inv.status = enforced` on a now-PAID invoice; the restore then flips `enforced` →
  `overdue` and dunning resumes against settled debt. Fix:
  - `_current_action_status(session, action_id)` — core-column
    `select(EnforcementAction.status)` (bypasses identity map).
  - Abort checks: top of each user-chunk iteration (`_run_user_chunks`, suspend/freeze
    actions only — a restore never self-aborts), between the user phase and the
    admin-limits phase, and immediately before finalize. On `reverted`: do NOT write
    status/snapshot; merge the just-completed chunk's progress into the pending restore
    (`_merge_into_pending_restore`: find the `restore` action whose
    `snapshot["source_action_id"]` matches and `status == planned` — the same panel
    lane is serial so it cannot have started; union users/limits, `flag_modified`,
    commit; if unexpectedly not `planned`, warn + owner ping instead of merging).
  - Harden `queue_restore`'s source read (`:557-580`) with `with_for_update()`.
  - Stamp `inv.status = enforced` only when `inv.status in invoice_state.OWED`; re-run
    the `_has_due_invoice` check in finalize for invoice-linked actions.
- **Queue serialization** (`:1352` + `api/operations.py:98-101`): `FOR UPDATE
  skip_locked` releases at the first mid-action commit, so a manual queue run
  overlapping the 5-min scheduler tick can double-run one action (and re-capture
  already-zeroed limits → restore-zeros, the M38 bug class). Fix: session-level
  `pg_try_advisory_lock` on a dedicated connection held for the whole
  `process_enforcement_queue` run (an xact lock dies at the first chunk commit); key
  constant adjacent to invoicing's `_BILLING_LOCK_KEY`; SQLite no-op (dialect check
  like `_serialize_billing`); when not acquired return an "already running" result the
  manual endpoint surfaces.
- **Prune guard** (`services/maintenance.py:127-134`): `prune_old_logs` deletes aged
  terminal `EnforcementAction` rows including the `done` disable/freeze action that is
  the restore SOURCE of a still-`enforced`/`frozen` reseller — after 90 days restore
  becomes permanently impossible (freeze is open-ended by design). Fix: exclude the
  newest live (`dry_run == False`) `disable_users`/`freeze` action per reseller whose
  `enforcement_state != active` (same pattern as the owed-invoice `delivery_log` guard
  directly above).

Primary files: `backend/app/services/enforcement.py`,
`backend/app/services/maintenance.py`, `backend/app/api/operations.py`.
Tests: `backend/tests/test_invoice_enforcement_fixes.py` (mid-flight payment via a fake
bulk-client hook on chunk 2: source stays `reverted`, paid invoice never `enforced`,
chunk-2 users present in the restore snapshot), `test_maintenance.py`
(prune-keeps-restore-source-for-enforced-reseller), PG-marked lock-skip test.

## H03 - Billing totals unification: recompute fee + fee-only months

Priority: P0 (money — recompute silently discounts). Version: PATCH. Status: DONE in `v1.59.4`. No migration.
(Decision: `preview_bundles`/«فروش صفر» intentionally still lists fee-only resellers as
zero SALES — the fee is not a sale; only invoice generation changed.)

- Extract shared helpers in `services/invoicing.py`: `_compute_totals(...)` (gb
  rounding, base amount, `_effective_min_sale` floor, `storefront.monthly_fee_for`,
  final `amount_toman`, users_count) and `_write_lines(...)` (base lines, metering
  extra lines, the storefront-fee `InvoiceLine`) — used by BOTH `_persist_bundle` and
  `recompute_invoice`. Fixes: `recompute_invoice` (`:343-357`) currently drops the
  storefront monthly fee and its line entirely (fee only exists in `_persist_bundle:429`).
- Fee-only months (`:182-184`): the zero-skip guard runs before `_persist_bundle`, so a
  reseller with an active storefront bot but zero billable GB gets NO invoice and a
  prior fee-only draft is reconcile-deleted. Skip only when
  `bundle.total_gb + extra <= 0 AND monthly_fee_for(...) <= 0`; fee-only invoices
  persist and enter `billed_reseller_ids` (reconciliation-safe). Mirror in
  `preview_bundles` so the zero-sale view doesn't list fee-only resellers as zero.
- Also in this batch: free-threshold epsilon consistency
  (`invoice_engine.py:178` → `<= free_threshold_gb + 1e-9`, matching `:127`); explicit
  `panel_id` must keep the `Panel.enabled` filter (`invoicing.py:136-138`);
  `discard_drafts` takes the `_serialize_billing` advisory lock
  (`api/invoices.py:101-121`); split the conflated `ValueError` mapping at
  `api/invoices.py:356-361` (panel-not-found ≠ paid-invoice); fee/adjustment lines
  excluded from breakdown user counts (`invoice_pdf.py:171`); single-bundle PDF
  (`invoice_pdf.py:34-66`) reconciled with the persisted-lines totals after manual
  edits (or documented).

Primary files: `backend/app/services/invoicing.py`,
`backend/app/services/invoice_engine.py`, `backend/app/services/invoice_pdf.py`,
`backend/app/api/invoices.py`.
Tests: new `backend/tests/test_invoice_totals.py` — recompute-keeps-fee(+line),
zero-usage-month-generates-fee-only-invoice, reconcile-keeps-fee-only-draft,
persist-vs-recompute agreement on identical inputs.

## H04 - Owner payment-review delivery (bot)

Priority: P0 (big pay-all payments invisible to the owner). Version: PATCH. Status: DONE in `v1.59.5`. No migration.

- New `send_owner_review(bot, chat_id, *, intro, review_html, photo=None,
  reply_markup=None)` in `app/bot/handlers/common.py`: builds `rtl(intro + review)`;
  with a photo, caption truncated to ≤1024 chars at a newline boundary (strip unbalanced
  trailing `<a` tag) and, when truncated, the FULL text follows as a second message
  (≤4096); on `send_photo` failure ALWAYS falls back to
  `send_message(full + "(ارسال تصویرِ رسید ناموفق بود؛ …)")`.
  Fixes the inverted condition at `bot/handlers/intake.py:296-303` (fallback keyed on
  the disk-save flag `if not saved` instead of the forward failure → owner got NOTHING
  when the file saved but the forward failed, e.g. a >1024-char pay-all caption) and
  the untruncated captions at `intake.py:289` and `owner.py:98`
  (`cb_owner_payment_view` — big payments unreviewable from the bot). Convert both call
  sites.
- `_finalize_review_message` (`owner.py:116`): `msg.html_text` is read OUTSIDE the try
  and `InaccessibleMessage` has no `html_text` — the AttributeError fires after
  confirm/reject committed, so the owner gets no ✅/❌ and the buttons stay tappable.
  Move it inside the existing try.
- Attribute the review to the payment's reseller row
  (`payment.reseller_id`), not `resellers[0]` (`intake.py:238, 288`).
- Clamp `link_tag` to 255 chars at parse (`bot/matching.py:92-94` — a longer fragment
  currently blows up the registration commit with no user reply).
- Catch the `_track_user` first-contact `IntegrityError` race
  (`bot/handlers/common.py:294-307`).

Primary files: `backend/app/bot/handlers/common.py`,
`backend/app/bot/handlers/intake.py`, `backend/app/bot/handlers/owner.py`,
`backend/app/bot/matching.py`.
Tests: `backend/tests/test_payment_review.py` (fake bot enforcing the 1024-char photo
caption cap): truncation+follow-up, fallback-fires-even-when-file-saved,
long-caption view path, inaccessible-message finalize.

## H05 - UUID case normalization (MIGRATION batch — released alone)

Priority: P1 (silent unbilling). Version: PATCH. Status: DONE in `v1.59.6`. **Migration: `b1c3e5a7f9d2`, HEAD pin bumped.**
(Rehearsed against a restored prod clone on Postgres 16: prod data is already all-lowercase,
so the migration is a verified no-op there — but the full path incl. the JSON snapshot
rewrite ran clean and row counts were preserved. Normalization added at the `parse_backup`
ingest choke point covers both the backup-JSON and admin-API adapters.)

- Billing compares uuids case-SENSITIVELY (`invoice_engine.py:76-114`
  `build_children_map`/`select_billable_roots`/`users_by_adder`;
  `metering.py:170-175`; `reseller_stats.py`; `gb_cap.py`) while the resellers tree
  (M54) and the persisted-line PDF paths lowercase — a case-mismatched
  `parent_admin_uuid`/`added_by_uuid` detaches a subtree from its bundle and it is
  silently never billed. Normalize at the sync choke point (`services/sync.py`):
  lowercase `admin_uuid`, `parent_admin_uuid`, `user_uuid`, `added_by_uuid` on ingest
  (map keys AND stored values, `_upsert_resellers` + `_upsert_users`); keep existing
  consumer-side `.lower()` calls as a belt.
- Data migration (data-only, in-Python):
  1. Case-duplicate resellers on `(panel_id, lower(admin_uuid))` (the unique constraint
     is case-sensitive, so `ABC`/`abc` may coexist): deterministic merge — keeper = more
     invoices → non-null `bot_chat_id` → newest `last_seen_at` → lowest id; repoint FK
     rows (invoices, payments, enforcement_actions, delivery_log, storefront_* by
     reseller_id); copy override fields onto the keeper where NULL; delete the loser.
     If both rows have a NON-draft invoice for the same period → abort loudly naming
     the rows (owner resolves; a migration never merges settled money).
  2. `end_user_snapshots` `(panel_id, lower(user_uuid))` dupes: keep latest
     `last_synced_at`. `usage_meters` dupes: keep the larger meter (never sum —
     double-billing risk).
  3. Lowercase pass on resellers/snapshots/meters/invoice_lines uuid columns.
  4. Rewrite uuid keys inside live `enforcement_actions.snapshot` JSON (users/admins/
     limits/progress) for non-reverted disable/freeze/restore rows.
  5. Downgrade: documented no-op (case cannot be restored); rollback = DB backup.
- Rehearse against a restored clone of production Postgres before release (M54
  precedent). Update `HEAD` pin in `backend/tests/test_migrations_contracts.py`.

Primary files: `backend/app/services/sync.py`, `backend/alembic/versions/` (new),
`backend/tests/test_migrations_contracts.py`.
Tests: new `backend/tests/test_uuid_case.py` (mixed-case parent/child bundles together —
fails before; metering/gb_cap keying), migration test with mixed-case + duplicate seed
(merge rule, FK repointing, constraint integrity).

## H06 - TON txid canonicalization (MIGRATION batch — released alone)

Priority: P1. Version: PATCH. Status: DONE in `v1.59.7`. **Migration: `c2d4f6b8a1e3`, HEAD pin bumped.**
(Rehearsed on a prod clone (PG16): 0 TON payments there → verified no-op; migration path ran clean.)

- A TON tx hash in HEX form is case-insensitive on-chain, but rows are stored
  case-sensitively (`payments.py:199-200`) — one real transfer submitted as `ABC…` and
  `abc…` creates two distinct rows under the unique constraint (double-settle risk via
  hurried manual confirm). Fix: after the `TON_TXID_RE` match, if the txid fullmatches
  `[0-9a-fA-F]{64}` → lowercase it. Base64(url) forms are case-SENSITIVE (different
  bytes) and must NOT be touched.
- Data migration: lowercase existing hex-form TON rows where possible; on collision
  (`ABC` and `abc` both present) keep the more-settled row (`confirmed` > `pending` >
  `rejected`; tie → lower id) on the canonical txid, set the loser's `txid = NULL`
  (unique allows multiple NULLs) + note-tag `"[duplicate txid; case-merged into #N]"`.
  Two CONFIRMED duplicates = an already-materialized double-credit: tag both notes
  `"[review: duplicate confirmed TON txid]"` + log a warning — a migration never
  changes payment statuses. Downgrade: no-op.

Primary files: `backend/app/services/payments.py`, `backend/alembic/versions/` (new),
`backend/tests/test_migrations_contracts.py`.
Tests: `test_ton_deposit.py` submit-dedup-across-casings (fails before); migration
collision test (keeper/NULL-loser/notes).

## H07 - PATCH null-vs-absent semantics + frontend forms

Priority: P1 (billing-relevant, false success). Version: PATCH. Status: DONE in `v1.59.8`. No migration.
(Panels PATCH left on `is not None` — its fields can't be nulled, so null = no-op is already
correct there; only the reseller override-clear path was the bug.)

- `api/resellers.py:500-512` (`update_reseller`) gates every field on `is not None`, so
  the explicit JSON `null` the edit dialog sends to clear
  `price_per_gb`/`min_sale_toman`/`storefront_monthly_fee_toman` («خالی = پیش‌فرض») is
  silently ignored while the UI toasts success; for `min_sale_toman` there is NO UI
  path back to the global default (0 is the distinct "no floor" state per M38). Fix
  with Pydantic v2: `data = body.model_dump(exclude_unset=True)`; presence =
  `"field" in data`; explicit null clears nullable overrides; null booleans are
  ignored. Apply the same pattern to `update_panel` (`api/panels.py:131-152`) for
  uniformity (+ test that `{"enabled": null}` is a no-op, not a crash). Settings API
  uses full key-value writes — verified unaffected.
- Frontend: `Settings.tsx:436` — an emptied numeric field stages `0`; make empty =
  untouched (drop from staged diff). `Invoices.tsx:415` — «حذف مهلت» relies on
  `setState` + `setTimeout(0)`; pass the cleared value into `mutate(value)` directly.
  Shared invalidation-keys constant (payments/invoices/dashboard/debts) used by both
  `Invoices.tsx:87-88` and `Payments.tsx:34`. `Invoices.tsx:314` colSpan 9→8.

Primary files: `backend/app/api/resellers.py`, `backend/app/api/panels.py`,
`backend/app/schemas/reseller.py`, `frontend/src/pages/Invoices.tsx`,
`frontend/src/pages/Settings.tsx`, `frontend/src/pages/Payments.tsx`.
Tests: new `backend/tests/test_reseller_patch.py` — null-clears-override (fails
before), absent-leaves-untouched, zero-min-sale-kept; frontend via tsc/build + local
visual harness on the reseller edit dialog.

## H08 - Enforcement & dunning correctness set

Priority: P1. Version: PATCH. Status: TODO. No migration.

- Failed live enforcement is never retried and blocks re-queueing forever
  (`enforcement.py:449-461` includes `failed` in the dedup but the worker only picks
  `planned/partial`): mirror `queue_restore`'s failed→planned reset (`:541-554`).
- Parent suspend/restore round-trip silently unfreezes/de-snapshots independently
  frozen or suspended descendants (`:341-385, 973-976`): exclude descendants whose own
  `enforcement_state != active` from the parent's limits phases and never clear their
  snapshots on the parent's finalize.
- Captured limit of 0 treated as missing (`:363-365` `lim.get("max_users") or …`):
  use `is not None` chains.
- `services/sync.py:118-119`: don't overwrite `panel_max_users`/`panel_max_active_users`
  while the reseller's `enforcement_state != active` (stale zeros feed the H02
  restore-zeros class and lie in the capacity UI).
- `api/invoices.py:399-422` (`revert_to_draft`): call `dunning.reset_cycle` and clear
  the invoice's reminder `DeliveryLog` rows so a revert→regenerate→resend gets fresh
  reminders instead of jumping straight to enforcement day.
- Manual dunning run vs daily job double-send (`api/operations.py:93-95`): advisory
  try-lock (H02's key family). Dry-run dedup narrowed to dry-run rows
  (`dunning.py:203-212`). Ledger: record overdue/enforced status flips via
  `financial_archive.record` at `dunning.py:192`, `enforcement.py:864, 990` (or, if the
  archive can't represent them, document the granularity in CLAUDE.md — decide
  in-batch).

Primary files: `backend/app/services/enforcement.py`, `backend/app/services/sync.py`,
`backend/app/services/dunning.py`, `backend/app/api/invoices.py`,
`backend/app/api/operations.py`.
Tests: `test_enforcement_dunning.py` (failed-requeues), `test_freeze.py`
(parent-restore-skips-frozen-child, snapshots intact), `test_billing_sync.py`
(sync-preserves-limits-while-enforced), `test_invoice_enforcement_fixes.py`
(revert-resets-dunning), zero-limits honest restore.

## H09 - Billing engine hardening + rates

Priority: P2. Version: PATCH. Status: TODO. No migration.

- Orphan/cyclic subtrees are silently unbilled while listed as main resellers
  (`invoice_engine.py:109-113` vs `reseller_stats.py:37-40`): after root selection,
  collect non-owner, non-excluded resellers unreachable from any billable root/owner
  into `GenerationSummary.unbilled_subtrees`; `generate_invoices` owner-pings when
  non-empty. (After H05, so case-orphans are gone and real orphans remain.)
- `storefront.py:50-58`: widen `trial_user_uuids` to ANY order that ever had a panel
  uuid (`provisioned`, `disabled`, `deleted`, `failed`-with-uuid) — a deleted/failed
  trial must stay a giveaway (its lingering snapshot currently gets billed via the
  deleted-user rule).
- `reseller_report.py:405-408`: pass `deleted_full_quota_over_gb` in the per-sub call
  (own call has it; subs under-report vs the real invoice).
- `exclude_from_billing` on a non-root is silently ignored
  (`invoice_engine.py:84-107`): BLOCK it — API 409 in `update_reseller` + hide the
  toggle for children in the frontend hierarchy (changing bundle math silently is
  riskier). CLAUDE.md wording updated in H13.
- `pdf.py:114-119` `gb()`: one decimal for fractional values (integer values stay
  clean) so PDF lines visibly sum to the total.
- Rates: TON/AVAX get the same `rate_max_age_hours` manual-fallback as USDT
  (`rates.py:150-160, 211-221` — cached auto rate currently returned forever; Wallex
  delisting froze it once already); `rates.py:264` `or 48` → `None`-aware so explicit
  0 disables staleness as documented. `metering.py:120-124` quota-decrease: document
  as intended (decreases are credit-free by design).

Primary files: `backend/app/services/invoice_engine.py`,
`backend/app/services/invoicing.py`, `backend/app/services/storefront.py`,
`backend/app/services/reseller_report.py`, `backend/app/services/pdf.py`,
`backend/app/services/rates.py`, `backend/app/api/resellers.py`, frontend hierarchy.
Tests: orphan-reported-not-silent, deleted-trial-still-excluded,
sub-interim-matches-invoice (deleted user ≥ cutoff), rate-staleness fallback
(TON/AVAX), pdf gb rendering.

## H10 - Storefront money & co-admin completion

Priority: P1 (double-credit). Version: PATCH. Status: TODO. No migration.

- `storefront_wallet.py:47-73` `confirm_topup` + `:89-107` `manual_adjust`: plain
  read-modify-write — owner + co-admin double-tapping the same pending top-up
  double-credits. Take `with_for_update` on the txn/customer rows + re-check
  `status == "pending"` after the lock (idempotent «قبلاً رسیدگی شده» on the loser).
  Pattern: `payments.confirm_manually:1033`.
- `storefront_provision.py:228-231`: step-3 refund is unguarded vs the pending-order
  reaper (double-refund, or provision-success-after-reaper-refund = free config). CAS:
  `UPDATE storefront_orders SET status=… WHERE id=… AND status='pending'` + the
  `order_has_refund` guard the reaper already uses (`:338`); only credit when the CAS
  won and no refund row exists.
- Co-admin completion (completes shipped v1.59.0 behavior): `_notify_admin`
  (`handlers.py:132-138`), the new-topup ping + proof photo (`:1117-1124`), and the
  provision-failure nudge (`:660-665`) go to the owner AND every id in
  `co_admin_ids` (best-effort per recipient; a blocked co-admin never aborts the
  others).
- Reaper-failed trial resets `free_trial_used` (customer can re-claim; trial has no
  refund path). Broadcast filters `banned` customers (`handlers.py:1701-1718` →
  `list_customers`, match the expiry sweep). `storefront.add_co_admin`
  (`storefront.py:103-116`): reject a currently-banned customer + enforce
  `telegram_id > 0` in the service layer.
- `storefront_expiry.py:60`: `periods.to_local_date(anchor)` instead of raw `.date()`
  (Tehran day-count — the v1.53.2 dunning bug class; 00:00–03:29 Tehran orders expire
  a day early and can skip the final reminder).

Primary files: `backend/app/services/storefront_wallet.py`,
`backend/app/services/storefront_provision.py`, `backend/app/services/storefront.py`,
`backend/app/services/storefront_expiry.py`, `backend/app/bot/storefront/handlers.py`.
Tests: `test_storefront.py` — concurrent-double-confirm-credits-once,
refund-race-single-credit, provision-success-after-reaper-refund, trial-re-eligible,
banned-broadcast-filtered, co-admin-notify-fanout, expiry tz boundary.

## H11 - Bot state hygiene & UX correctness

Priority: P1/P2. Version: PATCH. Status: TODO. No migration.

- `clear_stale_flow(state)` in `bot/handlers/common.py` (clears any active FSM state;
  no-op otherwise), called at the top of EVERY non-flow entry point: terminal slash
  commands (`commands.py:89-160` — `cmd_invoices/panels/portal/interim/subs/removelink/
  register`), `menu:*`/`inv:` callbacks (`reseller_cb.py:29-51`,
  `storefront_setup.py:362-403`), owner dispatchers. Restores the M46 "navigation
  clears PayState" invariant lost when the menu went inline — a stale
  `pay_invoice_ids` currently mis-attributes the next txid. NOT called from
  flow-continuation handlers (`paychain:`, pay selections, `setcap:`, `capok:`, `SF.*`
  wizard, broadcast, owner-reply) — enumerate in a comment.
- Router order: slash-command handlers register BEFORE FSM text handlers (owner typing
  `/broadcast` while in reply-state currently sends "/broadcast" to the customer as a
  support reply). Deliberately re-baseline `tests/fixtures/bot_router_inventory.json`.
- `iso_html(v)` helper (= `_iso(html.escape(str(v)))`) for panel-sourced strings in
  HTML sends (`views.py:78-80, 104, 473-478` — an admin name/link_tag with `<` breaks
  the parse and the flow appears dead).
- `_safe_int(cb.data, idx)` for the ~20 raw `int(cb.data.split(":")[1])` callback
  sites (forged data → graceful guard like `cb_pay_invoice:491-494`, not a crash).
- Assorted: `cmd_owner_action` gates `_reshow_menu` on `_OWNER_TERMINAL`
  (`owner.py:487-496` — `/payments` buries its own picker); `cb_rm` double-answer
  (`storefront_setup.py:685`); `cb_check_membership` answers + tolerates
  "message is not modified" (`reseller_cb.py:14-27`); gb_cap input clamp
  (`subs.py:193-237`, Integer column overflow); non-owner owner-commands get an
  explicit denial instead of silence; stale `paychain:` tap must not abort a FRESH pay
  flow (`storefront_setup.py:650-653` — check the held-hash presence first);
  `cb_invoice_view` payability check before rendering the pay-CTA text (paid invoice
  currently re-shows «مبلغ قابل پرداخت»).

Primary files: `backend/app/bot/handlers/` (common, commands, reseller_cb,
storefront_setup, owner, views, subs), `backend/tests/fixtures/bot_router_inventory.json`.
Tests: `test_bot_ux.py` — stale-pay-selection-cleared-by-navigation (fails before:
pending payment lands on the old set), router-inventory re-baseline with in-diff
justification, `_safe_int`/paychain/invoice-view cases in `test_bot_resilience.py`.

## H12 - Security & deploy hardening

Priority: P2. Version: PATCH. Status: TODO. No migration. Deploy-sensitive — verify the
setup wizard + HTTPS still work in production smoke.

- `core/loginsec.py:31-71`: `_buckets` grows without bound (a bucket per failed
  `(username, IP)` and per username, evicted only on successful login) — add
  opportunistic eviction of expired windows/lockouts + a hard size cap. Per-IP rate
  limit on `GET /api/auth/captcha` (`api/auth.py:63-66` — unauthenticated PIL PNG
  amplifier), reusing the bucket machinery.
- `deploy/Caddyfile:14`: `admin 0.0.0.0:2019` is reachable from every compose
  container. Scope it: dedicated backend↔caddy network (backend keeps `CADDY_ADMIN`
  for `domain_setup`) so frontend/db/bot can't rewrite the proxy.
  Add a caddy healthcheck to `docker-compose.prod.yml` (only service without one).
- `backend/Dockerfile`: add a non-root `USER` (chown `/app/data`; verify the
  bind-mounted `.env` stays readable and the `../update` flag dir stays writable in
  coordination with `install.sh`).
- `deploy/bootstrap.sh:35-48` root-runs mutable `main` (contradicts B09): retire it;
  rewrite the README section around the checksum-verified `release-installer.sh`.
- `deploy/rollback.sh`: print a migration warning (H05/H06 make schema-behind-code
  real); reference the pre-upgrade pg_dump. `install.sh`: create `.env` with 600 perms
  BEFORE writing secrets (`install -m 600 /dev/null`). `updater.sh`: cap/rotate
  `update/update.log`. `deploy/README.md`: fix the stale backup-passphrase paragraph
  (shipped in M49) + configurable interval.

Primary files: `backend/app/core/loginsec.py`, `backend/app/api/auth.py`,
`deploy/Caddyfile`, `deploy/docker-compose.prod.yml`, `backend/Dockerfile`,
`deploy/{bootstrap.sh,rollback.sh,install.sh,updater.sh,README.md}`.
Tests: `test_auth_hardening.py` (bucket eviction/cap, captcha limit); extend
`deploy/test-release-tools.sh` (rollback warning, compose healthcheck presence);
documented manual check that Caddy admin is unreachable from the bot container.

## H13 - Docs, help & dead code (final)

Priority: P3. Version: PATCH. Status: TODO. No migration.

- `frontend/src/pages/Help.tsx`: add the missing storefront section (owner enable +
  monthly fee in the reseller dialog, `/storefront` wizard, plans/top-ups/trials,
  v1.59.0 co-admins) — the "complete guide" currently has ZERO storefront coverage;
  portal Help adds AVAX (`portal/pages/Help.tsx:24,31`); pay-all ordering emphasis
  (`Help.tsx:223`).
- `services/invoice_state.py`: `ensure_transition`/`can_transition`/`_TRANSITIONS`
  have zero production references — either wire `ensure_transition` into the direct
  `inv.status =` writes (dunning `:192`, payments `:1000/:1019`, enforcement) or
  delete them (decide in-batch); unify the duplicated `_OWED` tuples (`payments.py`,
  `bot/handlers/common.py:236`) → import `invoice_state.OWED`.
- Frontend dead code: delete `client.ts:255` `verifyPayment` (dead since v1.37.83;
  confirm the backend `/verify` endpoint's remaining callers before touching it);
  remove `Invoices.tsx:4-5` unused imports; enable `noUnusedLocals` in
  `tsconfig.json` (CI'd via the build). Cancel-invoice: the endpoint + `canceled`
  filter exist but no UI trigger since M43 — restore the button in the invoice actions
  (state-gated via `ensure_can_cancel`).
- Bot dead code: remove the unused docked reply-keyboard builders
  (`keyboards.py:112-138`) + the unreachable `"monthly"` dispatch branch
  (`views.py:150-163`); refresh the M69 docstrings, `settings_service.py:48-51`
  pay-CTA comment, and CLAUDE.md corrections (M69 persistent-keyboard claim,
  `exclude_from_billing` wording per H09, collect_descendants BFS/DFS docstring).
- Mark this plan complete; move the program summary into CLAUDE.md's archive line.

Primary files: `frontend/src/pages/Help.tsx`, `frontend/src/portal/pages/Help.tsx`,
`frontend/src/api/client.ts`, `frontend/tsconfig.json`,
`backend/app/services/invoice_state.py`, `backend/app/bot/keyboards.py`,
`backend/app/bot/handlers/views.py`, `CLAUDE.md`, this file.
Tests: build gate with `noUnusedLocals`; `test_invoice_state.py` extended if wired;
router inventory if handlers removed.

## Recommended order

`H01 -> H02 -> H03 -> H04 -> H05 -> H06 -> H07 -> H08 -> H09 -> H10 -> H11 -> H12 -> H13`

H01 before H02 (H02's owed-guard builds on corrected verify/confirm). H05 before H09
(case-orphans must be gone before orphan detection is meaningful). H05/H06 released
alone (migrations). H02's advisory-lock key family is reused by H08. H12
second-to-last (deploy-sensitive); H13 closes the program.
