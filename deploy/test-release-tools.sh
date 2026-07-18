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

# ── The schema pre-flight must ENGAGE when a real stack is present ────────────────────────────
# Rolling back code while the database is on a newer revision leaves Alembic unable to resolve the
# chain, which restart-loops backend and bot. The skip path above (no .env/compose) must NOT be the
# only thing exercised, or a regression that silently disables the check would go unnoticed.
stackdir="$tmp/stack"
mkdir -p "$stackdir/deploy" "$stackdir/update/releases"
cp "$REPO_DIR/deploy/rollback.sh" "$stackdir/deploy/"
cp "$REPO_DIR/deploy/docker-compose.prod.yml" "$stackdir/deploy/"
printf 'POSTGRES_USER=u\nPOSTGRES_DB=d\n' > "$stackdir/.env"
cp "$assets/invoice-system-v1.0.0.tar.gz" "$stackdir/update/releases/"

# The target release ships one migration; the fake stack reports a DIFFERENT (newer) revision.
target_root="$tmp/target-with-migrations/invoice-system-v1.0.0"
mkdir -p "$target_root/backend/alembic/versions" "$target_root/deploy"
printf 'revision = "aaaaaaaa1111"\ndown_revision = None\n' \
  > "$target_root/backend/alembic/versions/aaaaaaaa1111_base.py"
printf '%s\n' "1.0.0" > "$target_root/VERSION"
cp "$REPO_DIR/deploy/release-installer.sh" "$REPO_DIR/deploy/env.sh" "$target_root/deploy/"
printf '#!/usr/bin/env bash\nset -euo pipefail\n' > "$target_root/deploy/install.sh"
printf 'VERSION\n' > "$target_root/.release-files"
tar -czf "$stackdir/update/releases/invoice-system-v1.0.0.tar.gz" \
  -C "$tmp/target-with-migrations" "invoice-system-v1.0.0"

# A `docker` stub that answers `alembic current` with a revision the target does not contain.
fakebin="$tmp/fakebin"
mkdir -p "$fakebin"
cat > "$fakebin/docker" <<'DOCKER'
#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "$arg" == "current" ]]; then echo "bbbbbbbb2222 (head)"; exit 0; fi
done
exit 0
DOCKER
chmod +x "$fakebin/docker"

set +e
PATH="$fakebin:$PATH" REPO_DIR="$stackdir" RELEASE_CACHE_DIR="$stackdir/update/releases" \
  ALLOW_NON_ROOT=1 SKIP_INSTALL=1 bash "$stackdir/deploy/rollback.sh" v1.0.0 \
  > "$tmp/preflight.log" 2>&1
preflight_rc=$?
set -e
[[ "$preflight_rc" -ne 0 ]] || {
  echo "rollback did NOT refuse despite the database being ahead of the target" >&2
  cat "$tmp/preflight.log" >&2
  exit 1
}
grep -q "SCHEMA DOWNGRADE REQUIRED" "$tmp/preflight.log" || {
  echo "rollback refused, but not because of the schema mismatch" >&2
  cat "$tmp/preflight.log" >&2
  exit 1
}
grep -q "ALLOW_DOWNGRADE=1" "$tmp/preflight.log" || {
  echo "the refusal does not tell the operator how to proceed" >&2
  exit 1
}
echo "rollback schema pre-flight OK (refuses a code-only rollback across a migration)"
