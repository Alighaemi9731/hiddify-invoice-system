# Hiddify panel compatibility

The compatibility adapter targets **Manager/Panel 12.0.0 and 14.2.0**, including a fleet
running both at once. A newer 14.x release needs its own source and runtime validation.
Passing a read-only preflight is not an acceptance test of billing, restoration or VPN clients.

## Contracts

| Operation | 12.0.0 | 14.2.0 |
|---|---|---|
| Backup | Existing Basic/UUID backup URLs | `/api/v2/admin/backup/download/`, owner key |
| Single user/admin reads and updates | v2 API | v2 API |
| Bulk enable/disable/delete | Scoped legacy action + CSRF | Scoped JSON `users/bulk/`, UUID targets |
| Password set/clear | Legacy admin form + password-clear hook | Owner-only invoice extension |
| Removed users | Missing from the next backup | Explicit `deleted=true` tombstones |

`/api/v2/admin/backup/` on v14 is page metadata. Neither read adapter accepts it as
an empty backup. Valid downloads require user/admin lists and at least one parsed admin.
Backup redirects are not followed with secret credentials. A failed read preserves
previous snapshots and prevents billing from a failed sync.

Generation selection for bulk/password transport uses a read of the legacy route:
only its **404** selects the other transport, which must then pass an authenticated
read-only capability probe. An auth failure, timeout, or failed mutation never triggers
an alternate mutation. Capabilities are rechecked rather than persisted across a host,
path, credential, worker restart or panel upgrade.

Bulk JSON mutations require a UUID obtained from a fresh per-target read in this client
pass. Each UUID, numeric id and exact owning admin is checked again before the write.
No full-panel id-map fallback is used. Count and per-user readback are both required;
an uncertain response remains a failure eligible for reconciliation. Existing queues
continue to carry their original UUID targets and resolve numeric ids anew each pass.

Admin capacity changes verify the live parent before using the configured owner identity.
This permits agent parents to manage their own children through existing authorized
application workflows, without treating a stale local parent assignment as authorization.

## Application call coverage

The public adapter methods and their application consumers are listed below. Direct
wrappers are retained for existing callers; the bulk queue resolves each UUID afresh.

| Adapter method | Application consumer / contract |
|---|---|
| `fetch_backup` | Sync, connection test, user creation reconciliation; validated full backup |
| `set_user_enabled` | Storefront toggle; scoped single-user PATCH |
| `create_user` | Reseller creation flow, storefront, trial/test configuration, recovery |
| `get_user_identity` | Enforcement target UUID/id/owner validation |
| `get_user_id` | End-user deletion, cascade deletion; fresh UUID lookup |
| `admin_exists` | Enforcement distinguishes deleted admin from authentication failure |
| `get_user` | Storefront status/reapers, enforcement readback, recovery |
| `patch_user` | Storefront reset/update; v12 partial start-date compatibility |
| `delete_user` | Storefront removal; scoped single deletion |
| `prepare_renew_user` | Persist deterministic renewal target before remote write |
| `apply_renew_user_target` | Renewal and reconciliation replay the same target |
| `renew_user` | Convenience wrapper around prepare/apply; exercised by live probe |
| `get_user_ids` | Retained compatibility helper; hot bulk path uses individual lookups |
| `bulk_set_users_enabled` | Suspend, reassert, restore, including old queue targets |
| `bulk_delete_users` | End-user and descendant cascade deletion |
| `create_admin` | Sub-reseller provisioning with verified parent |
| `get_admin` | Parent/permission/capacity validation and live probe readback |
| `get_server_status` | Reseller traffic audit; unchanged v2 status contract |
| `get_admin_limits` | Enforcement snapshots, parent capacity management |
| `set_can_add_admin` | Owner/portal permission management, parent capacity |
| `set_admin_limits` | Freeze, suspend, restore, parent capacity management |
| `set_admin_password` | Enforcement lock/restore through generation-specific transport |
| `admin_link_login_works` | Fresh, unauthenticated UUID-login verification |
| `delete_admin` | Verified descendant cascade after deleting its users |

The single-user API in both generations may omit `remaining_day`. Storefront live
status and refresh normalize expiry from `start_date` and `package_days` using the
application's Tehran calendar, matching the panel's package semantics. An unstarted
package keeps its full duration; expired packages retain negative days. This also
lets time-only automatic renewal pass its final live confirmation when volume remains.
An explicit remaining-day value, when supplied by the panel, remains authoritative.

## Financial state

`end_user_snapshots.panel_deleted` records explicit tombstones. Presence requires both
no explicit tombstone and the existing timestamp presence rule. A disabled/inactive user
is still present. Billing retains deleted snapshots and applies the existing consumption,
free threshold and full-quota cutoff rules; neither paid invoices nor the permanent
financial ledger are rewritten by this migration.

Sync records final tombstone usage. Repeated tombstones do not represent new sales,
and explicit deletion is excluded from panel rollback detection/recovery. Reappearance
clears the flag and cached numeric identity. Old tombstones already eligible for retention
are not recreated from every subsequent backup. Current and previous billing months
retain the same retention protection as before.

The schema revision is `e9b2c4d6f803`, following `d8a1f3b6c902`. It adds two boolean
columns with database defaults: snapshot `panel_deleted` and panel `maintenance`.
Rolling back code across this revision requires the existing schema-aware rollback
procedure in [RELEASE_PROCESS.md](RELEASE_PROCESS.md), not replacing application files.

## Maintenance and upgrade readiness

Maintenance is independent of `enabled`. It preserves data and financial identity while
pausing writes for one panel. PostgreSQL shared advisory locks cover complete panel
mutations, sync fetch/upserts and storefront money/provision workflows. Entering maintenance takes the
matching exclusive lock and refuses while an operation is still in flight. Detached or
stale application objects cannot bypass the persisted flag.

Enforcement queues pause without spending retry attempts. Purchases/trials/renewals
refuse before money or operation creation. Billing reports a skipped maintenance panel,
continues other panels and retries that period without spending its error retry budget
when intentional maintenance is the only remaining issue. Already delivered invoices
retain the existing no-duplicate-delivery guards.

Owner endpoints:

- `GET /api/panels/{id}/preflight`: read-only report, no credential values.
- `POST /api/panels/{id}/maintenance` with `{"enabled":true}`: drain then pause.
- The same POST with `false`: require preflight and a fresh validated sync before resuming.
- Panel list/detail responses include `maintenance`; the existing enabled flag is unchanged.

Container CLI (defaults to read-only):

```bash
python -m app.services.panel_preflight --panel-id PANEL_ID
python -m app.services.panel_preflight --panel-id PANEL_ID --maintenance on
python -m app.services.panel_preflight --panel-id PANEL_ID --maintenance off
```

For v12, read-only preflight reports `password: legacy_hook_required`. The installed
legacy host hook must additionally pass its `--check`, and the write acceptance probe
must prove clear/restore. For v14, readiness requires the loaded extension capability.
Neither result authorizes upgrading a production panel by itself.

## Repeatable live acceptance

Create a private JSON file with mode 0600 containing `host`, `proxy_path`, `owner_uuid`.
Use a dedicated test panel; do not commit this file or place credentials in CLI arguments.

```bash
python -m app.services.panel_contract_probe --connection-file /private/connection.json --output /private/read-report.json
python -m app.services.panel_contract_probe --connection-file /private/connection.json --write --allow-host TEST_HOST --output /private/write-report.json
```

Write mode mints its own disposable UUIDs and tests creation/ownership, bulk toggles,
renewal, capacity, permission toggles, password lock/clear/login, traffic and deletion.
Cleanup runs for every minted UUID even after an uncertain create, then compares
preexisting business fields. v14 deleted fixture tombstones may remain as dictated by
panel soft deletion. A failed cleanup or preexisting business-field change fails the run.
The output contains counts/check results, not owner UUIDs, secret paths or passwords.
A process termination before cleanup needs operator reconciliation; no test tool can
promise cleanup after SIGKILL or a host loss.
