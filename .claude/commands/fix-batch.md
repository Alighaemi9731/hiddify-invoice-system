---
description: Fix one audited remediation batch end-to-end, including tests and docs
---

Fix exactly one batch from `docs/REMEDIATION_PLAN.md`, identified by `$ARGUMENTS`.

Rules:

1. Read `CLAUDE.md`, the selected batch, and every referenced implementation/test file.
2. Restate the invariants and failure modes before editing.
3. Do not mix unrelated cleanup or another remediation batch into the change.
4. Add regression tests that fail on the old behavior.
5. Run focused tests first, then the complete release gate.
6. Update the batch status and all behavior documentation that changed.
7. Stop before commit/release/deploy unless those actions were explicitly requested.
8. Never read, print, commit, or copy secrets. Production coordinates are local-only.

For payment, billing, backup, restore, or migration changes, include a rollback and
data-integrity review in the final report.
