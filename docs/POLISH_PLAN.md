# Polish plan

Execution tracker for the polish program from the owner review on 2026-07-03 against
`v1.55.0` (successor to the completed `docs/UIUX_PLAN.md`, U01–U04). One batch per
release; full gate + production smoke before the next.

Status values: `TODO`, `IN PROGRESS`, `DONE`, `DEFERRED`.

## Release gate for every batch

```bash
cd backend && .venv/bin/pytest -q && .venv/bin/ruff check app tests alembic && .venv/bin/mypy app && .venv/bin/pip check
cd ../frontend && npm ci && npm audit && npm run build   # tsc + Vite + 600 KiB budget
cd .. && bash -n deploy/*.sh get.sh && bash deploy/test-release-tools.sh
```

## Local visual-verification harness (NOT committed)

A throwaway `seed_local.py` seeds a SQLite DB (panels/resellers-with-subtree/invoices/
payments/snapshots) and prints a valid owner JWT (skips the login CAPTCHA). Run
`uvicorn` on :8010 + `npm run dev` pointed at it; Playwright sets `invoice_token` +
`color_mode=dark` and screenshots `/resellers`, `/invoices`, `/financial-history` at
375/1440. This is how UI batches are verified before release.

**Key diagnosis (P01):** the current code renders dark-mode cards CRISP (verified
locally) — the owner's "foggy" screenshot is a **stale service-worker precache** of
pre-`v1.53.3` CSS, not a live bug.

## P01 - Dark-mode delivery + mobile tab scroll

Priority: P1. Version: PATCH. Status: DONE in `v1.55.1`.

- Restore `registerType: "autoUpdate"` + `skipWaiting` so a new deploy's SW activates
  immediately (sw.js is no-cache → always revalidated), unsticking clients stranded on
  stale precached CSS by the `prompt` mode. ErrorBoundary gains a one-shot chunk-error
  reload to cover autoUpdate's rare mid-session lazy-chunk 404. Removed the now-inert
  `UpdateToast`.
- `SegmentedTabs.tsx`: drop the `.MuiTabs-scroller { overflow: visible !important }`
  override (it defeated `variant="scrollable"` → the «نماینده‌های غایب» segment was
  unreachable); the scroller is now `overflow-x: auto` and scrolls. Verified via DOM
  (scrollWidth > clientWidth, all 3 tabs present).
- No card CSS change needed (already correct).

Primary files: `frontend/vite.config.ts`, `frontend/src/components/ErrorBoundary.tsx`,
`frontend/src/components/SegmentedTabs.tsx`, `frontend/src/main.tsx`.

## P02 - Responsive desktop tables + pagination

Priority: P1. Version: MINOR. Status: DONE in `v1.56.0`.

- Wrap wide tables in a horizontally-scrollable `TableContainer` (min-width table,
  `overflowX: auto`) so columns scroll instead of being clipped at 100% zoom; stop the
  `<Card overflow:hidden>` clipping. Add `TablePagination` to FinancialHistory, Logs
  (delivery + enforcement), Debts. Keep desktop icon action rows.

Primary files: `frontend/src/pages/{Invoices,Payments,FinancialHistory,Logs,Sales,Debts}.tsx`,
`frontend/src/pages/resellers/ResellerTable.tsx`, `frontend/src/theme.ts`.

## P03 - Min-sale floor: first-invoice grace + clearer text

Priority: P2. Version: MINOR. Status: DONE in `v1.57.0`. Billing — isolated.

- Skip the floor on the reseller's FIRST non-draft invoice (query prior invoices); apply
  from the next. Clearer Persian floor-explanation text on the delivered invoice only.
  Verify PDFs stay real-GB and interim shows real usage; confirm the Settings UI exposes
  `default_min_sale_toman` + per-reseller `min_sale_toman`. Tests.

Primary files: `backend/app/services/invoicing.py`, `backend/app/services/invoice_engine.py`,
`backend/app/services/delivery.py`, `backend/app/services/reseller_report.py`.

## P04 - Storefront renewal display clarity

Priority: P3. Version: PATCH. Status: DONE in `v1.57.1`.

- Relabel the my-services view so a renewed plan reads unambiguously (plan gb vs current
  limit incl. renewal). Keep the accumulate behavior.

Primary files: `backend/app/bot/storefront/handlers.py`, `backend/tests/test_storefront.py`.

## P05 - Backend hygiene

Priority: P3. Version: PATCH. Status: TODO. Migration batch — released alone.

- `DeliveryLog.tg_message_ids` `String(255)` → `Text`; drop write-only `Invoice.pdf_path`
  (same migration). Retention sweeps in `maintenance.py` for `data/payment_proofs/`
  (terminal payments) + `data/invoices/` PDFs; `portal_login_nonce` + `bot_users` prune.
  Ledger-safety warning comment at `wipe_data`. Tests + PG16 migration up/down.

Primary files: `backend/app/models/{logs,invoice}.py`, `backend/alembic/versions/`,
`backend/app/services/maintenance.py`, `backend/app/services/settings_service.py`,
`backend/app/api/operations.py`.

## Recommended order

`P01 -> P02 -> P03 -> P04 -> P05`
