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
    admin_api = "admin_api"      # the Hiddify REST API (future read path)
    sample = "sample"            # the bundled sample backup fixture


class SyncStatus(str, enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class EnforcementState(str, enum.Enum):
    active = "active"        # normal
    warned = "warned"        # hard warning sent
    enforced = "enforced"    # users disabled + limits zeroed


class InvoiceStatus(str, enum.Enum):
    draft = "draft"          # generated, not yet delivered
    sent = "sent"            # delivered to reseller
    paid = "paid"
    overdue = "overdue"      # past due, reminders running
    enforced = "enforced"    # panel access suspended
    canceled = "canceled"


class PaymentMethod(str, enum.Enum):
    usdt_txid = "usdt_txid"  # MVP: reseller submits a BEP-20 TXID
    usdt_hd = "usdt_hd"      # future: per-reseller HD deposit address
    manual = "manual"        # owner records it by hand


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"
    duplicate = "duplicate"


class DeliveryKind(str, enum.Enum):
    invoice = "invoice"
    reminder1 = "reminder1"
    reminder2 = "reminder2"
    warning = "warning"
    payment_ack = "payment_ack"
    generic = "generic"


class DeliveryStatus(str, enum.Enum):
    sent = "sent"
    failed = "failed"        # Telegram error
    blocked = "blocked"      # reseller blocked the bot
    unmatched = "unmatched"  # reseller never registered / no telegram id
    skipped = "skipped"


class EnforcementActionType(str, enum.Enum):
    warn = "warn"
    disable_users = "disable_users"
    zero_limits = "zero_limits"
    restore = "restore"


class EnforcementActionStatus(str, enum.Enum):
    planned = "planned"
    dry_run = "dry_run"      # logged only; no live writes
    done = "done"
    failed = "failed"
    reverted = "reverted"
