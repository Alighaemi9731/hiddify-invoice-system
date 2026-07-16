# Plan 003: Add audited shared plan and settings management

> Execute only after explicit approval and plan 002 is DONE. The reviewer maintains the index.
> **Drift check**: `git diff --stat 2514a96..HEAD -- backend/app/models/storefront.py backend/app/services/storefront.py backend/app/bot/storefront/handlers.py backend/app/api/portal_storefront.py backend/app/schemas/portal_storefront.py frontend/src/portal/storefront backend/alembic backend/tests/test_migrations_contracts.py`
> Plan 002 will intentionally cause drift; this file is a design blueprint, not an executable
> handoff. After plan 002 ships, the advisor must re-read the release, replace the SHA/excerpts,
> produce an exact file allowlist and exact commands. Replacing only the SHA is insufficient.

## Status

- **Roadmap item**: 3, Release B of F
- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: plan 002
- **Category**: direction / migration / security
- **Planned at**: commit `2514a96`, mandatory reconcile after plan 002
- **Candidate release**: `v1.84.0`

## Why this matters

Adding portal writes directly to current bot-coupled ORM mutations would create two divergent rule
sets and unaudited retry hazards. This slice first establishes shared absolute-state commands,
optimistic concurrency, idempotency and audit, then exposes plans and settings through those same
commands. Both bot and portal must end the release using one business layer.

## Required migration and invariants

Add one additive Alembic migration and update the hard-coded `HEAD` contract:

1. `storefront_bots.config_version INTEGER NOT NULL DEFAULT 1`.
2. `storefront_audit_events`: shop (SET NULL for history), actor Telegram ID (BIGINT), actor role,
   source (`bot|portal|system`), action, entity type/id, correlation/request ID, redacted before/after
   JSON, outcome/error class and timestamp. Never store tokens, credentials, proof paths or sub-links.
3. `storefront_api_commands`: shop, actor, idempotency key, action, canonical request hash,
   `pending|succeeded|failed|unknown`, response status/body, `lease_expires_at`, attempt count and
   timestamps. Unique
   `(storefront_bot_id, actor_telegram_id, idempotency_key)`.
4. Supporting indexes for shop/time, actor/action/entity and command status.

Configuration mutations require `Idempotency-Key` and `If-Match: "sf-config-N"`. Every settings GET
and plan-list response returns `ETag: "sf-config-N"`. The service performs an atomic
`UPDATE storefront_bots ... WHERE id=:shop AND config_version=:N`; zero updated rows returns 409 with
the current ETag but never silently overwrites. Same key/action/payload replays the stored safe
response; same key with a different action or canonical payload returns 409. A pending command has a
60-second lease: after expiry a DB-only command is safely reclaimed, while an external-I/O command
becomes `unknown` and follows its action-specific reconciler rather than blind replay. Cached response
JSON is an explicit redacted DTO, at most 16 KiB, retained 30 days; it never contains tokens,
credentials, proof paths or subscription links. Bot actions capture the version when the action/FSM
starts and receive the same conflict result, with one user-visible reload/retry rather than hidden
overwrite. Audit failure fails a DB-only mutation closed in the same transaction.

## API contract

- Plans: list, create, partial edit, absolute enabled state, hard delete, exact-set atomic reorder,
  and price/history view derived from audit. Hard delete preserves historical order snapshots and
  sets `StorefrontOrder.plan_id` to null through the existing FK; history remains queryable by the
  deleted entity ID in audit. There is no plan archive field in this release.
- Settings: GET plus PATCH groups for payment, trial, messages/support, shop state.
- Channel: validate/save and delete; enabling requires a verified shop-bot admin check.
- Managers: owner-only list/add/remove; portal co-admin login is not added.
- Preview: a pure DTO/web rendering based on settings/plans; never call `get_or_create_customer`.

All numeric/text/network formats receive Pydantic bounds and service-layer validation. Toggle APIs
must set absolute desired state rather than invert current state.

Exact settings bounds: plan `gb 1..100000`, `days 1..3650`, `price_toman 0..10^12`; welcome and
closed messages `0..1000` Unicode characters; support contact `0..128`; trial GB `1..1000` and days
`1..90`; manager Telegram ID is a positive signed-64-bit integer and the existing maximum is 10.
Card numbers normalize digits and require 16 digits; holder is `2..128`; USDT/TON values are
trimmed `3..128` and validated for the configured network without resolving them externally.
Settings routes are `GET/PATCH /{shop}/settings/{payment|trial|messages|shop-state}`,
`POST/DELETE /{shop}/channel`, `GET/POST/DELETE /{shop}/managers`, and plan routes are
`GET/POST /{shop}/plans`, `PATCH/DELETE /{shop}/plans/{plan}`, `PUT /{shop}/plans/{plan}/enabled`,
`PUT /{shop}/plans/order`, `GET /{shop}/plans/{plan}/history`. Domain mappings are 404 foreign/missing,
409 idempotency/version conflict, 422 validation, 502 verified external failure.

## Scope

**In scope**:

- new migration, `backend/app/models/storefront.py`, model exports, migration HEAD test
- `backend/app/services/storefront_admin.py` and `storefront_audit.py` (new)
- existing `storefront.py` plan helpers hardened/reused
- bot plan/settings/admin/channel/shop/preview handler paths refactored to shared commands
- portal storefront router/schemas and plans/settings/preview pages
- backend tests: portal plans/settings, audit/idempotency, migrations, bot/service parity
- frontend tests for plans/settings/conflict/preview flows

**Out of scope**:

- Customers, orders, wallet/top-ups, credit codes and broadcasts
- Bot token display/rotation; token stays encrypted and absent from all DTO/audit payloads
- Co-admin portal authentication and global RBAC/audit UI
- Portal user creation (roadmap item 2 is rejected)
- Pre-existing metering changes

## Commands and git workflow

| Gate | Command | Success |
|---|---|---|
| Focused | `cd backend && .venv/bin/pytest -q tests/test_portal_storefront_plans.py tests/test_portal_storefront_settings.py tests/test_migrations_contracts.py` | all pass |
| Backend full | `cd backend && .venv/bin/pip check && .venv/bin/ruff check app tests alembic && .venv/bin/mypy app && .venv/bin/pytest -q` | exit 0 |
| PG contracts | CI/Postgres 16: `pytest -q -m pg_contract` | all pass |
| Schema | Alembic upgrade/check on SQLite and PostgreSQL 16 | new HEAD, no drift |
| Frontend | `cd frontend && npm ci && npm audit --audit-level=high && npm test -- --run && npm run build` | exit 0 |
| Release tools | syntax, release-tool test, release asset checksums and Compose config | exit 0 |

- Branch: `feature/storefront-portal-3b-plans-settings` from the plan-002 release commit.
- Migration revision must be a new unique 12-char ID off the current HEAD; update the HEAD pin.
- Keep migration + plans/settings batch alone. Reviewer approval precedes version/tag/release/deploy.

## Steps and verification

### Step 1: Add migration, models and low-level audit/idempotency helpers

Implement canonical JSON hashing, command claim/replay/conflict and redacted audit writes. Pending
commands use the lease/recovery contract above and must not remain permanently in progress or falsely
successful after exceptions. Add SQLite migration tests and
PostgreSQL uniqueness/race contracts.

**Verify**: migration upgrade/check and focused command/audit tests pass on SQLite and PG16.

### Step 2: Extract shared settings and plan commands

Move handler-owned validation/mutations for payment, trial, support/welcome, channel, shop state,
co-admin management and customer preview into `storefront_admin.py`. Add plan enable/disable and
exact ordered-ID reorder. Require exact tenant ownership inside services, not only routes/handlers.
Increment `config_version` with the atomic compare-and-swap described above and capture before/after
audit. Plan reorder uses the native HTML drag/drop + move-up/down keyboard controls; add no new
frontend drag library in this slice.

**Verify**: service tests cover invalid values, tenant isolation, retries, stale versions and atomic
reorder; bot parity tests show old Telegram actions produce the same stored values.

### Step 3: Expose thin portal routes

Routes resolve `StorefrontAccess`, validate headers/bodies, call one shared command and map known
domain outcomes to 404/409/422/502. Channel validation creates a temporary aiogram Bot from the
server-side credential, performs Telegram checks without holding a DB session, closes it, then saves.
The save transaction must compare the original config version and a hash of the encrypted credential
identity; if either changed while Telegram I/O was in flight, return 409 and do not save the channel.
Write append-only audit outcomes `started` then `succeeded|failed|unknown`: DB-only mutation plus
success audit is atomic; validation/definite Telegram failure is recorded separately and fails closed;
ambiguous external results are never represented as success.

**Verify**: every foreign shop/plan ID returns 404; no credential appears in JSON/log captures.

### Step 4: Build plans, settings, managers and preview UI

Use a web-native form, drag-and-drop reorder with keyboard fallback, conflict dialog
reload/reapply, payment/trial/channel health cards, open/closed confirmation, owner-only manager
panel and a pure responsive customer preview. Double-clicks reuse one browser-generated idempotency
key until a request settles.

**Verify**: frontend tests cover validation, reorder, 409, double-click, retry, mobile and preview
without creating a customer.

### Step 5: Full gates and isolated release

Run every CI/release/staging/production gate. Migration requires pre-deploy backup, verified HEAD,
rollback target and post-deploy bot↔portal parity smoke.

## Done criteria

- [ ] Bot and portal call the same plan/settings command layer.
- [ ] Every mutation is tenant-scoped, audited, idempotent and version-conflict safe.
- [ ] Plan enable/disable and atomic reorder work without duplicates/lost plans.
- [ ] Customer preview performs zero writes.
- [ ] Only the primary owner changes managers; co-admin web access is absent.
- [ ] Migration, PG races, frontend tests, full gates, release and smoke pass after approval.

## STOP conditions

- Audit and domain mutation cannot share a transaction for DB-only actions.
- Channel verification would keep a request DB session open across Telegram I/O.
- Existing bot behavior cannot be represented as an absolute command without a product decision.
- A migration would delete/rewrite wallet/order/redemption history.
- Scope touches metering or any unrelated dirty file.

## Maintenance notes

Plans 004–006 must use the command/audit/idempotency contracts introduced here; they must not build
separate API-only mutation logic.
