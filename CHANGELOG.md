# Changelog

All user-visible changes, important fixes, migrations, and operational notes are
recorded here from `v1.37.35` onward. Older detailed history remains available in
`CLAUDE.md` and Git commit/tag history.

## Unreleased

No changes yet.

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
