# Security Remediation — Round 2 (2026-07-15)

A **second-round** external re-review of the round-1 program (`SECURITY_REVIEW_PLAN.md`, v1.75.0→v1.79.0)
found that 8 of the 16 findings were only **partially** fixed or **not** fixed — including a reproducible
**Critical setup-takeover (F1)**. Three independent `Explore` verification passes plus direct code reads
confirmed **all 8 residuals are real** against v1.79.1 (Alembic head `f5b8d1a3c6e9`). They ship in three
batches (front-door first, then the coupled storefront-money core, then the medium-severity correctness
fixes), each its own tested release + production deploy. Concurrency invariants are asserted by
`pg_contract`-marked barrier tests on real Postgres 16 in CI (`backend-postgres`).

Only **Batch B** carries an Alembic migration (chained off head `f5b8d1a3c6e9`), so Alembic stays a single
linear head.

Genuinely fixed in round 1 (preserved, not regressed): **F3, F6, F7, F8, F9, F14, F15, F16.**

| Batch | Release | Findings | Migration | Status |
|-------|---------|----------|-----------|--------|
| A Front door | v1.80.0 | F1, F2 | — | ✅ done |
| B Storefront money durability | v1.81.0 | F4, F11, F5 (durable-operation + lease) | 1 | ⏳ |
| C Sync / metering / bot resource | v1.82.0 | F12, F10, F13 | — | ⏳ |

**F2 posture = STRICT** (owner-chosen): public plaintext is refused for *all* credential + bearer requests
even before HTTPS is configured; loopback (SSH tunnel) is always allowed; domain/relay installs are
unaffected (Caddy sends `X-Forwarded-Proto: https`). A bare-IP box with no domain must be set up over a
domain (auto-HTTPS) or an SSH tunnel.

## Residual findings

- **F1 (Critical) — DONE.** `do_setup` cleared+committed the bootstrap token *before* validating the
  username/password and creating the owner (and released the `setup_done FOR UPDATE` early), so a correct
  token + a bad username burned the token and a later no-token request could finish setup. → validate every
  input first; create owner + set `setup_done` + consume the token in **one** commit (`commit=False` on the
  token clear); a legacy no-token install may only be set up from loopback (fail-closed).
- **F2 (High) — DONE.** `secure_or_loopback_ok` allowed public plaintext whenever HTTPS was merely
  unconfigured, and bearer/JWT requests had no transport check at all. → Strict gate `https OR loopback`
  (no settings read); `require_secure` on setup/login/passkey-begin **and** passkey-complete; every bearer
  request gated via `get_current_subject`.
- **F11 (High)** Renewal debit commits before the panel call; `except Exception` misses `CancelledError`;
  the reaper blind-promotes `renewing→provisioned` with no verify/refund. → durable renewal operation +
  reconciler (reverse-on-uncertainty, exactly-once via per-operation reversal uniqueness). (B)
- **F4 (High)** Only `order.status` guards replay → a sequential second renewal/purchase re-charges. →
  durable `op_id` idempotency (cached terminal result on replay). (B)
- **F5 (High)** `purchase()` provisions with the order row unlocked → the reaper can refund an in-flight
  provision and the success is discarded as `reaped` (orphaned config). → durable provisioning **lease** the
  reaper honors; reconcile-by-UUID after lease expiry. (B)
- **F10 (Med)** `metering.apply()` leaves partial `meter_*`/`UsageMeter` mutations on failure → next sync
  computes from inconsistent state. → pure calculation applied only on success. (C)
- **F12 (Med)** Failed-sync bookkeeping runs after the advisory lock releases → an old failure clobbers a
  newer success. → reacquire the lock + recency guard (newer success always wins). (C)
- **F13 (Med)** Storefront polling creates a task per update before acquiring the semaphore → queued tasks
  grow unbounded. → acquire capacity before `create_task` (backpressure). (C)

See `.claude/plans/codex-dreamy-candy.md` for the full implementation design.
