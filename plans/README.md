# Implementation Plans

Generated with the `improve` workflow on 2026-07-16. Execute in the order below unless
dependencies say otherwise. Each executor must read its plan fully, honor its STOP
conditions, and leave evidence for every verification claim.

## Execution order & status

| Plan | Title | Priority | Effort | Depends on | Status |
|---|---|:---:|:---:|:---:|---|
| 001 | Open the reseller portal directly in a normal browser | P1 | S | — | DONE |

Status values: TODO | IN PROGRESS | DONE | BLOCKED (with one-line reason) | REJECTED
(with one-line rationale).

## Dependency notes

- Plan 001 has no migration or product-plan dependency. It reuses the existing secure,
  short-lived, one-time portal login-token exchange.
- Plan 001 implementation review approved commit `f7770a2`, merged to `main`, and assigned
  release `v1.82.3`. Production deployment and smoke evidence are recorded in the roadmap.

## Findings considered and rejected

- Telegram Mini App/Web App was rejected by the product owner for plan 001; the button
  must be a normal HTTPS URL button.
- Telegram `LoginUrl` was deferred because it requires a BotFather-linked domain and a
  separate Telegram login-signature verification flow. That external prerequisite is not
  needed for this UX improvement.
