#!/usr/bin/env bash
# ============================================================================
#  True one-line installer — public repo, no key needed.
#
#    curl -fsSL https://raw.githubusercontent.com/Alighaemi9731/hiddify-invoice-system/main/get.sh | sudo bash
#
#  Clones the repo to /opt/hiddify-invoice-system and runs deploy/install.sh.
#  Installs with NO domain by default → reachable at http://<server-ip> right away;
#  set your domain later inside the panel (Account & Backup → دامنه و HTTPS).
#
#  Optional overrides (env):
#    REPO_URL, DEST, BRANCH, DOMAIN, ACME_EMAIL, ADMIN_USERNAME, ADMIN_PASSWORD
# ============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Alighaemi9731/hiddify-invoice-system.git}"
DEST="${DEST:-/opt/hiddify-invoice-system}"
BRANCH="${BRANCH:-main}"

c() { printf "\033[1;36m%s\033[0m\n" "$*"; }
err() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }
[[ $EUID -ne 0 ]] && { err "Run with sudo:  curl -fsSL .../get.sh | sudo bash"; exit 1; }

command -v git >/dev/null 2>&1 || { apt-get update -y && apt-get install -y git; }

if [[ -d "$DEST/.git" ]]; then
  c "Updating $DEST …"; git -C "$DEST" fetch -q --all; git -C "$DEST" checkout -q "$BRANCH"; git -C "$DEST" pull -q
else
  c "Cloning into $DEST …"; git clone -q --branch "$BRANCH" "$REPO_URL" "$DEST"
fi

cd "$DEST"
DOMAIN="${DOMAIN:-}" ACME_EMAIL="${ACME_EMAIL:-}" \
  ADMIN_USERNAME="${ADMIN_USERNAME:-owner}" ADMIN_PASSWORD="${ADMIN_PASSWORD:-}" \
  bash deploy/install.sh
