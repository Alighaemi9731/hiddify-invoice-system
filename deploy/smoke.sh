#!/usr/bin/env bash
# Post-deploy readiness checks. Fails the installer when production is not usable.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$REPO_DIR/deploy/docker-compose.prod.yml")
EXPECTED_VERSION="$(cat "$REPO_DIR/VERSION")"

domain="$(grep -E '^SERVER_DOMAIN=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
if [[ -n "${SMOKE_URL:-}" ]]; then
  base_url="${SMOKE_URL%/}"
elif [[ -n "$domain" ]]; then
  base_url="https://$domain"
else
  base_url="http://127.0.0.1"
fi

for service in db backend bot frontend caddy; do
  id="$("${COMPOSE[@]}" ps -q "$service")"
  [[ -n "$id" ]] || { echo "missing container: $service" >&2; exit 1; }
  state="$(docker inspect -f '{{.State.Status}}' "$id")"
  [[ "$state" == "running" ]] || { echo "$service is $state" >&2; exit 1; }
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")"
  [[ "$health" != "unhealthy" ]] || { echo "$service is unhealthy" >&2; exit 1; }
done

body=""
for _ in $(seq 1 36); do
  if body="$(curl -fsS --max-time 10 "$base_url/health" 2>/dev/null)" \
    && grep -q '"database":"ok"' <<<"$body"; then
    break
  fi
  body=""
  sleep 5
done
[[ -n "$body" ]] || { echo "database-aware health check failed: $base_url/health" >&2; exit 1; }

info="$(curl -fsS --max-time 10 "$base_url/api/info")"
grep -q "\"version\":\"$EXPECTED_VERSION\"" <<<"$info" || {
  echo "deployed API version does not match $EXPECTED_VERSION: $info" >&2
  exit 1
}

revision="$("${COMPOSE[@]}" exec -T db sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version;"')"
[[ -n "$revision" ]] || { echo "database migration revision is empty" >&2; exit 1; }
printf 'smoke OK: version=%s revision=%s url=%s\n' "$EXPECTED_VERSION" "$revision" "$base_url"
