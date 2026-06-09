# Release and production deploy process

The application deployer installs the highest `v*` Git tag. A production release is
therefore complete only after the commit, version files, tag, GitHub release, deploy,
and smoke checks all agree.

Local production coordinates belong in `.claude/OPS.local.md`, which is gitignored.
Never put host IPs, SSH usernames, tokens, passwords, panel URLs, or private keys in
tracked documentation.

## 1. Prepare one remediation batch

- Work on a dedicated branch.
- Keep the release scoped to one batch from `docs/REMEDIATION_PLAN.md`.
- Add focused regression tests for every fixed failure mode.
- Update behavior documentation and the batch status.
- Review `git diff` for secrets and unrelated changes.

## 2. Run the release gate

Run the complete gate from `docs/REMEDIATION_PLAN.md`. Also run any focused
integration or E2E tests required by the batch. A Vite build alone is not sufficient;
TypeScript checking must pass independently.

## 3. Version and commit

Update these files to the same version:

- `VERSION`
- `backend/app/__init__.py`
- `CHANGELOG.md`

Use the existing commit style:

```text
fix(scope): concise outcome - v1.37.36

Explain the failure mode, invariant, implementation, tests, and deployment notes.
```

Create an annotated tag:

```bash
git tag -a v1.37.36 -m "v1.37.36 - concise release title"
```

## 4. Push and create the GitHub release

```bash
git push origin main
git push origin v1.37.36
gh release create v1.37.36 --title "v1.37.36 - concise title" --generate-notes
```

`gh auth status` must succeed first. If GitHub CLI authentication is unavailable,
stop after the Git push and repair authentication before creating or deploying a
release. Do not silently skip the GitHub release.

## 5. Pre-deploy production checks

- Confirm local `main`, `origin/main`, tag, and GitHub release point to the same commit.
- Confirm the production host and path from `.claude/OPS.local.md`.
- Record the currently deployed version and commit.
- Create a fresh application backup and verify that it contains a non-empty database dump.
- Record the prior tag as the rollback target.

## 6. Deploy

The current installer deploys the highest release tag:

```bash
ssh <production-host> \
  'curl -fsSL https://raw.githubusercontent.com/Alighaemi9731/hiddify-invoice-system/main/get.sh | bash'
```

Longer term, B09 replaces this mutable root-download path with a pinned,
checksum-verified release artifact.

## 7. Smoke check

Verify all of the following before declaring success:

```bash
cat /opt/hiddify-invoice-system/VERSION
git -C /opt/hiddify-invoice-system rev-parse --short HEAD
cd /opt/hiddify-invoice-system
docker compose --env-file .env -f deploy/docker-compose.prod.yml ps
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs \
  --since=10m backend bot
systemctl is-active hiddify-updater
```

Also check:

- `/api/info` reports the new version.
- Login and dashboard load.
- Database, backend, bot, frontend, and Caddy are healthy.
- No migration, decrypt, scheduler, Telegram, or restore errors appear.
- The specific fixed workflow passes a non-destructive production smoke test.

## 8. Rollback

Do not use destructive Git reset. Deploy the recorded prior tag explicitly:

```bash
ssh <production-host> \
  'curl -fsSL https://raw.githubusercontent.com/Alighaemi9731/hiddify-invoice-system/main/get.sh | BRANCH=v1.37.35 bash'
```

If the release changed the database schema, follow that release's migration rollback
instructions. Restoring a database backup is a last-resort operation and must use the
validated restore procedure from B02.
