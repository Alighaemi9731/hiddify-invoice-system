# Security & Correctness Remediation Program (2026-07-15)

An external review reported 16 findings; three independent verification passes confirmed **all 16
are real**. They ship in six batches (highest live-box risk first), each its own tested release +
production deploy. Concurrency invariants are asserted by `pg_contract`-marked tests that run on a
real Postgres 16 in CI (`backend-postgres` job) — SQLite can't assert row/advisory locking.

Only **Batch 2** carries an Alembic migration (chained off head `e3a5c7f9b1d4`).

| Batch | Release | Findings | Migration | Status |
|-------|---------|----------|-----------|--------|
| 0 Test/CI foundation | v1.75.0 | `pg_contract` marker + PG contract CI job + barrier helper | — | ✅ done (with B1) |
| 1 Owner money integrity | v1.75.0 | F6, F7 | — | ✅ done |
| 2 Storefront money integrity | v1.76.0 | F3, F14, F4-renewal, F11, F5-refund | 1 | ⬜ |
| 3 Storefront provisioning | v1.77.0 | F5-reaper, F4-purchase, F13, F15 | — | ⬜ |
| 4 Billing/sync correctness | v1.78.0 | F9, F10, F12 | — | ⬜ |
| 5 Front-door & auth | v1.79.0 | F1, F2, F8 | — | ⬜ |

## Findings

- **F1 (High)** Unauthenticated first-run `/api/setup` → owner takeover on a fresh box. → bootstrap token (B5). *Mitigated on the current prod box: owner already claimed → 409.*
- **F2 (Med-High)** Credentials/JWT accepted over plaintext HTTP. → require HTTPS/loopback for credential endpoints (B5). *Current box already on HTTPS.*
- **F3 (High)** Storefront admin can confirm/reject/re-amount ANOTHER tenant's top-up (`sfok:`/`sfno:`/`sfokamt:` take a global txn_id). → tenant check in confirm/reject_topup (B2).
- **F4 (High renewal / low purchase)** Renewal confirm button is reusable → repeat charge for ~1× quota; purchase FSM read/clear race. → idempotency guard + button strip (B2 renewal, B3 purchase).
- **F5 (High)** Reaper races the provisioner: unlocked select, no refund-uniqueness constraint, unlocked refund balance. → order lock + partial-unique refund index + customer lock (B2 refund, B3 reaper).
- **F6 (High) — DONE** Owner payment confirm/reject + every invoice-mutation route loaded invoices UNLOCKED → last-commit-wins could leave a confirmed payment on a canceled invoice. → all money paths now lock the Payment then invoices FOR UPDATE in ascending id order; `unmark_paid` retires (locks) manual payments before the invoice to keep Payment→Invoice order.
- **F7 (Med) — DONE** `submit_reseller_payment` never checked the method policy; portal coerced unknown chains → BSC. → shared submit path now enforces `payment_methods.load_options()` (enabled + configured); portal passes the chain through for the allow-list to reject.
- **F8 (Low-Med)** Passkey `login-begin` unthrottled + unbounded challenge store. → per-IP throttle + store cap (B5).
- **F9 (Med)** No max snapshot age; recompute bills stale data after a failed sync. → age setting + recompute abort on failed sync (B4).
- **F10 (Med)** Metering failure advances the snapshot baseline → lost usage. → preserve baseline on metering failure (B4).
- **F11 (Med)** Renewal renews on the panel before reserving funds. → debit-first, compensate-on-failure (B2).
- **F12 (Med)** Panel sync not serialized per panel. → `pg_advisory_xact_lock(panel_id)` (B4).
- **F13 (Med)** Storefront polling spawns unbounded, untracked update tasks; stop orphans them. → semaphore + task registry + drain (B3).
- **F14 (Med)** Storefront top-up txids unnormalized/non-unique → replay. → normalize + tenant-scoped partial-unique (B2, migration).
- **F15 (Low)** Per-customer lock registry grows forever. → refcounted/weakref registry (B3).
- **F16 (Low) — DONE** Concurrency untested (suite runs on SQLite). → `pg_contract` marker + `backend-postgres` runs `pytest -m pg_contract`; each concurrency fix ships its barrier test.
