---
description: Heavy, whole-codebase review — deterministic tools first, then semantic logic/consistency review (report only, no edits)
---

You are doing a thorough, read-only review of this repository. **Do not change any code** — only produce a report.

## Step 1 — run the deterministic tools and summarize their output

Backend (from `backend/`, using the venv: `.venv/bin/<tool>`):
- `ruff check app/ --select F` — unused imports/vars, redefinitions (install ruff if missing).
- `vulture app/ --min-confidence 80` — dead-code candidates (NOTE: it false-positives on
  decorator-registered routes/handlers, ORM columns, pydantic/enum members — verify before trusting).
- A precise dead-function pass: for each non-decorated `def`/`async def`, grep the whole tree for
  its name; if it appears only at its definition, it's truly dead.
- `mypy app/ --ignore-missing-imports --no-error-summary` — scan for high-signal errors only
  (`is not defined`, `has no attribute`, `unexpected keyword`, wrong arg counts), not type noise.

Frontend (from `frontend/`):
- `npx tsc --noEmit --noUnusedLocals` — unused locals/imports.
- A dead-export pass: each `export const/function` never referenced elsewhere in `src/`.

Report what the tools found, with file:line. Trust the tools for dead imports/vars; verify each
dead-function/dead-export candidate by grep before listing it.

## Step 2 — semantic review the tools can't do

Read the changed/whole code and report, grouped by **confidence** (must-fix / should-fix / nit) and by category, each with `file:line`:

1. **Real bugs & logic errors** — off-by-one, wrong status/branch, missing await, unhandled None, money/rounding, tz.
2. **Logic mismatches after recent changes** — when a behavior changed, did every dependent place change too? (UI labels, API endpoints, schemas, bot menus/commands, `/help` text, Help page, comments/docstrings that now lie.) This is the most important category for this project — it has been iterated heavily.
3. **Dead/unused code & imports** — from Step 1, verified.
4. **Type errors & edge cases** — empty lists, None, malformed input.
5. **Cross-module inconsistency** — two places that should agree but don't (e.g. a count computed two ways, a constant duplicated).

## Rules
- Report only — make NO edits.
- Cite `file:line` for every item; keep it high-signal.
- Focus the semantic pass on the diff vs `main` if a branch is given in `$ARGUMENTS`, otherwise the whole codebase.
- Before finishing, double-check the invoice formula, the payment flow, and the dunning/enforcement
  state machine against `CLAUDE.md` — these are the project's highest-stakes logic.
