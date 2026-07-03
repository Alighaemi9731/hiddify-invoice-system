# UI/UX plan

Execution tracker for the UI/UX improvement program from the owner-reported review on
2026-07-02 against `v1.53.2` (successor to the completed `docs/IMPROVEMENT_PLAN.md`,
I01–I12). Fix one batch at a time; every batch runs the full verification gate, a
release, and a production smoke check before the next starts.

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
npm run build   # tsc --noEmit + Vite + scripts/check-bundle.mjs (600 KiB budget)

cd ..
bash -n deploy/*.sh get.sh
bash deploy/test-release-tools.sh
```

## Reported problems (root causes verified in code)

- **Dark-mode "haze" on cards**: dark card surface was `rgba(255,255,255,0.07)` + `blur(40px)`;
  mobile row-cards stacked a SECOND translucency `alpha(paper,0.48)` → ~3.4% white over black +
  heavy blur = washed gray. `text.secondary #86868b` borderline. StatCard icon double-blur halo.
- **Mobile ergonomics**: 7–10 icon-only 32px tooltip-dependent buttons per card; 14-item drawer
  (2+ taps everywhere); dialogs not fullScreen; crowded AppBar at 360px.
- **PWA**: silent `autoUpdate + skipWaiting` (can 404 lazy chunks mid-session; no update prompt);
  missing `viewport-fit`/apple meta.
- **Portal** (shares the theme via the single Root → theme fixes propagate): 7-day session forces
  re-tapping the bot; no auto dark; no enforcement banner / pay-overdue shortcut.

Owner decisions: dark = "glass but readable" (near-opaque cards, glass stays on sidebar/AppBar/
dialogs); bottom nav + drawer; 1–2 primary labeled buttons + ⋮ menu on mobile cards; portal =
QoL + 30-day sliding session (NO owner-panel end-users page).

## U01 - Dark-mode readability

Priority: P1. Version: PATCH. Status: DONE in `v1.53.3`.

- `theme.ts` dark branch only (light byte-identical): `glassBg` → `rgba(28,28,30,0.90)`;
  `glassBlur` dark → `blur(20px) saturate(140%)`; `text.secondary` → `#a1a1a6`; divider
  0.08→0.10; table hover 0.04→0.07, even-stripe 0.02→0.03; resp-table row bg near-opaque +
  reduced blur; resp-table label color `#a1a1a6`. New exported `nestedCardBg(t)`.
- Replace `alpha(paper,0.48)` with `nestedCardBg` in the Invoices/Payments/Resellers mobile
  cards. Remove the StatCard icon `blur(12px)` halo.

Primary files: `frontend/src/theme.ts`, `frontend/src/pages/Invoices.tsx`,
`frontend/src/pages/Payments.tsx`, `frontend/src/pages/resellers/ResellerMobileCard.tsx`,
`frontend/src/components/StatCard.tsx`.

## U02 - Mobile ergonomics

Priority: P1. Version: MINOR. Status: DONE in `v1.54.0`.

- New `BottomNav.tsx` (4 destinations + «بیشتر» → drawer, `< md` only, safe-area padding);
  Layout content bottom-padding + AppBar tidy.
- New `RowActionsMenu.tsx`: `RowActionIcons` (desktop, unchanged) + `RowActionsMenu` (mobile:
  1–2 labeled primary buttons + a ⋮ Menu). Pages expose `actionsFor(row): RowAction[]`.
- `useXsFullScreen()` helper → fullScreen dialogs on xs (invoices/payments/resellers + portal
  PayDialog). `.MuiDialog-paperFullScreen { border-radius: 0 }`.

Primary files: `frontend/src/components/BottomNav.tsx`,
`frontend/src/components/RowActionsMenu.tsx`, `frontend/src/components/Layout.tsx`,
`frontend/src/responsive.ts`, `frontend/src/pages/{Invoices,Payments}.tsx`,
`frontend/src/pages/resellers/ResellerActions.tsx`.

## U03 - PWA polish

Priority: P2. Version: PATCH. Status: TODO.

- `vite.config.ts`: `registerType: "prompt"`, drop `skipWaiting`. New `UpdateToast.tsx`
  (`useRegisterSW`, themed Snackbar «نسخهٔ جدید — بارگذاری مجدد», hourly update check) mounted in
  `main.tsx`. `index.html`: `viewport-fit=cover` + apple-web-app + theme-color media pair +
  toggle-synced theme-color. Safe-area top padding on AppBars.

Primary files: `frontend/vite.config.ts`, `frontend/src/components/UpdateToast.tsx`,
`frontend/src/vite-env.d.ts`, `frontend/index.html`, `frontend/src/main.tsx`,
`frontend/src/components/Layout.tsx`, `frontend/src/portal/PortalLayout.tsx`.

## U04 - Portal QoL + sliding session

Priority: P2. Version: MINOR. Status: TODO. Only batch touching the backend.

- `portal_auth.py`: `PORTAL_SESSION_TTL_MIN = 30*24*60`. `portal.py`: `POST /api/portal/auth/refresh`
  (guarded by `get_current_reseller`) → fresh 30-day token; login-link mechanics untouched;
  revocation = unbind/delete reseller (per-request re-check) or SECRET_KEY rotation.
- `portalClient.ts`: sliding renewal (store token ts; refresh once when > 24h old).
- Portal Dashboard: enforcement banner, «پرداختِ بدهی» pay-all shortcut, payment-method chips.
- Auto dark on first visit (seed `color_mode` from `prefers-color-scheme` when unset).

Primary files: `backend/app/core/portal_auth.py`, `backend/app/api/portal.py`,
`backend/tests/test_portal.py`, `frontend/src/portal/portalClient.ts`,
`frontend/src/portal/pages/Dashboard.tsx`, `frontend/src/main.tsx`.

## Not touched (all batches)

Desktop table visuals; light-mode glass (except the no-op StatCard blur removal); `.resp-table`
layout pattern; dialog/menu/sidebar/AppBar glass recipe; ECharts vendor chunk; backend outside
U04's two files; no migrations.

## Recommended order

`U01 -> U02 -> U03 -> U04`

## Owner decisions pending

- U03: PWA updates become user-driven (tap the toast) — documented ops change.
- U04: 30-day tokens widen the theft window; portal token-epoch revocation earmarked as a future
  isolated migration batch.
