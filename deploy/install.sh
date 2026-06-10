#!/usr/bin/env bash
# ============================================================================
#  Hiddify Reseller Invoicing System — one-line installer (Phase 2)
#
#  Target: Ubuntu 24.04 / 26.04 (detects + adapts). Installs Docker, generates
#  a secure .env, and brings up the full stack behind Caddy (automatic HTTPS).
#
#  Usage (on a fresh server, as root):
#     bash deploy/install.sh
#  or non-interactive:
#     DOMAIN=panel.example.com ACME_EMAIL=you@mail.com ADMIN_PASSWORD=... bash deploy/install.sh
# ============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"

c() { printf "\033[1;36m%s\033[0m\n" "$*"; }
err() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

# ---- 0. checks --------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then err "Please run as root (sudo)."; exit 1; fi

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  c "Detected: ${PRETTY_NAME:-unknown}"
  if [[ "${ID:-}" != "ubuntu" ]]; then
    err "This installer targets Ubuntu. Continuing anyway in 3s…"; sleep 3
  fi
  case "${VERSION_ID:-}" in
    24.*|25.*|26.*) ;;
    *) err "Tested on Ubuntu 24.04/26.04; yours is ${VERSION_ID:-?}. Continuing…"; sleep 2 ;;
  esac
fi

# ---- 1. inputs --------------------------------------------------------------
# Fully non-interactive. By default NOTHING is asked: the panel's first-run wizard
# (in the browser) collects username/password/domain. Power users can still preseed
# DOMAIN/ADMIN_PASSWORD via env to skip the wizard.
DOMAIN="${DOMAIN:-}"
ACME_EMAIL="${ACME_EMAIL:-}"
ADMIN_USERNAME="${ADMIN_USERNAME:-owner}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"   # empty → setup wizard runs in the browser

rand() { openssl rand -base64 "${1:-48}" | tr -dc 'A-Za-z0-9' | cut -c1-"${2:-44}"; }

SERVER_IP="$(curl -fsSL https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

# ---- 2. docker --------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  c "Installing Docker…"
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
else
  c "Docker already installed."
fi

# ---- 3. .env ----------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  c "Generating $ENV_FILE …"
  SECRET_KEY="$(rand 48 44)"
  DB_PASS="$(rand 24 24)"
  cat > "$ENV_FILE" <<EOF
APP_ENV=production
SECRET_KEY=$SECRET_KEY
ACCESS_TOKEN_EXPIRE_MINUTES=720

ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_PASSWORD=$ADMIN_PASSWORD

POSTGRES_USER=invoice
POSTGRES_PASSWORD=$DB_PASS
POSTGRES_DB=invoice
POSTGRES_HOST=db
POSTGRES_PORT=5432
DATABASE_URL=postgresql+asyncpg://invoice:$DB_PASS@db:5432/invoice

# Domain + automatic HTTPS (Caddy)
SERVER_DOMAIN=$DOMAIN
ACME_EMAIL=$ACME_EMAIL

# Bot / payments — set these from the panel Settings tab after first login,
# or fill them here before first boot.
TELEGRAM_BOT_TOKEN=
USDT_BEP20_ADDRESS=
USDT_BEP20_CONTRACT=0x55d398326f99059fF775485246999027B3197955
BSCSCAN_API_KEY=
USDT_MASTER_XPUB=
DEFAULT_PRICE_PER_GB_TOMAN=1000
TOMAN_PER_USDT=70000
EOF
  chmod 600 "$ENV_FILE"
  c "Generated SECRET_KEY + DB password (kept in $ENV_FILE, mode 600)."
else
  c "$ENV_FILE already exists — keeping it. Ensure SERVER_DOMAIN/ACME_EMAIL are set."
fi

# ---- 4. up ------------------------------------------------------------------
c "Building and starting the stack (this can take a few minutes)…"
cd "$REPO_DIR"
COMPOSE="docker compose --env-file $ENV_FILE -f deploy/docker-compose.prod.yml"
# Clean rebuild to the latest release. `down` (WITHOUT -v) removes the old
# containers/network but KEEPS the named volumes — so the Postgres database
# (db_data) and Caddy certs survive across re-installs. Only the panel's
# «پاک‌سازی داده‌ها» wipes data.
$COMPOSE down --remove-orphans 2>/dev/null || true
$COMPOSE up -d --build --force-recreate

# ---- 5. in-panel updater (host-side watcher) --------------------------------
# Install/refresh a tiny systemd service that lets the panel's «به‌روزرسانی» button
# trigger this very script. The backend container is sandboxed (no Docker socket), so a
# host-side helper is the safe way to rebuild. Idempotent: re-running the installer
# updates the unit. Skipped gracefully if systemd isn't available.
if command -v systemctl >/dev/null 2>&1; then
  c "Installing the in-panel update watcher (systemd: hiddify-updater)…"
  mkdir -p "$REPO_DIR/update"
  cat > /etc/systemd/system/hiddify-updater.service <<UNIT
[Unit]
Description=Hiddify Invoice System — in-panel update watcher
After=docker.service network-online.target
Wants=docker.service

[Service]
Type=simple
Environment=REPO_DIR=$REPO_DIR
Environment=UPDATE_DIR=$REPO_DIR/update
ExecStart=/usr/bin/env bash $REPO_DIR/deploy/updater.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  chmod +x "$REPO_DIR/deploy/updater.sh" 2>/dev/null || true
  systemctl daemon-reload
  if [[ "${IN_PANEL_UPDATE:-0}" == "1" ]] && systemctl is-active --quiet hiddify-updater; then
    systemctl enable hiddify-updater >/dev/null 2>&1 || true
  else
    systemctl enable --now hiddify-updater >/dev/null 2>&1 \
      && systemctl restart hiddify-updater >/dev/null 2>&1 || true
  fi
  c "Update watcher active — the panel can now self-update."
else
  err "systemd not found — the in-panel «به‌روزرسانی» button won't work (update from the terminal instead)."
fi

# English banner — terminals render LTR, so RTL Persian looks scrambled here.
VER="$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo "")"
# If a domain was configured on a prior install (kept in .env), the panel lives at
# https://<domain> and the bare IP redirects there — point the user at the domain.
EXISTING_DOMAIN="$(grep -E '^SERVER_DOMAIN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')"
if [[ -n "$EXISTING_DOMAIN" ]]; then
  URL="https://$EXISTING_DOMAIN"
else
  URL="http://$SERVER_IP"
fi

if [[ -n "$ADMIN_PASSWORD" ]]; then
  LOGIN_NOTE="Log in with the username/password you set (user: $ADMIN_USERNAME)."
elif [[ -n "$EXISTING_DOMAIN" ]]; then
  LOGIN_NOTE="Already set up — log in with your existing username/password (your data was kept)."
else
  LOGIN_NOTE="First run: the page asks for a username, password and domain, then gets SSL automatically."
fi

c "Running post-deploy smoke checks…"
bash "$REPO_DIR/deploy/smoke.sh"

cat <<DONE

────────────────────────────────────────────────────────────
  Installation complete${VER:+  (v$VER)}.

  Open in your browser:
      $URL

  $LOGIN_NOTE
  After login, set your Bot token, USDT wallet and BscScan key under Settings.

  Manage from the server (run as root):
      Update :  cd $REPO_DIR && sudo bash deploy/release-installer.sh
      Rollback:  cd $REPO_DIR && sudo bash deploy/rollback.sh vX.Y.Z
      Logs   :  cd $REPO_DIR && docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f
      Restart:  cd $REPO_DIR && docker compose --env-file .env -f deploy/docker-compose.prod.yml restart
────────────────────────────────────────────────────────────
DONE
