#!/usr/bin/env bash
# Roll back application code to a previously verified, locally cached release.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/hiddify-invoice-system}"
TAG="${1:-}"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "usage: $0 vX.Y.Z" >&2
  exit 1
}

cat >&2 <<'WARN'
────────────────────────────────────────────────────────────────────────────
  ⚠️  DATABASE MIGRATIONS ARE NOT ROLLED BACK.
  Rolling back only restores older CODE; the schema stays at whatever the newer
  release migrated it to. Additive migrations are usually tolerated by older
  code, but if the release you are leaving contained a migration (see its
  CHANGELOG entry), restore the pre-upgrade pg_dump you took before deploying
  if the app misbehaves after this rollback.
────────────────────────────────────────────────────────────────────────────
WARN

RELEASE_TAG="$TAG" \
DEST="$REPO_DIR" \
RELEASE_CACHE_DIR="${RELEASE_CACHE_DIR:-$REPO_DIR/update/releases}" \
OFFLINE=1 \
SKIP_INSTALL="${SKIP_INSTALL:-0}" \
ALLOW_NON_ROOT="${ALLOW_NON_ROOT:-0}" \
  bash "$REPO_DIR/deploy/release-installer.sh"
