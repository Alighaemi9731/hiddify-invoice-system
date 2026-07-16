# Plan 007: Close bot/portal parity and simplify the storefront bot

> Execute only after explicit approval and plans 002–006 are DONE. **Drift check**:
> `git diff --stat 2514a96..HEAD -- backend/app/bot/storefront backend/app/bot/handlers/common.py backend/app/bot/handlers/storefront_setup.py backend/app/api/portal_storefront.py frontend/src/portal plans docs CHANGELOG.md`
> Plans 002–006 will intentionally cause drift; this is a design blueprint. Re-read the released
> predecessor, regenerate exact schemas/file allowlist/commands and refresh the SHA before dispatch;
> replacing only the SHA is insufficient.

## Status

- **Roadmap item**: 3, Release F of F
- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans 002–006
- **Category**: direction / docs
- **Planned at**: commit `2514a96`, mandatory reconcile after plan 006
- **Candidate release**: `v1.88.0`

## Why this matters

Moving functionality to the portal only delivers the original product goal if the Telegram bot stops
being a crowded second admin panel. This final slice proves parity against all fourteen existing menu
entries, adds context deep links, keeps urgent actions/notifications available and makes the portal the
primary management surface without stranding co-admins or older messages.

## Final product decision

- Primary storefront owner: gets a compact inline admin home with a standard HTTPS «مدیریت فروشگاه
  در مرورگر» URL, urgent pending-top-up shortcut, compact stats, customer preview and help.
- Co-admins: remain Telegram-admin users and retain the full legacy management menu because they are
  not reseller portal principals. The UI states this clearly. Do not grant them ordinary reseller JWTs.
- Full co-admin web access is a separate optional RBAC release under roadmap item 15: normalized
  memberships, storefront-scoped login/session epoch/permissions, and zero access to invoices/panels.
- Customer bot behavior, config/QR delivery, membership gate and notifications remain Telegram-native.
- No Mini App is introduced.
- Final primary-owner inline home contains exactly: HTTPS URL «مدیریت فروشگاه در مرورگر», callback
  «شارژهای در انتظار», callback «آمار سریع», callback «نمای مشتری», and callback «راهنما».
  Entering owner admin mode sends `ReplyKeyboardRemove`; `/start` regenerates a fresh URL. The 14
  legacy labels remain routable but hidden from the primary owner. Co-admins keep the legacy keyboard.

## Parity checklist to verify

For each current `ADMIN_MENU` action—plans, payment methods, trial, forced join, welcome, top-ups,
customers, stats, broadcast, support, managers, credit codes, shop state, preview—record:

1. portal route and API;
2. shared service command/query;
3. owner permission and co-admin fallback;
4. bot shortcut/notification behavior;
5. focused backend/frontend test;
6. production smoke evidence.

Nested parity also covers customer search/detail/ban/message, ledger adjustment, subscription
renew/pause/delete, proof decisions, code analytics and delivery progress.

## Scope

**In scope**:

- storefront admin bot menu/handlers/keyboards and direct browser link generation
- portal deep-link helpers for shop/customer/top-up/order/campaign contexts
- Telegram notification buttons pointing to owned portal resources where owner identity permits
- compatibility fallbacks for old callback messages and co-admins
- end-to-end characterization tests, docs/help/roadmap/changelog and release smoke checklist
- machine-readable inventory at `backend/tests/fixtures/storefront_admin_parity.json`; each record has
  `menu_key`, `telegram_label`, `portal_route`, `api_operation`, `shared_symbol`, `owner_mode`,
  `coadmin_fallback`, and `test_id`

**Out of scope**:

- Removing customer bot functions or deleting legacy callbacks immediately
- Co-admin portal RBAC, customer Mini App, user creation, profit/accounting, CRM automation
- Any unrelated source/meters

## Commands and git workflow

| Gate | Command | Success |
|---|---|---|
| Focused | `cd backend && .venv/bin/pytest -q tests/test_storefront_parity.py tests/test_bot_ux.py tests/test_portal_storefront.py` | all pass |
| Backend full | pip check, Ruff, mypy, full pytest and PG contracts | exit 0 |
| Frontend/E2E | component tests, type/build/bundle and approved browser parity smoke | exit 0 |
| Release | deploy tools, assets/checksums, staging matrix, backup, production smoke | exit 0 |

- Branch: `feature/storefront-portal-3f-parity` from plan-006 release commit.
- Compatibility callbacks stay for at least two released versions. Reviewer approval precedes the
  final version/tag/release/deploy and roadmap DONE transition.

## Steps

### Step 1: Generate and verify the parity matrix

Create a machine-readable/maintained test inventory mapping all fourteen labels plus nested actions to
portal routes and shared services. Fail CI if an admin menu action lacks its declared parity or a portal
mutation bypasses the shared command layer.

`backend/tests/test_storefront_parity.py` parses the fixture, compares keys/labels with `ADMIN_MENU`,
asserts uniqueness/required fields, resolves every named shared symbol, and exercises each declared API
route's auth contract. **Verify**: parity contract tests cover every current action exactly once.

### Step 2: Add safe portal deep links

For the owning reseller, mint the existing short-lived one-time login URL and append percent-encoded
`next` only when it matches the exact relative prefix `/portal/storefront/{numeric_shop_id}` and one
of the registered customer/top-up/order/broadcast suffix patterns; reject `//`, schemes, backslashes,
control characters and double decoding. `PortalLogin` captures the raw token and validated next into
memory/sessionStorage, calls `history.replaceState` immediately to remove both from the address bar,
exchanges the token, then asks the owned shop endpoint to authorize the destination before navigation.
An existing authenticated session without a token follows the same authorization check. Invalid next
falls back to the owned shop dashboard; foreign next returns the dashboard without revealing existence.
Expired/replayed tokens render a safe «دریافت لینک تازه از ربات» link to the reseller bot `/start
portal`; the bot sends a newly minted dashboard URL. Thus old notification URLs fail safely instead of
becoming an open redirect. Notifications for top-ups, customer/service events and broadcast completion
deep-link to their owned resource while valid. Co-admin buttons retain bot flows.

**Verify**: open-redirect, foreign-resource, expired/replay token and path-allowlist tests pass.

### Step 3: Replace the owner's crowded menu with a compact admin home

Use inline buttons so the browser URL is directly clickable. Keep urgent top-up decision and compact
stats in Telegram; move complex forms/tables to portal links. Preserve legacy handlers for old messages
and co-admin fallback for at least two releases; do not delete business services.

Record `legacy_owner_callbacks_remove_not_before` as two MINOR releases after this release in the
parity inventory and create a follow-up roadmap task. This release proves the handlers remain; it does
not claim their future removal as a Done condition.

**Verify**: owner sees compact menu, co-admin sees functional fallback, old callbacks still work,
customer menu is unchanged and no `web_app`/Mini App field exists.

### Step 4: End-to-end staging and production acceptance

Run the full six-release acceptance matrix in staging on two disposable storefront tenants and an
owner with two shops:
own/foreign access, bot→portal and portal→bot parity, duplicate mutations, audit, money, panel recovery,
proof, broadcast restart, responsive UI and direct browser navigation. Use dedicated reversible fixtures;
never mutate a real customer subscription/payment for smoke. Production acceptance is read-only plus a
pre-created harmless notification recipient; destructive panel, wallet and payment cases remain staging-only.

### Step 5: Document and release parity completion

Update portal Help, README/architecture, roadmap item 3 and changelog. Record remaining explicit
boundaries (co-admin RBAC/item 15, CRM/item 14, accounting/item 12). Release/deploy with backup,
rollback and smoke per `docs/RELEASE_PROCESS.md`.

## Done criteria

- [ ] Every daily admin-bot capability has an owner web equivalent backed by shared services.
- [ ] Owner bot menu is compact and its portal button opens a normal browser directly.
- [ ] Co-admin and old-message fallbacks remain functional; no privilege broadening occurs.
- [ ] For the primary owner, Telegram stays notification/shortcut/customer transport rather than a
      duplicate complex admin UI; co-admins intentionally retain the full fallback menu.
- [ ] All parity, security, E2E, CI, release, deploy and production smoke gates pass after approval.
- [ ] Roadmap item 3 is marked DONE only after production acceptance.

## STOP conditions

- A parity item still depends on bot-only direct ORM mutation.
- Deep links allow an open redirect or bypass tenant authorization.
- Simplification would strand co-admins or invalidate existing callback messages.
- Production smoke would require destructive action on real customer/payment data.
- Any unrelated dirty file must be modified.

## Maintenance notes

Keep the parity inventory updated whenever a new storefront admin capability is added. A new portal
feature should not automatically enlarge the Telegram menu; prefer a deep link or urgent shortcut.
