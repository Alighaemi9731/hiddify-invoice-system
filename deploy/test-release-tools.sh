#!/usr/bin/env bash
# Exercise verified release application, stale-file cleanup, and offline rollback.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
assets="$tmp/assets"
dest="$tmp/install"
mkdir -p "$assets"

# Optional .env keys are genuinely optional under `set -euo pipefail`. A consumed setup token is
# removed from production .env; reading that normal state must not abort install before smoke checks.
. "$REPO_DIR/deploy/env.sh"
env_fixture="$tmp/optional.env"
printf 'SERVER_DOMAIN=panel.example.test\n' > "$env_fixture"
[[ "$(read_env_value SERVER_DOMAIN "$env_fixture")" == "panel.example.test" ]]
[[ -z "$(read_env_value SETUP_BOOTSTRAP_TOKEN "$env_fixture")" ]]
[[ -z "$(read_env_value SERVER_DOMAIN "$tmp/absent.env")" ]]

make_fixture() {
  local tag="$1" marker="$2" stale="${3:-0}"
  local root="$tmp/invoice-system-$tag"
  rm -rf "$root"
  mkdir -p "$root/deploy"
  printf '%s\n' "${tag#v}" > "$root/VERSION"
  printf '%s\n' "$marker" > "$root/marker.txt"
  cp "$REPO_DIR/deploy/env.sh" "$root/deploy/"
  cp "$REPO_DIR/deploy/release-installer.sh" "$root/deploy/"
  cp "$REPO_DIR/deploy/rollback.sh" "$root/deploy/"
  for script in install.sh updater.sh smoke.sh; do
    printf '#!/usr/bin/env bash\nset -euo pipefail\n' > "$root/deploy/$script"
  done
  printf '%s\n' VERSION marker.txt deploy/env.sh deploy/install.sh deploy/updater.sh \
    deploy/release-installer.sh deploy/rollback.sh deploy/smoke.sh .release-files \
    > "$root/.release-files"
  if [[ "$stale" == "1" ]]; then
    printf 'stale\n' > "$root/stale.txt"
    printf 'stale.txt\n' >> "$root/.release-files"
  fi
  tar -czf "$assets/invoice-system-$tag.tar.gz" -C "$tmp" "invoice-system-$tag"
  (cd "$assets" && sha256sum "invoice-system-$tag.tar.gz" \
    > "invoice-system-$tag.tar.gz.sha256")
}

make_fixture v1.0.0 old 1
RELEASE_TAG=v1.0.0 RELEASE_ASSET_DIR="$assets" DEST="$dest" \
  ALLOW_NON_ROOT=1 SKIP_INSTALL=1 bash "$REPO_DIR/deploy/release-installer.sh"
[[ "$(cat "$dest/marker.txt")" == "old" && -f "$dest/stale.txt" ]]

make_fixture v1.0.1 new
RELEASE_TAG=v1.0.1 RELEASE_ASSET_DIR="$assets" DEST="$dest" \
  ALLOW_NON_ROOT=1 SKIP_INSTALL=1 bash "$REPO_DIR/deploy/release-installer.sh"
[[ "$(cat "$dest/marker.txt")" == "new" && ! -e "$dest/stale.txt" ]]

REPO_DIR="$dest" RELEASE_CACHE_DIR="$dest/update/releases" \
  ALLOW_NON_ROOT=1 SKIP_INSTALL=1 bash "$dest/deploy/rollback.sh" v1.0.0
[[ "$(cat "$dest/marker.txt")" == "old" && -f "$dest/stale.txt" ]]
echo "release apply + rollback OK"
