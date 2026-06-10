#!/usr/bin/env bash
# ============================================================================
#  Local compatibility wrapper for the checksum-verified release installer.
#
#  Do not pipe this file from a mutable branch into a root shell. Follow README.md:
#  download release-installer.sh and its SHA-256 from GitHub Releases, verify, run.
#
#  Run it the FIRST time to install, and run the SAME command any time to update
#  to the latest release. It always rebuilds from scratch to the newest version,
#  but your DATABASE is PRESERVED across installs (data is never wiped here).
#  To erase data, use the panel: «حساب و پشتیبان → پاک‌سازی کامل داده‌ها».
#
#  After install it prints http://<server-ip> — open it and the first-run wizard
#  asks for username / password / domain.
#
#  Optional overrides (env): DEST, RELEASE_TAG, DOMAIN, ACME_EMAIL,
#  ADMIN_USERNAME, ADMIN_PASSWORD (set the last two only to skip the wizard).
# ============================================================================
set -euo pipefail

DEST="${DEST:-/opt/hiddify-invoice-system}"

c() { printf "\033[1;36m%s\033[0m\n" "$*"; }
err() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }
[[ $EUID -ne 0 ]] && { err "Run with sudo:  curl -fsSL .../get.sh | sudo bash"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
installer="$SCRIPT_DIR/deploy/release-installer.sh"
[[ -f "$installer" ]] || {
  err "release-installer.sh is missing; use the verified GitHub Release installer."
  exit 1
}
c "Installing verified release ${RELEASE_TAG:-latest} …"
DEST="$DEST" RELEASE_TAG="${RELEASE_TAG:-latest}" \
DOMAIN="${DOMAIN:-}" ACME_EMAIL="${ACME_EMAIL:-}" \
ADMIN_USERNAME="${ADMIN_USERNAME:-owner}" ADMIN_PASSWORD="${ADMIN_PASSWORD:-}" \
  bash "$installer"
