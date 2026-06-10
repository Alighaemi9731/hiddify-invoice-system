#!/usr/bin/env bash
# Roll back application code to a previously verified, locally cached release.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/hiddify-invoice-system}"
TAG="${1:-}"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "usage: $0 vX.Y.Z" >&2
  exit 1
}

RELEASE_TAG="$TAG" \
DEST="$REPO_DIR" \
RELEASE_CACHE_DIR="${RELEASE_CACHE_DIR:-$REPO_DIR/update/releases}" \
OFFLINE=1 \
SKIP_INSTALL="${SKIP_INSTALL:-0}" \
ALLOW_NON_ROOT="${ALLOW_NON_ROOT:-0}" \
  bash "$REPO_DIR/deploy/release-installer.sh"
