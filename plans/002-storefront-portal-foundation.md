# Plan 002: Add the storefront portal shell and read-only dashboard

> **Executor instructions**: Execute only after the owner explicitly approves item 3.
> Follow every step and verification gate. Stop on a STOP condition; do not improvise.
> The reviewer maintains `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat b37f587..HEAD -- backend/app/main.py backend/app/api/portal.py backend/app/core/portal_auth.py backend/app/services/storefront.py backend/app/models/storefront.py frontend/src/portal frontend/package.json frontend/package-lock.json .github/workflows/ci.yml`

## Status

- **Roadmap item**: 3, Release A of F
- **Priority**: P1
- **Effort**: L
- **Risk**: MED
- **Depends on**: plan 001 DONE
- **Category**: direction
- **Planned at**: commit `b37f587`, 2026-07-16 (drift checked; intervening v1.82.4 changed only metering/version files outside this slice)
- **Candidate release**: `v1.83.0` (recalculate if another release lands first)
- **Execution result**: DONE in `v1.83.0`; CI-stability follow-up deployed as `v1.83.1`; no migration
  or manual upgrade step

## Why this matters

The reseller portal has no storefront route while the storefront admin bot has fourteen
management entries. This first slice establishes the safe owner-to-storefront boundary and
delivers useful read-only visibility without exposing any mutation. It also introduces a real
frontend test harness before later money and panel operations make the portal riskier.

## Current state and decisions

- Portal authentication reloads all `Reseller` rows bound to one Telegram chat through
  `backend/app/core/portal_auth.py:ResellerContext` and `get_current_reseller`.
- A Telegram identity may own top-level reseller rows on multiple panels and therefore multiple
  `StorefrontBot` rows. The UI must use a URL-scoped shop selector, never assume one shop.
- `backend/app/api/portal.py` is already about 974 lines. Storefront routes go in a new router.
- Existing aggregate `backend/app/services/storefront.py:stats_for_bot` is useful but lacks
  today/month trends, purchase-versus-renew split, failures and trial conversion.
- Portal storefront access is owner-only in plans 002–007: `StorefrontBot.reseller_id` must be in
  `ctx.ids`. A raw co-admin Telegram ID is not a portal principal.
- Foreign and absent IDs both return HTTP 404, matching existing portal resource behavior.
- No response may contain `bot_token_enc`, a decrypted token, panel credentials, proof paths or
  subscription links unrelated to the selected shop.
- Reporting timezone is fixed to `Asia/Tehran` in this release. A requested date means
  `[00:00:00, next-day 00:00:00)` in Tehran converted to UTC for SQL. `from <= to`, the inclusive
  calendar span is at most 366 days, and malformed/reversed/oversized ranges return 422.

## API contract

Create prefix `/api/portal/storefronts`:

- `GET /api/portal/storefronts`
  returns owned configured shops only: `id`, `reseller_id/name`, `panel_id/key`, bot username,
  enabled/status, `health_error_class` (`unauthorized|network|configuration|unknown|null`),
  `health_state_updated_at` (record update time, not a fabricated error timestamp), shop-open state
  and role=`owner`. Raw `last_error` is never returned.
- `GET /api/portal/storefronts/{shop_id}/dashboard?from=YYYY-MM-DD&to=YYYY-MM-DD`
  returns sales today/month, net sales, purchase/renew/legacy-unknown counts, customers total and
  active-30d, service status counts, near-expiry, pending top-up count/amount, wallet liability,
  credit redemptions/bonus, provisioning state counts and trial-to-paid conversion.
- `GET /api/portal/storefronts/{shop_id}/health`
  returns persisted bot/panel health and operation counts; it must not inspect manager globals.

Metric definitions are fixed as follows:

- `gross_sales_toman`: absolute sum of negative `purchase` wallet rows in the range.
- `reversals_toman`: positive `refund` plus `renew_reversal` rows; `net_sales_toman` is gross minus
  reversals and may be negative. Top-ups, bonuses and manual adjustments are never revenue.
- purchase/renewal split follows the linked `StorefrontOperation.op_type`; unlinked legacy purchase
  rows are `unknown`, never guessed. Counts and amounts are returned for all three buckets.
- trial conversion numerator is unique customers with a successful historical trial order and a
  later non-refunded `StorefrontOperation.op_type=purchase`; renewals and legacy-unknown debits never
  count. Denominator excludes pending/failed trial attempts. Zero denominator returns rate `null`.
- near-expiry means a non-trial `provisioned` order with 0–3 calendar days remaining, using the same
  snapshot calculation as `storefront_expiry`; service states are
  `pending|provisioned|renewing|disabled|failed|deleted`, and operation states are
  `pending|in_progress|done|failed|reversed`.
- credit redemption/bonus metrics are scoped to the current Tehran calendar month, matching the bot
  admin statistic. Historical/date-range analytics remain a later reporting surface.
- Release A has no activity-feed endpoint. Audit-backed activity is introduced after plan 003.

Dashboard query budget: at most 12 SQL round trips for one shop. Do not fetch live Hiddify status
for list/dashboard rows and do not materialize all pending top-ups merely to count them.

## Scope

**In scope**:

- `backend/app/api/portal_storefront.py` (new thin router)
- `backend/app/schemas/portal_storefront.py` (new DTOs)
- `backend/app/core/storefront_access.py` (new owner-scoped dependency/helpers)
- `backend/app/services/storefront_reporting.py` (new read/query layer)
- `backend/app/main.py`
- `backend/tests/test_portal_storefront.py` (new)
- `frontend/src/portal/PortalApp.tsx`, `PortalLayout.tsx`
- `frontend/src/portal/storefront/api.ts`, `types.ts`, `StorefrontIndexPage.tsx`,
  `StorefrontShell.tsx`, `StorefrontDashboardPage.tsx`, `StorefrontHealthPanel.tsx`, and
  `__tests__/storefront.test.tsx` (new)
- `frontend/src/test/setup.ts` (new), `frontend/vite.config.ts`
- `frontend/package.json`, `frontend/package-lock.json`
- `.github/workflows/ci.yml` for frontend tests

**Out of scope**:

- Any POST/PATCH/PUT/DELETE storefront endpoint
- Storefront bot handlers and all storefront mutations
- Models/migrations, co-admin authentication, BotFather token rotation
- `backend/app/services/metering.py` and `backend/tests/test_metering_reset.py` (v1.82.4 behavior is
  already in the baseline and remains outside this feature)
- User creation, customer Mini App, profit accounting and CRM campaigns

## Commands and git workflow

| Gate | Command | Success |
|---|---|---|
| Backend focused | `cd backend && .venv/bin/pytest -q tests/test_portal_storefront.py` | all pass |
| Backend full | `cd backend && .venv/bin/pip check && .venv/bin/ruff check app tests alembic && .venv/bin/mypy app && .venv/bin/pytest -q` | exit 0 |
| Schema | `cd backend && DATABASE_URL=sqlite+aiosqlite:////tmp/invoice-plan002.db .venv/bin/alembic upgrade head && DATABASE_URL=sqlite+aiosqlite:////tmp/invoice-plan002.db .venv/bin/alembic check` | no drift |
| Frontend | `cd frontend && npm ci && npm audit --audit-level=high && npm test -- --run && npm run build` | exit 0, bundle ≤600 KiB |
| Deploy tools | `bash -n deploy/*.sh get.sh && bash deploy/test-release-tools.sh` | exit 0 |
| Compose | `docker compose config -q` | exit 0 |

- Branch: `feature/storefront-portal-3a-foundation` from the refreshed base SHA.
- Conventional commits; do not mix another roadmap item or the dirty metering work.
- Owner approval to start plan 002 includes its release/deploy. Follow `docs/RELEASE_PROCESS.md`
  with fresh backup and rollback target; stop only on a new STOP condition or scope expansion.

## Steps

### Step 1: Introduce frontend component-test infrastructure

Pin `vitest@4.1.10`, `jsdom@29.1.1`, `@testing-library/react@16.3.2`,
`@testing-library/user-event@14.6.1`, `@testing-library/jest-dom@6.9.1` and `msw@2.15.0` as
dev dependencies. Configure jsdom in `frontend/vite.config.ts`, load jest-dom and MSW cleanup from
`frontend/src/test/setup.ts`, add script `"test": "vitest"`, and add a CI frontend-test step before
the production build. Keep the existing build and 600 KiB chunk budget unchanged.

**Verify**: `cd frontend && npm test -- --run && npm run build` → exit 0.

### Step 2: Add the storefront access dependency and DTO boundary

Implement `StorefrontAccess` resolution from `ResellerContext`. List only bots whose
`reseller_id` belongs to the current context and whose reseller remains storefront-enabled.
Provide child-resource helper patterns for later plans. Use 404 for foreign IDs and explicit DTOs
so ORM serialization can never leak credentials.

**Verify**: focused tests for two owners, one owner with two shops, no-shop and foreign ID pass.

### Step 3: Add reporting queries and read-only routes

Create tenant-scoped aggregate queries. Split purchase and renewal by joining wallet
`operation_id` to `StorefrontOperation.op_type`; report legacy unlinked rows as `unknown`. Use the
fixed Tehran timezone/date contract above. Net sales must handle refunds/reversals consistently and must
not count wallet top-ups as revenue.

Instrument query count in the focused test with SQLAlchemy's `before_cursor_execute` event over a
fixture of two shops, 200 customers, 400 orders and 2,000 ledger rows. Assert no more than 12 SQL
round trips for one dashboard request and remove the listener in `finally`.

**Verify**: `pytest -q tests/test_portal_storefront.py` → all dashboard/health tests pass.

### Step 4: Build the web shell and dashboard

Add one sidebar entry «فروشگاه من». Routes use `/portal/storefront/:shopId` so refresh/deep links
retain selection. `/portal/storefront` redirects to the sole shop or renders a picker/empty state.
Add feature-local nested tabs, responsive stat cards/charts, health warnings and loading/error/empty
states using `DataState`, `SectionCard`, `StatCard`, React Query and current RTL/MUI conventions.

**Verify**: component tests cover shop switching, no-shop, foreign/404, loading/error, responsive
navigation and dashboard rendering; production build passes.

### Step 5: Run full Release A gates

Run backend full pytest/Ruff/mypy/pip, frontend audit/tests/build, migration drift check (no new
migration), release-tool tests, Compose validation, staging smoke, then the documented release and
production process under the owner's approval to start this plan.

## Test plan

- Owner shop discovery, multiple shops, no-shop, entitlement disabled and credential redaction.
- Cross-tenant storefront IDs return 404 for every read route.
- Tehran day/month boundaries and invalid date ranges.
- Purchase/renew/unknown split; refund/reversal math; trial conversion; failure counts.
- Dashboard query-count ceiling and no database writes from GET routes.
- Frontend route/picker/cache/empty/error/mobile tests.

## Done criteria

- [x] Storefront navigation appears only when at least one owned shop exists.
- [x] One owner can switch multiple shops; no foreign ID leaks data.
- [x] Dashboard reconciles with existing bot stats and adds the named metrics.
- [x] No storefront mutation or migration exists in this release.
- [x] Frontend tests are part of CI and all full gates pass.
- [x] Release/deploy/smoke are completed under the owner's explicit execution approval.

## STOP conditions

- Relevant source symbols drift from the current-state contract.
- Access requires broadening ordinary reseller portal permissions to co-admin IDs.
- A metric cannot be derived without guessing accounting semantics; omit and report it.
- Query budget requires speculative PostgreSQL extensions/indexes before measurement.
- Any source file outside scope is required or pre-existing metering changes would be overwritten.

## Maintenance notes

Every later plan must use `StorefrontAccess` and the feature-local client/query keys. Never add
storefront routes back into monolithic `portal.py` or expose ORM objects directly.
