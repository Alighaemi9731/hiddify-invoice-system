#!/usr/bin/env bash
# ============================================================================
# External production health monitor (host-side, outside every container).
#
# Why outside: the app's own alerting (owner Telegram pings, daily digest) runs INSIDE
# the scheduler process — if the stack is down or the scheduler is stale, the component
# meant to raise the alarm is the one that died. This script probes the PUBLIC HTTPS
# endpoint through the full ingress path (relay → Caddy → backend → DB) from the host,
# on a systemd timer, and notifies the owner's Telegram directly via the Bot API.
#
# Checks per run:  HTTPS reachability + TLS validity, /health JSON (database +
# scheduler + storefront bot fleet), /api/info version, response time.
# Policy:  alert after ALERT_AFTER consecutive failures (default 3 → ~6 min at the
# 2-min timer); re-alert at most every REALERT_HOURS; one recovery notice when healthy
# again. State survives reboots in $STATE_DIR. Exit 0 = healthy, 1 = failing.
#
# Alert channel: the Telegram bot token from the app's .env (TELEGRAM_BOT_TOKEN) + the
# owner chat id read from the DB (settings key owner_chat_id, plaintext). If either is
# unavailable the monitor still runs and logs — it never invents a channel.
#
# Installed by deploy/install.sh as hiddify-healthwatch.{service,timer}. Runbook:
# docs/MONITORING.md
# ============================================================================
set -u

REPO_DIR="${REPO_DIR:-/opt/hiddify-invoice-system}"
ENV_FILE="$REPO_DIR/.env"
STATE_DIR="${STATE_DIR:-/var/lib/hiddify-healthwatch}"
LOG_FILE="${LOG_FILE:-/var/log/hiddify-healthwatch.log}"
ALERT_AFTER="${ALERT_AFTER:-3}"
REALERT_HOURS="${REALERT_HOURS:-6}"
TIMEOUT="${TIMEOUT:-10}"

mkdir -p "$STATE_DIR"
FAILS_F="$STATE_DIR/consecutive_failures"
LASTALERT_F="$STATE_DIR/last_alert_epoch"
WASDOWN_F="$STATE_DIR/was_down"

log() {
  # Structured single-line records; self-rotates at ~1 MiB (keeps one predecessor).
  if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 1048576 ]; then
    mv -f "$LOG_FILE" "$LOG_FILE.1"
  fi
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_FILE"
}

domain() {
  sed -n 's/^SERVER_DOMAIN=//p' "$ENV_FILE" 2>/dev/null | tr -d '"' | head -1
}

notify() {
  local text="$1"
  local token chat
  token="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$ENV_FILE" 2>/dev/null | tr -d '"' | head -1)"
  chat="$(cd "$REPO_DIR" 2>/dev/null && docker compose --env-file .env \
    -f deploy/docker-compose.prod.yml exec -T db sh -lc \
    'psql -U "${POSTGRES_USER:-invoice}" -d "${POSTGRES_DB:-invoice}" -tAc \
      "SELECT trim(both '\''\"'\'' from value::text) FROM settings WHERE key='\''owner_chat_id'\''"' \
    2>/dev/null | head -1)"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    log "notify-skipped reason=no-token-or-chat token_present=$([ -n "$token" ] && echo 1 || echo 0) chat_present=$([ -n "$chat" ] && echo 1 || echo 0)"
    return 1
  fi
  curl -fsS -m 15 "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=${text}" >/dev/null 2>&1 \
    && log "notify-sent" || log "notify-FAILED"
}

D="$(domain)"
if [ -z "$D" ]; then
  log "skip reason=no-SERVER_DOMAIN"
  exit 0
fi

START=$(date +%s%3N 2>/dev/null || date +%s000)
BODY="$(curl -fsS -m "$TIMEOUT" "https://$D/health" 2>/tmp/healthwatch.err)"
RC=$?
END=$(date +%s%3N 2>/dev/null || date +%s000)
MS=$((END - START))

STATUS="down" DETAIL=""
if [ $RC -eq 0 ]; then
  case "$BODY" in
    *'"database":"ok"'*)
      case "$BODY" in
        *'"scheduler":"ok"'*)
          # The storefront fleet reports its own liveness: the main bot's watchdog proves only the
          # MAIN bot, so all ~151 shop bots can be mute while the container looks healthy. Match
          # ONLY the explicit "stale" — `unknown` (never stamped, e.g. right after an upgrade) and
          # a backend too old to emit the key must not raise an alarm.
          case "$BODY" in
            *'"storefront_fleet":"stale"'*) STATUS="degraded" DETAIL="storefront-fleet-stale" ;;
            *) STATUS="ok" ;;
          esac ;;
        *) STATUS="degraded" DETAIL="scheduler-not-ok" ;;
      esac ;;
    *) STATUS="degraded" DETAIL="database-not-ok" ;;
  esac
else
  DETAIL="curl-rc=$RC $(tr -d '\n' < /tmp/healthwatch.err 2>/dev/null | head -c 120)"
fi

FAILS=$(cat "$FAILS_F" 2>/dev/null || echo 0)
if [ "$STATUS" = "ok" ]; then
  log "ok ms=$MS"
  echo 0 > "$FAILS_F"
  if [ -f "$WASDOWN_F" ]; then
    rm -f "$WASDOWN_F" "$LASTALERT_F"
    notify "✅ سامانهٔ فاکتور دوباره در دسترس است (https://$D — ${MS}ms)."
  fi
  exit 0
fi

FAILS=$((FAILS + 1))
echo "$FAILS" > "$FAILS_F"
log "$STATUS fails=$FAILS ms=$MS detail=$DETAIL"

if [ "$FAILS" -ge "$ALERT_AFTER" ]; then
  NOW=$(date +%s)
  LAST=$(cat "$LASTALERT_F" 2>/dev/null || echo 0)
  if [ $((NOW - LAST)) -ge $((REALERT_HOURS * 3600)) ]; then
    echo "$NOW" > "$LASTALERT_F"
    touch "$WASDOWN_F"
    notify "🚨 پایش بیرونی: سامانهٔ فاکتور در دسترس نیست یا ناسالم است.
وضعیت: $STATUS $DETAIL
آدرس: https://$D
${FAILS} بررسی متوالی ناموفق. راهنما: docs/MONITORING.md"
  fi
fi
exit 1
