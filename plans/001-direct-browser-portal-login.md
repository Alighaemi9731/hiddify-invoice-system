# Plan 001: Open the reseller portal directly in a normal browser

> **Executor instructions**: Follow this plan step by step. Run every verification
> command and confirm the expected result before moving to the next step. If anything
> in the "STOP conditions" section occurs, stop and report — do not improvise. A
> reviewer maintains `plans/README.md`; do not edit the plan index.
>
> **Drift check (run first)**:
> `git diff --stat cf80dfd..HEAD -- backend/app/bot/keyboards.py backend/app/bot/handlers/common.py backend/tests/test_bot_ux.py`
> Expected at dispatch: no output. If an in-scope file changed, compare the current-state
> excerpts below with live code and stop on any mismatch.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `cf80dfd`, 2026-07-16
- **Review verdict**: APPROVED at `f7770a2`, 2026-07-16
- **Release**: v1.82.3

## Why this matters

The reseller currently taps «ورود به پنل تحت وب», waits for another bot message, and
then taps the temporary link in that message. Replace that two-step interaction with one
normal HTTPS URL button in the existing inline menu. It must open the web portal through
the Telegram client's ordinary browser handling and must not use `WebAppInfo`, a Mini App,
or a Telegram Web App. Authentication continues to use the already-audited 15-minute,
signed, one-time portal login token; this plan changes delivery UX, not the auth protocol.

## Current state

- `backend/app/bot/keyboards.py` — `reseller_menu_keyboard` builds the inline reseller
  menu. Its portal row currently has `callback_data="menu:portal"`.
- `backend/app/bot/handlers/common.py` — `_send_menu` and `_reshow_menu` calculate feature
  visibility and call `reseller_menu_keyboard`, but do not provide a portal URL.
- `backend/app/bot/handlers/common.py` — `_resellers_for_chat(session, chat_id)` is the
  canonical ownership lookup and returns all reseller rows bound to the Telegram chat.
- `backend/app/bot/handlers/views.py:_send_portal_link` is the existing fallback. It checks
  reseller ownership, normalizes `server_domain`, creates a portal login token, and sends
  `https://{domain}/portal/login?t={token}`. Do not change or remove it.
- `backend/app/core/portal_auth.py:create_portal_login_token` creates a signed login token
  with a random JTI and a 15-minute TTL. The exchange endpoint consumes the JTI so replay
  fails. Reuse this function; do not invent a new token.
- `backend/tests/test_bot_ux.py:test_reshow_menu_sends_role_aware_menu` is the nearest menu
  behavior regression test and uses a temporary SQLite database.

Relevant current shapes at `cf80dfd`:

```python
# backend/app/bot/keyboards.py:55-73
def reseller_menu_keyboard(
    *, show_create_user: bool = False, show_storefront: bool = False
) -> InlineKeyboardMarkup:
    ...
    [InlineKeyboardButton(text="🌐 ورود به پنلِ تحتِ وب", callback_data="menu:portal")],
```

```python
# backend/app/bot/handlers/common.py:446-477
reply_markup=keyboards.reseller_menu_keyboard(
    show_create_user=can_create, show_storefront=can_storefront),
```

```python
# backend/app/bot/handlers/views.py:487-501 (fallback contract)
resellers = await _resellers_for_chat(session, chat_id)
domain = (await settings_service.get(session, "server_domain", "") or "").strip()
domain = domain.replace("https://", "").replace("http://", "").strip("/")
url = f"https://{domain}/portal/login?t={create_portal_login_token(chat_id)}"
```

## Commands you will need

Run backend commands from `backend/` with its existing virtual environment activated.

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `pytest -q tests/test_bot_ux.py tests/test_portal.py` | exit 0, all pass |
| Dependencies | `pip check` | exit 0, no broken requirements |
| Lint | `ruff check app tests alembic` | exit 0, no errors |
| Typecheck | `mypy app` | exit 0, no errors |
| Full tests | `pytest -q` | exit 0, all pass |

## Scope

**In scope** (the only source/test files the executor may modify):

- `backend/app/bot/keyboards.py`
- `backend/app/bot/handlers/common.py`
- `backend/tests/test_bot_ux.py`

**Out of scope** (do not touch):

- `backend/app/bot/handlers/views.py` — its callback/message link is the required fallback.
- `backend/app/core/portal_auth.py` and portal API endpoints — auth semantics are unchanged.
- All frontend files — the existing `/portal/login?t=...` route already exchanges the token.
- Mini App/Web App support, `WebAppInfo`, Telegram `LoginUrl`, BotFather settings, deep links,
  migrations, version bumps, changelogs, deployment files, and infrastructure.
- Persistent reply-keyboard behavior; the active menu is already inline.

## Git workflow

- Work in an isolated worktree on branch `advisor/001-direct-browser-portal-login`.
- Use the repository's Conventional Commit style, for example:
  `feat(bot): open reseller portal directly from menu`.
- Commit the implementation and tests in the isolated worktree.
- Do not push, merge, tag, release, or deploy.

## Steps

### Step 1: Let the portal menu row carry a normal HTTPS URL

In `backend/app/bot/keyboards.py`, add a keyword-only optional `portal_url: str | None =
None` parameter to `reseller_menu_keyboard`. When it is non-empty, build the portal row
as `InlineKeyboardButton(..., url=portal_url)`. When it is absent, preserve the exact
`callback_data="menu:portal"` button so configuration/ownership failures still take the
existing explanatory fallback path.

Do not set `web_app`, `login_url`, or both `url` and `callback_data`. Existing callers that
omit the new argument must behave exactly as before.

**Verify**: run from `backend/`:
`pytest -q tests/test_bot_ux.py -k 'portal or menu'` → exit 0.

### Step 2: Create a direct URL only for a configured, registered reseller

In `backend/app/bot/handlers/common.py`, add one private async helper near
`_resellers_for_chat`, named `_portal_menu_url(session, chat_id: int) -> str | None`.
It must:

1. Return `None` when `_resellers_for_chat` returns no rows.
2. Load `server_domain` using `settings_service.get(session, "server_domain", "")`.
3. Apply the same normalization as `_send_portal_link`: trim whitespace, remove an
   optional `http://` or `https://` prefix, and strip `/` around the host.
4. Return `None` when the normalized domain is empty.
5. Lazily import and call `create_portal_login_token(chat_id)` only after both gates pass.
6. Return exactly `https://{domain}/portal/login?t={token}`.

Call this helper once in each non-owner menu path (`_send_menu` and `_reshow_menu`) and
pass its result as `portal_url=...`. Owner menus are unchanged. Keep `_reshow_menu`'s
best-effort exception boundary.

This helper intentionally mirrors the existing fallback URL contract. Do not broaden
accepted schemes, add query parameters, log the token, or cache/store the token.

**Verify**: run from `backend/`:
`pytest -q tests/test_bot_ux.py tests/test_portal.py` → exit 0, all pass.

### Step 3: Add security and fallback regression tests

In `backend/tests/test_bot_ux.py`, add focused tests that prove all of the following:

1. `reseller_menu_keyboard(portal_url="https://example.test/portal/login?t=token")`
   puts that exact value in the portal button's `url`, leaves its `callback_data` unset,
   and leaves `web_app` unset.
2. Omitting `portal_url` preserves `callback_data="menu:portal"` and leaves `url` unset.
3. `_portal_menu_url` returns `None` for a Telegram chat with no reseller rows.
4. `_portal_menu_url` returns `None` for a registered reseller when `server_domain` is
   empty.
5. For a registered reseller and a configured domain containing whitespace/scheme/slashes,
   the helper returns a normalized HTTPS `/portal/login?t=...` URL. Extract the token and
   verify with `verify_portal_login_token` that its subject is the requested chat ID and its
   JTI is non-empty. Never assert or print the full random token.
6. The existing `_reshow_menu` database test now asserts that its portal button is a URL
   button when `server_domain` is configured.

Use temporary SQLite and the existing `asyncio.run` convention; do not add test-only
branches to production code.

**Verify**: run from `backend/`:
`pytest -q tests/test_bot_ux.py tests/test_portal.py` → exit 0, all pass.

### Step 4: Run the full backend quality gate and commit

Run every command in the command table. Inspect `git status --short` and `git diff --check`.
Only the three in-scope files may be changed. Commit only after all gates pass.

**Verify**:

- `pip check` → exit 0.
- `ruff check app tests alembic` → exit 0.
- `mypy app` → exit 0.
- `pytest -q` → exit 0.
- `git diff --check` → exit 0, no output.
- `git status --short` before commit lists only the three in-scope files.

## Test plan

- Keyboard happy path: a normal URL button is emitted and Mini App fields are absent.
- Keyboard fallback: no URL keeps the existing `menu:portal` callback usable.
- Eligibility edges: unregistered chats and missing domains do not receive bearer URLs.
- Domain normalization: configured scheme/slashes become a single canonical HTTPS URL.
- Auth regression: the generated value is a valid portal-login token for the same chat ID
  and has a non-empty one-time JTI.
- Integration path: `_reshow_menu` sends the URL-bearing keyboard for a real reseller row.
- Existing portal suite remains the replay, expiry, HTTPS, and tenant-isolation authority.

## Done criteria

- [x] The reseller menu opens `/portal/login?t=...` directly with a standard HTTPS URL
      button when reseller ownership and domain configuration are valid.
- [x] The direct button has neither `web_app` nor `login_url`; no Mini App is introduced.
- [x] Missing reseller/domain state retains the existing `menu:portal` callback fallback.
- [x] `_send_menu` and `_reshow_menu` both use the same helper and do not cache or log tokens.
- [x] New tests cover the six cases in Step 3 and pass.
- [x] `pip check`, Ruff, mypy, focused portal tests, and full `pytest -q` exit 0. The
      independently reproduced date-sensitive billing fixture was stabilized before release.
- [x] `git diff --check` exits 0.
- [x] No files outside the three in-scope files are modified in the executor worktree.

## STOP conditions

Stop and report without improvising if:

- The drift check shows an in-scope source file changed and its relevant symbols no longer
  match the current-state excerpts.
- A standard inline `url` button cannot be represented by the installed aiogram version.
- The existing portal login endpoint no longer accepts the `t` query parameter or token
  exchange tests fail before changes.
- Correctness appears to require modifying portal auth, frontend, database schema,
  deployment configuration, or any file outside scope.
- Any verification command fails twice after one focused correction attempt.

## Maintenance notes

- Telegram decides whether ordinary HTTPS links open in its in-app browser or the user's
  configured external browser. The product guarantee is “normal browser URL, not Mini App”;
  server code cannot override a Telegram client preference.
- A menu message older than the 15-minute login-token TTL contains an expired URL. The
  existing `/menu` and fallback portal command provide a fresh link; do not extend the token
  lifetime merely to keep an old chat button alive.
- URL buttons are bearer links, like the existing link message. The one-time JTI and short
  TTL limit replay, but users should not forward menu messages before using the link.
- A future BotFather-linked `LoginUrl` flow could eliminate pre-minted tokens in menu
  messages, but it is a separate auth/infrastructure project and was explicitly deferred.
