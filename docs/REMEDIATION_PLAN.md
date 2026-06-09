# Remediation plan

This is the execution tracker for the whole-codebase audit performed on
2026-06-09 against `v1.37.35`. Fix one batch at a time. Every batch must include
focused regression tests, the full verification gate, a release, and a production
smoke check before the next batch starts.

Status values: `TODO`, `IN PROGRESS`, `DONE`, `DEFERRED`.

## Release gate for every batch

```bash
cd backend
.venv/bin/pytest -q
.venv/bin/ruff check app tests --select F

cd ../frontend
npx tsc --noEmit
npm run build

cd ..
bash -n deploy/install.sh deploy/bootstrap.sh deploy/updater.sh get.sh
docker compose -f deploy/docker-compose.prod.yml config >/dev/null
```

Do not release if any command fails. Production deploy also requires a fresh,
verified backup and a documented rollback point.

## B00 - Restore a trustworthy build baseline

Priority: P0 prerequisite. Status: DONE in `v1.37.37`.

- Fix the two current TypeScript errors in `Invoices.tsx` and `theme.ts`.
- Make the frontend production build run TypeScript checking before Vite.
- Fix the Playwright CAPTCHA selector and require an explicit E2E target instead of
  defaulting test execution to production.
- Add a minimal CI workflow for backend tests, Ruff `F` checks, TypeScript, frontend
  build, and deploy-script syntax.
- Record existing mypy and non-`F` Ruff debt without making it a false green gate.

Primary files:
`frontend/src/pages/Invoices.tsx`, `frontend/src/theme.ts`,
`frontend/package.json`, `frontend/Dockerfile`, `e2e/`, `.github/workflows/`.

## B01 - Authentication and account security

Priority: P0. Status: DONE in `v1.37.38`.

- Make JWT authentication fail closed when the account lookup/database fails.
- Require `token_epoch` on accepted JWTs and enforce account role/authorization.
- Store a new TOTP secret as pending until the confirmation code succeeds.
- Serialize first-run setup so concurrent requests cannot create multiple owners.
- Add byte-length validation for passwords before bcrypt.
- Add regression tests for DB failure, disabled users, legacy JWTs, TOTP rollback,
  and concurrent setup.

Primary files:
`backend/app/core/security.py`, `backend/app/api/auth.py`,
`backend/app/api/setup.py`, `backend/app/core/loginsec.py`.

## B02 - Backup, restore, and operational recovery

Priority: P0. Status: DONE in `v1.37.39`.

- Abort backup creation when `pg_dump` fails, is empty, or cannot be validated.
- Validate ZIP size, members, dump presence, metadata, and decompression limits.
- Restore into a staging database or transaction-safe path; never leave the live
  database partially dropped.
- Persist a restored `SECRET_KEY` only after the database restore succeeds.
- Restart both backend and bot after restore and verify they use the same key.
- Run blocking dump/restore work outside the async request event loop.
- Add backup encryption or a clearly defined password-protected export format.
- Add integration tests for failed dump, malformed ZIP, failed restore, rollback,
  cross-server restore, and process restart.

Primary files:
`backend/app/services/backup.py`, `backend/app/services/backup_delivery.py`,
`backend/app/api/operations.py`, `backend/app/bot/handlers.py`.

## B03 - Payment and invoice state machine

Priority: P0. Status: DONE in `v1.37.40`.

- Define and enforce legal invoice/payment transitions in one service.
- Prevent cancel/defer/edit/mark-paid operations on incompatible invoice states.
- Track which payment actually settled an invoice; do not infer it from status.
- Rejecting/deleting one payment must not unpay an invoice settled elsewhere.
- Restore a reseller only when no other due, non-deferred invoice remains.
- Revalidate invoice owner/status/deadline under lock when proof/TXID is submitted.
- Recalculate dunning state correctly when a confirmed payment is reversed.
- Clear stale ledger TXIDs and make ledger writes retryable/reconcilable.
- Add concurrency and multi-payment/multi-invoice regression tests.

Primary files:
`backend/app/services/payments.py`, `backend/app/api/invoices.py`,
`backend/app/services/dunning.py`, `backend/app/services/financial_archive.py`,
`backend/app/bot/handlers.py`.

## B04 - Billing and synchronization correctness

Priority: P0. Status: DONE in `v1.37.41` (per-node-PDF-from-persisted-lines item
consciously deferred — see CHANGELOG; the payable amount is already authoritative).

- Abort monthly billing, or exclude affected panels, when sync fails.
- Reconcile an existing invoice when recomputation becomes zero.
- Render delivered PDFs from persisted invoice lines, not current live snapshots.
- Validate that both expected panel backup collections exist and pass sanity checks.
- Mark missing panel resellers inactive instead of billing stale records forever.
- Include metering extras consistently in previews, reports, PDFs, and user counts.
- Add a stale-age policy for automatic exchange rates.
- Add end-to-end tests covering stale sync, zero recompute, deleted resellers,
  metering extras, and immutable delivered invoice details.

Primary files:
`backend/app/scheduler/jobs.py`, `backend/app/services/operations.py`,
`backend/app/services/sync.py`, `backend/app/services/invoicing.py`,
`backend/app/services/delivery.py`, `backend/app/integrations/backup_json.py`.

## B05 - Enforcement and reminder consistency

Priority: P1. Status: DONE in `v1.37.42`.

- Keep partial restores retryable; do not mark a reseller active until every required
  user/admin operation succeeds.
- Rework pending-payment holds so one proof cannot shield unrelated debts forever.
- Distinguish attempted and successfully delivered reminders in reports.
- Set GB-cap alert flags only after required notifications succeed, or retain retry state.
- Add tests for partial API failures, multiple debts, pending proof expiry, and retries.

Primary files:
`backend/app/services/enforcement.py`, `backend/app/services/dunning.py`,
`backend/app/services/gb_cap.py`.

## B06 - Bot identity, membership, and input safety

Priority: P1. Status: DONE in `v1.37.43`.

- Apply membership gates to direct commands and payment-state messages, not callbacks only.
- Match registration by normalized host + proxy path + UUID; never select the first
  ambiguous candidate.
- Escape all user-controlled values before Telegram HTML rendering.
- Use one Tehran-local date helper for bot and API eligibility checks.
- Add router-level and matching regression tests.

Primary files:
`backend/app/bot/handlers.py`, `backend/app/bot/matching.py`.

## B07 - Database evolution and input contracts

Priority: P1. Status: TODO.

- Replace ad-hoc `create_all`/`ADD COLUMN` evolution with versioned Alembic migrations.
- Baseline the current production schema before applying new constraints.
- Add validation for non-negative money/usage/pricing and typed settings allowlists.
- Add cycle protection and panel-scoped UUID identity to reseller-tree construction.
- Use `Field(default_factory=list)` for mutable schema defaults.

Primary files:
`backend/app/core/db.py`, `backend/alembic/`,
`backend/app/schemas/`, `backend/app/api/settings.py`,
`backend/app/api/resellers.py`.

## B08 - Build, test, and frontend quality gate

Priority: P1. Status: TODO.

- Resolve high-signal mypy errors; configure third-party stubs separately from real errors.
- Add staging configuration and meaningful backup/payment/billing workflow tests.
- Expand Ruff from `--select F` to the agreed full rule set and fix the baseline.
- Split oversized frontend chunks where practical.

Primary files:
`backend/pyproject.toml`, backend typing hotspots, `e2e/`, frontend chunk boundaries.

## B09 - Scheduler, deployment, and supply-chain hardening

Priority: P2. Status: TODO.

- Replace misleading `*/N` schedules with true intervals or restrict values to divisors.
- Live-apply `rate_refresh_hours` consistently with other schedule settings.
- Stop executing a mutable `main/get.sh` directly as root; pin and verify a release asset.
- Use `npm ci` with the lockfile and tighten backend dependency reproducibility.
- Add a database-aware health check and a post-deploy smoke script.
- Add a tested rollback command to the release process.

Primary files:
`backend/app/scheduler/jobs.py`, `backend/app/api/settings.py`,
`deploy/updater.sh`, `get.sh`, Dockerfiles and dependency manifests.

## B10 - Cleanup and documentation

Priority: P3. Status: TODO.

- Decide whether unused enum branches are roadmap items or remove them:
  `admin_api`, `sample`, `usdt_hd`, `duplicate`, `skipped`, `warned`,
  `warn`, and `zero_limits`.
- Remove verified unreachable cold-payment branches and development-only scripts.
- Fix Ruff findings: ambiguous `l`, import placement, and semicolon statements.
- Update `CLAUDE.md`, Help, README, architecture, and release notes after each behavior change.
- Keep local production connection data out of Git.

## Recommended order

`B00 -> B01 -> B02 -> B03 -> B04 -> B05 -> B06 -> B07 -> B08 -> B09 -> B10`

Do not combine B02, B03, and B04 in one release. They independently affect data
recovery, money, and invoice generation and need isolated rollback points.
