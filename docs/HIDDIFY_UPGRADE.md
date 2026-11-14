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

## Owner-only password extension on v14

Use `deploy/hiddify-invoice-compat.py` from the verified invoice release. It accepts
only the source of Panel 14.2.0 pinned by Manager 14.2.0; an unknown layout/hash fails
closed. The reference checkout is not an installation target.

On the dedicated test host, then on each v14 production host during its maintenance:

```bash
install -m 755 hiddify-invoice-compat.py /usr/local/sbin/invoice-hiddify-compat-v14
/usr/local/sbin/invoice-hiddify-compat-v14 --check
/usr/local/sbin/invoice-hiddify-compat-v14
```

The script does not restart services. After a coordinated restart of `hiddify-panel`,
the application's read-only preflight must report the loaded password extension.
An installed file without a loaded capability is not ready. A restart can apply panel
database migrations, so take the full rollback backup first.

For persistence across package replacement of the same supported release, a root-run
`ExecStartPre=+/usr/local/sbin/invoice-hiddify-compat-v14` may be placed in a dedicated
`hiddify-panel.service.d` drop-in **on a v14 host** after installation is tested. An
unrecognized future source intentionally prevents that hook from starting the service;
validate future versions before unattended panel updates. Preserve the legacy v12 hook
on v12 hosts. Restore the appropriate old service/drop-ins with the complete rollback
snapshot; do not leave a v14-only startup hook enabled on a v12 rollback host.

`--check` is read-only. `--uninstall` removes only the exact recognized extension/hook;
it also needs a coordinated panel restart to unload the routes. If the module or
registration file was independently modified, the installer refuses to overwrite it.

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
5. Install/load the v14 password extension; read-only invoice preflight must pass.
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
