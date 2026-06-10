#!/usr/bin/env bash
# Build deterministic, checksum-verifiable assets for a tagged release.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-v$(cat "$REPO_DIR/VERSION")}"
OUT_DIR="${2:-$REPO_DIR/dist-release}"
REF="${RELEASE_REF:-$TAG}"

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "invalid release tag: $TAG" >&2
  exit 1
}
[[ "v$(cat "$REPO_DIR/VERSION")" == "$TAG" ]] || {
  echo "VERSION does not match $TAG" >&2
  exit 1
}
git -C "$REPO_DIR" rev-parse --verify "${REF}^{commit}" >/dev/null

root="invoice-system-$TAG"
asset="$root.tar.gz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$OUT_DIR" "$tmp/$root"

git -C "$REPO_DIR" archive --format=tar --prefix="$root/" "$REF" > "$tmp/release.tar"
git -C "$REPO_DIR" ls-tree -r --name-only "$REF" > "$tmp/$root/.release-files"
printf '%s\n' ".release-files" >> "$tmp/$root/.release-files"
tar -rf "$tmp/release.tar" -C "$tmp" "$root/.release-files"
gzip -n -9 < "$tmp/release.tar" > "$OUT_DIR/$asset"

(
  cd "$OUT_DIR"
  sha256sum "$asset" > "$asset.sha256"
)

git -C "$REPO_DIR" show "$REF:deploy/release-installer.sh" > "$OUT_DIR/release-installer.sh"
chmod 755 "$OUT_DIR/release-installer.sh"
(
  cd "$OUT_DIR"
  sha256sum release-installer.sh > release-installer.sh.sha256
)

printf '%s\n' \
  "$OUT_DIR/$asset" \
  "$OUT_DIR/$asset.sha256" \
  "$OUT_DIR/release-installer.sh" \
  "$OUT_DIR/release-installer.sh.sha256"
