# Production deployment

Production stack: **Caddy** (automatic HTTPS) in front of the **React SPA** and the
**FastAPI backend**, with **PostgreSQL** and the **Telegram bot** — all in Docker.
One domain serves everything (`/api/*` → backend, the rest → SPA).

## Requirements
- A server running **Ubuntu 24.04 or 26.04** with a public IP.
- A **domain** (or subdomain) whose **A record points to the server's IP**.
- Ports **80** and **443** open to the internet (Caddy needs them for SSL).

## Verified release install
Download the release bootstrap and checksum before granting root:

```bash
tmp="$(mktemp -d)" && cd "$tmp"
curl -fLO https://github.com/Alighaemi9731/hiddify-invoice-system/releases/latest/download/release-installer.sh
curl -fLO https://github.com/Alighaemi9731/hiddify-invoice-system/releases/latest/download/release-installer.sh.sha256
sha256sum -c release-installer.sh.sha256
sudo bash release-installer.sh
```

It will:
1. detect the OS and install Docker (+ compose plugin) if missing,
2. verify and apply one exact release archive,
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
sudo bash deploy/release-installer.sh                                               # verified update
sudo bash deploy/rollback.sh v1.37.46                                               # cached rollback
```

## Backups
Automatic backups (DB + settings) are sent to the owner's Telegram PV on a
configurable interval (`backup_interval_hours` in Settings; default every few
hours), and can be downloaded/sent on demand from **Account & Backup**. After a
successful restore the backend and bot **self-restart** to reload the restored
encryption key; if you restore by hand, restart them yourself:

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml restart backend bot
```

Restore is atomic (`psql --single-transaction`, with a pre-restore safety dump), and
the backend refuses to produce a backup that has no usable database image.

Treat every backup archive as highly sensitive — it carries the encryption material
needed for a cross-server restore. **Optional passphrase encryption is available**: set
`backup_passphrase` in Settings and the archive is encrypted (PBKDF2 → Fernet) before it
leaves the server. It is off by default; enable it if backups are stored or forwarded
anywhere outside the owner's own Telegram chat.

## Release and deploy

The updater resolves the latest GitHub Release once, downloads that exact tarball and
SHA-256 file, verifies it, then applies it. `deploy/smoke.sh` checks containers, the
database-aware health endpoint, API version, and migration revision. Verified archives
are retained under `update/releases` for offline rollback.

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
