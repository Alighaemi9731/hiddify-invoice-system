"""
Runtime settings: the values the owner edits from the web panel, stored in the
DB `settings` table. Bootstrap (.env) values seed the initial row, after which
the DB is the source of truth.

Secret settings are encrypted at rest (see app.core.crypto) and masked by the API.

Message templates use Python `str.format` placeholders, e.g. {name}, {amount_usdt}.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, proccache
from app.core.config import settings as boot
from app.models.setting import Setting


@dataclass(frozen=True)
class SettingDef:
    key: str
    default: Any
    is_secret: bool = False
    group: str = "general"
    label: str = ""


# Persian-facing default message templates.
_TPL_WELCOME = (
    "👋 {name} عزیز، سلام!\n"
    "به سامانهٔ مدیریت فاکتورها و پرداخت‌ها خوش آمدید؛ همراه شما هستیم."
)
_TPL_MEMBERSHIP = (
    "برای استفاده از ربات، لطفاً ابتدا در کانال/گروه ما عضو شوید.\n"
    "سپس دکمهٔ «بررسی عضویت» را بزنید."
)
_TPL_MENU = "لطفاً از منوی زیر یک گزینه را انتخاب کنید:"
_TPL_LINK_MATCHED = "✅ لینک شما با موفقیت ثبت شد؛ شما به عنوان «{name}» شناسایی شدید."
_TPL_LINK_NOT_FOUND = (
    "❌ متأسفانه این لینک به هیچ نماینده‌ای متصل نشد.\n"
    "لطفاً لینکِ کامل پنل خود را ارسال کنید."
)
# Previous defaults — kept ONLY so seed_defaults can migrate an un-customized install to the
# current template. The current invoice text no longer embeds the wallet/card/amount; it shows a
# CTA pointing to the «💳 پرداخت فاکتور» button (so a stale TON amount is never printed and the
# payment details aren't duplicated — those live on the button path).
_TPL_INVOICE_LEGACY = (  # the «معادل USDT» form
    "🧾 فاکتور دوره {period}\n"
    "نماینده: {name}\n"
    "مجموع مصرف: {usage_gb} گیگابایت\n"
    "مبلغ: {amount_toman} تومان\n"
    "معادل: {amount_usdt} USDT\n\n"
    "{payment_instructions}"
)
_TPL_INVOICE_PAYINSTR = (  # the previous minimal default that embedded payment instructions
    "🧾 فاکتور دوره {period}\n"
    "👤 نماینده: {name}\n"
    "📊 مصرف این دوره: {usage_gb} گیگابایت\n"
    "💰 مبلغ قابل پرداخت: {amount_toman} تومان\n\n"
    "{payment_instructions}"
)
_TPL_INVOICE = (
    "🧾 فاکتور دورهٔ {period}\n"
    "👤 نماینده: {name}\n"
    "📊 مجموع مصرف این دوره: {usage_gb} گیگابایت\n"
    "💰 مبلغ قابل پرداخت: {amount_toman} تومان\n\n"
    "{pay_cta}"
)
_TPL_REMINDER1 = (
    "⏰ یادآوری: فاکتور دورهٔ {period} شما هنوز پرداخت نشده است.\n"
    "مبلغ قابل پرداخت: {amount_toman} تومان"
)
_TPL_REMINDER2 = (
    "⏰ یادآوری دوم: لطفاً نسبت به پرداخت فاکتور دورهٔ {period} اقدام فرمایید.\n"
    "مبلغ قابل پرداخت: {amount_toman} تومان"
)
_TPL_WARNING = (
    "⚠️ اخطار نهایی\n"
    "فاکتور دورهٔ {period} شما همچنان پرداخت نشده است (مبلغ {amount_toman} تومان).\n"
    "در صورت عدم پرداخت، تمام کاربران شما و زیرمجموعه‌هایتان غیرفعال می‌شوند، "
    "سقف کاربران شما صفر خواهد شد و امکان ویرایش در پنل را نخواهید داشت."
)
_TPL_PAYMENT_RECEIVED = (
    "✅ پرداخت شما با موفقیت تأیید شد. سپاس از همراهی شما!\n"
    "فاکتور دورهٔ {period} تسویه شد."
)
_TPL_PAYMENT_REJECTED = (
    "❌ متأسفانه پرداخت شما برای فاکتور دورهٔ {period} تأیید نشد.\n"
    "لطفاً از صحت مبلغ و شناسهٔ تراکنش/رسید اطمینان حاصل کنید و دوباره ارسال نمایید، "
    "یا برای پیگیری با پشتیبانی در تماس باشید."
)

# Sent by a SHOP's own bot to that shop's customers when its admin re-arms the free trial.
# Placeholders: {shop} (bot @username or shop name), {gb}, {days}. Deliberately free of any
# expiry claim or countdown — the trial stays available until the shop admin turns it off, and a
# promise we don't enforce would be a lie the bot repeats every month.
_TPL_SF_TRIAL_RESET = (
    "🎁 خبر خوب!\n"
    "تست رایگان {shop} دوباره برای شما فعال شد.\n\n"
    "می‌توانید یک سرویس آزمایشی {gb} گیگابایتی {days} روزه دریافت کنید و کیفیت اتصال را "
    "بدون هیچ هزینه‌ای بسنجید.\n"
    "برای دریافت، کافی است دکمهٔ «🎁 تست رایگان» را در منوی ربات بزنید."
)


DEFS: list[SettingDef] = [
    # Telegram
    SettingDef("telegram_bot_token", boot.telegram_bot_token, True, "telegram"),
    SettingDef("announcement_channel_id", boot.announcement_channel_id, False, "telegram"),
    SettingDef("announcement_channel_link", boot.announcement_channel_link, False, "telegram"),
    # Forced-membership gate for the channel: when on, a non-owner must be a member to
    # use the bot. Default ON if a channel is configured (matches old behaviour).
    SettingDef("channel_membership_required", True, False, "telegram"),
    # Forced-membership gate for a Telegram GROUP (may be private). Independent toggle;
    # when both are on, the user must be in BOTH (channel AND group).
    SettingDef("announcement_group_id", "", False, "telegram"),
    SettingDef("announcement_group_link", "", False, "telegram"),
    SettingDef("group_membership_required", False, False, "telegram"),
    # Guard: kick people who started the bot but are NOT registered resellers from the
    # channel AND the group. ON by default (the production install runs it live); set to
    # False for dry-run reports only.
    SettingDef("channel_kick_enabled", True, False, "telegram"),
    # Grace period (MINUTES) before the guard removes a NON-registered bot user. Default 15:
    # short on purpose — the guard runs every 10 min, so a newcomer who joins right at a
    # check tick is skipped that round and removed on the NEXT one (the "second 10-minute
    # cycle"), not instantly. Counted from when they first started the bot (BotUser.created_at).
    SettingDef("kick_grace_minutes", 15, False, "telegram"),
    SettingDef("one_time_invite_links", True, False, "telegram"),
    # Payments
    SettingDef("usdt_bep20_address", boot.usdt_bep20_address, False, "payments"),
    SettingDef("usdt_bep20_contract", boot.usdt_bep20_contract, False, "payments"),
    SettingDef("bscscan_api_key", boot.bscscan_api_key, True, "payments"),
    SettingDef("bscscan_api_url", "https://api.bscscan.com/api", False, "payments"),
    # Public BSC JSON-RPC node used for the (free, key-less) USDT on-chain deposit read — reads
    # the tx receipt's ERC-20 Transfer logs. No API key needed; swap for another public node if
    # this one rate-limits.
    SettingDef("bsc_rpc_url", "https://bsc-rpc.publicnode.com", False, "payments"),
    SettingDef("usdt_master_xpub", boot.usdt_master_xpub, True, "payments"),
    SettingDef("min_confirmations", 12, False, "payments"),
    SettingDef("payment_amount_tolerance_usdt", 0.5, False, "payments"),
    # Payment methods shown to resellers (on the invoice + the bot «پرداخت») — each can be
    # toggled on/off. USDT-by-TXID on by default (matches the existing behaviour).
    SettingDef("pay_usdt_enabled", True, False, "payments"),       # USDT wallet + TXID
    SettingDef("pay_screenshot_enabled", True, False, "payments"),  # pay-by-deposit-photo
    SettingDef("pay_card_enabled", False, False, "payments"),       # card-to-card transfer
    SettingDef("card_number", "", False, "payments"),               # the destination card
    SettingDef("card_holder_name", "", False, "payments"),          # name on the card
    SettingDef("pay_ton_enabled", False, False, "payments"),        # TON (Toncoin) transfer
    SettingDef("ton_wallet_address", "", False, "payments"),        # the destination TON wallet
    SettingDef("ton_toman_auto", 0, False, "payments"),             # last live TON→Toman (status)
    # Optional toncenter API key (higher rate limit) for the manual TON-deposit check; works
    # without one. And the tolerance for matching the on-chain deposit to the invoice amount.
    SettingDef("toncenter_api_key", "", True, "payments"),
    SettingDef("ton_amount_tolerance_pct", 5, False, "payments"),
    # AVAX (Avalanche C-Chain) transfer — manual confirm + clickable Snowtrace link.
    SettingDef("pay_avax_enabled", False, False, "payments"),       # AVAX C-Chain transfer
    SettingDef("avax_address", "", False, "payments"),              # the destination AVAX address
    SettingDef("avax_toman_auto", 0, False, "payments"),            # last derived AVAX→Toman (status)
    # Public Avalanche C-Chain JSON-RPC node for the (free, key-less) on-chain deposit read — reads the
    # native AVAX transfer's value/recipient. No API key needed; swap if it rate-limits.
    SettingDef("avalanche_rpc_url", "https://api.avax.network/ext/bc/C/rpc", False, "payments"),
    SettingDef("avax_amount_tolerance_pct", 5, False, "payments"),  # match tolerance for the AVAX read
    # Auto-confirm a submitted TXID when the on-chain read matches the invoice EXACTLY (same
    # tolerances the owner already sees in the review message). Anything else — different amount,
    # too few confirmations, an older deposit, an unreadable chain — stays pending for manual
    # review, exactly as before. Screenshot/card proofs are never auto-confirmed.
    SettingDef("payment_auto_confirm_enabled", True, False, "payments"),
    # Freshness guard: only a deposit that landed within this many hours may auto-confirm. Our
    # wallet addresses are public, so without it a reseller could claim the TXID of some OLD
    # never-submitted deposit and settle their own debt with it.
    SettingDef("payment_auto_confirm_max_age_hours", 24, False, "payments"),
    # Pricing
    SettingDef("default_price_per_gb", boot.default_price_per_gb_toman, False, "pricing"),
    SettingDef("toman_per_usdt", boot.toman_per_usdt, False, "pricing"),  # manual rate / fallback
    SettingDef("rate_mode", "auto", False, "pricing"),  # manual | auto (live)
    # Where the live USDT (and TON) rate is read from: wallex | tetherland. TON exists only on
    # Wallex, so TON always uses Wallex; this picks the USDT source (the other is the fallback).
    SettingDef("rate_source", "wallex", False, "pricing"),
    # TON (Toncoin) rate, mirroring the USDT manual/auto split.
    SettingDef("ton_rate_mode", "auto", False, "pricing"),   # manual | auto (live from Wallex)
    SettingDef("ton_toman_manual", 0, False, "pricing"),     # manual TON→Toman rate / fallback
    # AVAX (Avalanche) rate, mirroring the TON manual/auto split. Auto is DERIVED:
    # AVAX→USD (CoinGecko) × the live USDT→Toman rate (no Iranian AVAX market exists).
    SettingDef("avax_rate_mode", "auto", False, "pricing"),  # manual | auto (live, derived)
    SettingDef("avax_toman_manual", 0, False, "pricing"),    # manual AVAX→Toman rate / fallback
    # Last live USDT→Toman rate fetched from Tetherland/Wallex + when (read-only status, auto-updated).
    SettingDef("toman_per_usdt_auto", 0, False, "pricing"),
    SettingDef("toman_per_usdt_auto_at", "", False, "pricing"),
    # In auto mode, a cached live rate older than this many hours is treated as stale and billing
    # falls back to the manual rate (0 disables the staleness check). Default 48h.
    SettingDef("rate_max_age_hours", 48, False, "pricing"),
    # Skip billing a panel whose last successful sync is older than this many hours (0 = no age
    # cap). Stale snapshots would invoice last month's reality (F9).
    SettingDef("billing_max_snapshot_age_hours", 26, False, "pricing"),
    SettingDef("rate_refresh_hours", 1, False, "schedule"),  # how often to refresh the live rate
    SettingDef("excluded_usage_gb", [1], False, "pricing"),  # extra exact sizes to skip
    # Flag end-users created with at least this sold quota (GB) as "high volume" — the default
    # Hiddify create size is 1000 GB, often left by mistake and over-billing the reseller.
    SettingDef("high_volume_gb_threshold", 1000, False, "pricing"),
    # Any config whose quota is <= this many GB is a free test config and is NOT
    # billed (e.g. 1 → both 0.5 GB and 1 GB are free; 1.5+ GB is billed).
    SettingDef("free_under_gb", 1, False, "pricing"),
    # Minimum-sale floor: an invoice below this is raised to it (0 = no floor). Per-reseller
    # override on the Reseller row wins.
    SettingDef("min_sale_toman", 300_000, False, "pricing"),
    # Default monthly fee billed to a reseller running a storefront bot (per-reseller override on
    # the Reseller row wins; 0 = free).
    SettingDef("storefront_monthly_fee_toman", 0, False, "pricing"),
    # A storefront's FREE TRIAL is excluded from the reseller's invoice entirely (see
    # `storefront.trial_user_uuids` → `invoice_engine`), so its quota is paid for by YOU, not by
    # the reseller — and the shop admin picks the size. These two keys are the only thing bounding
    # that giveaway:
    #   * `storefront_trial_max_gb` caps what a shop may set as `free_trial_gb`, and also clamps
    #     the size actually provisioned, so a shop configured above the cap before it existed
    #     stops costing more without needing a remediation sweep.
    #   * `storefront_trial_reset_enabled` is the master switch for letting shop admins re-arm
    #     every customer's trial once a month. OFF makes the trial lifetime-once again.
    SettingDef("storefront_trial_max_gb", 1, False, "pricing"),
    SettingDef("storefront_trial_reset_enabled", True, False, "pricing"),
    # Abuse-resistant metering (billing model "C"): bill usage beyond the paid quota
    # (daily-reset trick) + renew-by-edit that skips start_date. On by default.
    SettingDef("metering_enabled", True, False, "pricing"),
    # Overage THRESHOLD: a user whose overage is at/below this many GB is treated as unavoidable
    # xray "soft-cutoff" slack (consuming a bit for a couple of minutes after the quota is hit)
    # and billed NOTHING; above it, the FULL overage is billed as real over-consumption. Real
    # reset-abuse is many GB, so it's always billed.
    SettingDef("overage_tolerance_gb", 3.0, False, "pricing"),
    # A user DELETED from the panel that consumed >= this many GB is billed its full SOLD quota
    # (not just consumption) — so a reseller can't delete a config to be charged only the used
    # part. Below it, the deleted config is billed on consumption. 0 disables. Default 5 GB.
    SettingDef("deleted_full_quota_over_gb", 5, False, "pricing"),
    # Scheduler timings — ALL automated jobs. Every value here actually drives the
    # APScheduler triggers (see app.scheduler.jobs.load_config). Hour/day values fire at
    # that fixed clock time in the owner's timezone (Asia/Tehran); repeating values use
    # deterministic interval anchors and therefore keep true N-hour/N-minute spacing.
    SettingDef("invoice_day_of_month", 1, False, "schedule"),   # monthly invoice: day (1=run on the 1st for prev month)
    SettingDef("invoice_hour", 9, False, "schedule"),            # monthly invoice: hour
    SettingDef("dunning_hour", 10, False, "schedule"),           # daily reminders/enforcement: hour
    SettingDef("sync_interval_hours", 1, False, "schedule"),     # panel sync: every N hours
    SettingDef("guard_interval_minutes", 10, False, "schedule"), # channel/group guard: every N minutes
    SettingDef("backup_enabled", True, False, "schedule"),       # auto-backup on/off
    SettingDef("backup_interval_hours", 2, False, "schedule"),   # auto-backup: every N hours
    # Log retention: the daily maintenance job deletes sync_runs, delivery_log, and
    # terminal enforcement_actions older than this many days (keeps the DB lean). Live
    # rows — an owed invoice's reminder logs, in-flight enforcement queue work — are never
    # pruned. Default 60 days; minimum 7. Does NOT touch the financial ledger or invoices.
    SettingDef("log_retention_days", 60, False, "schedule"),
    # Owner-side disk/PII hygiene (daily): delete payment-proof screenshots of terminal payments +
    # cached invoice PDFs + tire-kicker bot_users older than this. The DB rows (money facts) stay;
    # only files/aged non-registered bot users go. 0 = off. Default 180.
    SettingDef("owner_data_retention_days", 180, False, "schedule"),
    # usage_meters older than this many months are pruned by the daily maintenance (old periods are
    # already locked into invoices + the financial ledger). Keeps the metering table bounded. 0 = off.
    SettingDef("meter_retention_months", 6, False, "schedule"),
    # Storefront pending-order reaper cadence (minutes): how often to reconcile purchases orphaned by a
    # mid-buy crash (complete the ones whose config exists on the panel, refund the rest).
    SettingDef("storefront_pending_order_reaper_minutes", 15, False, "schedule"),
    # F5/F11: how long a storefront purchase/renewal holds a durable provisioning LEASE on its order
    # while the panel call is in flight. The reaper/reconciler ignores an order whose lease is still
    # valid, so a live provision is never reaped mid-flight. A renewal performs GET+PATCH (each may use
    # the 90s panel timeout), therefore the validated minimum is five minutes including safety margin.
    SettingDef("storefront_operation_lease_seconds", 300, False, "schedule"),
    # Minimum seconds between an owner's explicit "live refresh" of one storefront order's panel
    # status (plan 004). Cross-process throttle via storefront_orders.live_refreshed_at; a repeat
    # inside the window returns 429. Keeps list/detail off the panel and rate-limits manual reads.
    SettingDef("storefront_live_refresh_seconds", 30, False, "schedule"),
    # Plan 006 durable delivery: how often the storefront broadcast/direct-message worker runs (minutes),
    # and how long a delivered job's per-recipient DETAIL rows are kept before the daily maintenance
    # prunes them (job aggregate totals + audit are kept forever; 0 = never prune detail).
    SettingDef("storefront_delivery_worker_interval_minutes", 1, False, "schedule"),
    SettingDef("storefront_delivery_retention_days", 90, False, "schedule"),
    # Storefront retention: the daily maintenance purges customers with NO financial footprint (zero
    # balance, no provisioned/disabled order, no confirmed top-up) inactive for this many days, plus
    # failed/deleted orders + rejected top-ups. Confirmed top-ups + provisioned services are kept. 0 = off.
    SettingDef("storefront_stale_customer_days", 90, False, "schedule"),
    # Near-expiry reminder for storefront customers: alert when a provisioned config is this
    # many days (or fewer) from expiring, once per service period, with a renew button. 0 = off.
    SettingDef("storefront_expiry_notify_days", 3, False, "schedule"),
    SettingDef("storefront_expired_notify_days", 7, False, "schedule"),
    # «تستت تمام شد» nudge: only trials that ended within this many days are contacted, so the
    # first sweep after a deploy can't message every trial the shop has ever issued. 0 = off.
    SettingDef("storefront_trial_ended_notify_days", 7, False, "schedule"),
    # Warn a paid customer at this percentage of their volume. Was hardcoded at 80.
    SettingDef("storefront_usage_alert_percent", 80, False, "schedule"),
    # Deferred one-shot auto-renew: FIRE margins + sweep cadence. A config armed by its customer is
    # auto-renewed once it has less than `fire_gb` GB left OR fewer than `fire_days` days left. The
    # sweep that checks this runs every `interval_minutes`.
    SettingDef("storefront_autorenew_fire_gb", 1, False, "schedule"),
    SettingDef("storefront_autorenew_fire_days", 1, False, "schedule"),
    SettingDef("storefront_autorenew_interval_minutes", 15, False, "schedule"),
    # Max simultaneous PENDING top-ups one customer may have (anti-spam on the admin review queue).
    SettingDef("storefront_max_pending_topups", 3, False, "schedule"),
    # Daily owner digest to the owner's Telegram PV (KPIs + health). On by default at 09:00.
    SettingDef("daily_digest_enabled", True, False, "schedule"),
    SettingDef("daily_digest_hour", 9, False, "schedule"),
    # Optional passphrase: when set, every backup archive is encrypted (PBKDF2→Fernet) and
    # restore requires the same passphrase. Keep it somewhere safe OUTSIDE the system.
    SettingDef("backup_passphrase", "", True, "schedule"),
    # Dunning / enforcement
    # Days after the invoice was sent (or after a granted payment deadline). The production
    # cadence: reminders on D+3 / D+5, hard warning («سررسید گذشته») on D+10, suspension D+30.
    SettingDef("reminder1_day", 3, False, "dunning"),
    SettingDef("reminder2_day", 5, False, "dunning"),
    SettingDef("warning_day", 10, False, "dunning"),
    SettingDef("enforcement_day", 30, False, "dunning"),
    SettingDef("enforcement_enabled", True, False, "dunning"),  # False = dry-run
    # Live enforcement is queued and chunked. The dunning job only plans work; this worker
    # processes a small, resumable slice at a time so large panels never block a scheduler tick.
    SettingDef("enforcement_worker_interval_minutes", 5, False, "dunning"),
    # Actions processed PER PANEL per worker tick (panels run in parallel — see below).
    SettingDef("enforcement_action_batch_limit", 3, False, "dunning"),
    SettingDef("enforcement_user_chunk_size", 500, False, "dunning"),
    SettingDef("enforcement_admin_chunk_size", 10, False, "dunning"),
    # How many panels are processed concurrently per tick. Panels are independent, so cross-panel
    # suspensions/restores run in parallel; each panel stays sequential (never hammered).
    SettingDef("enforcement_panel_concurrency", 20, False, "dunning"),
    SettingDef("auto_restore_on_payment", True, False, "dunning"),
    # Admin panel-login lockout on full suspension. Hiddify's UUID-link login is refused once an
    # admin has a non-empty password, so on a FULL suspend we also set every suspended admin's
    # (subtree) Flask-Admin password to `enforcement_lock_password` — this kills their existing
    # UUID link so they cannot log in and bulk-re-enable their own users. On restore/payment the
    # password is set back to `enforcement_restore_password`. ON by default (the production install
    # runs it); NOT applied on freeze (freeze intentionally keeps existing users online, so
    # login-lockout isn't wanted there).
    SettingDef("enforcement_admin_lockout_enabled", True, False, "dunning"),
    SettingDef("enforcement_lock_password", "blocked-node", False, "dunning"),
    SettingDef("enforcement_restore_password", "123", False, "dunning"),
    # A pending (under-review) payment pauses dunning on ITS invoice for at most this many days,
    # so a stale, never-reviewed proof can't shield a debt forever. Default 7.
    SettingDef("pending_payment_hold_days", 7, False, "dunning"),
    # Owner
    SettingDef("owner_name", "", False, "general"),
    SettingDef("owner_telegram", "", False, "general"),
    SettingDef("owner_chat_id", "", False, "general"),
    # First-run setup wizard state (locked once the owner completes setup).
    SettingDef("setup_done", False, False, "general"),
    # Timestamp (ISO, UTC) of the last SUCCESSFUL backup — read by the health report's
    # «آخرین پشتیبان». Internal/read-only; written by backup.mark_backup_done.
    SettingDef("last_backup_at", "", False, "general"),
    # Internal/read-only observability stamps (I01): the scheduler liveness heartbeat
    # (written every ~2 min by `scheduler_heartbeat_job`, read by /health) and the cursor
    # for the daily digest's "new tracked errors" section.
    SettingDef("scheduler_last_heartbeat", "", False, "general"),
    SettingDef("error_digest_last_ts", "", False, "general"),
    # The storefront fleet's own liveness stamp (written ~every 60s by the bot process while some
    # storefront bot provably reaches Telegram). The scheduler heartbeat above says nothing about
    # the ~151 shop bots, and neither does the main bot's watchdog.
    SettingDef("storefront_fleet_last_heartbeat", "", False, "general"),
    # Deployment (Phase 2): domain + automatic HTTPS, applied by the installer.
    SettingDef("server_domain", "", False, "deploy"),
    SettingDef("https_enabled", False, False, "deploy"),
    SettingDef("acme_email", "", False, "deploy"),
    # Bot user-creation: top-level resellers create end-users from the bot. The owner curates the
    # bounded option lists; an empty/zero value uses the default. Master toggle gates the feature.
    SettingDef("user_create_enabled", True, False, "usercreate"),
    SettingDef("user_create_gb_options", [20, 30, 40, 50, 80, 100], False, "usercreate"),
    SettingDef("user_create_day_options", [30, 60], False, "usercreate"),
    SettingDef("user_create_bulk_counts", [5, 10, 20], False, "usercreate"),
    # Reseller follow-up board («پیگیری») — the owner's churn thresholds. Panel-only: nothing
    # here sends a message or changes billing; they only decide which bucket a reseller lands
    # in on the board. "Days" are counted from the reseller's LAST BILLABLE SALE.
    SettingDef("crm_dormant_days", 14, False, "crm"),
    SettingDef("crm_churned_days", 45, False, "crm"),
    # A brand-new admin has legitimately sold nothing yet; below this age they are not called
    # "never activated". Above `crm_onboarding_days` they stop being excused as newcomers.
    SettingDef("crm_never_active_min_age_days", 14, False, "crm"),
    SettingDef("crm_onboarding_days", 30, False, "crm"),
    # Month-to-date volume is projected to a full month, then compared with the mean of the
    # previous 3 months: under `declining` percent = shrinking, over `growing` = expanding.
    SettingDef("crm_declining_pct", 50, False, "crm"),
    SettingDef("crm_growing_pct", 125, False, "crm"),
    SettingDef("crm_snooze_default_days", 15, False, "crm"),
    # Message templates
    SettingDef("tpl_welcome", _TPL_WELCOME, False, "templates"),
    SettingDef("tpl_membership", _TPL_MEMBERSHIP, False, "templates"),
    SettingDef("tpl_menu", _TPL_MENU, False, "templates"),
    SettingDef("tpl_link_matched", _TPL_LINK_MATCHED, False, "templates"),
    SettingDef("tpl_link_not_found", _TPL_LINK_NOT_FOUND, False, "templates"),
    SettingDef("tpl_invoice", _TPL_INVOICE, False, "templates"),
    SettingDef("tpl_reminder1", _TPL_REMINDER1, False, "templates"),
    SettingDef("tpl_reminder2", _TPL_REMINDER2, False, "templates"),
    SettingDef("tpl_warning", _TPL_WARNING, False, "templates"),
    SettingDef("tpl_payment_received", _TPL_PAYMENT_RECEIVED, False, "templates"),
    SettingDef("tpl_payment_rejected", _TPL_PAYMENT_REJECTED, False, "templates"),
    SettingDef("tpl_storefront_trial_reset", _TPL_SF_TRIAL_RESET, False, "templates"),
]

_DEF_BY_KEY = {d.key: d for d in DEFS}
_API_READ_ONLY = {
    "owner_chat_id", "setup_done", "toman_per_usdt_auto", "toman_per_usdt_auto_at",
    "ton_toman_auto", "avax_toman_auto", "last_backup_at",
    "scheduler_last_heartbeat", "error_digest_last_ts", "storefront_fleet_last_heartbeat",
}
# Bounded positive-integer option lists (deduped + sorted) — the bot user-creation menus.
_POSITIVE_INT_LISTS = {
    "user_create_gb_options",
    "user_create_day_options",
    "user_create_bulk_counts",
}
_INT_RANGES: dict[str, tuple[int, int | None]] = {
    "default_price_per_gb": (0, None),
    "toman_per_usdt": (0, None),
    "rate_max_age_hours": (0, 24 * 365),
    "billing_max_snapshot_age_hours": (0, 24 * 365),
    "rate_refresh_hours": (1, 24),
    "min_sale_toman": (0, None),
    "ton_toman_manual": (0, None),
    "avax_toman_manual": (0, None),
    "high_volume_gb_threshold": (1, None),
    "invoice_day_of_month": (1, 28),
    "invoice_hour": (0, 23),
    "dunning_hour": (0, 23),
    "sync_interval_hours": (1, 24),
    "guard_interval_minutes": (1, 60),
    "backup_interval_hours": (1, 24),
    "log_retention_days": (7, 3650),
    "meter_retention_months": (0, 120),
    "storefront_pending_order_reaper_minutes": (1, 1440),
    "storefront_operation_lease_seconds": (300, 3600),
    "storefront_live_refresh_seconds": (5, 3600),
    "storefront_delivery_worker_interval_minutes": (1, 60),
    "storefront_delivery_retention_days": (0, 3650),
    "storefront_stale_customer_days": (0, 3650),
    "owner_data_retention_days": (0, 3650),
    "storefront_expiry_notify_days": (0, 60),
    "storefront_expired_notify_days": (0, 60),
    "storefront_trial_ended_notify_days": (0, 60),
    # Lower bound 1, NOT 0: `_INT_RANGES.get(key, (0, None))` would silently admit 0, and a 0%
    # threshold would warn every customer on every sweep.
    "storefront_usage_alert_percent": (1, 100),
    "storefront_autorenew_fire_gb": (0, 100),
    "storefront_autorenew_fire_days": (0, 60),
    "storefront_autorenew_interval_minutes": (1, 1440),
    "storefront_max_pending_topups": (1, 50),
    # Lower bound 1 for the same reason as `storefront_usage_alert_percent`: the
    # `.get(key, (0, None))` fallback admits 0, and a 0 GB cap would provision an empty trial
    # config for every customer instead of disabling the feature.
    "storefront_trial_max_gb": (1, 100),
    "daily_digest_hour": (0, 23),
    "reminder1_day": (0, 365),
    "reminder2_day": (0, 365),
    "warning_day": (0, 365),
    "enforcement_day": (0, 365),
    "enforcement_worker_interval_minutes": (1, 60),
    "enforcement_action_batch_limit": (1, 20),
    "enforcement_user_chunk_size": (1, 500),
    "enforcement_admin_chunk_size": (1, 50),
    "enforcement_panel_concurrency": (1, 20),
    "pending_payment_hold_days": (1, 365),
    # Follow-up board. Lower bound 1 for the same reason as `storefront_usage_alert_percent`
    # above: the `.get(key, (0, None))` fallback admits 0, and a 0 here is not a mild setting
    # — `crm_dormant_days = 0` buckets every reseller as dormant, `crm_declining_pct = 0`
    # turns the shrink rule off invisibly. `crm_growing_pct` starts at 100 because "growing"
    # means "above the previous 3-month mean"; under 100 it would fire on a decline.
    "crm_dormant_days": (1, 365),
    "crm_churned_days": (1, 365),
    "crm_never_active_min_age_days": (1, 365),
    "crm_onboarding_days": (1, 365),
    "crm_declining_pct": (1, 100),
    "crm_growing_pct": (100, 1000),
    "crm_snooze_default_days": (1, 365),
    "kick_grace_minutes": (0, 24 * 60),
    "min_confirmations": (0, 10_000),
    "ton_amount_tolerance_pct": (0, 100),
    "avax_amount_tolerance_pct": (0, 100),
    # Lower bound 1, NOT 0: a 0-hour freshness window would make auto-confirm unreachable
    # (every deposit is at least seconds old) — to switch the feature off, use its own toggle.
    "payment_auto_confirm_max_age_hours": (1, 24 * 365),
}
_NONNEGATIVE_NUMBERS = {
    "payment_amount_tolerance_usdt", "free_under_gb", "overage_tolerance_gb",
}
_STRING_MAX = 10_000


def _api_definition(key: str) -> SettingDef:
    definition = _DEF_BY_KEY.get(key)
    if definition is None:
        raise ValueError(f"Unknown setting key: {key}")
    if key in _API_READ_ONLY:
        raise ValueError(f"Setting is managed internally: {key}")
    return definition


def is_unchanged_secret_mask(key: str, value: Any) -> bool:
    definition = _api_definition(key)
    return (
        definition.is_secret
        and isinstance(value, str)
        and bool(value)
        and set(value) <= {"•"}
    )


def validate_api_value(key: str, value: Any) -> Any:
    """Validate and normalize one owner-editable runtime setting."""
    definition = _api_definition(key)

    default = definition.default
    if isinstance(default, bool):
        if type(value) is not bool:
            raise ValueError(f"{key} must be a boolean")
        return value
    if isinstance(default, int) and not isinstance(default, bool):
        if type(value) is not int:
            raise ValueError(f"{key} must be an integer")
        lo, hi = _INT_RANGES.get(key, (0, None))
        if value < lo or (hi is not None and value > hi):
            suffix = f"..{hi}" if hi is not None else " or greater"
            raise ValueError(f"{key} must be {lo}{suffix}")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{key} must be finite")
        if key in _NONNEGATIVE_NUMBERS and number < 0:
            raise ValueError(f"{key} must be non-negative")
        return number
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        # The user-creation option lists are bounded positive-integer menus (deduped, capped).
        if key in _POSITIVE_INT_LISTS:
            seen: list[int] = []
            for item in value:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise ValueError(f"{key} entries must be numbers")
                number = int(item)
                if number <= 0 or number > 100000:
                    raise ValueError(f"{key} entries must be between 1 and 100000")
                if number not in seen:
                    seen.append(number)
            if not seen:
                raise ValueError(f"{key} must have at least one value")
            return sorted(seen)
        if key != "excluded_usage_gb":
            raise ValueError(f"{key} must be a list")
        normalized: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"{key} entries must be numbers")
            number = float(item)
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"{key} entries must be finite and non-negative")
            normalized.append(number)
        return normalized
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if len(value) > _STRING_MAX:
        raise ValueError(f"{key} is too long")
    if key in ("rate_mode", "ton_rate_mode", "avax_rate_mode") and value not in {"manual", "auto"}:
        raise ValueError(f"{key} must be 'manual' or 'auto'")
    if key == "rate_source" and value not in {"wallex", "tetherland"}:
        raise ValueError("rate_source must be 'wallex' or 'tetherland'")
    return value


async def seed_defaults(session: AsyncSession) -> None:
    """Insert any missing settings rows with their default (bootstrap) values."""
    existing = set(
        (await session.execute(select(Setting.key))).scalars().all()
    )
    for d in DEFS:
        if d.key in existing:
            continue
        value = d.default
        if d.is_secret and value:
            value = crypto.encrypt(str(value))
        session.add(Setting(key=d.key, value=value, is_secret=d.is_secret))
    await session.commit()

    # One-time upgrade of the invoice template to the current default, which shows a CTA to the
    # «💳 پرداخت فاکتور» button instead of embedding the wallet/card/amount (so the invoice never
    # prints a stale TON figure and doesn't duplicate the pay-button details). Migrate ONLY an
    # un-customized install — an exact match of a previous default, or a very old `{wallet_address}`
    # form — so an owner's hand-edited template is never clobbered. Idempotent: once on the new
    # template (which has `{pay_cta}`) nothing matches.
    inv = await session.get(Setting, "tpl_invoice")
    if inv is not None and isinstance(inv.value, str):
        cur = inv.value.strip()
        if cur in (_TPL_INVOICE_LEGACY.strip(), _TPL_INVOICE_PAYINSTR.strip()) or (
            "{wallet_address}" in cur and "{pay_cta}" not in cur
        ):
            inv.value = _TPL_INVOICE
            await session.commit()

    # One-time upgrade of the reject template: older installs don't name the invoice period.
    # Migrate any saved template that lacks {period} to the new default (a customized one that
    # already uses {period} is left untouched).
    rej = await session.get(Setting, "tpl_payment_rejected")
    if rej is not None and isinstance(rej.value, str) and "{period}" not in rej.value:
        rej.value = _TPL_PAYMENT_REJECTED
        await session.commit()

    # One-time upgrade: reminders/warning used to quote the USDT amount; the price is shown in
    # Toman everywhere now. Migrate any saved template still on {amount_usdt} to the Toman default.
    for _key, _new in (("tpl_reminder1", _TPL_REMINDER1), ("tpl_reminder2", _TPL_REMINDER2),
                       ("tpl_warning", _TPL_WARNING)):
        row = await session.get(Setting, _key)
        if row is not None and isinstance(row.value, str) and "{amount_usdt}" in row.value:
            row.value = _new
    await session.commit()


# Settings are read-mostly (the owner edits them rarely) but were fetched from the DB —
# and Fernet-decrypted for secrets — on EVERY access, several times per request on hot
# paths (pricing, rates, payments). A short process-local TTL cache absorbs that. The
# cache stores the RESOLVED value (post-decrypt) or _ROW_ABSENT; the caller-supplied
# `default` is applied at read time so differing defaults can't poison each other.
# Writers invalidate via set_value below; cross-process (bot vs API) staleness is
# bounded by the 5s TTL, which no current setting is sensitive to.
_ROW_ABSENT = object()
_settings_cache = proccache.TTLCache(ttl_seconds=5.0)


def _resolve_absent(key: str, default: Any) -> Any:
    d = _DEF_BY_KEY.get(key)
    return d.default if d else default


def default_for(key: str) -> Any:
    """The REGISTERED default for a key, ignoring whatever is stored. Used as the fallback when a
    stored value turns out to be unusable (e.g. a message template the owner edited into an
    unknown placeholder), so a bad edit degrades to shipped copy instead of failing the action."""
    d = _DEF_BY_KEY.get(key)
    return d.default if d else None


def clear_settings_cache() -> None:
    _settings_cache.clear()


async def get(session: AsyncSession, key: str, default: Any = None) -> Any:
    """Read a setting (secrets returned decrypted). Falls back to the registered default."""
    ns = proccache.engine_ns(session)
    hit = _settings_cache.get((ns, key))
    if hit is not proccache.MISS:
        return _resolve_absent(key, default) if hit is _ROW_ABSENT else hit
    row = await session.get(Setting, key)
    if row is None:
        _settings_cache.put((ns, key), _ROW_ABSENT)
        return _resolve_absent(key, default)
    value = row.value
    if row.is_secret and isinstance(value, str):
        value = crypto.decrypt(value)
    _settings_cache.put((ns, key), value)
    return value


async def get_many(session: AsyncSession, keys: list[str]) -> dict[str, Any]:
    """Batch read: one SELECT for every cache-miss key (was one round-trip PER key)."""
    ns = proccache.engine_ns(session)
    out: dict[str, Any] = {}
    missing: list[str] = []
    for k in keys:
        hit = _settings_cache.get((ns, k))
        if hit is proccache.MISS:
            missing.append(k)
        else:
            out[k] = _resolve_absent(k, None) if hit is _ROW_ABSENT else hit
    if missing:
        rows = (
            await session.execute(select(Setting).where(Setting.key.in_(missing)))
        ).scalars().all()
        found = {r.key: r for r in rows}
        for k in missing:
            row = found.get(k)
            if row is None:
                _settings_cache.put((ns, k), _ROW_ABSENT)
                out[k] = _resolve_absent(k, None)
            else:
                value = row.value
                if row.is_secret and isinstance(value, str):
                    value = crypto.decrypt(value)
                _settings_cache.put((ns, k), value)
                out[k] = value
    return out


async def set_value(
    session: AsyncSession, key: str, value: Any, *, commit: bool = True
) -> None:
    """Write a setting. Secret keys are encrypted at rest."""
    d = _DEF_BY_KEY.get(key)
    is_secret = d.is_secret if d else False
    stored = value
    if is_secret and value:
        stored = crypto.encrypt(str(value))
    row = await session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=stored, is_secret=is_secret)
        session.add(row)
    else:
        row.value = stored
        row.is_secret = is_secret
    # Invalidate before AND after the write: dropping only before would let a concurrent
    # reader re-cache the old row between our drop and the commit.
    _settings_cache.drop((proccache.engine_ns(session), key))
    if commit:
        await session.commit()
    _settings_cache.drop((proccache.engine_ns(session), key))


async def all_for_api(session: AsyncSession) -> list[dict]:
    """Return all settings for the panel, masking secret values."""
    rows = (await session.execute(select(Setting))).scalars().all()
    out: list[dict] = []
    for row in rows:
        d = _DEF_BY_KEY.get(row.key)
        if row.is_secret and isinstance(row.value, str):
            display: Any = crypto.mask(row.value)
        else:
            display = row.value
        out.append(
            {
                "key": row.key,
                "value": display,
                "is_secret": row.is_secret,
                "group": d.group if d else "general",
                "has_value": bool(row.value),
            }
        )
    return out
