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
DOMAIN="${DOMAIN:-}"
ACME_EMAIL="${ACME_EMAIL:-}"
ADMIN_USERNAME="${ADMIN_USERNAME:-owner}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

if [[ -z "$DOMAIN" ]]; then read -rp "Domain (A record must point to THIS server's IP): " DOMAIN; fi
if [[ -z "$ACME_EMAIL" ]]; then read -rp "Email for SSL certificate (Let's Encrypt): " ACME_EMAIL; fi
if [[ -z "$ADMIN_PASSWORD" ]]; then read -rsp "Admin panel password: " ADMIN_PASSWORD; echo; fi

[[ -z "$DOMAIN" || -z "$ADMIN_PASSWORD" ]] && { err "Domain and admin password are required."; exit 1; }

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
rand() { openssl rand -base64 "${1:-48}" | tr -d '\n/+=' | cut -c1-"${2:-44}"; }

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
# --env-file points compose at the repo-root .env (the compose file lives in deploy/).
docker compose --env-file "$ENV_FILE" -f deploy/docker-compose.prod.yml up -d --build

cat <<DONE

────────────────────────────────────────────────────────────
✅ Done. Open:  https://$DOMAIN
   Login:  $ADMIN_USERNAME  /  (the password you entered)

Next:
  • Make sure $DOMAIN's A record points to this server, and ports 80/443 are open.
  • In the panel → Settings: set the Telegram bot token, USDT wallet, BscScan key.
  • Logs:    docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f
  • Restart: docker compose --env-file .env -f deploy/docker-compose.prod.yml restart
  • Update:  git pull && docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
────────────────────────────────────────────────────────────
DONE
