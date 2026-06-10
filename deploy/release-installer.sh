#!/usr/bin/env bash
# Download an exact GitHub release asset, verify SHA-256, apply it, then install.
set -euo pipefail

REPO_SLUG="${REPO_SLUG:-Alighaemi9731/hiddify-invoice-system}"
DEST="${DEST:-/opt/hiddify-invoice-system}"
RELEASE_TAG="${RELEASE_TAG:-latest}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
OFFLINE="${OFFLINE:-0}"

if [[ $EUID -ne 0 && "${ALLOW_NON_ROOT:-0}" != "1" ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

resolve_latest_tag() {
  local effective
  effective="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
    "https://github.com/$REPO_SLUG/releases/latest")"
  basename "$effective"
}

if [[ "$RELEASE_TAG" == "latest" ]]; then
  [[ "$OFFLINE" != "1" ]] || { echo "offline mode requires an exact tag" >&2; exit 1; }
  RELEASE_TAG="$(resolve_latest_tag)"
fi
[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "invalid release tag: $RELEASE_TAG" >&2
  exit 1
}

asset="invoice-system-$RELEASE_TAG.tar.gz"
cache_dir="${RELEASE_CACHE_DIR:-$DEST/update/releases}"
mkdir -p "$cache_dir"
archive="$cache_dir/$asset"
checksum="$archive.sha256"

if [[ -n "${RELEASE_ASSET_DIR:-}" ]]; then
  cp "$RELEASE_ASSET_DIR/$asset" "$archive"
  cp "$RELEASE_ASSET_DIR/$asset.sha256" "$checksum"
elif [[ "$OFFLINE" != "1" ]]; then
  base="https://github.com/$REPO_SLUG/releases/download/$RELEASE_TAG"
  curl -fL --retry 4 --retry-all-errors -o "$archive.tmp" "$base/$asset"
  curl -fL --retry 4 --retry-all-errors -o "$checksum.tmp" "$base/$asset.sha256"
  mv "$archive.tmp" "$archive"
  mv "$checksum.tmp" "$checksum"
fi

[[ -s "$archive" && -s "$checksum" ]] || {
  echo "cached release assets are missing for $RELEASE_TAG" >&2
  exit 1
}
if [[ -n "${EXPECTED_SHA256:-}" ]]; then
  printf '%s  %s\n' "$EXPECTED_SHA256" "$asset" > "$checksum"
fi
(cd "$cache_dir" && sha256sum -c "$asset.sha256")

root="invoice-system-$RELEASE_TAG"
if tar -tzf "$archive" | awk -v root="$root/" '
  index($0, root) != 1 || $0 ~ /(^|\/)\.\.($|\/)/ || $0 ~ /^\// { bad=1 }
  END { exit bad }
'; then
  :
else
  echo "release archive contains an unsafe path" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tar -xzf "$archive" -C "$tmp"
src="$tmp/$root"
[[ -f "$src/.release-files" && -f "$src/VERSION" && -f "$src/deploy/install.sh" ]] || {
  echo "release archive is incomplete" >&2
  exit 1
}
[[ "v$(cat "$src/VERSION")" == "$RELEASE_TAG" ]] || {
  echo "release VERSION does not match $RELEASE_TAG" >&2
  exit 1
}
scripts=("$src/deploy/install.sh")
for script in updater.sh release-installer.sh rollback.sh smoke.sh; do
  [[ -f "$src/deploy/$script" ]] && scripts+=("$src/deploy/$script")
done
bash -n "${scripts[@]}"

mkdir -p "$DEST"
if [[ -f "$DEST/.release-files" ]]; then
  while IFS= read -r old; do
    [[ "$old" =~ ^[A-Za-z0-9._/-]+$ && "$old" != /* && "$old" != *".."* ]] || continue
    if ! grep -Fqx "$old" "$src/.release-files"; then
      rm -f "$DEST/$old"
    fi
  done < "$DEST/.release-files"
fi
cp -a "$src/." "$DEST/"
printf '%s\n' "$RELEASE_TAG" > "$DEST/.deployed-release"

if [[ "$SKIP_INSTALL" != "1" ]]; then
  bash "$DEST/deploy/install.sh"
fi
