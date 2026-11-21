# Production deployment

Production stack: **Caddy** (automatic HTTPS) in front of the **React SPA** and the
**FastAPI backend**, with **PostgreSQL**, a dedicated **scheduler**, and the
**Telegram bot** — six services in Docker.
One domain serves everything (`/api/*` → backend, the rest → SPA).

The invoice application runs on the central server. The **panel add-on** has its own
installer for each existing Hiddify host; see [PANEL_ADDON.md](../docs/PANEL_ADDON.md).
The two installers are independent.

راهنمای فارسی مرحله‌به‌مرحله: [نصب و نگهداری](../docs/OPERATIONS_FA.md).

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

## Co-locating on a TLS-passthrough relay (shares 80/443 with an nginx SNI relay)

If the box already runs an nginx SNI relay (`deploy/relay.sh`) that owns ports 80
and 443 for other domains, Caddy cannot also bind them (`address already in use`).
Run the panel **behind** the relay instead — the relay forwards the panel's own
domain to a localhost-bound Caddy, and Caddy still gets its own Let's Encrypt cert
(TLS-ALPN-01 passes through SNI passthrough unchanged):

1. Keep `deploy/relay.sh`'s `MAP` current (your Hiddify panel origins) and run it.
   You never edit it for the invoice panel: its **default route** forwards any
   domain not in `MAP` to the co-located Caddy on `127.0.0.1:8443` / `127.0.0.1:8080`.
2. Point the panel's domain A record at the relay's IP.
3. Install the panel with the **same command as any other install** — the installer
   auto-detects the relay (a host nginx running) and binds Caddy to localhost ports:

   ```bash
   DOMAIN=panel.example.com ACME_EMAIL=you@mail.com \
     ADMIN_PASSWORD='choose-a-strong-one' sudo -E bash deploy/install.sh
   ```

The installer **auto-detects a co-located relay** (host `nginx` active) and switches to
localhost-port publishing, so no `BEHIND_RELAY` flag is needed and the command is the same
with or without a relay. Force it with `BEHIND_RELAY=1` / `BEHIND_RELAY=0`; override the
local ports with `CADDY_HTTP_PUBLISH` / `CADDY_HTTPS_PUBLISH`. Behind a relay a `DOMAIN` is
required (there is no bare-IP access) — either passed now or already in `.env`. **Run the
relay first** so Caddy's first ACME attempt can reach it.

## After install
- On a public HTTP/IP address, first enter the HTTPS activation token and a domain whose A record
  points here. Caddy enables HTTPS before the wizard asks for an owner username/password. On the
  HTTPS page use the separate account setup token. The HTTP token cannot create an owner account;
  account creation consumes both tokens once. Both are printed by the installer.
- A failed domain/certificate attempt does not create an owner or consume the token. Correct DNS
  before continuing. If a certificate is still issuing, wait before opening the HTTPS link.
- **Settings tab** → set the Telegram **bot token**, **USDT wallet** (BEP-20),
  **BscScan API key**, exchange rate, and (optionally) the **master xpub**.
- **Panels tab** → add your real Hiddify panels (paste each admin link) and sync.
- Certificate **renewal is automatic** (Caddy).

## Hiddify passwordless restoration

The default restore password is empty: an unsuspended admin opens its existing UUID link without
a password prompt. Hiddify v12's stock edit form ignores an empty `new_password`, so each Hiddify
host needs the provided owner-only extension. It changes only explicit clear requests for
non-super-admin accounts; the normal edit form and UUIDs/limits/users retain their behavior.

Use the separate, version-aware [one-line panel add-on installer](../docs/PANEL_ADDON.md).
It supports exactly 12.0.0 and 14.2.0, selects the active environment, backs up SQL and
changed files, migrates recognized old startup hooks, restarts only `hiddify-panel`,
and checks the loaded extension. Repeating an already-ready install does not restart.
The common boot hook also selects the right helper after a 12.0.0 → 14.2.0 upgrade.
Unknown versions/source stop the hook; validate future panel releases before upgrading.

```bash
sudo invoice-panel-addon --check      # read-only readiness after install/upgrade
sudo invoice-panel-addon --uninstall  # backup, remove, restart panel and verify
```

The underlying `hiddify-password-clear.py` and `hiddify-invoice-compat.py` files are
internal generation-specific helpers; copying them manually is no longer the normal
installation path. The invoice worker verifies passwordless login and records/retries
failed restores. A missing extension cannot be recorded as a successful clear.
Rolling the invoice app back does not require removing this extension. Before using
an older app that cannot clear passwords, set `enforcement_restore_password` to `123`
in Settings so that older worker can restore logins.

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
- `relay.sh` — optional nginx SNI **TLS-passthrough relay** template (panel origins +
  optional co-located invoice panel). Edit the map at the top before running.

## Optional source checkout from a private repository

The published release installer above is the normal installation/update path and does
not need Git, a GitHub account, or a deploy key for public releases. The legacy bootstrap
below requires an existing source checkout containing `deploy/bootstrap.sh`; it is not a
fresh-server download command and follows the repository rather than a verified release.

One-time setup of a read-only key on the server:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/invoice_deploy -N ''
cat ~/.ssh/invoice_deploy.pub
```
Add that public key in **GitHub → repo → Settings → Deploy keys → Add deploy key**
(leave *Allow write access* **unchecked**).

From that existing checkout:
```bash
sudo REPO=git@github.com:Alighaemi9731/hiddify-invoice-system.git \
     KEY=~/.ssh/invoice_deploy \
     DOMAIN=panel.example.com ACME_EMAIL=you@mail.com ADMIN_PASSWORD='choose-strong' \
     bash deploy/bootstrap.sh
```
This clones to `/opt/hiddify-invoice-system`, then runs `deploy/install.sh`
(Docker + secure `.env` + Caddy auto-HTTPS). Updating later: re-run `bootstrap.sh`.

## Hiddify 12/14 compatibility

The v12 form hook above remains required for v12. Hiddify 14.2.0 uses the separate,
source-pinned `hiddify-invoice-compat.py` extension. See
[panel compatibility](../docs/HIDDIFY_COMPATIBILITY.md) and the
[staged panel upgrade runbook](../docs/HIDDIFY_UPGRADE.md) before installing or upgrading.

## Issuance/registration upgrade

The issuance migration runs automatically on boot. After settings are seeded, a serialized,
resumable recovery fixes historical positive drafts with failed/unmatched/blocked delivery evidence.
It never rewrites amounts or first successful delivery timestamps. Recovered invoices and existing
unregistered main resellers receive a full current collection/enforcement cycle from this upgrade;
new main resellers receive 72 hours to register. The existing live/dry-run enforcement switch is
preserved. No production action is taken merely by checking out or building this code.

In Invoices, inspect «گزارش اصلاح فاکتورهای قدیمی». Drafts lacking clear failure evidence remain
in that report until explicitly issued, canceled, or discarded by the owner. Financial status,
Telegram status, collection deadlines, and reseller access appear separately. The API keeps the
legacy status string `sent` for client/backup compatibility; it means issued and unpaid.

Schema downgrade removes the new lifecycle fields. An older application cannot track collection
for an issued invoice that has never been delivered; use a forward fix, or restore the matching
pre-upgrade database backup together with old code when rolling back this feature. Do not invent
`sent_at` timestamps to make the old worker collect undelivered debt.
