# Plan 006: Add credit-code analytics and durable communications

> Execute only after explicit approval and plans 003–005 are DONE. **Drift check**:
> `git diff --stat 2514a96..HEAD -- backend/app/models/storefront.py backend/app/services/storefront_credit.py backend/app/services/broadcast.py backend/app/bot/storefront/handlers.py backend/app/scheduler backend/app/api/portal_storefront.py backend/alembic backend/tests frontend/src/portal/storefront`
> Plans 002–005 will intentionally cause drift; this is a design blueprint. Re-read the released
> predecessor, regenerate exact schemas/file allowlist/commands and refresh the SHA before dispatch;
> replacing only the SHA is insufficient.

## Status

- **Roadmap item**: 3, Release E of F
- **Priority**: P2
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: plans 003–005
- **Category**: direction / migration
- **Planned at**: commit `2514a96`, mandatory reconcile after plan 005
- **Candidate release**: `v1.87.0`

## Why this matters

Credit codes affect wallet value, while current storefront broadcast is an untracked fire-and-forget
task. A trustworthy portal needs history-preserving code management and durable Telegram delivery
that survives process restarts and reports real progress. Telegram remains the transport; the portal
becomes the management and status surface.

## Required migration

- Add `archived_at` to credit codes; archive atomically sets `enabled=false`. Code text is immutable
  and its normalized value cannot be reused because the row/unique key remains. A pending top-up
  revalidates at confirmation; an archived/invalid code yields no bonus while the base top-up still
  confirms, and the audit records that decision.
- Add `storefront_broadcast_jobs`: shop, actor, `broadcast|direct`, segment, message text, status,
  counts, idempotency key,
  timestamps/cancel marker.
- Add `storefront_delivery_recipients`: job/customer/chat,
  `pending|sending|retry_wait|sent|blocked|failed|unknown|canceled`, attempt count, lease owner/expiry,
  next-attempt time, last error class and timestamps; unique job+customer. Direct messages use this
  exact job/recipient model with kind=`direct`, not a second outbox.
- Add scheduler/worker indexes and a 90-day recipient-detail retention setting in
  `settings_service.py`; prune through the existing maintenance scheduler. Preserve aggregate job
  totals and audit permanently after detail retention.

This is not CRM automation: no recurrence, funnel workflow, customer portal or broad campaign
attribution. Call these one-shot broadcast jobs, not campaigns; recurring campaigns/templates/
scheduling/conversion remain roadmap item 14.

## API contract

- Credit codes: cursor list/filter, create, partial edit, absolute enable, archive, usage summary and
  paginated redemptions. Enforce normalized tenant uniqueness and existing percentage/fixed/gift,
  cap, minimum, total/per-customer and date rules in shared services.
- Code text is immutable after creation. Before first redemption, economic fields and limits may be
  edited with audit/version checks. After any redemption, only `enabled` and `expires_at` may change;
  reports use each redemption's stored `bonus_toman`, never recompute history from current code data.
- Audience preview: `all|expired|inactive30|trial_no_purchase`, count and sample only.
- Broadcast create returns `202 {job_id,status:"queued"}`; status/cancel routes show persisted
  sent/blocked/failed/pending counts.
- Direct customer message returns queued delivery ID/status. Initial portal supports bounded plain
  text only; image uploads require a separately specified safe upload path.
- Worker uses the shop's server-side bot credential, shared flood control and bounded concurrency,
  closes temporary bot sessions and never holds DB connections during sends.
- Plain text is trimmed `1..4000` characters; preview sample is at most 20; one job is capped at
  50,000 recipients; direct messages are limited to 10 per owner/shop per minute using a DB-backed
  gate. Only redacted Telegram error classes are stored.

## Scope

**In scope**:

- credit archive/edit/usage shared commands and bot parity refactor
- broadcast-job/outbox models, migration, worker/scheduler, `settings_service.py`, maintenance
  pruning, retention and flood-control reuse
- portal credit-code, broadcast and customer-message APIs/UI
- bot broadcast path moved from `asyncio.create_task` to the same durable queue
- audit/idempotency, backend/PG/worker/frontend tests

**Out of scope**:

- Recurring/scheduled marketing automation, conversion attribution and CRM journeys
- Image/file campaigns in the first release, customer Mini App, email/SMS
- Authoritative profit/cost accounting
- Co-admin portal login and unrelated metering work
- Portal user creation (roadmap item 2 is rejected)

## Commands and git workflow

| Gate | Command | Success |
|---|---|---|
| Focused | `cd backend && .venv/bin/pytest -q tests/test_portal_storefront_credit.py tests/test_portal_storefront_communications.py tests/test_storefront_credit.py tests/test_broadcast.py` | all pass |
| Backend full | pip check, Ruff, mypy and full pytest | exit 0 |
| PG worker races | `pytest -q -m pg_contract` on PostgreSQL 16 | all pass |
| Schema | Alembic upgrade/check on SQLite and PG16 | no drift |
| Frontend | clean install, high audit, component tests and production build | exit 0 |
| Release | scheduler-disabled staging, worker recovery, tools/assets/checksums and production smoke | exit 0 |

- Branch: `feature/storefront-portal-3e-credits-comms` from plan-005 release commit.
- Migration and durable worker ship together; no CRM automation. Reviewer approval precedes release.

## Steps

### Step 1: Preserve credit history and centralize code commands

Add archive migration, edit validation and tenant-scoped usage queries. Refactor bot code CRUD to the
same command layer. Existing locked reservation/redemption remains authoritative.

**Verify**: used-code archive preserves redemptions; concurrent last-use succeeds once on PG16;
foreign/invalid/date/limit cases pass.

### Step 2: Add durable campaign/outbox state

Snapshot eligible non-banned customers when a campaign is created. Claim recipients in bounded rows
of 100 with `FOR UPDATE SKIP LOCKED` on PostgreSQL. A sending lease is 120 seconds; retry uses
Telegram `Retry-After` or exponential 5s/30s/2m/10m/30m, maximum five attempts. Sends occur outside
DB transactions. Terminal `sent|blocked|failed|canceled` rows are never automatically claimed again.
An expired `sending` lease becomes `unknown` and is retried automatically once, so delivery is
explicitly **at least once**: a rare duplicate is possible if Telegram accepted the message just
before the worker crashed. The UI and docs state this limitation. Cancel marks
`pending|retry_wait|unknown` only; an already claimed send may finish.

**Verify**: crash-before-send, crash-after-accept (possible duplicate documented), duplicate start,
two-shop credential isolation, 429 schedule, max-attempt failure, cancel and bounded-memory tests pass.

### Step 3: Refactor bot broadcasts and messaging

Bot and portal enqueue the same campaign/direct-message records. Bot keeps quick compose/notification
shortcuts and receives summaries, but no untracked background task remains.

**Verify**: existing segment/flood-control tests plus new bot/portal parity and durable-status tests.

### Step 4: Build credits and communications UI

Add code form/history/usage pages, audience preview, campaign composer/progress/history/cancel and
customer direct-message status. Make all money effects and delivery limitations explicit.

**Verify**: frontend tests cover code validation/archive, empty audience, idempotent start, progress,
blocked/failure, cancel, reload/resume and mobile layout.

### Step 5: Full migration/worker/release gates

Run PG worker concurrency under multiple processes, scheduler-disabled staging assertions, retention,
backup, release and production smoke with a dedicated harmless recipient—not a real broadcast list.

## Done criteria

- [ ] Credit CRUD never destroys redemption history and bot/portal share commands.
- [ ] Broadcast/direct delivery is durable, resumable, tenant-correct and flood-controlled.
- [ ] Terminal sent rows are never reclaimed; the documented at-least-once ambiguous-crash case is
      visible and covered by tests.
- [ ] Banned customers are excluded and cancellation affects unsent recipients only.
- [ ] All audit/idempotency/migration/worker/frontend/release gates pass after approval.

## STOP conditions

- Delivery cannot satisfy the documented at-least-once/lease policy without unbounded duplication.
- Current bot token lifecycle cannot safely be used from the backend worker.
- Credit archive cannot preserve redemption rows or the fixed pending-top-up rule above.
- Scope expands into CRM automation or requires touching unrelated dirty files.

## Maintenance notes

Campaign recipient detail needs explicit retention; aggregate totals and audit must remain after detail
pruning. Treat Telegram delivery as asynchronous, never as an HTTP request transaction.
