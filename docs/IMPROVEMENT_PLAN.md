# Improvement plan

This is the execution tracker for the verified-improvements program from the
whole-project review performed on 2026-07-02 against `v1.49.3` (successor to the
completed `docs/REMEDIATION_PLAN.md` audit program, B00–B10). Fix one batch at a
time. Every batch must include focused regression tests, the full verification
gate, a release, and a production smoke check before the next batch starts.

Status values: `TODO`, `IN PROGRESS`, `DONE`, `DEFERRED`.

## Release gate for every batch

```bash
cd backend
.venv/bin/pytest -q
.venv/bin/ruff check app tests alembic
.venv/bin/mypy app
.venv/bin/pip check

cd ../frontend
npm ci
npm audit
npm run build

cd ..
bash -n deploy/*.sh get.sh
bash deploy/test-release-tools.sh
docker compose --env-file .env -f deploy/docker-compose.prod.yml config >/dev/null
```

Do not release if any command fails. Production deploy also requires a fresh,
verified backup and a documented rollback point.

## Isolation rules

- **I06 is a money batch and must be released alone.**
- **I05, I06, and I10 each contain a schema migration** and must not be combined
  with each other or with any other money/billing/payment change.
- **I07 and I08 are pure-refactor releases** (zero behavior change) and must not
  carry other items.
- Any new Alembic migration must update the `HEAD` pin in
  `backend/tests/test_migrations_contracts.py`.

## I01 - Scheduler heartbeat and in-app error tracking

Priority: P1. Version: MINOR. Status: DONE in `v1.50.0`.

- New `scheduler_heartbeat` job (interval ~2 min, not owner-configurable) stamping
  `scheduler_last_heartbeat`; `/health` reports `status: degraded` +
  `scheduler: stale` as HTTP **200** when the stamp is older than ~10 minutes
  (`deploy/smoke.sh` greps `"database":"ok"` and the compose healthcheck uses
  `curl -fsS`, so degraded must stay 200 with `database` intact).
- New `app/core/errortrack.py`: a `logging.Handler` (level ≥ ERROR) installed from
  both `app/main.py` and `app/bot/run.py`, writing fingerprinted JSONL events to
  per-process rotating files `data/logs/errors-backend.jsonl` /
  `errors-bot.jsonl`. `/health` gains `errors_24h`; the daily digest appends a
  "new errors" section (cursor setting `error_digest_last_ts`). Every tracking
  path swallows its own failures.

Primary files:
`backend/app/scheduler/jobs.py`, `backend/app/api/meta.py`,
`backend/app/core/errortrack.py`, `backend/app/services/owner_report.py`,
`backend/app/main.py`, `backend/app/bot/run.py`,
`backend/app/services/settings_service.py`.

## I02 - Deploy and CI hardening

Priority: P1. Version: PATCH. Status: DONE in `v1.50.1`.

- `deploy/docker-compose.prod.yml`: shared `x-logging` anchor (json-file,
  `max-size: 10m`, `max-file: "3"`) on all services; conservative memory limits
  (validate against production `docker stats` before finalizing — a too-tight
  limit with `restart: unless-stopped` becomes an OOM restart loop).
- Bot liveness: the bot touches a heartbeat file every 30 s; compose healthcheck
  probes its mtime. Frontend healthcheck: nginx `wget` localhost.
- CI: new `backend-postgres` job (postgres:16 service) running
  `alembic upgrade head` + `alembic check` + an env-gated
  `tests/test_postgres_smoke.py` (full suite on Postgres is out of scope — 44
  test files hardcode SQLite).

Primary files:
`deploy/docker-compose.prod.yml`, `backend/app/bot/run.py`,
`.github/workflows/ci.yml`, `backend/tests/test_postgres_smoke.py`.

## I03 - PDF thread offload and frontend 401 cache clear

Priority: P2. Version: PATCH. Status: DONE in `v1.50.2`.

- Wrap the synchronous reportlab rendering in `asyncio.to_thread` inside
  `app/services/invoice_pdf.py` (all render functions funnel there); guard
  `pdf.py` font registration for thread safety.
- `frontend/src/api/client.ts`: on 401 clear the react-query cache before
  redirecting; move the `QueryClient` out of `main.tsx` into a shared module.

Primary files:
`backend/app/services/invoice_pdf.py`, `backend/app/services/pdf.py`,
`frontend/src/api/client.ts`, `frontend/src/main.tsx`.

## I04 - Bot messaging and resilience fixes

Priority: P2. Version: PATCH. Status: DONE in `v1.50.3`.

- Outgoing-message `rtl()` middleware (`BaseRequestMiddleware` on the bot client
  session) rewriting `text`/`caption` on send/edit methods, installed at all
  three Bot construction sites; `AnswerCallbackQuery` toasts excluded initially.
  `rtl()` is idempotent so double application through the central helpers is safe.
- Catch `TelegramForbiddenError` / "chat not found" on the owner→reseller reply
  path with clean Persian messages instead of raw API errors.
- Storefront polling: after ~3 consecutive `TelegramUnauthorizedError`s mark the
  bot errored and stop the loop; also exclude `status == "errored"` bots from
  `storefront.active_bots()` so reconcile stops re-validating dead tokens.

Primary files:
`backend/app/bot/rtl_middleware.py`, `backend/app/bot/run.py`,
`backend/app/bot/telegram.py`, `backend/app/bot/handlers.py`,
`backend/app/bot/storefront/manager.py`, `backend/app/services/storefront.py`.

## I05 - Invoice status indexes

Priority: P2. Version: PATCH. Status: DONE in `v1.50.4`. Migration batch — kept alone.

- Add `ix_invoices_status` and compound `ix_invoices_reseller_status`
  (`reseller_id`, `status`) to `app/models/invoice.py` + one additive Alembic
  migration; update the migrations `HEAD` pin.

Primary files:
`backend/app/models/invoice.py`, `backend/alembic/versions/`,
`backend/tests/test_migrations_contracts.py`.

## I06 - Payment settlements join table

Priority: P1. Version: PATCH. Status: DONE in `v1.50.6`. MONEY batch — strictly isolated.

- New `PaymentSettlement(payment_id, invoice_id)` model (PK pair, CASCADE FKs,
  index on `invoice_id`); migration backfills from `settled_invoice_ids`
  (fallback `invoice_id`), skipping and logging dangling ids.
- Dual-write, indexed-read: writers keep the comma column (safe rollback) and
  also insert join rows; the pending/confirmed lookup helpers become single
  indexed queries; `_settled_ids` reads the table first with comma fallback.
- Dropping the comma column is a later, separate cleanup batch.

Primary files:
`backend/app/models/payment.py`, `backend/app/services/payments.py`,
`backend/alembic/versions/`, `backend/tests/test_invoice_state.py`.

## I07 - Split bot handlers into domain routers

Priority: P2. Version: PATCH. Status: DONE in `v1.50.7`. Pure refactor — released alone.
(Implemented with the same-router pattern rather than include_router: all modules register
on one Router in original order, proven by the order-sensitive inventory snapshot test.)

- Convert `app/bot/handlers.py` (~3,400 lines) into the package
  `app/bot/handlers/` with domain modules (registration, menus, payments, subs,
  usercreate, storefront setup, owner) included in an order that preserves
  aiogram first-match semantics; middlewares/filters stay on the root router.
  `__init__` re-exports every externally imported name so `run.py` and tests
  are unchanged. One declared test edit: retarget the three
  `monkeypatch.setattr(handlers, ...)` calls in `test_bot_identity_safety.py`.

Primary files:
`backend/app/bot/handlers.py` → `backend/app/bot/handlers/`.

## I08 - Frontend table/dialog/mutation refactor

Priority: P3. Version: PATCH. Status: DONE in `v1.50.8`. Pure refactor — released alone.

- New `useToastMutation` / `useDialogState` hooks; split `Resellers.tsx`
  (~1,100 lines) into `pages/resellers/` feature components + `useResellerTree`;
  refactor Invoices/Payments/Panels onto the shared hooks.

Primary files:
`frontend/src/hooks/`, `frontend/src/pages/Resellers.tsx`,
`frontend/src/pages/Invoices.tsx`, `frontend/src/pages/Payments.tsx`,
`frontend/src/pages/Panels.tsx`.

## I09 - Mobile cards and CSV export

Priority: P3. Version: MINOR. Status: DONE in `v1.51.0`.

- Copy the Resellers mobile-card pattern to Invoices + Payments.
- Extract the existing UTF-8-BOM CSV export from `FinancialHistory.tsx` into
  `src/csv.ts`; add export buttons to Invoices/Payments fetching with current
  filters at the endpoint caps.

Primary files:
`frontend/src/csv.ts`, `frontend/src/pages/Invoices.tsx`,
`frontend/src/pages/Payments.tsx`, `frontend/src/pages/FinancialHistory.tsx`.

## I10 - Storefront expiry notifications

Priority: P3. Version: MINOR. Status: DONE in `v1.52.0`. Contains a migration — isolated.

- New `app/services/storefront_expiry.py` + daily scheduler job joining
  provisioned `storefront_orders` to `end_user_snapshots`; threshold setting
  `storefront_expiry_notify_days` (default 3, 0 = off, global-only first).
  Dedup via new additive column `storefront_orders.expiry_alerted_at`, re-armed
  on renewal. Message customers via a short-lived tenant bot with the existing
  renew button; update the migrations `HEAD` pin.

Primary files:
`backend/app/services/storefront_expiry.py`, `backend/app/scheduler/jobs.py`,
`backend/app/models/` (storefront order), `backend/alembic/versions/`.

## I11 - Storefront admin stats dashboard

Priority: P3. Version: MINOR. Status: DONE in `v1.53.0`.

- Extend the storefront admin `stats` view with `storefront.stats_for_bot()`:
  monthly sales, confirmed top-ups, active customers, provisioned orders,
  expiring-soon count (reuses I10), wallet liability.

Primary files:
`backend/app/services/storefront.py`, `backend/app/bot/storefront/handlers.py`.

## I12 - Per-sub PDFs from persisted invoice lines

Priority: P3. Version: PATCH. Status: DONE in `v1.53.1`. **Program complete.**

- Delivered invoices already render from persisted lines; fix the ON-DEMAND
  sub/interim PDF paths: when the requested period has a persisted non-draft
  invoice for the root, source lines from `InvoiceLine` filtered by the sub's
  subtree (snapshot pruning currently shrinks historic on-demand PDFs); keep the
  live path for the open period. Scheduled after I07 so the bot call site is
  touched once.

Primary files:
`backend/app/services/invoice_pdf.py`, `backend/app/services/reseller_report.py`.

## Owner decisions pending

- I02: memory-limit sizing must be validated against production `docker stats`.
- I04: whether `AnswerCallbackQuery` toasts also get `rtl()` (excluded initially).
- I09: a pagination-free backend export endpoint if real datasets exceed the
  current list caps (2000/2000/10000).
- I10: per-storefront expiry-notification toggle (global-only first).
- I04: whether the reseller is notified when their storefront bot is auto-marked
  errored (not initially).

## Recommended order

`I01 -> I02 -> I03 -> I04 -> I05 -> I06 -> I07 -> I08 -> I09 -> I10 -> I11 -> I12`
