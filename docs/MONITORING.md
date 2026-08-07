# Monitoring & external health probe

## Layers

1. **In-app** — request-timing log lines (`app/core/timing.py`, `SLOW` warnings ≥1.5 s),
   error tracking (`app/core/errortrack.py`, surfaced in `/health` as `errors_24h` and
   the owner's daily Telegram digest), scheduler heartbeat (written to the settings
   table by the scheduler container; `/health` reports `scheduler: ok|stale` from every
   process), and the **storefront fleet heartbeat** (below).
2. **Container** — compose healthchecks on all six services; `restart: unless-stopped`
   self-heals crashed containers. A stale scheduler is deliberately NOT a container
   failure (restart isn't the remedy); it surfaces as `degraded` in `/health`.
3. **External (host-side)** — `hiddify-healthwatch.timer` (every 2 min) runs
   `deploy/healthwatch.sh`: probes `https://$SERVER_DOMAIN/health` from the host through
   the full public path (relay → Caddy → backend → DB), so it also validates DNS, TLS,
   and the ingress. Installed automatically by `deploy/install.sh`.

## Storefront fleet heartbeat

The bot container's watchdog (`app/bot/run.py`) proves only the **main** bot's session. Every
per-reseller storefront bot could be mute — `_poll_one` swallows `getUpdates` errors and retries
forever — while `getMe` on the main bot still succeeds, so the heartbeat file stayed fresh and the
container kept reporting healthy. That is the 2026-07-31 silent-outage shape one layer down.

So the fleet stamps its own liveness, mirroring the scheduler pattern:

- `app/bot/storefront/manager.py` refreshes an in-process stamp on **every** completed `getUpdates`
  by any bot (an empty long-poll counts — that is what reachability means) and on a successful
  `getMe` at runner start.
- `fleet_beacon` writes `storefront_fleet_last_heartbeat` into the settings table every 60 s, but
  **only while the fleet is provably live**: some bot reached Telegram within 5 min, *or* there are
  no runners at all (a shop-less install is healthy, not stale).
- `/health` reports `storefront_fleet: ok | stale | unknown` and counts `stale` toward `degraded`.
  `unknown` means never stamped — a just-upgraded install that has not run the new bot code yet —
  and deliberately does **not** alert.
- A bot stuck failing `getUpdates` logs one WARNING after 5 consecutive failures (then stays quiet
  until it recovers), so a permanent per-bot failure reaches `errors_24h` and the daily digest
  instead of vanishing into an unprinted debug line.

## healthwatch policy

- **Failure** = unreachable/TLS-invalid/non-200, `database != ok`, `scheduler != ok`, or
  `storefront_fleet == stale`.
- Alerts after **3 consecutive failures** (~6 min), re-alerts at most every **6 h**,
  sends one **recovery** notice. State in `/var/lib/hiddify-healthwatch/`.
- Log: `/var/log/hiddify-healthwatch.log` (one line per run incl. response ms;
  self-rotates at 1 MiB).
- Channel: Telegram Bot API using `TELEGRAM_BOT_TOKEN` from the app `.env` and the
  `owner_chat_id` settings row (read via `docker compose exec db psql` at alert time).
  If either is missing the probe still runs and logs `notify-skipped` — connect the
  channel by ensuring `TELEGRAM_BOT_TOKEN=` is present in `/opt/hiddify-invoice-system/.env`
  and the owner has `/start`-ed the bot once (which pins `owner_chat_id`).

## Runbook — alert received

1. `systemctl status hiddify-healthwatch.timer` and
   `tail -50 /var/log/hiddify-healthwatch.log` — what failed (`down` vs `degraded`) and
   since when.
2. `cd /opt/hiddify-invoice-system && docker compose --env-file .env -f deploy/docker-compose.prod.yml ps`
   — any container not `healthy`?
3. `curl -sS https://<domain>/health` from the host: `database` not ok → check `db`
   container/disk; `scheduler` stale → `docker compose logs --since=30m scheduler`.
4. Full-stack restart if needed:
   `docker compose --env-file .env -f deploy/docker-compose.prod.yml restart`.
5. Rollback path if a fresh deploy caused it: `sudo deploy/rollback.sh <prior-tag>`
   (see `docs/RELEASE_PROCESS.md` §8; migrations need `ALLOW_DOWNGRADE=1`).

## Manual probe

```bash
sudo bash /opt/hiddify-invoice-system/deploy/healthwatch.sh; echo "exit=$?"
```
