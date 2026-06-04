#!/usr/bin/env bash
# ============================================================================
#  Host-side update watcher for the Hiddify Invoice System.
#
#  The backend container is sandboxed (no Docker socket), so it cannot rebuild
#  itself. This tiny watcher runs on the HOST as a systemd service: it polls a
#  flag file that the panel's «به‌روزرسانی» button writes, and when it appears
#  runs get.sh (pull latest release + docker compose up --build). Progress is
#  written back to a status file the panel polls.
#
#  Installed + enabled automatically by deploy/install.sh. Safe: it only ever
#  runs the project's own get.sh; it takes no arguments from the request.
#
#  Flag/status files live in the repo's update dir (bind-mounted into the
#  backend container at /app/data/.update-* — see docker-compose.prod.yml).
# ============================================================================
set -uo pipefail

REPO_DIR="${REPO_DIR:-/opt/hiddify-invoice-system}"
UPDATE_DIR="${UPDATE_DIR:-$REPO_DIR/update}"
REQUEST="$UPDATE_DIR/.update-requested"
STATUS="$UPDATE_DIR/.update-status"
GET_URL="https://raw.githubusercontent.com/Alighaemi9731/hiddify-invoice-system/main/get.sh"
POLL="${POLL:-5}"

mkdir -p "$UPDATE_DIR"

# A persistent presence marker so the panel knows the watcher is installed even between
# update requests (the status file is reset on each request). Refreshed on every startup.
date -u +%Y-%m-%dT%H:%M:%SZ > "$UPDATE_DIR/.updater-alive" 2>/dev/null || true

# Write a status JSON the panel reads: phase + message + version + timestamp.
write_status() {
  local phase="$1" message="$2"
  local ver
  ver="$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo "")"
  printf '{"phase":"%s","message":"%s","version":"%s","ts":"%s"}\n' \
    "$phase" "$message" "$ver" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATUS"
}

run_update() {
  rm -f "$REQUEST"
  write_status "running" "در حال دریافت آخرین نسخه و بازسازی…"
  # Run the same one-line updater the user would run by hand. It pulls the latest
  # release tag and rebuilds, preserving the database. Capture the log for diagnosis.
  if curl -fsSL "$GET_URL" | bash >>"$UPDATE_DIR/update.log" 2>&1; then
    write_status "done" "به‌روزرسانی با موفقیت انجام شد."
  else
    write_status "failed" "به‌روزرسانی ناموفق بود؛ گزارش را در update/update.log ببینید."
  fi
}

write_status "idle" "آماده"
while true; do
  if [[ -f "$REQUEST" ]]; then
    run_update
  fi
  sleep "$POLL"
done
