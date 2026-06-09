---
description: Validate, version, publish, and optionally deploy one completed release
---

Release the completed change described by `$ARGUMENTS`.

Follow `docs/RELEASE_PROCESS.md` exactly:

1. Confirm the worktree contains only intended changes.
2. Run the full release gate and required focused tests.
3. Update `VERSION`, `backend/app/__init__.py`, remediation status, and release notes.
4. Commit with the repository's existing message style.
5. Create and push an annotated `v*` tag.
6. Require a healthy `gh auth status`, then create the GitHub release.
7. Deploy only when `$ARGUMENTS` explicitly includes `deploy`.
8. Before deploy, read `.claude/OPS.local.md`, verify the current production version,
   and create a fresh validated backup.
9. Run production smoke checks and report the deployed commit/version.

Never deploy when tests fail, the release tag is missing, GitHub publication failed,
the backup is invalid, or production identity is uncertain.
