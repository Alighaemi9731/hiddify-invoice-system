# Production deployment

Production stack: **Caddy** (automatic HTTPS) in front of the **React SPA** and the
**FastAPI backend**, with **PostgreSQL** and the **Telegram bot** — all in Docker.
One domain serves everything (`/api/*` → backend, the rest → SPA).

## Requirements
- A server running **Ubuntu 24.04 or 26.04** with a public IP.
- A **domain** (or subdomain) whose **A record points to the server's IP**.
- Ports **80** and **443** open to the internet (Caddy needs them for SSL).

## One-line install
On the server (as root), from the project folder:

```bash
sudo bash deploy/install.sh
```

It will:
1. detect the OS and install Docker (+ compose plugin) if missing,
2. ask for your **domain**, **SSL email**, and **admin password**,
3. generate a secure `.env` (random `SECRET_KEY` + DB password),
4. build and start the stack, and obtain the **HTTPS certificate automatically**.

Non-interactive:
```bash
DOMAIN=panel.example.com ACME_EMAIL=you@mail.com ADMIN_PASSWORD='choose-a-strong-one' \
  sudo -E bash deploy/install.sh
```

When it finishes, open `https://<your-domain>` and log in.

## After install
- **Settings tab** → set the Telegram **bot token**, **USDT wallet** (BEP-20),
  **BscScan API key**, exchange rate, and (optionally) the **master xpub**.
- **Panels tab** → add your real Hiddify panels (paste each admin link) and sync.
- Certificate **renewal is automatic** (Caddy).

## Operations
```bash
# from the project folder:
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f          # tail logs
docker compose --env-file .env -f deploy/docker-compose.prod.yml restart          # restart all
docker compose --env-file .env -f deploy/docker-compose.prod.yml down             # stop
git pull && docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build   # update
```

## Backups
Automatic backups (DB + settings) are sent to the owner's
Telegram PV every 2 hours, and can be downloaded/sent on demand from
**Account & Backup**. To restore: upload a backup zip there, or send it to the bot,
then restart both application processes so they reload the restored encryption key:

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml restart backend bot
```

Before relying on a backup, verify that the ZIP contains a non-empty PostgreSQL dump.
Treat every backup archive as highly sensitive: it carries the encryption material
needed for cross-server restore and is not currently password-protected.
The hardened backup/restore work is tracked in `docs/REMEDIATION_PLAN.md` B02.

## Release and deploy

The updater deploys the highest `v*` tag, not an arbitrary untagged `main` commit.
Use [`docs/RELEASE_PROCESS.md`](../docs/RELEASE_PROCESS.md) for the required test,
version, tag, GitHub release, backup, deploy, smoke-check, and rollback sequence.

## Isolated staging

`docker-compose.staging.yml` runs a separate PostgreSQL/backend/frontend stack on
`127.0.0.1:18080`. It has its own volumes, disables scheduler jobs, and omits the bot:

```bash
cp deploy/.env.staging.example .env.staging
# Replace every placeholder in .env.staging first.
docker compose -p invoice-staging --env-file .env.staging \
  -f deploy/docker-compose.staging.yml up -d --build
```

Stop it with the same command plus `down`; add `-v` only when its staging data should
be discarded.

## Files
- `docker-compose.prod.yml` — the production stack (db, backend, bot, frontend, caddy).
- `Caddyfile` — reverse-proxy + auto-TLS rules.
- `install.sh` — the one-line installer.

## Install on a fresh server from the PRIVATE GitHub repo (deploy key)

One-time setup of a read-only key on the server:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/invoice_deploy -N ''
cat ~/.ssh/invoice_deploy.pub
```
Add that public key in **GitHub → repo → Settings → Deploy keys → Add deploy key**
(leave *Allow write access* **unchecked**).

Then clone + install in one go:
```bash
sudo REPO=git@github.com:Alighaemi9731/hiddify-invoice-system.git \
     KEY=~/.ssh/invoice_deploy \
     DOMAIN=panel.example.com ACME_EMAIL=you@mail.com ADMIN_PASSWORD='choose-strong' \
     bash deploy/bootstrap.sh
```
This clones to `/opt/hiddify-invoice-system`, then runs `deploy/install.sh`
(Docker + secure `.env` + Caddy auto-HTTPS). Updating later: re-run `bootstrap.sh`.
