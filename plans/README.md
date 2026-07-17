# Implementation Plans

Generated with the `improve` workflow on 2026-07-16. Execute in the order below unless
dependencies say otherwise. Each executor must read its plan fully, honor its STOP
conditions, and leave evidence for every verification claim.

Plan 002 is the only dispatch-ready implementation plan at the current source commit. Plans
003–007 are approved-design blueprints, not stale executable handoffs: after each predecessor
ships, the reviewer must re-open the actual released source, replace the planned-at SHA, resolve
drift, turn the broad blueprint scope into an exact file allowlist, and write exact commands before
the next plan may move to `IN PROGRESS`. Updating only the SHA is not sufficient.

When the owner says to start a numbered plan, that approval covers the complete slice described in
that plan: implementation, tests, version/changelog, release, production deploy and smoke/rollback
verification. Stop for a second approval only if the implementation reveals a new destructive
migration, external prerequisite, product choice, or scope expansion not already described here.

## Execution order & status

| Plan | Title | Priority | Effort | Depends on | Status |
|---|---|:---:|:---:|:---:|---|
| 001 | Open the reseller portal directly in a normal browser | P1 | S | — | DONE |
| 002 | Add the storefront portal shell and read-only dashboard | P1 | L | 001 | DONE |
| 003 | Add audited shared plan and settings management | P1 | L | 002 | DONE (v1.84.0) |
| 004 | Add tenant-safe customer and subscription management | P1 | L | 003 | DONE (v1.85.0) |
| 005 | Add the wallet and top-up operations center | P1 | L | 003, 004 | DONE (v1.86.0) |
| 006 | Add credit-code analytics and durable communications | P2 | L | 003, 004, 005 | DONE (v1.87.0) |
| 007 | Close bot/portal parity and simplify the storefront bot | P2 | M | 002–006 | TODO |

Status values: TODO | IN PROGRESS | DONE | BLOCKED (with one-line reason) | REJECTED
(with one-line rationale).

## Dependency notes

- Plan 001 has no migration or product-plan dependency. It reuses the existing secure,
  short-lived, one-time portal login-token exchange.
- Plan 001 implementation review approved commit `f7770a2`, merged to `main`, and assigned
  release `v1.82.3`. Production deployment and smoke evidence are recorded in the roadmap.
- Plans 002–007 are the six independently releasable slices of product-roadmap item 3.
  They must be executed in order because each mutation slice depends on the storefront
  access, audit, idempotency, and shared-command contracts established before it.
- Candidate releases are `v1.83.0` through `v1.88.0`. Recalculate the exact SemVer if an
  intervening release lands; every slice is a backward-compatible user-facing capability and
  therefore a MINOR under `docs/VERSIONING.md`.

## Findings considered and rejected

- Telegram Mini App/Web App was rejected by the product owner for plan 001; the button
  must be a normal HTTPS URL button.
- Telegram `LoginUrl` was deferred because it requires a BotFather-linked domain and a
  separate Telegram login-signature verification flow. That external prerequisite is not
  needed for this UX improvement.
- Product-roadmap item 2 (user creation in the portal) was rejected by the owner on
  2026-07-16: reseller users can already be created from the Telegram bot or directly in
  Hiddify, so a third creation surface would add maintenance without enough value.
- Storefront co-admin web access is not implicit in plans 002–007. Existing co-admin IDs are
  not reseller portal principals. They continue to manage through Telegram; normalized team
  membership/RBAC remains roadmap item 15 unless separately approved.

## Item 3 scope boundary

- Included: all 14 daily storefront-admin bot capabilities, web-native customer preview,
  dashboard, customer/service management, wallet/top-up decisions, credit codes, durable
  broadcast delivery, audit/idempotency, and final bot simplification.
- Excluded: portal user creation (item 2), customer Mini App/portal (item 9), authoritative
  profit accounting (item 12), CRM automation/recurring campaigns (item 14), full team RBAC
  (item 15), global audit UI (item 17), and white-label work (item 20).
