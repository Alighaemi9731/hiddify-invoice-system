#!/usr/bin/env bash
# ============================================================================
#  Fresh-server bootstrap: clone the PRIVATE repo via a read-only SSH deploy key,
#  then run the installer. Use this on a brand-new Ubuntu 24.04/26.04 server.
#
#  Steps you do ONCE (locally / on GitHub):
#    1) On the server:  ssh-keygen -t ed25519 -f ~/.ssh/invoice_deploy -N ''
#    2) Copy the PUBLIC key:  cat ~/.ssh/invoice_deploy.pub
#    3) GitHub → your repo → Settings → Deploy keys → "Add deploy key"
#       (paste the public key; leave "Allow write access" UNCHECKED — read-only).
#
#  Then run:
#    sudo REPO=git@github.com:Alighaemi9731/hiddify-invoice-system.git \
#         KEY=~/.ssh/invoice_deploy \
#         DOMAIN=panel.example.com ACME_EMAIL=you@mail.com ADMIN_PASSWORD='...' \
#         bash deploy/bootstrap.sh
# ============================================================================
set -euo pipefail

REPO="${REPO:-git@github.com:Alighaemi9731/hiddify-invoice-system.git}"
KEY="${KEY:-$HOME/.ssh/invoice_deploy}"
DEST="${DEST:-/opt/hiddify-invoice-system}"
BRANCH="${BRANCH:-main}"

c() { printf "\033[1;36m%s\033[0m\n" "$*"; }
err() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

[[ $EUID -ne 0 ]] && { err "Run as root (sudo)."; exit 1; }
[[ -f "$KEY" ]] || { err "Deploy key not found at $KEY. Create it and add the .pub to GitHub Deploy keys first."; exit 1; }

command -v git >/dev/null 2>&1 || { apt-get update -y && apt-get install -y git; }

export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [[ -d "$DEST/.git" ]]; then
  c "Updating existing checkout at $DEST …"
  git -C "$DEST" fetch --all -q
  git -C "$DEST" checkout "$BRANCH" -q
  git -C "$DEST" pull -q
else
  c "Cloning $REPO → $DEST …"
  git clone --branch "$BRANCH" "$REPO" "$DEST"
fi

c "Running installer …"
cd "$DEST"
DOMAIN="${DOMAIN:-}" ACME_EMAIL="${ACME_EMAIL:-}" ADMIN_PASSWORD="${ADMIN_PASSWORD:-}" \
  bash deploy/install.sh
