# Architecture

Hiddify Reseller Management & Invoicing System — Phase 1 (localhost MVP).

## 1. Overview

The system automates what was previously a manual monthly process: read every Hiddify
panel, work out what each reseller sold, bill them, deliver the invoice on Telegram,
take payment in USDT (BEP-20), and chase/suspend non-payers.

Design principles:

- **One source of usage truth:** each panel's backup JSON (read), snapshotted into Postgres.
- **Write only where needed:** enforcement (disable users / zero limits) uses the Hiddify Admin REST API; everything else is read-only.
- **Bootstrap vs runtime config:** `.env` only bootstraps; the owner edits everything else from the web panel (stored in DB, secrets encrypted).
- **Phase-2-ready:** modular services, a `PanelClient` interface, and a scheduler that can move to systemd later — no rewrite.

## 2. Components & data flow

```mermaid
flowchart LR
    subgraph Panels["Hiddify panels (~10)"]
        P1[(Panel 1)]
        P2[(Panel N)]
    end

    subgraph Backend["Backend (FastAPI + APScheduler)"]
        SYNC[Sync service<br/>PanelClient: backup-JSON adapter]
        DB[(PostgreSQL<br/>snapshots, invoices,<br/>payments, settings)]
        ENGINE[Invoice engine<br/>+ pricing Toman→USDT]
        PDF[PDF builder<br/>Persian / RTL]
        DUN[Dunning + enforcement<br/>PanelClient: admin-API adapter]
        PAY[Payment verifier<br/>BscScan / BEP-20]
        API[REST API + JWT auth]
        SCHED[Scheduler<br/>monthly + daily jobs]
    end

    subgraph Clients
        WEB[Owner web panel<br/>React SPA · RTL · Persian]
        BOT[Telegram bot<br/>aiogram v3]
    end

    RESELLER([Reseller])
    CHAIN[(BSC chain<br/>via BscScan API)]
    CHANNEL[[Announcement channel]]

    P1 -- backup JSON --> SYNC
    P2 -- backup JSON --> SYNC
    SYNC --> DB
    SCHED --> SYNC
    SCHED --> ENGINE
    SCHED --> DUN
    ENGINE --> DB
    ENGINE --> PDF
    DB --> API
    API --> WEB
    ENGINE -- invoice --> BOT
    DUN -- reminders/warnings --> BOT
    DUN -- disable users / zero limits --> P1
    BOT <--> RESELLER
    BOT -- membership check --> CHANNEL
    RESELLER -- TXID --> BOT
    BOT --> PAY
    PAY -- verify --> CHAIN
    PAY -- mark paid + auto-restore --> DUN
```

## 3. Monthly invoicing sequence

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant SY as Sync
    participant E as Invoice engine
    participant B as Bot
    participant R as Reseller
    participant V as Payment verifier
    participant P as Hiddify panel

    S->>SY: sync all panels (start of month)
    SY->>P: GET backup JSON
    P-->>SY: admins + users
    SY->>SY: upsert resellers + end_user_snapshots
    S->>E: generate invoices for previous month
    E->>E: bundle sub-resellers; Σ usage_limit_GB of services created in month (skip 1GB); ×price; →USDT
    E->>B: send invoice
    B->>R: invoice text (+ wallet address)
    Note over S,R: if unpaid → D+2 reminder, D+4 reminder, D+5 warning + enforcement (dry-run unless enabled)
    R->>B: submit TXID
    B->>V: verify on-chain (dest, amount, confirmations)
    V-->>B: confirmed
    V->>P: (if enforced) re-enable users + restore limits
    B->>R: payment confirmed
```

## 4. Reseller ↔ Telegram matching

The bot gates on **announcement-channel membership** (`getChatMember`), then asks the
reseller to paste their panel link, e.g.
`https://<host>/<path>/<uuid>/#<tag>`. The system parses **host + path + uuid**
(rest ignored): `path` identifies the panel, `uuid` matches `resellers.admin_uuid`,
`#tag` is stored as `link_tag`. On match we bind `bot_chat_id` so invoices/reminders
reach that reseller. `panel_telegram_id` (when the panel has it) is a secondary auto-match.

## 5. PanelClient interface

```mermaid
classDiagram
    class PanelClient {
        <<interface>>
        +fetch_backup(panel) PanelData
        +set_user_enabled(panel, user_uuid, enabled)
        +set_admin_limits(panel, admin_uuid, max_users, max_active_users)
    }
    class BackupJsonClient {
        +fetch_backup(panel) PanelData
    }
    class AdminApiClient {
        +set_user_enabled(...)
        +set_admin_limits(...)
    }
    PanelClient <|.. BackupJsonClient
    PanelClient <|.. AdminApiClient
```

Read path = `BackupJsonClient` (the `/admin/backup/backupfile/` endpoint). Write path =
`AdminApiClient` (Hiddify Admin REST API, needs the per-panel admin API key). Enforcement
uses the write path; a future REST read adapter and HD-wallet payment monitor slot in here.

## 6. Deployment (Phase 1)

`docker compose` runs four services: `db` (Postgres), `backend` (API + scheduler),
`bot` (aiogram polling), `frontend` (nginx serving the built SPA). The bot and backend
share the same code image and DB; the backend's scheduler instantiates a Telegram `Bot`
only to *send* invoices/reminders (the `bot` service owns polling). Phase 2 swaps compose
for a one-line installer + systemd units + Caddy for domain/TLS.
