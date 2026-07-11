# Next-batch plan (post-v1.60.4 feature hardening)

Execution tracker for the 2026-07-11 advisor audit (4 parallel review streams, every
finding re-verified against the code) of the surface shipped AFTER the completed
hardening program — releases `v1.60.5`–`v1.69.0` (~2,000 changed lines: broadcast
audience filters, storefront expiry/trial/usage notices + shop-closed, payment receipt
PDFs, bulk payment-deadline extension, portal monthly sales chart, search ranking) —
plus lighter repo-wide security/perf/debt passes. **Planned at commit `0c6a9a6`
(`v1.69.0`).**

The dedicated security stream returned a **clean bill** on the new surface (bulk-defer
owner-gated + per-invoice state-validated, portal chart scoped to the caller's own
rows, parameterized search, tenant-scoped storefront handlers and expiry job,
sanitized receipt filenames, no tracked secrets, no injection sinks). This program is
therefore reliability/correctness only — five batches, no migrations, no schema
changes, backend-only (no frontend edits needed; the frontend gate still runs per
release).

Status values: `TODO`, `IN PROGRESS`, `DONE`, `DEFERRED`.

## Ground rules for every batch (executor instructions)

- **Drift check first**: `git diff --stat 0c6a9a6..HEAD -- <the batch's primary files>`.
  If an in-scope file changed since `0c6a9a6`, compare the excerpts quoted in the batch
  against the live code before starting; on a mismatch, STOP and report — do not
  improvise.
- One batch per release (PATCH bumps), in the documented order, via `/fix-batch N0x`
  and `docs/RELEASE_PROCESS.md`. Record user-visible notes in `CHANGELOG.md`.
- Every regression test added must FAIL on the pre-batch code (verify by writing the
  test first, or by a stash/branch check).
- No batch here adds an Alembic migration. If you find yourself writing one, STOP —
  that's out of scope for this program (and would require bumping the `HEAD` pin in
  `backend/tests/test_migrations_contracts.py`).
- A batch's "Out of scope" list is binding. If the fix appears to require touching an
  out-of-scope file, STOP and report.

## Release gate for every batch

```bash
cd backend && .venv/bin/pytest -q && .venv/bin/ruff check app tests alembic && .venv/bin/mypy app && .venv/bin/pip check
cd ../frontend && npm ci && npm audit && npm run build   # tsc + Vite + bundle budget
cd .. && bash -n deploy/*.sh get.sh && bash deploy/test-release-tools.sh
```

## N01 - Storefront notice sweeps: delivery reliability, bounded transactions, dedup

Priority: P1 (silently lost renewal reminders). Version: PATCH. Status: DONE in `v1.69.1`. No migration.
(Implementation notes: stamps are applied as ORM attribute writes + batched commits — not a
Core `update()` — so the identity map stays fresh for same-session dedup checks; the sweep
counters gained a `blocked` key, and `test_zero_threshold_disables_the_feature` was
re-baselined for it. The cross-tenant test is a guard — isolation was already structurally
sound, and the test passes pre-batch by design. The other three new tests fail pre-batch,
verified by stash-run.)

**Why.** The three proactive notices (near-expiry reminder, «trial ended → buy» nudge,
«~80% volume used» warning — `v1.66.0`) exist to drive renewals instead of silent
churn. Today any transient send failure permanently cancels the notice, large shops
will hit Telegram flood limits with no pacing, and each sweep holds one open write
transaction across its entire send loop.

**Current state** — all in `backend/app/services/storefront_expiry.py`:

- Stamp-on-any-failure: the send at `:160-170` is wrapped in a bare
  `except Exception` and the next line unconditionally runs
  `order.expiry_alerted_at = now`. Same pattern at `:263-266`
  (`trial_ended_alerted_at`) and `:359-362` (`usage_alerted_at`). The module
  docstring (`:12-15`) documents the *intent*: "The stamp is written even when
  Telegram rejects the send (blocked bot), so a blocked customer isn't retried daily
  forever." That intent covers `TelegramForbiddenError` only — the implementation
  also swallows `TelegramRetryAfter` (429) and network blips, so those reminders are
  never delivered and never retried (only a renewal re-arms the stamp).
- No pacing: the loops call `bot.send_message` back-to-back. Contrast the correct
  policy in `backend/app/services/broadcast.py:331-356` (`_RateLimiter` at
  `BROADCAST_RATE_PER_SEC = 25`, retry on `TelegramRetryAfter` up to
  `BROADCAST_MAX_RETRY = 3`, `TelegramForbiddenError` → blocked, no retry).
- One transaction per sweep: `notify_expiring` selects rows (`:108-121`), loops all
  sends, and commits once at `:171` (same shape at `:267`, `:363`). The scheduler
  (`backend/app/scheduler/jobs.py`, `storefront_expiry_job`) passes one
  `SessionLocal()` per sweep. A mid-run crash discards every stamp written so far →
  duplicate-notice storm next day; and a pooled DB connection is held for the whole
  send. The storefront purchase flow deliberately avoids this ("no DB connection held
  across the panel/Telegram I/O", `backend/app/bot/storefront/handlers.py:581-583`).
- Half-finished dedup: `notify_expiring:125-138` inlines a byte-for-byte copy of the
  extracted `_load_snaps` helper (`:183-197`) that the other two sweeps use; the three
  sweeps share a near-identical ~50-line skeleton (query → snapshot load → per-order
  loop → per-`sf_bot.id` bot cache → send → stamp → commit → close bots in `finally`).
- Minor: `_needs_alert` (`:69-73`) compares `last_renewed_at > expiry_alerted_at`
  raw, while its sibling `_usage_needs_alert` (`:289-294`) coerces both through
  `_aware()` ("SQLite loses tz"). Near-untriggerable (both columns are
  `DateTime(timezone=True)`), fix for symmetry while in the file.

**Fix (in this order):**

1. In `backend/app/services/broadcast.py`, extract the per-recipient send policy of
   `run_broadcast`'s inner `_send` (`:337-356`) into a reusable
   `async def send_with_flood_control(bot, chat_id, text, limiter, *, reply_markup=None,
   parse_mode=None, max_retry=BROADCAST_MAX_RETRY) -> str` returning `"sent" |
   "blocked" | "failed"`: acquire the limiter, send; on `TelegramRetryAfter` sleep
   `e.retry_after` and retry (budget `max_retry`, exhausted → `"failed"`); on
   `TelegramForbiddenError` → `"blocked"`; on any other exception → `"failed"`.
   Refactor `run_broadcast._send` to call it and translate the status into its
   existing `counts`/`_status` updates. `tests/test_broadcast.py` must stay green
   unchanged — it pins exactly this behavior (429-retry, blocked, retry-budget).
2. In `storefront_expiry.py`, collapse the three sweeps onto one private runner that
   preserves each sweep's public function, query, and message/keyboard builders
   (`_message_fa`/`_renew_keyboard`, `_trial_ended_msg`/`_buy_keyboard`,
   `_usage_high_msg`). Shape: each `notify_*` builds its `rows` (unchanged query),
   calls the shared `_load_snaps` (DELETE the inline copy at `:125-138`), computes its
   due list, and delegates the send+stamp loop to the runner, parametrized by a
   `due(order, snap) -> message-payload | None` callable, a
   `render(order, payload) -> (text, keyboard)` callable, and the stamp column name.
3. Two-phase send in the runner: materialize the due list fully (all needed
   attributes read) while the read transaction is open, then send with **no session
   I/O**, then stamp via
   `update(StorefrontOrder).where(StorefrontOrder.id.in_(batch)).values(**{stamp_attr: now})`
   + `commit()` in bounded batches (every 25 outcomes and once at the end) — so a
   crash re-sends at most one batch window, not the whole sweep, and no transaction
   spans Telegram I/O.
4. Outcome taxonomy in the runner, via `send_with_flood_control` with one shared
   `_RateLimiter(BROADCAST_RATE_PER_SEC)` per sweep run: `"sent"` → stamp;
   `"blocked"` → stamp (the documented blocked-customer case) and count under a new
   `counts["blocked"]`; `"failed"` → do **NOT** stamp (the daily job retries
   tomorrow) and count under `counts["failed"]`. Keep the existing counter keys
   (`checked/due/sent/failed`) and add `blocked`. Update the module docstring
   (`:12-15`) to the new semantics.
5. Route `_needs_alert`'s comparison through `_aware()` like `_usage_needs_alert`.

**Tests** (`tests/test_storefront_expiry.py`, extend `tests/test_storefront.py` for
trial/usage): the existing 7 expiry cases + the trial/usage cases must stay green.
Update `test_blocked_customer_is_stamped_not_retried`: `_FakeBot(fail=True)` currently
raises a generic `RuntimeError` — change it to raise
`TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="blocked")`
(construction pattern: `tests/test_broadcast.py:45,50`) and assert stamped +
`counts["blocked"] == 1`. NEW (must fail pre-batch): (a) transient failure (generic
`RuntimeError`, or `TelegramRetryAfter` past the budget) → NOT stamped,
`counts["failed"] == 1`, and a second run with the fake no longer failing delivers the
notice; (b) cross-tenant isolation — two storefront bots, one due order each, assert
each fake bot sent exactly its own customer's message and nothing else; (c) the 429
path — fake raises `TelegramRetryAfter(..., retry_after=0)` once then succeeds →
delivered and stamped in the SAME run.

Primary files: `backend/app/services/storefront_expiry.py`,
`backend/app/services/broadcast.py`, `backend/tests/test_storefront_expiry.py`,
`backend/tests/test_storefront.py`, `backend/tests/test_broadcast.py`.
Out of scope: `bot/storefront/handlers.py` (`sf_broadcast` is batch N04); an index on
`StorefrontOrder.status` (rejected — seq scan on a few-thousand-row table is noise);
any change to WHICH orders are due (`_days_left`, thresholds, query predicates).
STOP if: `tests/test_broadcast.py` fails after step 1 (the extraction changed
behavior); or the trial/usage tests in `test_storefront.py` turn out to assert
stamp-on-generic-failure semantics beyond the blocked case (re-baseline them in the
same spirit as the expiry test and say so in the release notes).

## N02 - Bulk-defer: skip invoices with missing context instead of aborting the batch

Priority: P2 (contract violation, low real-world frequency). Version: PATCH. Status: DONE in `v1.69.2`. No migration.
(Regression test constructs the dangling state by deleting a second panel row after its
invoice exists; verified failing pre-batch — old code 409s at `_invoice_context`.)

**Why.** `POST /api/invoices/bulk-defer` promises per-invoice skip semantics but a
single invoice whose reseller/panel row is gone aborts the entire batch with an
opaque 409 and rolls everything back. Atomicity is intact (nothing partially
applied) — the bug is the all-or-nothing failure contradicting the endpoint's own
documented contract. Likelihood is low (`Invoice.reseller_id` is
`ondelete="CASCADE"`, so reseller deletion removes its invoices on Postgres), but
`panel_id` has **no** cascade (`backend/app/models/invoice.py:49-52`), and the
codebase treats missing context as reachable (`_invoice_context` exists precisely for
it).

**Current state** — `backend/app/api/invoices.py`:

- `:373-397` `bulk_defer`: docstring says "a non-owed (draft/paid/canceled) or
  missing invoice is reported in `skipped`, never silently applied … One commit for
  the whole set." The loop catches `invoice_state.InvoiceStateError` (`:388-392`) and
  `inv is None` (`:385-387`), but line `:393` calls `_invoice_context(session, inv)`
  UNCAUGHT — and `_invoice_context` (`:81-88`) raises `HTTPException(409, "Invoice
  references a missing reseller or panel")` when either row is gone, aborting the
  request and discarding `done`/`skipped`.

**Fix:** in the `bulk_defer` loop, replace the `_invoice_context` call with inline
lookups and a skip:

```python
reseller = await session.get(Reseller, inv.reseller_id)
panel = await session.get(Panel, inv.panel_id)
if reseller is None or panel is None:
    skipped.append({"id": iid, "reason": "نماینده یا پنلِ فاکتور حذف شده است"})
    continue
```

Do NOT change `_invoice_context` itself — the single-invoice endpoints (`:367` and
others) intentionally 409.

**Tests** (`tests/test_bulk_defer.py`, model after
`test_bulk_defer_applies_to_owed_and_skips_others` at `:50`): seed two deferrable
invoices plus one whose `panel_id` points at a deleted/nonexistent panel row (create
the invoice, then delete the panel row directly via the session — SQLite in tests
does not enforce the FK, which is exactly what makes the state constructible); assert
the response applies the two good ones (`done == 2`), reports the dangling one in
`skipped` with the new reason, and the two good invoices actually carry the deadline.
Must fail pre-batch (currently raises 409).

Primary files: `backend/app/api/invoices.py`, `backend/tests/test_bulk_defer.py`.
Out of scope: `_invoice_context`, the single-invoice defer/mark-paid/cancel
endpoints, `schemas/invoice.py` (no cap on `ids` — single-owner-authenticated,
consciously accepted), frontend `Invoices.tsx`.
STOP if: the loop body at `:383-395` no longer matches the excerpt (drift).

## N03 - Broadcast audience fail-safety: threshold required, no silent fall-through to «all»

Priority: P2 (server-side footgun; currently mitigated by the frontend + preview). Version: PATCH. Status: DONE in `v1.69.3`. No migration.
(`test_invoice_above_filter` re-baselined with in-diff justification; the three behavioral
regressions — threshold-collapse, unknown-audience, validator-400 — fail pre-batch by
stash-run; the panel-scope and real-usage tests are coverage guards closing the TEST-03 gap.)

**Why.** Two independent gaps compose into "message every billable reseller by
accident": (1) `invoice_above` with a missing threshold matches *everyone* — the
dangerous direction (its siblings collapse to *nobody*); (2) `_matching_roots` ends
with `return roots  # "all"`, so ANY unknown audience string silently falls through
to the full base set — the exact failure the API validator's own docstring warns
about ("An unknown audience must NEVER silently fall back to «all»"), but the
service itself doesn't enforce it, and the bot path bypasses the API validator.

**Current state:**

- `backend/app/services/broadcast.py:203-215` (`_matching_roots`): `few_active` does
  `limit = int(threshold or 0)` (None→0→ matches nobody, safe direction);
  `invoice_above`/`invoice_below` do `toman = float(threshold or 0)`, and
  `invoice_above` then filters `amounts.get(...)[1] >= toman` — with None/0 that is
  `>= 0`, i.e. every billable root. `:215` `return roots  # "all"` is the unknown-
  audience fall-through.
- `backend/app/api/operations.py:81-92`: `BroadcastBody.threshold: float | None =
  None`; `_validate_audience` checks only membership in
  `broadcast_service.AUDIENCES`. Both `/broadcast` (`:252-265`) and
  `/broadcast/preview` (`:274-283`) call it and pass `body.threshold` through
  unvalidated.
- The BOT deliberately relies on the fall-through: `bot/handlers/broadcast.py:52`
  passes `audience="panel"` (not in `AUDIENCES`) with a `panel_id`, and the panel
  narrowing happens in `load_billable_roots` — "panel" reaching `return roots` is
  what makes it work. The bot's audiences (`all/debtors/overdue/deferred/zero_sale/
  panel`, `:19-25`) never need a threshold and always pass `threshold=None`.
- The frontend always supplies a default threshold for threshold audiences
  (`frontend/src/pages/Broadcast.tsx:85-87`, `Number(threshold || audDef!.threshold!.def)`),
  so requiring it server-side breaks nothing.
- `tests/test_broadcast_filters.py:155-163` currently PINS the bad behavior:
  `test_invoice_above_filter` asserts `invoice_above` with threshold `0` equals
  `all`. This test must be re-baselined by this batch.

**Fix:**

1. `_matching_roots`: make the terminal branch explicit —
   `if audience in ("all", "panel"): return roots`; any OTHER unknown audience →
   `log.warning(...)` and `return []` (never everyone). Keep "panel" documented
   inline as the bot-only alias whose narrowing lives in `load_billable_roots`.
2. `_matching_roots`: for `invoice_above` AND `invoice_below` AND `few_active`, treat
   `threshold is None or threshold <= 0` as match-nobody (`return []`) — the safe
   collapse for every caller including the bot. `new_resellers` keeps its documented
   30-day default (`:192-194`).
3. `operations.py`: extend validation — after `_validate_audience(body.audience)`,
   reject `audience in ("invoice_above", "invoice_below", "few_active")` with a
   missing or non-positive threshold via
   `HTTPException(400, "این فیلتر به مقدارِ آستانه نیاز دارد.")`, in BOTH `/broadcast`
   and `/broadcast/preview` (one shared helper, e.g. rename to
   `_validate_audience_and_threshold(audience, threshold)`). Update the
   `BroadcastBody` comment at `:83`.
4. Update the `AUDIENCES` comment block in `broadcast.py:76-92` to state the
   threshold requirement and the no-fall-through rule.

**Tests** (`tests/test_broadcast_filters.py`): re-baseline
`test_invoice_above_filter` — threshold `0`/`None` now yields `[]` (in-diff
justification comment: pre-N03 it equaled `all`); NEW (must fail pre-batch):
(a) unknown audience string (e.g. `"typo"`) resolves to zero recipients while
`"panel"` still resolves like `all` under a panel restriction; (b) two panels seeded —
`resolve_recipients(s, "debtors", panel_id=<A>, None)` returns only panel A's roots
(the `panel_id` combination is currently never exercised); (c) seeded real usage —
a reseller with sales above / below a real Toman threshold lands in
`invoice_above` / `invoice_below` respectively, and `zero_sale` catches the no-sales
root (the `_bundle_amounts` path with actual data); (d) API-level: the shared
validator raises 400 for `invoice_above` without threshold (call the helper
directly, matching how `_validate_audience` is unit-testable).

Primary files: `backend/app/services/broadcast.py`,
`backend/app/api/operations.py`, `backend/tests/test_broadcast_filters.py`.
Out of scope: `frontend/src/pages/Broadcast.tsx` (already sends defaults);
`bot/handlers/broadcast.py` (its audiences are threshold-free; verify by reading,
don't edit); the `_bundle_amounts` `preview_bundles` recompute cost (consciously
accepted — owner-initiated and preview-gated).
STOP if: any OTHER caller of `resolve_recipients`/`preview` passes audiences outside
`AUDIENCES ∪ {"panel"}` (grep first — as of `0c6a9a6` the callers are
`operations.py`, `bot/handlers/broadcast.py`, and tests).

## N04 - Storefront segmented broadcast through the shared flood-control sender

Priority: P2. Version: PATCH. Status: DONE in `v1.69.4`. No migration. Depends on: N01 (uses `send_with_flood_control`).
(The regression test drives `_sf_broadcast_bg` directly — 429-retried-then-delivered,
blocked-counted-loop-continues, admin summary with keyboard; on pre-batch code it fails
with AttributeError since the background sender did not exist and the old inline loop
dropped throttled recipients. The empty-segment race now answers «مشتری‌ای نیست» in the
handler as well, not only in the segment picker.)

**Why.** The reseller-facing shop broadcast (`v1.65.0`) reimplements fan-out as a
foreground loop inside the aiogram handler: the admin's bot is unresponsive for the
whole send (hundreds of customers × 50 ms floor = minutes), `except Exception: pass`
silently drops any recipient that hits a Telegram flood-wait, and there is no retry.
The owner-side `services/broadcast.py` already solves all of this; the storefront
copy has diverged into the weaker implementation.

**Current state** — `backend/app/bot/storefront/handlers.py:1874-1895`:

```python
@storefront_router.message(SF.broadcast, F.text)
async def sf_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    ...
    custs = await storefront.customers_in_segment(s, sf.id, segment)
    sent = 0
    for c in custs:
        try:
            await bot.send_message(c.telegram_id, rtl(text))
            sent += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    scope = _SEGMENT_FA.get(segment, "مشتری‌ها")
    await message.answer(rtl(f"📢 به {sent} نفر ({scope}) ارسال شد."),
                         reply_markup=kb.admin_reply_kb())
```

The owner-side pattern to mirror: `bot/handlers/broadcast.py:35-64` — resolve
recipients in the handler, `asyncio.create_task(<bg fn>)`, reply immediately with
«ارسال در پس‌زمینه شروع شد — N گیرنده», and send the final summary from the background
task when done.

**Fix:**

1. Add a module-level `async def _sf_broadcast_bg(bot: Bot, admin_chat_id: int,
   chat_ids: list[int], text: str, scope: str) -> None` in
   `bot/storefront/handlers.py`: build one
   `broadcast.  _RateLimiter(broadcast.BROADCAST_RATE_PER_SEC)`, loop `chat_ids`
   calling `broadcast.send_with_flood_control(bot, cid, rtl(text), limiter)` (from
   N01), tally `sent/blocked/failed`, and finish with
   `await bot.send_message(admin_chat_id, rtl(f"📢 به {sent} نفر ({scope}) ارسال شد."
   + <blocked/failed detail lines when nonzero>), reply_markup=kb.admin_reply_kb())`.
   Wrap the whole body in `try/except` with `log.exception` (a background task must
   never die silently — mirror `_bot_broadcast_bg`'s shape). No DB session inside.
2. Rework `sf_broadcast`: after resolving `custs` (unchanged, inside the session),
   extract `[c.telegram_id for c in custs]`, close the session scope, reply
   immediately «📢 ارسال به N نفر ({scope}) در پس‌زمینه شروع شد؛ خلاصهٔ نتیجه همین‌جا
   می‌آید.» with `kb.admin_reply_kb()`, then
   `asyncio.create_task(_sf_broadcast_bg(bot, message.chat.id, ids, text, scope))`.
   The tenant `bot` object is the long-lived polling bot for this storefront — safe
   to use after the handler returns; if the manager cancels the bot mid-send, sends
   fail and are counted, which is acceptable.

**Tests** (extend `tests/test_storefront.py`, model after the existing segmented-
broadcast tests there): test `_sf_broadcast_bg` DIRECTLY (await it — don't test the
`create_task` wiring) with a fake bot: (a) one recipient raises
`TelegramRetryAfter(..., retry_after=0)` once then succeeds → counted `sent`, message
delivered in the same run (fails pre-batch: the old loop drops it); (b) one recipient
raises `TelegramForbiddenError` → counted blocked, the loop continues to later
recipients; (c) the final summary message goes to the admin chat id with the counts.
Exception construction pattern: `tests/test_broadcast.py:45,50`.

Primary files: `backend/app/bot/storefront/handlers.py`,
`backend/tests/test_storefront.py`.
Out of scope: `storefront.customers_in_segment` and the segment definitions;
`sf_bcast`/segment-picker callbacks (`:1840-1871`); `services/broadcast.py` beyond
what N01 already added; any god-module split of `handlers.py` (recorded separately —
not this program).
STOP if: N01 has not shipped (no `send_with_flood_control` in
`services/broadcast.py`); or `sf_broadcast` no longer matches the excerpt.

## N05 - Portal sales-by-month: one lean aggregate instead of N×full node_report

Priority: P2 (reseller-facing hot path; grows with data volume). Version: PATCH. Status: DONE in `v1.69.5`. No migration.
(Implemented as planned: `_NodeCtx` + `node_months(_ctx=)` in reseller_report,
`_extra_from_rows` + `bundle_extra_many` in metering (bundle_extra now delegates),
portal calls `node_months`. Parity test — including a seeded UsageMeter abuse row —
fails pre-batch (AttributeError) and gates the money math; the multi-row and
delta-None endpoint tests close the TEST-04 gap as coverage guards.
`_billable_gb_with_metering` stays — interim_breakdown and gb_cap still use it.)

**Why.** The portal dashboard chart (`v1.67.0`) calls the full `node_report` once per
reseller row per request. Each call re-loads ALL of the panel's resellers, ALL
subtree end-user snapshots (thousands per panel), ~6 pricing settings, computes the
GB-cap/enabled-user section the chart never uses, and issues one `UsageMeter` query
PER MONTH (up to 12). A 3-row portal account at `months=6` costs ~40+ queries and
repeated full-table scans on every dashboard load.

**Current state:**

- `backend/app/api/portal.py:177-207` (`sales_by_month`): loops `for r in
  ctx.resellers: rep = await reseller_report.node_report(session, r, months=months)`
  and uses ONLY `rep["months"]` (aggregating `label/amount_toman/gb/new_services`),
  then computes `delta_pct = ... if prev > 0 else None`.
- `backend/app/services/reseller_report.py:294-349` (`node_report`): loads
  `node_descendants` (`:295` — full panel reseller scan), subtree snapshots
  (`:297-304`), six settings reads (`:306-312`), then
  `for p in _last_months(months): await _billable_gb_with_metering(...)` (`:315-319`)
  — whose `metering.bundle_extra` call is one `UsageMeter` query per month
  (`backend/app/services/metering.py:159-181`, filter `period_label == label`) —
  and finally the cap/enabled-users section (`:327-349`) the chart ignores.
- Money-parity constraint: these numbers are shown to resellers and must equal the
  bot's «فاکتور علی‌الحساب» / sub-report figures — i.e. remain byte-identical to
  `node_report`'s months. The refactor must be a pure hoist, not a re-derivation.

**Fix:**

1. In `reseller_report.py`, extract the per-node context loading from `node_report`
   (`:295-312`) into one helper (e.g. a small dataclass `_NodeCtx`: `descendants,
   uuids, users, price, free_threshold, excluded, psa, deleted_over, trial_uuids`)
   and have `node_report` consume it — behavior-identical, no output change.
2. In `metering.py`, split `bundle_extra`'s per-row math (the loop over `rows`
   applying `overage_tolerance_gb`, `edit_renewal_gb`, `renew_used_gb` rules) into a
   pure `_extra_from_rows(rows, free_threshold, overage_tol, exclude_user_uuids)`,
   then add `bundle_extra_many(session, panel_id, admin_uuids, period_labels,
   free_threshold, exclude_user_uuids) -> dict[str, dict]` that runs ONE query with
   `UsageMeter.period_label.in_(period_labels)`, groups rows by label in Python, and
   applies `_extra_from_rows` per label (reading `is_enabled` and
   `overage_tolerance_gb` once). Refactor `bundle_extra` to delegate to it with a
   single label — existing metering tests must stay green unchanged.
3. Add `reseller_report.node_months(session, reseller, *, months, _ctx=None) ->
   list[MonthSummary]`: build/accept the ctx, call `bundle_extra_many` once for all
   `_last_months(months)` labels, and assemble each month exactly like
   `node_report:314-325` (`_billable_gb_for_period` + extra GB/lines,
   `amount_toman = round(gb * price)`). Have `node_report` build its `by_month` via
   `node_months(..., _ctx=ctx)` so there is exactly ONE implementation of the month
   math.
4. In `portal.py:189`, replace the `node_report` call with
   `await reseller_report.node_months(session, r, months=months)` and iterate the
   returned list directly. Response shape unchanged.

**Tests** (`tests/test_portal.py`, model after
`test_sales_by_month_aggregates_and_delta` at `:621`; parity test in
`tests/test_invoice_interim_consistency.py` or a new `test_reseller_report.py` —
whichever matches the existing fixture style better after reading both):
(a) **parity, the gate for this whole batch**: a fixture with real usage AND a
seeded `UsageMeter` row (so the metering path is exercised) asserting
`node_months(...) == node_report(...)["months"]` exactly, for `months=3`;
(b) multi-row aggregation: a ctx with 2 reseller rows asserting `sales_by_month` sums
per-month values across rows; (c) `prev == 0` → `summary["delta_pct"] is None`.
(b) and (c) must fail pre-batch only if written against `node_months` — write (b)/(c)
against the endpoint (they close the TEST-04 gap regardless) and (a) against the new
function.

Primary files: `backend/app/services/reseller_report.py`,
`backend/app/services/metering.py`, `backend/app/api/portal.py`,
`backend/tests/test_portal.py` (+ the parity test file chosen above).
Out of scope: `node_report`'s cap/enabled-users section and its bot consumers
(`subs.py` views); `interim_breakdown`; `node_invoice`/`node_invoice_own`; any
caching layer; frontend `portal/` files.
STOP if: the parity test cannot be made to pass without changing a money figure —
report the discrepancy instead of adjusting either side; or `node_report`'s month
loop no longer matches the excerpt.

## Recommended order

`N01 -> N02 -> N03 -> N04 -> N05`

N04 depends on N01 (`send_with_flood_control`). N02/N03/N05 are independent of
everything else; N02 is the smallest and can be released whenever a quick batch is
convenient. One batch per release: `v1.69.1` … `v1.69.5` (all PATCH per
`docs/VERSIONING.md`).

**Program complete** (N01–N05, released `v1.69.1` → `v1.69.5` on 2026-07-11, each
deployed to production with a fresh pre-deploy `pg_dump` and smoke-checked). No
migrations were introduced. Deferred candidates for a future program are recorded in
the "considered and rejected" section above (frontend Vitest baseline, docs refresh,
storefront handlers split) plus the direction backlog below.

## Findings considered and rejected (do not re-audit)

From the same 2026-07-11 audit, verified against the code and rejected:

- **Case-sensitive UUID lookup in the `few_active` filter** (`broadcast.py:117-129`
  vs `resellers.py` `_usage_counts`): moot — since H05 (`v1.59.6`) ingest lowercases
  all admin/user uuids at the `parse_backup` choke point and the migration lowercased
  legacy rows; the two columns cannot diverge. The `func.lower()` in `_usage_counts`
  is defensive legacy.
- **"~8 operational settings have no panel UI"**: wrong — `Settings.tsx`'s «متفرقه»
  catch-all section (`:359-370`) auto-renders every non-hidden key; they're editable,
  just uncurated.
- **MUI v5 → v7 migration**: no EOL or security cost today; full RTL/theme regression
  surface with zero frontend tests. Defer until a needed component or advisory forces it.
- **Missing `StorefrontOrder.status` / `Payment.status` indexes**: sequential scans on
  few-thousand-row tables at daily/owner-action cadence are noise. Revisit only if
  order volume grows 10×.
- **Sales-amount broadcast filters recompute `preview_bundles`**
  (`broadcast.py:132-145`): accepted cost — owner-initiated, infrequent, preview-gated.
- **Receipt PDF with a None reseller name**: filename already guarded
  (`receipt_pdf.py:70` + `_safe_name`), send is best-effort post-commit; worst case is
  a silently skipped receipt for a nameless reseller.
- **`pytest-xdist` for the 454-test suite**: nice-to-have; MED risk of exposing
  fixture-ordering assumptions. Adopt opportunistically, not as a batch.
- **Frontend Vitest baseline + `Invoices.tsx` bulk-selection tests, docs refresh
  (`DATABASE.md` missing the 7 storefront/portal tables, `ARCHITECTURE.md` missing
  storefront+portal, CLAUDE.md router list), storefront `handlers.py` god-module
  split (1,935 lines, top churn)**: real findings, deliberately not selected for this
  program (owner decision 2026-07-11). Candidates for the next one.
- **Security stream**: clean bill on the whole post-`v1.60.4` surface (details in the
  program header above); nothing suppressed.

## Direction backlog (audited, grounded, not planned — owner deferred)

Recorded so the grounding isn't lost: (1) storefront management in the reseller
portal (M72's "dashboards" is the last big deferred item; `portal.py` has zero
storefront endpoints); (2) receipt PDFs for storefront customers (`receipt_pdf.py`
is never called from storefront code — asymmetry with `v1.62.0`); (3) an
enforcement-preview report ("what would enforcement do to today's debtors") to
de-risk enabling `enforcement_enabled`; (4) portal parity: broadcast-to-subs and
user-creation (both services exist; portal already manages subs). M72's
"owner-bot toggle" remains ambiguous — clarify intent before building.
