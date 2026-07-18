#!/usr/bin/env bash
# Roll back application code to a previously verified, locally cached release.
#
# Rolling back CODE alone is not enough. The database keeps whatever schema the newer release
# migrated it to, and Alembic refuses to start when `alembic_version` names a revision the older
# code does not contain:
#
#     CommandError: Can't locate revision identified by '<newer head>'
#
# That is raised on boot from app/core/db.py::_upgrade_schema BEFORE any DDL runs, so it fires even
# for a purely additive migration the older code would otherwise tolerate. Nothing catches it, and
# `restart: unless-stopped` turns it into an endless restart loop on BOTH backend and bot.
#
# So this script downgrades the schema to the target release's head FIRST, while the newer code —
# which still holds the `downgrade()` bodies — is on disk. Order is load-bearing:
# release-installer.sh deletes files absent from the target's manifest, i.e. exactly those modules.
#
# Fail-closed: any problem aborts non-zero and leaves the system on the CURRENT release, still
# running. A half-rolled-back system is worse than one that did not roll back.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/hiddify-invoice-system}"
RELEASE_CACHE_DIR="${RELEASE_CACHE_DIR:-$REPO_DIR/update/releases}"
COMPOSE="docker compose --env-file $REPO_DIR/.env -f $REPO_DIR/deploy/docker-compose.prod.yml"
TAG="${1:-}"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "usage: $0 vX.Y.Z" >&2
  exit 1
}

log() { printf '%s\n' "$*" >&2; }

# ── Is there a live deployment whose schema we could even check? ──────────────────────────────
# A real install always has both an .env and the compose file. Without them there is no database
# here (a bare code-tree rollback, or the CI fixture), so the schema pre-flight has nothing to do.
# This is not an escape hatch for production: a running stack always has both.
schema_check=1
if [[ ! -f "$REPO_DIR/.env" || ! -f "$REPO_DIR/deploy/docker-compose.prod.yml" ]]; then
  schema_check=0
  log "No deployed stack found at $REPO_DIR (missing .env or compose file) —"
  log "rolling back CODE only, without a schema check."
fi

# ── Locate the target archive (release-installer.sh re-verifies its checksum) ─────────────────
archive=""
for candidate in \
  "$RELEASE_CACHE_DIR/invoice-system-$TAG.tar.gz" \
  "$RELEASE_CACHE_DIR/$TAG/invoice-system-$TAG.tar.gz"
do
  [[ -f "$candidate" ]] && { archive="$candidate"; break; }
done
if [[ -z "$archive" ]]; then
  log "No cached archive for $TAG under $RELEASE_CACHE_DIR."
  log "Rollback works offline from the cache and never downloads. Nothing was changed."
  exit 1
fi

# ── Which revisions does the TARGET release know about? ───────────────────────────────────────
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tar -xzf "$archive" -C "$tmp" 2>/dev/null
target_versions_dir="$(find "$tmp" -type d -path '*/backend/alembic/versions' -print -quit)"
if [[ "$schema_check" == "1" && -z "$target_versions_dir" ]]; then
  # A live database exists but the target ships no migrations at all — we cannot reason about
  # whether its code can read this schema, and guessing is how you get a restart loop.
  log "Could not read $TAG's migration set from the archive. Refusing to roll back blind."
  exit 1
fi

if [[ "$schema_check" == "1" ]]; then

# The revision id is the `revision = "..."` assignment inside each module, NOT the filename prefix.
target_revs="$(grep -rhoE '^revision[[:space:]:a-zA-Z]*=[[:space:]]*["'"'"'][0-9a-f]+["'"'"']' \
  "$target_versions_dir" | grep -oE '[0-9a-f]{8,}' | sort -u)"
[[ -n "$target_revs" ]] || { log "No revisions found in $TAG's archive. Aborting."; exit 1; }

# ── Which revision is the database actually on? ───────────────────────────────────────────────
db_rev="$($COMPOSE exec -T backend alembic current 2>/dev/null \
  | grep -oE '^[0-9a-f]{8,}' | head -1 || true)"
if [[ -z "$db_rev" ]]; then
  log "Could not read the database revision — is the stack running?"
  log "Refusing to roll back blind. Start the stack, or follow docs/DATABASE.md § Rolling back."
  exit 1
fi

if grep -qx "$db_rev" <<<"$target_revs"; then
  log "Database revision $db_rev exists in $TAG — no schema downgrade needed."
else
  # The DB is AHEAD of the target. Downgrade to the target's head before swapping the code.
  # The target's head is the revision nothing else in its own set points down to.
  target_head=""
  for rev in $target_revs; do
    if ! grep -rhqE "down_revision[[:space:]:a-zA-Z|]*=[[:space:]]*[\"']$rev[\"']" \
         "$target_versions_dir"; then
      target_head="$rev"
      break
    fi
  done
  [[ -n "$target_head" ]] || { log "Could not determine $TAG's head revision. Aborting."; exit 1; }

  log "────────────────────────────────────────────────────────────────────────────"
  log "  SCHEMA DOWNGRADE REQUIRED"
  log "    database is at : $db_rev"
  log "    $TAG expects   : $target_head"
  log ""
  log "  Without this the rolled-back containers cannot start AT ALL: Alembic will not"
  log "  find $db_rev in the older code, and backend + bot will restart-loop."
  log "────────────────────────────────────────────────────────────────────────────"

  if [[ "${ALLOW_DOWNGRADE:-0}" != "1" ]]; then
    log "This reverses schema changes. Re-run to confirm:"
    log "    sudo ALLOW_DOWNGRADE=1 $0 $TAG"
    log "Nothing was changed."
    exit 1
  fi

  # Safety dump BEFORE any DDL — the artifact you restore if the downgrade goes wrong.
  mkdir -p "$REPO_DIR/update"
  dump="$REPO_DIR/update/rollback-$TAG-$(date -u +%Y%m%d-%H%M%S).sql"
  log "Taking a pre-downgrade dump → $dump"
  # shellcheck disable=SC1091
  set -a; . "$REPO_DIR/.env"; set +a
  if ! $COMPOSE exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "$dump" 2>/dev/null; then
    log "Pre-downgrade pg_dump FAILED. Aborting — never migrate down without a way back."
    rm -f "$dump"
    exit 1
  fi
  if [[ ! -s "$dump" ]] || ! grep -q '^CREATE TABLE public\.' "$dump"; then
    log "Pre-downgrade dump is empty or has no tables. Aborting."
    rm -f "$dump"
    exit 1
  fi
  log "Dump OK ($(wc -c < "$dump") bytes)."

  log "Downgrading $db_rev → $target_head in the CURRENT container (it still ships the"
  log "downgrade() bodies that $TAG does not have)…"
  if ! $COMPOSE exec -T backend alembic downgrade "$target_head"; then
    log "Schema downgrade FAILED. The system is untouched and still on the current release."
    log "Restore point if you need it: $dump"
    exit 1
  fi
  log "Schema is now at $target_head."
fi

fi  # end schema_check

# ── Swap the code ─────────────────────────────────────────────────────────────────────────────
RELEASE_TAG="$TAG" \
DEST="$REPO_DIR" \
RELEASE_CACHE_DIR="$RELEASE_CACHE_DIR" \
OFFLINE=1 \
SKIP_INSTALL="${SKIP_INSTALL:-0}" \
ALLOW_NON_ROOT="${ALLOW_NON_ROOT:-0}" \
  bash "$REPO_DIR/deploy/release-installer.sh"
