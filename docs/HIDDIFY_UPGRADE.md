# Upgrade a Hiddify fleet after invoice compatibility acceptance

This runbook separates the invoice application release from the panel upgrade. It is
not permission to upgrade all servers automatically. Target exactly 12.0.0 → 14.2.0.

## Required evidence before the first production panel

1. All backend, frontend, PostgreSQL migration/concurrency and deployment gates pass.
2. Dedicated v12 and v14 write acceptance and mixed-generation workflow tests pass,
   including billing equality, terminal usage, ownership changes, partial queue replay,
   storefront financial reconciliation and UUID-login restoration.
3. A rehearsal on an isolated copy of v12 data succeeds. Disable real Telegram bots,
   payment activity and production-panel network writes in that copy before starting it.
4. Restore the rehearsal's full database/state snapshot and prove rollback works.
   Downloading old source or restoring JSON alone is not a tested rollback.
5. Deploy the invoice compatibility release by [RELEASE_PROCESS.md](RELEASE_PROCESS.md).
   Leave every production panel on v12 initially. Check at least three healthy syncs
   per panel, invoice dry-run, queues, bot/storefront health and migration/decrypt logs.

Record exact source/image digests, schema versions, checksums, test results and unresolved
failures in private operator artifacts. Do not label a missing live test PASS.

## Owner-only password extension on both generations

Use the separate [panel add-on installer](PANEL_ADDON.md) on each Hiddify host.
The same command supports exactly 12.0.0 and 14.2.0 and selects the active environment.
It verifies the release, backs up SQL/config/source/service files, replaces recognized
legacy hooks with one version-aware hook, restarts only `hiddify-panel`, and proves
that the extension was loaded. Keep the full upgrade rollback backup below as well;
the add-on backup is not a full host snapshot or database-migration rollback.

If the common add-on was installed on v12 before upgrading, its local boot hook will
select the v14 helper after the upgrade. Otherwise run the add-on installer on the
healthy upgraded panel. After either path:

```bash
sudo invoice-panel-addon --check
```

Also run the invoice application's read-only preflight. An installed file without a
loaded capability is not ready. Unknown versions/source fail closed at service start;
validate later Hiddify releases before unattended upgrades. Recognized legacy hooks
are migrated automatically; ambiguous/custom overrides are rejected for review.

The same hook supports a restored v12 host, but restore the complete matching panel
state/database/configuration first. Removal uses `sudo invoice-panel-addon --uninstall`
on a supported healthy panel and includes a panel restart; see the add-on guide for
backup locations and recovery when source is unknown.

The extension exposes only owner-authenticated capability and set/clear routes. It
forbids editing the owner, rejects extra fields, preserves name/role/alias/limits and
allows explicit empty password for the existing UUID-link restoration. It does not
revoke a reseller's UUID API key; enforcement users/limits/reassert remain necessary.

## Upgrade one panel

1. Select a low-risk panel. If it is a parent/child deployment, treat the dependent group
   as the rollout unit; do not split incompatible node protocols across versions.
2. Capture a restorable full host/state/database backup, custom templates/configuration,
   certificates, service units/drop-ins and the invoice application's release/schema.
3. Enter maintenance for that panel through the invoice owner API or CLI. If it reports
   an in-flight writer, let it finish and retry; do not force a mid-write upgrade.
4. Upgrade by the exact official 14.2.0 procedure validated in rehearsal. Do not run a
   mutable `dev`/`latest` installer. Account for the new layout, Python 3.13, database
   migration, proxy engines, node relationships and customization changes.
5. Verify the common panel add-on is loaded (`sudo invoice-panel-addon --check`);
   install it if missing. Read-only invoice preflight must pass.
6. Validate the converted data and invoice dry-run against the baseline. Check a
   designated disposable account's create/renew, scoped suspend/restore, UUID login,
   client subscription and actual VPN protocols used by customers. Never use an
   unrelated customer's account as a test fixture.
7. Exit maintenance through the guarded API/CLI. It performs a fresh sync before the
   flag is cleared. Reconcile pending operations, then monitor sales, queues and sync.
8. Observe at least 24 hours and explicitly test the monthly invoice dry-run; passage
   of time alone does not exercise month-end billing. Continue other panels one by one
   only after acceptance of the first.

## Abort / rollback

Stop for unknown billing differences, ownership mutations, incomplete restoration,
failed cleanup, invalid subscriptions, broken client traffic or unverified database
migration/rollback. Keep the affected panel in maintenance.

Do not downgrade the invoice application to its pre-compatibility release while any
panel still requires v14 support. Restore those panels first or keep a compatible
application release.

Restore the panel's matching database **and** state/configuration/software/service
snapshot using the rehearsed procedure. A code-only downgrade after database migration
is insufficient. Re-run preflight, sync, invoice dry-run and designated-account checks
on the restored generation before leaving maintenance. If the invoice application also
needs rollback, use its migration-aware release rollback, not `git reset` or volume deletion.
