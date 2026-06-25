# Versioning policy

The project version lives in `VERSION` (and is mirrored in `backend/app/__init__.py`). It is a
**Semantic Version** `MAJOR.MINOR.PATCH` (e.g. `1.38.0`). Every release MUST pick the next number with
this rule. The release process (`docs/RELEASE_PROCESS.md` §3) reads this file before bumping.

The single question to ask: **what is the largest kind of change in this release?**

## MAJOR — `X.0.0` (bump the first number, reset the other two to 0)
A breaking / incompatible release: upgrading an existing production install needs **manual operator
action**, or existing behavior/data is broken. Examples:
- a database change that is **not** applied automatically by the boot-time Alembic migrations (needs a
  manual step, a data backfill, or is destructive to existing rows);
- a changed or removed **API endpoint / request contract / setting key** that an existing frontend, bot,
  installer, or external client depends on;
- a changed **`.env` / deployment contract** (renamed/removed required variable, new mandatory secret);
- a fundamental architecture or data-model change (e.g. swapping the database engine, re-keying money).

If a normal `release-installer.sh` run on a live server would break or need hand-holding → MAJOR.

## MINOR — `1.Y.0` (bump the middle number, reset PATCH to 0)
A **new, backward-compatible feature or capability**, or a substantial enhancement. Existing installs
upgrade with **no manual steps** (auto-migrations only, no contract breakage). Examples:
- a new page, tab, bot command, or management action (e.g. the sub-reseller **«توقف ساخت کاربر» / freeze**);
- a new payment method, a new report, a new scheduler job;
- a meaningful new option/setting that defaults to today's behavior.

## PATCH — `1.37.Z` (bump the last number)
A backward-compatible change that adds **no new capability**:
- bug fix, correctness fix, security patch, performance/optimization;
- refactor, dependency bump, copy/UI tweak, doc-only change;
- additive nullable column applied automatically by a migration with no behavior change.

## Decision checklist
1. Does upgrading a live install need a manual step, or does it break an existing contract/data? → **MAJOR**.
2. Otherwise, does it add a new user-facing feature/capability or a substantial enhancement? → **MINOR**.
3. Otherwise (fix / perf / refactor / dep bump / docs / copy) → **PATCH**.

## Worked examples (this project)
| Release | Change | Bump |
|---------|--------|------|
| v1.37.105 | per-target user-id resolution (perf, no new feature) | PATCH |
| v1.37.106 | owner can disconnect a reseller's Telegram binding | (was PATCH; a small capability → MINOR under this policy) |
| v1.37.108 | pay several invoices with one transfer | feature → MINOR going forward |
| v1.38.0 | sub-reseller limits-only **freeze** («توقف ساخت کاربر») | **MINOR** (new capability, auto-upgrades cleanly) |
| a destructive/manual DB migration, or a removed endpoint | — | **MAJOR** |

> Note: releases before this policy (≤ v1.37.109) bumped only PATCH regardless of size. From here on, apply
> the rule above — a new feature moves MINOR (so the long `1.37.x` run ends at the next feature release).
