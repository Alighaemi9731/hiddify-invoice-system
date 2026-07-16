# Plan 004: Add tenant-safe customer and subscription management

> Execute only after explicit approval and plan 003 is DONE. The reviewer maintains the index.
> **Drift check**: `git diff --stat 2514a96..HEAD -- backend/app/services/storefront.py backend/app/services/storefront_provision.py backend/app/services/storefront_subscription.py backend/app/services/storefront_admin.py backend/app/api/portal_storefront.py backend/app/schemas/portal_storefront.py frontend/src/portal/storefront backend/tests`
> Plans 002–003 will intentionally cause drift; this is a design blueprint. Re-read the released
> predecessor, regenerate exact schemas/file allowlist/commands and refresh the SHA before dispatch;
> replacing only the SHA is insufficient.

## Status

- **Roadmap item**: 3, Release C of F
- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: plan 003
- **Category**: direction / security
- **Planned at**: commit `2514a96`, mandatory reconcile after plan 003
- **Candidate release**: `v1.85.0`

## Why this matters

The bot exposes customer search/detail, ban, service status, free admin renewal, pause/resume and
delete, but ownership is often established only in handler helpers. A public API cannot safely call
the low-level order services with a global ID. This slice adds a 360-degree customer surface and
hardens every panel operation so tenant authorization is part of the shared command itself.

## Current-state constraints

- `storefront.list_customers_page` provides offset search but large web lists need stable keyset
  pagination. Numeric Telegram ID search is exact; substring name search should require at least
  three characters.
- `storefront_provision.live_status` performs Hiddify I/O. Never call it N times from list pages.
- `storefront_subscription.renew/set_enabled/delete_subscription` currently accept order ID and rely
  on the caller for ownership. Add expected shop/actor context inside the command path.
- Admin renewal is free but still changes a real panel. The browser idempotency key must become the
  durable operation ID; a retry must not grant a second renewal.
- Hiddify and Telegram I/O must occur without holding the request DB connection.
- Wallet/manual-adjust and top-up decisions remain plan 005; this release is read-only for ledger.
- Add one measured migration for customer/order keyset indexes:
  `(storefront_bot_id, created_at, id)` and `(customer_id, status, created_at, id)`. Do not add
  trigram indexes unless `EXPLAIN ANALYZE` on a production-shaped fixture proves they are needed.

## API contract

Under `/api/portal/storefronts/{shop_id}`:

- `GET /customers?q=&banned=&activity=&has_service=&cursor=&limit=1..100`
- `GET /customers/{customer_id}`: identity, activity/trial/ban, balance, LTV, service counts and
  recent redacted ledger; never expose internal file paths or subscription links.
- `GET /customers/{customer_id}/ledger?kind=&status=&from=&to=&cursor=` (read-only)
- `PATCH /customers/{customer_id}/status` with absolute `banned` and mandatory reason.
- `GET /customers/{customer_id}/orders?status=&cursor=`
- `GET /orders/{order_id}` using stored order + batched snapshot data.
- `POST /orders/{order_id}/refresh`: explicit, rate-limited live Hiddify read.
- `POST /orders/{order_id}/renew`: free admin renewal, idempotent and audited.
- `PUT /orders/{order_id}/enabled`: absolute state, idempotent and audited.
- `DELETE /orders/{order_id}`: confirmation string/reason, idempotent and audited.

All child-resource IDs re-establish the chain child → customer → shop and foreign IDs return 404.
No endpoint may trust a `customer_id` or `order_id` merely because it came from the current page.

Contracts are fixed as follows:

- Cursor is URL-safe base64 JSON `{created_at,id}` plus HMAC using the server secret; invalid,
  modified or wrong-endpoint cursors return 422. Default limit 25, maximum 100. All lists order by
  `(created_at DESC,id DESC)`.
- `banned` is `true|false`; `activity` is `active30|inactive30`; `has_service` is `true|false`;
  order status is `pending|provisioned|disabled|failed|deleted`; ledger kinds/statuses use only the
  model's documented enums. Date span is at most 366 Tehran calendar days.
- `net_ltv_toman` is absolute paid purchase debits minus positive refund and renewal-reversal rows;
  top-ups, gifts and manual adjustments are excluded. It may not be guessed from plan/order prices.
- Ban/unban reason is trimmed `3..255` characters. Order delete requires literal confirmation
  `DELETE` plus a `3..255` reason. List/detail DTOs do not return `sub_link`; subscription delivery
  remains through the customer bot.
- Live refresh returns stored snapshot plus `freshness=live|stale|unknown` and an external error
  class, never a raw panel error. Rate limit is one owner refresh per order per 30 seconds: lock the
  order row, check/insert its audit timestamp, then release the DB session before Hiddify I/O.
  Exceeding it returns 429 with integer `Retry-After`; the DB-backed check works across processes.

## Scope

**In scope**:

- reporting/customer query services and tenant-aware customer/order commands
- hardening `storefront_subscription` and relevant `storefront_provision` entry points
- bot customer ban and admin subscription handlers migrated to shared commands
- portal customer list/detail/ledger/order pages and live-refresh control
- audit/idempotency integration from plan 003
- focused API, service, bot-parity and PostgreSQL concurrency tests
- migration/model/HEAD tests for the measured keyset indexes

**Out of scope**:

- Manual wallet credit/debit and top-up decisions (plan 005)
- Credit-code management and broadcast/direct messaging (plan 006)
- Customer portal/Mini App, user creation and co-admin web access
- Bulk panel operations or live refresh of every row
- Pre-existing metering changes

## Commands and git workflow

| Gate | Command | Success |
|---|---|---|
| Focused | `cd backend && .venv/bin/pytest -q tests/test_portal_storefront_customers.py tests/test_portal_storefront_orders.py tests/test_storefront.py` | all pass |
| Backend full | `pip check`, Ruff, mypy, full `pytest -q` from `backend/` | exit 0 |
| PG contracts | `pytest -q -m pg_contract` against PostgreSQL 16 | all pass |
| Schema | Alembic upgrade/check on SQLite and PostgreSQL 16 | new HEAD, no drift |
| Frontend | `npm ci`, high-level audit, `npm test -- --run`, `npm run build` | exit 0 |
| Release | Alembic drift, deploy tools, assets/checksums, staging and production smoke | exit 0 |

- Branch: `feature/storefront-portal-3c-customers-orders` from plan-003 release commit.
- No money mutation may slip into this batch. Reviewer approval precedes version/release/deploy.

## Steps

### Step 1: Add stable, tenant-scoped read models

Implement opaque keyset cursors on `(created_at,id)`, bounded filters, LTV from confirmed ledger
facts, and batch snapshot loading for stored usage/status. Add a per-order explicit live refresh with
a server-side rate limit; repeated list renders never hit Hiddify.

**Verify**: pagination remains stable across inserts and two-shop fixtures never mix rows.

### Step 2: Add shared customer-status and order commands

Ban/unban sets an absolute value and audits reason. Wrap renew/pause/resume/delete with expected
shop, actor and command context. Preserve existing crash-safe renewal operations and compensating
reconciler. External writes persist a pre-call correlation/audit-pending fact, release DB resources,
perform I/O, then finalize; ambiguous results remain recoverable rather than falsely successful.

For admin renewal, existing `StorefrontOperation` remains the sole domain authority: the browser
idempotency command stores and reuses one derived `op_id`, then caches only the safe response. For
absolute enable/disable and delete, the plan-003 API command is the durable authority; its canonical
request contains the target state, and an expired/unknown lease is reconciled by a live Hiddify read
and idempotent absolute re-apply. Extend the scheduled reconciler for these action names. Never create
a second renewal state machine.

**Verify**: same idempotency key replays; different payload conflicts; a foreign order never calls
the panel client; panel timeouts produce the documented recoverable state.

### Step 3: Refactor bot actions to the same commands

Replace handler-only ownership and direct ban writes with shared commands while preserving messages
and callbacks. Bot and portal actions must be mutually visible after refresh.

**Verify**: existing storefront subscription tests and new parity tests pass unchanged in behavior.

### Step 4: Build responsive customer and service UI

Add server-side filter/pagination, customer summary/LTV/ledger, service cards, explicit live refresh,
confirmation dialogs and clear processing/recovery states. Hide money adjustment controls until plan
005. Use feature query keys and invalidate only affected shop/customer/dashboard reads.

**Verify**: frontend tests cover loading/error/empty, filters, deep links, 404, double-click,
confirmation, processing and mobile action menus.

### Step 5: Run full gates and production smoke

Include PG contract tests for idempotent admin renewal and concurrent order actions. Mocks are CI and
staging tools only. Production smoke is read-only: owned/foreign routes, stored order view and health.
No production subscription is renewed, deleted, paused or resumed for smoke testing.

## Test plan

- Cross-tenant shop/customer/order/sub-link isolation.
- Cursor tampering and stable pagination.
- Ban/unban retry and audit.
- Renew retry across processes, operation-key binding and recovery after ambiguous Hiddify response.
- Absolute enable state and idempotent delete.
- No DB connection held during Hiddify I/O; no N+1 live panel calls.
- Bot/portal command parity and frontend back/refresh safety.

## Done criteria

- [ ] Customer search/detail/ban and subscription view/refresh/renew/enable/delete have owner web
      equivalents; wallet adjustments and messaging remain explicitly deferred to plans 005–006.
- [ ] Every child ID is tenant-checked inside the shared domain command.
- [ ] External I/O is short-session, recoverable and idempotent.
- [ ] Wallet remains read-only in this release.
- [ ] All focused/full/PG/frontend/release/smoke gates pass after approval.

## STOP conditions

- A safe panel mutation requires bypassing existing operation/lease/reconciler invariants.
- The service cannot distinguish a definite failure from an ambiguous remote success.
- List pages would trigger per-row Hiddify calls.
- Scope requires wallet/top-up changes or touches unrelated dirty files.

## Maintenance notes

Keep stored status and explicit live refresh visually distinct. A stale snapshot is not proof of a
live panel state; never label it as such.
