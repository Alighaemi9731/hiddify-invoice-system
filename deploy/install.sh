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
# --env-file points compose at the repo-root .env (the compose file lives in deploy/).
docker compose --env-file "$ENV_FILE" -f deploy/docker-compose.prod.yml up -d --build

URL="http://$SERVER_IP"

if [[ -n "$ADMIN_PASSWORD" ]]; then
  # Scripted install (password preseeded) → owner created at boot, no browser wizard.
  CRED_LINE="   نام کاربری:  $ADMIN_USERNAME
   رمز عبور:  همان رمزی که تعیین کردید"
  SETUP_NOTE="• حساب مدیر از قبل ساخته شد. مستقیم وارد شوید."
else
  # Default path → the in-browser «راه‌اندازی اولیه» wizard collects everything.
  CRED_LINE=""
  SETUP_NOTE="• آدرس بالا را در مرورگر باز کنید؛ صفحهٔ «راه‌اندازی اولیه» نام کاربری، رمز عبور و دامنه را می‌گیرد و خودش SSL را فعال می‌کند."
fi

cat <<DONE

────────────────────────────────────────────────────────────
✅ نصب کامل شد.

   این آدرس را در مرورگر باز کنید:
   $URL
$CRED_LINE

نکته‌ها:
  $SETUP_NOTE
  • پس از ورود، در «تنظیمات» توکن ربات، کیف پول USDT و کلید BscScan را وارد کنید.
  • لاگ‌ها:   docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f
  • ری‌استارت: docker compose --env-file .env -f deploy/docker-compose.prod.yml restart
  • به‌روزرسانی: git pull && docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
────────────────────────────────────────────────────────────
DONE
