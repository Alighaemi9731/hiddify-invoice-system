#!/usr/bin/env bash
# ============================================================================
#  One-line installer / updater — public repo, no key, no questions.
#
#    curl -fsSL https://raw.githubusercontent.com/Alighaemi9731/hiddify-invoice-system/main/get.sh | sudo bash
#
#  Run it the FIRST time to install, and run the SAME command any time to update
#  to the latest release. It always rebuilds from scratch to the newest version,
#  but your DATABASE is PRESERVED across installs (data is never wiped here).
#  To erase data, use the panel: «حساب و پشتیبان → پاک‌سازی کامل داده‌ها».
#
#  After install it prints http://<server-ip> — open it and the first-run wizard
#  asks for username / password / domain.
#
#  Optional overrides (env): REPO_URL, DEST, BRANCH, DOMAIN, ACME_EMAIL,
#  ADMIN_USERNAME, ADMIN_PASSWORD (set the last two only to skip the wizard).
# ============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Alighaemi9731/hiddify-invoice-system.git}"
DEST="${DEST:-/opt/hiddify-invoice-system}"

c() { printf "\033[1;36m%s\033[0m\n" "$*"; }
err() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }
[[ $EUID -ne 0 ]] && { err "Run with sudo:  curl -fsSL .../get.sh | sudo bash"; exit 1; }

command -v git >/dev/null 2>&1 || { apt-get update -y && apt-get install -y git; }

if [[ -d "$DEST/.git" ]]; then
  c "Updating $DEST …"
  git -C "$DEST" fetch -q --all --tags --prune
else
  c "Cloning into $DEST …"
  git clone -q "$REPO_URL" "$DEST"
  git -C "$DEST" fetch -q --tags
fi

cd "$DEST"
# Install the latest RELEASE tag (override with BRANCH=main for the dev tip).
if [[ -n "${BRANCH:-}" ]]; then
  TARGET="$BRANCH"
else
  TARGET="$(git tag -l 'v*' | sort -V | tail -n1)"
  [[ -z "$TARGET" ]] && TARGET="main"
fi
c "Deploying $TARGET …"
git checkout -q -f "$TARGET"
# For a branch target keep it current; for a tag (detached HEAD) this is a no-op.
git pull -q --ff-only 2>/dev/null || true

DOMAIN="${DOMAIN:-}" ACME_EMAIL="${ACME_EMAIL:-}" \
  ADMIN_USERNAME="${ADMIN_USERNAME:-owner}" ADMIN_PASSWORD="${ADMIN_PASSWORD:-}" \
  bash deploy/install.sh
