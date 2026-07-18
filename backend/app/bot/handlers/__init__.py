"""Reseller + owner bot handlers (package facade).

The original monolithic ``app/bot/handlers.py`` is split into domain modules that
all register on the ONE shared router created in ``common``. aiogram dispatches
first-match in REGISTRATION order and decorators register at import time, so the
import order below IS the dispatch order — it mirrors the original file's layout
exactly and must not be reordered (guarded by ``tests/test_bot_router_inventory.py``).
``views`` and ``intake`` are helper-only modules (no registration). This
``__init__`` re-exports the original module's complete top-level surface so
``from app.bot.handlers import X`` keeps working unchanged.
"""
from __future__ import annotations

# Registration order — DO NOT reorder these imports (see the module docstring).
# isort: off
from app.bot.handlers import common
from app.bot.handlers import views
from app.bot.handlers import intake
from app.bot.handlers import commands
from app.bot.handlers import broadcast
from app.bot.handlers import menus
from app.bot.handlers import support
from app.bot.handlers import reseller_cb
from app.bot.handlers import subs
from app.bot.handlers import usercreate
from app.bot.handlers import storefront_setup
from app.bot.handlers import owner
from app.bot.handlers import misc
from app.bot.handlers import text_fallback
# isort: on

# --- facade re-exports: first-party names imported by the original module ---
from app.bot import keyboards, texts
from app.bot.handlers.broadcast import (
    _AUDIENCE_FA,
    _audience_label,
    _bot_broadcast_bg,
    _do_broadcast,
    cb_broadcast_audience,
    cb_cancel,
    cmd_cancel,
)
from app.bot.handlers.commands import (
    cmd_help,
    cmd_interim,
    cmd_invoices,
    cmd_menu,
    cmd_panels,
    cmd_pay,
    cmd_portal,
    cmd_register,
    cmd_removelink,
    cmd_start,
    cmd_storefront,
    cmd_subs,
    cmd_support,
)
from app.bot.handlers.common import (
    _GATE_EXEMPT_CALLBACKS,
    _GATE_EXEMPT_COMMANDS,
    _OWED,
    _SETCHAT_RE,
    _STATUS_FA,
    _TON_EXPLORERS,
    _TXID_RE,
    _UNPAID,
    BroadcastState,
    CreateUserState,
    OwnerCapBumpState,
    OwnerReplyState,
    OwnerSearchState,
    PayState,
    StorefrontSetupState,
    SubCapState,
    SupportState,
    _can_create_users,
    _can_setup_storefront,
    _gate_or_menu,
    _is_member,
    _is_owner_user,
    _is_top_level_reseller,
    _iso,
    _join_link,
    _membership_gate_message_mw,
    _membership_gate_mw,
    _message_command,
    _missing_gates,
    _parse_txid,
    _proof_wanted_fa,
    _required_gates,
    _resellers_for_chat,
    _reshow_menu,
    _send_menu,
    _sync_command_menu,
    _top_level_resellers,
    _track_user,
    log,
    router,
)
from app.bot.handlers.intake import (
    _PAYMENT_METHOD_FA,
    _explorer_link,
    _handle_link,
    _handle_payment_proof,
    _handle_txid,
    _invoice_amount_for_chain,
    _network_status_fa,
    _payment_review_html,
    _registration_candidate,
)
from app.bot.handlers.menus import _do_owner_menu, _do_reseller_menu, on_menu_label
from app.bot.handlers.misc import on_forward
from app.bot.handlers.owner import (
    _OWNER_CMD_ACTION,
    _finalize_review_message,
    _send_reseller_card,
    cb_bump_type,
    cb_cap_approve,
    cb_cap_more,
    cb_cap_reject,
    cb_owner,
    cb_owner_bump,
    cb_owner_enforce,
    cb_owner_payment_confirm,
    cb_owner_payment_reject,
    cb_owner_payment_view,
    cb_owner_per_panel,
    cb_owner_reseller_card,
    cb_owner_reseller_invoices,
    cb_owner_restore,
    cb_owner_stats_period,
    cmd_owner_action,
    cmd_search,
    on_owner_search,
)
from app.bot.handlers.reseller_cb import (
    cb_check_membership,
    cb_panels,
    cb_portal,
    cb_register,
)
from app.bot.handlers.storefront_setup import (
    _finish_create_user,
    _load_pay_invoices,
    _start_pay_flow,
    cb_cu_confirm,
    cb_cu_count,
    cb_cu_days,
    cb_cu_gb,
    cb_cu_mode,
    cb_cu_panel,
    cb_interim,
    cb_invoice_view,
    cb_invoices,
    cb_menu_storefront,
    cb_noop,
    cb_pay,
    cb_pay_all,
    cb_pay_chain,
    cb_pay_invoice,
    cb_removelink,
    cb_rm,
    cb_sf_setup_panel,
    cb_support,
    on_cu_name,
    on_sf_setup_token,
    pay_state_other,
    pay_state_photo,
    pay_state_text,
)
from app.bot.handlers.subs import (
    _apply_sub_cap,
    cb_cap_custom,
    cb_set_cap,
    cb_sub_cap,
    cb_sub_enforce,
    cb_sub_freeze,
    cb_sub_invoice,
    cb_sub_panel,
    cb_sub_restore,
    cb_sub_view,
    cb_subs,
    on_owner_cap_bump_text,
    on_sub_cap_text,
)
from app.bot.handlers.support import (
    _support_message_html,
    cb_support_reply,
    cmd_broadcast,
    on_broadcast_text,
    on_owner_reply,
    on_support_text,
)
from app.bot.handlers.text_fallback import on_document, on_photo, on_text
from app.bot.handlers.usercreate import cb_cu_cancel, cb_menu_newuser, cmd_newuser
from app.bot.handlers.views import (
    _BOTFATHER_GUIDE,
    _begin_create_user,
    _begin_storefront_setup,
    _cap_bar,
    _dispatch_owner,
    _owner_debtors,
    _owner_pending_payments,
    _owner_stats,
    _owns_sub,
    _pending_invoice_ids,
    _pending_payment_for_invoice,
    _send_invoices,
    _send_panels,
    _send_pay,
    _send_portal_link,
    _send_removelink,
    _send_self_interim,
    _send_sub_detail,
    _send_sub_invoice,
    _send_sub_list,
    _send_sub_panels,
    _storefront_target_line,
)
from app.bot.matching import normalize_host, normalize_path, parse_link
from app.bot.rtl import rtl
from app.core.codes import payment_code
from app.core.db import SessionLocal
from app.models import BotUser, Invoice, Panel, Payment, Reseller
from app.models.enums import (
    EnforcementActionStatus,
    EnforcementState,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.services import owner_notify, settings_service
from app.services.periods import today as tehran_today

__all__ = [
    # submodules
    "common", "views", "intake", "commands", "broadcast", "menus", "support",
    "reseller_cb", "subs", "usercreate", "storefront_setup", "owner", "misc",
    "text_fallback",
    # first-party imports of the original module
    "keyboards", "texts", "normalize_host", "normalize_path", "parse_link", "rtl",
    "payment_code", "SessionLocal", "BotUser", "Invoice", "Panel", "Payment",
    "Reseller", "EnforcementActionStatus", "EnforcementState", "InvoiceStatus",
    "PaymentMethod", "PaymentStatus", "owner_notify", "settings_service",
    "tehran_today",
    # states / router / shared core
    "BroadcastState", "SupportState", "OwnerReplyState", "SubCapState",
    "OwnerCapBumpState", "PayState", "OwnerSearchState", "CreateUserState",
    "StorefrontSetupState", "log", "router", "_GATE_EXEMPT_CALLBACKS",
    "_GATE_EXEMPT_COMMANDS", "_membership_gate_mw", "_message_command",
    "_membership_gate_message_mw", "_TXID_RE", "_TON_EXPLORERS", "_parse_txid",
    "_proof_wanted_fa", "_UNPAID", "_OWED", "_STATUS_FA", "_resellers_for_chat",
    "_iso", "_is_owner_user", "_track_user", "_join_link", "_is_member",
    "_required_gates", "_missing_gates", "_gate_or_menu", "_send_menu",
    "_reshow_menu", "_top_level_resellers", "_can_create_users",
    "_can_setup_storefront", "_sync_command_menu",
    "_SETCHAT_RE", "_is_top_level_reseller",
    # commands
    "cmd_start", "cmd_menu", "cmd_help", "cmd_invoices", "cmd_pay", "cmd_panels",
    "cmd_portal", "cmd_interim", "cmd_support", "cmd_removelink", "cmd_subs",
    "cmd_storefront", "cmd_register",
    # broadcast + cancel
    "_AUDIENCE_FA", "_audience_label", "_bot_broadcast_bg", "_do_broadcast",
    "cb_broadcast_audience", "cmd_cancel", "cb_cancel",
    # docked menu
    "on_menu_label", "_do_reseller_menu", "_do_owner_menu",
    # support
    "on_support_text", "_support_message_html", "cb_support_reply",
    "on_owner_reply", "cmd_broadcast", "on_broadcast_text",
    # reseller callbacks
    "cb_check_membership", "cb_register", "cb_panels", "cb_portal",
    # sub-reseller management
    "cb_subs", "cb_sub_panel", "cb_sub_view", "cb_sub_enforce", "cb_sub_freeze",
    "cb_sub_restore", "cb_sub_invoice", "cb_sub_cap", "_apply_sub_cap",
    "cb_set_cap", "cb_cap_custom", "on_sub_cap_text", "on_owner_cap_bump_text",
    # create user
    "_begin_create_user", "cmd_newuser", "cb_menu_newuser", "cb_cu_cancel",
    # storefront setup + create-user picker + invoices/pay flow
    "_BOTFATHER_GUIDE", "_storefront_target_line", "_begin_storefront_setup",
    "cb_menu_storefront", "cb_sf_setup_panel", "on_sf_setup_token", "cb_cu_panel",
    "cb_cu_mode", "cb_cu_count", "cb_cu_gb", "cb_cu_days", "on_cu_name",
    "cb_cu_confirm", "_finish_create_user", "cb_support", "cb_invoices",
    "cb_invoice_view", "cb_interim", "cb_pay", "_load_pay_invoices",
    "_start_pay_flow", "cb_pay_invoice", "cb_pay_all", "pay_state_photo",
    "pay_state_text", "pay_state_other", "cb_pay_chain", "cb_removelink",
    "cb_rm", "cb_noop",
    # owner
    "_dispatch_owner", "cb_owner", "cb_owner_stats_period", "cb_owner_per_panel",
    "cb_owner_payment_view", "_finalize_review_message",
    "cb_owner_payment_confirm", "cb_owner_payment_reject", "on_owner_search",
    "cb_owner_reseller_card", "_send_reseller_card", "cb_owner_enforce",
    "cb_owner_restore", "cb_owner_bump", "cb_cap_approve", "cb_cap_reject",
    "cb_cap_more", "cb_bump_type", "cb_owner_reseller_invoices",
    "_OWNER_CMD_ACTION", "cmd_owner_action", "cmd_search", "_owner_stats",
    "_owner_debtors", "_owner_pending_payments",
    # shared reseller views
    "_pending_payment_for_invoice", "_pending_invoice_ids", "_send_invoices",
    "_send_self_interim", "_send_pay", "_send_removelink", "_send_panels",
    "_send_portal_link",
    # sub-reseller view helpers
    "_owns_sub", "_send_sub_panels", "_send_sub_list", "_send_sub_detail",
    "_cap_bar", "_send_sub_invoice",
    # forwarded post + free text
    "on_forward", "on_document", "on_photo", "on_text",
    # registration-link + payment intake helpers
    "_handle_link", "_registration_candidate",
    "_invoice_amount_for_chain", "_PAYMENT_METHOD_FA", "_explorer_link",
    "_network_status_fa", "_payment_review_html", "_handle_txid",
    "_handle_payment_proof",
]
