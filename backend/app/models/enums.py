"""Enumerations used across the data model."""
from __future__ import annotations

import enum


class PanelStatus(str, enum.Enum):
    unknown = "unknown"
    ok = "ok"
    error = "error"
    disabled = "disabled"


class SyncSource(str, enum.Enum):
    backup_json = "backup_json"  # the panel /admin/backup/backupfile/ endpoint


class SyncStatus(str, enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class EnforcementState(str, enum.Enum):
    active = "active"        # normal
    enforced = "enforced"    # users disabled + limits zeroed


class InvoiceStatus(str, enum.Enum):
    draft = "draft"          # generated, not yet delivered
    sent = "sent"            # delivered to reseller
    paid = "paid"
    overdue = "overdue"      # past due, reminders running
    enforced = "enforced"    # panel access suspended
    canceled = "canceled"


class PaymentMethod(str, enum.Enum):
    usdt_txid = "usdt_txid"      # reseller submits a BEP-20 TXID (owner confirms manually)
    manual = "manual"            # owner records it by hand
    screenshot = "screenshot"    # reseller sends a deposit screenshot (owner confirms)
    ton_txid = "ton_txid"        # reseller submits a TON tx hash (owner confirms manually)


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"


class DeliveryKind(str, enum.Enum):
    invoice = "invoice"
    reminder1 = "reminder1"
    reminder2 = "reminder2"
    warning = "warning"
    payment_ack = "payment_ack"
    abuse_notice = "abuse_notice"
    generic = "generic"


class DeliveryStatus(str, enum.Enum):
    sent = "sent"
    failed = "failed"        # Telegram error
    blocked = "blocked"      # reseller blocked the bot
    unmatched = "unmatched"  # reseller never registered / no telegram id


class EnforcementActionType(str, enum.Enum):
    disable_users = "disable_users"
    restore = "restore"


class EnforcementActionStatus(str, enum.Enum):
    planned = "planned"
    running = "running"
    partial = "partial"
    dry_run = "dry_run"      # logged only; no live writes
    done = "done"
    failed = "failed"
    reverted = "reverted"
