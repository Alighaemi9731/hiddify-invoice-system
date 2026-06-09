# Changelog

All user-visible changes, important fixes, migrations, and operational notes are
recorded here from `v1.37.35` onward. Older detailed history remains available in
`CLAUDE.md` and Git commit/tag history.

## Unreleased

No changes yet.

## 1.37.46 - 2026-06-09

Audit remediation B08 — build, test, and frontend quality gate.

### Fixed

- Added a maintained mypy configuration and resolved the high-signal typing failures in
  billing, payment, delivery, enforcement, API relationship loading, and ORM models.
  Untyped third-party packages and aiogram callback narrowing are isolated explicitly
  instead of globally suppressing type errors.
- Expanded Ruff from undefined-name checks to import, syntax, modernization, whitespace,
  ambiguous-name, and exception-chaining rules; CI now runs the full configured baseline.
- Normalized naive/aware snapshot timestamps before billing freshness comparisons, avoiding
  a runtime `TypeError` on SQLite/legacy timestamp rows.
- Added an isolated staging Compose stack bound to localhost with separate volumes, no bot,
  and scheduler jobs disabled.
- Split React, MUI, ECharts/zrender, data, and animation dependencies into bounded frontend
  chunks. The production build now fails when any JavaScript chunk exceeds 500 KiB.

### Verification

- Added an integrated billing → manual payment → ledger → backup workflow regression test.
- Backend gate: 83 tests, Ruff, mypy over 92 source files, and Alembic drift checks.
- Frontend production build/typecheck passes with every JavaScript chunk below 500 KiB.
- CI validates both production and isolated staging Compose configurations.

## 1.37.45 - 2026-06-09

### Fixed

- Preserved application logging when Alembic migrations run inside backend/bot startup.
  The migration environment now configures console logging only for direct Alembic CLI use,
  preventing startup migration from disabling normal service logs.

### Verification

- Added a subprocess regression test that runs the programmatic migration path and verifies
  that an existing application logger, level, and handler remain unchanged.

## 1.37.44 - 2026-06-09

Audit remediation B07 — database evolution and input contracts.

### Fixed

- Replaced startup `create_all` / ad-hoc `ADD COLUMN` evolution with versioned Alembic
  migrations. Fresh databases run the complete baseline; existing pre-Alembic databases are
  stamped only after every expected table and column is validated, then upgraded to head.
- Serialized backend/bot migration startup with a PostgreSQL advisory lock, preventing both
  processes from racing to migrate the same database.
- Added database check constraints for non-negative invoice, ledger, payment, reseller
  pricing/cap, and usage-meter values.
- Added strict API validation for non-negative invoice/reseller edits and capacity bumps.
  Runtime settings now use a known-key allowlist, strict value types, safe ranges, finite
  numeric values, read-only internal keys, and atomic bulk validation before writes.
- Reseller-tree construction now uses case-insensitive panel-scoped UUID identities, detects
  cycles, and surfaces malformed cyclic components without recursion failure or hidden rows.
- Replaced mutable Pydantic list defaults with `Field(default_factory=list)`.

### Verification

- Added migration tests for fresh install, safe adoption of an existing schema, rejection of
  an incomplete schema, input contracts, atomic settings, and cyclic reseller trees.
- Rehearsed the migrations against a restored clone of the production PostgreSQL database:
  revision `6a9c7f21d4e0`, 23 non-negative constraints, then removed the temporary database.

## 1.37.43 - 2026-06-09

Audit remediation B06 — bot identity, membership, and input safety.

### Fixed

- Forced channel/group membership is now checked for every private bot message, including
  direct slash commands and payment-state text/photos, not only inline button callbacks.
  `/start` remains available for join links and `/cancel` remains available to exit a flow.
  Membership-check failures now fail closed instead of granting access.
- Panel-link registration now requires one unique normalized `host + proxy path + UUID`
  match. Incomplete, mismatched, or ambiguous links are rejected instead of falling back to
  the first reseller with the same UUID.
- User names and support-message text are HTML-escaped before Telegram HTML rendering.
  The legacy invoice-template wallet placeholder is escaped as well.
- Bot and invoice API payment/deadline eligibility checks now use the same Tehran-local date
  helper, avoiding different results around UTC/Tehran midnight.

### Verification

- Added router-middleware, matching ambiguity, HTML-injection, and Tehran-date regression
  coverage in `tests/test_bot_identity_safety.py` and expanded `tests/test_matching.py`.

## 1.37.42 - 2026-06-09

Audit remediation B05 — enforcement and reminder consistency.

### Fixed

- A partial restore (some users fail to re-enable) now keeps the reseller **enforced** so the
  next trigger retries, instead of flipping to active and leaving those users disabled forever.
  The restore snapshot is preserved for the retry; the reseller is marked active only when every
  user re-enable succeeds.
- A pending (under-review) payment now pauses dunning only on **its own invoice** — not on the
  customer's unrelated invoices or other panels — matching the per-invoice payment model. The
  hold also **expires** after `pending_payment_hold_days` (default 7), so a stale, never-reviewed
  proof can no longer shield a debt indefinitely.
- The daily dunning report now distinguishes **delivered** reminders from merely **attempted**
  ones (a reminder that was blocked/unmatched/errored is shown as such, not counted as sent).
- The per-sub GB-cap monthly alert flag is armed only after the alert **actually reaches every
  configured recipient**; a transient Telegram failure is retried on the next check instead of
  being suppressed for the rest of the month.

### Verification

- Added `tests/test_enforcement_dunning.py`: partial-restore-stays-enforced-then-succeeds,
  per-invoice hold with expiry plus attempted-vs-delivered counting, and the GB-cap
  flag-only-after-delivery behaviour.

## 1.37.41 - 2026-06-09

Audit remediation B04 — billing and synchronization correctness.

### Fixed

- Billing now excludes any panel whose latest sync failed or never ran, instead of
  invoicing last month's stale snapshots. Skipped panels are reported in the generate
  result and the monthly job notifies the owner so the shortfall is never silent.
- A leftover DRAFT invoice whose reseller drops to zero usage (or is removed from the
  panel) is reconciled away when the period is regenerated, so a stale positive draft
  can no longer be delivered.
- A reseller (admin) removed from the panel is no longer billed forever — billing skips
  resellers not present in the panel's latest sync.
- The backup fetch now requires BOTH the `admin_users` and `users` collections (as lists,
  with a non-empty admin set). A truncated/partial backup fails the sync — which then
  excludes that panel from billing — instead of silently looking like every user/admin
  was deleted.
- In auto mode the exchange rate falls back to the manual rate when the cached live rate
  is stale (older than the new `rate_max_age_hours`, default 48h), so billing never uses a
  days-old quote when the source has been down.
- The "zero sale" preview now folds in the abuse-metered extra, so a reseller billed only
  on metered overage no longer shows up as a zero sale.

### Deferred (within B04, documented)

- Rendering the per-sub usage-breakdown PDFs from persisted invoice lines rather than live
  snapshots: the payable amount is always authoritative (rendered from the locked invoice
  row in the text and the line-based invoice PDF), and the per-node PDFs are GB-only usage
  breakdowns; reworking that pipeline is a larger, higher-risk change tracked as a follow-up.

### Verification

- Added `tests/test_billing_sync.py`: panel-billable gate, removed-reseller exclusion,
  partial-backup rejection, stale-rate fallback, and an end-to-end generate that skips a
  failed-sync panel and reconciles a zeroed draft while leaving the failed panel's draft intact.

## 1.37.40 - 2026-06-09

Audit remediation B03 — payment and invoice state machine.

### Fixed

- Invoice status transitions are now enforced in one place (`app/services/invoice_state.py`).
  The panel can no longer cancel/defer/edit a paid invoice, mark a draft or canceled invoice
  paid, or defer a draft/paid/canceled invoice — each returns a clear `400` instead of
  silently corrupting state. The invoice action buttons are gated to match.
- Confirming a payment for one invoice no longer restores a suspended reseller while other
  due (non-deferred) invoices remain — enforcement lifts only when the reseller has no other
  current debt. The same guard applies to the manual «ثبت پرداخت».
- Rejecting or deleting a payment no longer un-pays an invoice that another confirmed payment
  still settles.
- When a confirmed payment is reversed (reject/delete) or an invoice is un-marked paid, the
  invoice gets a fresh dunning window (reminder/warning marks cleared, `sent_at` re-anchored)
  instead of jumping straight back to overdue/enforcement on the next run.
- Reverting an invoice to unpaid clears the stale settling TXID from the durable financial
  ledger, so the ledger never shows a transaction hash against an unpaid invoice.
- A submitted TXID/receipt re-validates the chosen invoice under lock (still owned, still owed,
  not deferred to the future); a stale selection falls back to the oldest payable invoice
  instead of mis-attributing the payment.

### Verification

- Added regression tests (`tests/test_invoice_state.py`) for the transition matrix and
  operation guards, the multi-payment "don't un-pay an invoice settled elsewhere" rule, ledger
  TXID clearing + dunning reset on reversal, the restore-only-when-no-other-debt invariant, and
  the under-lock proof re-validation.

## 1.37.39 - 2026-06-09

Audit remediation B02 — backup, restore, and operational recovery.

### Fixed

- A backup is now refused (with a clear error) when no usable database image can be
  produced: a failed/empty `pg_dump` or an invalid SQLite file raises instead of shipping
  a dump-less archive that was previously reported as a successful backup.
- The scheduled backup job now notifies the owner on Telegram when an automatic backup
  fails, instead of failing silently.
- Postgres restore is now atomic: the import runs in a single transaction
  (`--single-transaction`, `ON_ERROR_STOP`), so a mid-restore failure rolls back and the
  live database is left exactly as it was — never half-dropped. A pre-restore safety dump
  of the current database is kept on disk before each restore.
- A restored `SECRET_KEY` is written to `.env` only after the database restore succeeds.
  A failed restore no longer leaves a new key against an unchanged database.
- Uploaded backups are validated before anything is read: archive size cap, member
  allowlist, per-member and total decompressed-size limits, compression-ratio (zip-bomb)
  guard, and `meta.json` shape.
- Blocking `pg_dump`/`psql` work runs off the request event loop (panel and bot restore).
- After a successful restore both the backend and the bot self-restart (via a shared
  restart marker) so neither keeps a stale `SECRET_KEY` or a handle to the pre-restore DB.

### Added

- Optional password-protected backups: set a `backup_passphrase` (Settings → زمان‌بندی) to
  encrypt every archive (PBKDF2 → Fernet). Restore then requires the same passphrase,
  entered on the panel restore form or read from the configured setting. Off by default —
  unencrypted self-sufficient cross-server restore is unchanged when no passphrase is set.

### Verification

- Added regression tests for dump/SQLite validation, passphrase encryption round-trip and
  wrong/missing passphrase, archive guards (stray member, oversize, zip bomb), the
  persist-key-only-after-success invariant on both failure and success paths, refusal to
  build a dump-less backup, encrypted-restore-without-passphrase, and the loop-free
  cross-process restart signal.

## 1.37.38 - 2026-06-09

### Security

- JWT authentication now fails closed with `503` when live account validation cannot
  reach the database; a signed token is never trusted by itself.
- Protected APIs require an active owner account plus matching mandatory `role` and
  `epoch` claims. Legacy or role-mismatched tokens are rejected.
- Password and passkey login reject non-owner accounts.
- New passwords enforce bcrypt's 72-byte UTF-8 limit with a controlled validation error.
- First-run setup is serialized with an in-process lock and a PostgreSQL row lock so
  concurrent requests cannot create multiple owners.

### Fixed

- Starting TOTP setup no longer overwrites an active authenticator secret. A replacement
  secret is stored separately and becomes active only after a valid confirmation code.
- Disabling TOTP clears both active and pending secrets.
- Existing databases receive the nullable pending-secret column through the current
  additive schema synchronization path.

### Verification

- Added regression coverage for DB failure, missing/mismatched JWT claims, inactive and
  non-owner accounts, bcrypt byte limits, TOTP replacement rollback/confirmation,
  concurrent setup, and legacy-schema column addition.

## 1.37.37 - 2026-06-09

### Fixed

- Made the Compose validation job self-contained by generating a temporary CI-only
  `.env` with the required dummy PostgreSQL password.

### Verification

- The preceding `v1.37.36` CI run proved backend and frontend jobs green and exposed
  the missing Compose interpolation input. This patch corrects that infrastructure-only
  failure without changing application behavior.

## 1.37.36 - 2026-06-09

### Fixed

- Restored a clean TypeScript build by typing invoice list responses and importing
  MUI's `PaletteMode` from its supported public entry point.
- Corrected the Playwright CAPTCHA locator to match the actual Persian image label.

### Changed

- Production frontend builds now run TypeScript checking before Vite.
- Docker frontend builds use the committed lockfile with `npm ci`.
- E2E tests require an explicit target and refuse production unless the operator
  deliberately enables a read-only production run.
- Added GitHub Actions checks for backend tests, Ruff, frontend type/build, deploy
  script syntax, and Compose configuration.

### Documentation

- Added a staged remediation tracker for the 2026-06-09 whole-codebase audit.
- Added a repeatable release, production deploy, smoke-check, and rollback process.
- Added local-only production operator metadata and Claude commands for batch fixes
  and releases.
- Corrected stale deployment and manual-payment documentation.

### Verification

- Backend: 36 tests passed; Ruff `F` checks passed.
- Frontend: TypeScript and Vite production build passed.
- Playwright: all 6 tests collected with an explicit non-production target.
- Deploy scripts passed `bash -n`; production Compose config validated.

## 1.37.35 - 2026-06-09

### Fixed

- Treated metering overage tolerance as a threshold: overage at or below the
  tolerance is ignored, while overage above it is billed in full.

### Verification

- Backend tests: 36 passed.
- Frontend Vite production build completed successfully.
- The later whole-codebase audit identified TypeScript and broader typing/lint debt;
  remediation starts with batch B00 in `docs/REMEDIATION_PLAN.md`.
