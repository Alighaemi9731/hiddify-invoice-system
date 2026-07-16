# Plan 005: Add the wallet and top-up operations center

> Execute only after explicit approval and plans 003–004 are DONE. This is a money-path release and
> must ship alone. **Drift check**: `git diff --stat 2514a96..HEAD -- backend/app/models/storefront.py backend/app/services/storefront_wallet.py backend/app/services/storefront_admin.py backend/app/api/portal_storefront.py backend/app/schemas/portal_storefront.py backend/alembic backend/tests frontend/src/portal/storefront`
> Plans 002–004 will intentionally cause drift; this is a design blueprint. Re-read the released
> predecessor, regenerate exact schemas/file allowlist/commands and refresh the SHA before dispatch;
> replacing only the SHA is insufficient.

## Status

- **Roadmap item**: 3, Release D of F
- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH (money)
- **Depends on**: plans 003 and 004
- **Category**: direction / security / migration
- **Planned at**: commit `2514a96`, mandatory reconcile after plan 004
- **Candidate release**: `v1.86.0`

## Why this matters

Pending top-ups and manual wallet adjustments are frequent admin work and financially sensitive.
Existing row locks and tenant guards are strong, but decision history lacks actor identity and several
ledger writers omit the denormalized shop ID. This slice exposes the workflow only after making the
tenant, idempotency, audit and notification ordering explicit.

## Required migration

- Backfill `storefront_wallet_txns.storefront_bot_id` through customer ownership, assert no orphaned
  rows, then make it non-null for all ledger kinds.
- Add nullable immutable `requested_amount_toman`. New top-up requests write both requested and
  current amount; correction changes only `amount_toman`. Backfill pending rows from `amount_toman`;
  confirmed/rejected legacy rows remain null because their original request cannot be reconstructed.
- Update every writer before enforcing non-null.
- Add measured indexes for `(storefront_bot_id, kind, status, created_at, id)` and
  `(customer_id, created_at, id)`.
- Preserve all ledger/order history; no destructive cleanup.
- Update Alembic `HEAD`, SQLite migration contracts and PG16 migration/drift tests.

## API contract

- `GET /{shop}/topups?status=&method=&min_amount=&max_amount=&from=&to=&q=&cursor=&limit=`
- `GET /{shop}/topups/{txn}` with customer, requested/current credited amount, method/chain/TXID,
  code/bonus, audit decision and proof-presence metadata.
- `GET /{shop}/topups/{txn}/proof`: authorized streaming only after canonical realpath containment
  under `data/storefront_proofs`; never return a path.
- `POST /{shop}/topups/{txn}/decision`:
  `confirm|reject`, optional corrected positive amount, mandatory reason for correction/rejection.
- `POST /{shop}/topups/bulk-decisions`: maximum 100; explicit per-item results; no all-or-nothing
  fiction after earlier rows have committed.
- `POST /{shop}/customers/{customer}/wallet-adjustments`: non-zero signed amount and mandatory reason;
  response contains requested delta, actual applied delta, old/new balance and ledger ID.
- `GET /{shop}/customers/{customer}/ledger` uses the same server filters. CSV/accounting export is
  not part of this release.

Top-up methods are `card|usdt|ton`, statuses are `pending|confirmed|rejected`, default limit is 25
and maximum 100. Amount bounds are `1..10^12` toman, decision/adjustment reason is `3..255`, the
date span is at most 366 Tehran calendar days, and cursors use the signed plan-004 contract. List
responses use `{items,next_cursor,total_hint:null}`; total counts are separate aggregate fields, not
an expensive exact count on every page.

## Money invariants

- Existing `confirm_topup`/`reject_topup` row locks and `expected_storefront_bot_id` remain the money
  authority; API code never updates balances directly.
- Manual adjustment accepts expected shop and actor and locks the customer. To preserve existing bot
  behavior, an overdraw request clamps the new balance to zero. The ledger records the actual applied
  negative delta; the response and UI show requested delta and actual applied delta distinctly.
- Same idempotency key/payload creates one ledger effect. Portal and bot racing on one top-up produce
  one terminal decision.
- Commit wallet/audit fact first. In this release Telegram notification is explicitly best-effort
  after commit and may be missed on process crash; its outcome is recorded but cannot roll back money.
  Plan 006 replaces this with the durable delivery queue.
- Corrected amount audit records original requested and final credited values.

## Scope

**In scope**:

- wallet tenant migration/model/writers/indexes
- tenant-aware, actor-aware manual adjustment and top-up orchestration
- top-up history/filter/proof/bulk services and portal routes/schemas
- bot top-up/manual-adjust paths migrated to shared orchestrators
- wallet/top-up/ledger UI and proof viewer
- SQLite, PG concurrency, API, bot parity and frontend tests

**Out of scope**:

- Credit-code CRUD/redemption rules (plan 006; existing bonus-on-confirm behavior remains)
- Automatic crypto verification, refund redesign, accounting exports
- Customer-initiated top-up UI, Mini App and co-admin web sessions
- Portal user creation (roadmap item 2 is rejected)
- Any metering edits

## Commands and git workflow

| Gate | Command | Success |
|---|---|---|
| Focused | `cd backend && .venv/bin/pytest -q tests/test_portal_storefront_wallet.py tests/test_portal_storefront_topups.py tests/test_payment_integrity.py` | all pass |
| Backend full | pip check, Ruff, mypy and full pytest | exit 0 |
| PG money races | `pytest -q -m pg_contract` on PostgreSQL 16, including decision/adjustment races | all pass |
| Schema | upgrade/check on SQLite + PG16 and migration contract HEAD | no drift |
| Frontend | clean install, high audit, component tests, type/build/bundle | exit 0 |
| Release | tools/assets/staging, verified backup, production smoke and rollback record | exit 0 |

- Branch: `feature/storefront-portal-3d-wallet-topups` from plan-004 release commit.
- This is an isolated money release: no unrelated refactor/docs batch. Reviewer approval precedes
  versioning and deploy.

## Steps and verification

### Step 1: Make ledger tenant identity complete

Patch all writers, add backfill/assertion/non-null migration and indexes. Test legacy rows and all
ledger kinds. The refreshed executable scope must enumerate and test writers in
`create_topup`, `confirm_topup` (base and bonus), `redeem_gift`, `manual_adjust`,
`charge_purchase`, `refund_purchase` and `reverse_renewal`; each must populate the locked customer's
shop ID. **Verify** Alembic upgrade/check on SQLite and PG16 and model-drift tests.

### Step 2: Build shared money commands

Wrap manual adjust, decision and bounded bulk decision with plan-003 command/audit contracts. Itemize
bulk results (`changed|already_decided|not_found|failed`) and never leak foreign existence. Send the
documented best-effort customer notification after commit through the correct storefront bot.

A bulk request owns a parent command key, but each item uses a deterministic child key
`sha256(parent_key + txn_id + canonical_decision)`. Each item commits independently and stores its
result. Retrying a crashed batch replays completed children and continues unfinished children; a
changed decision under the same parent conflicts. Parent status is derived from child results, never
used as the only exactly-once guard.

**Verify**: PG races confirm/confirm, confirm/reject and concurrent adjustments preserve exactly one
decision and all wallet changes.

### Step 3: Add secure read/proof APIs

Use cursor pagination, filters and tenant-owned joins. Validate stored proof paths against the single
allowed root, handle missing files as 404, set safe content types/disposition and block traversal.

**Verify**: foreign/path-tampered/missing proof tests and range/filter tests pass.

### Step 4: Build operations-center UI

Add pending badge, queue/history tabs, filters, receipt/TXID viewer, confirm/reject/correct dialogs,
bounded bulk review, customer ledger and manual adjustment reason form. Disable repeat submit while
reusing its idempotency key; refetch dashboard/customer/top-up keys on success.

**Verify**: frontend tests cover partial bulk, already-decided healing, 409, double-click, proof error,
mobile actions and exact displayed/applied amounts.

### Step 5: Money release gates

Full backend/frontend/PG/migration/staging gates, fresh verified backup, isolated MINOR release,
production smoke using a dedicated reversible test row—not a real customer payment—and rollback target.

## Done criteria

- [ ] Portal and bot top-up decisions share one locked, tenant-aware command.
- [ ] Duplicate/concurrent decisions never double-credit.
- [ ] Every adjustment/decision identifies actor, source, reason and before/after money.
- [ ] Proof paths and foreign IDs cannot leak.
- [ ] All historical ledger rows have a non-null shop identity.
- [ ] Money release ships alone and every gate passes after approval.

## STOP conditions

- Backfill finds orphaned or ambiguous wallet rows.
- Audit/idempotency cannot commit atomically with a money fact.
- Bulk semantics would rollback or conceal an already-committed item.
- Proof containment cannot be guaranteed from existing paths.
- Any unrelated or metering file must change.

## Maintenance notes

Do not add automatic crypto acceptance here. This release is admin decision UX over the existing
ledger truth, not a new payment verifier.
