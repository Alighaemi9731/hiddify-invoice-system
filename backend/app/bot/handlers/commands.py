"""/commands — the reseller slash-command handlers (owner `/` actions live in `owner`)."""
from __future__ import annotations

from aiogram import Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards
from app.bot.handlers import common
from app.bot.handlers.common import (
    SupportState,
    _reshow_menu,
    _sync_command_menu,
    _track_user,
    router,
)
from app.bot.handlers.views import (
    _begin_storefront_setup,
    _send_invoices,
    _send_panels,
    _send_pay,
    _send_portal_link,
    _send_removelink,
    _send_self_interim,
    _send_sub_panels,
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()  # leaving any in-progress pay flow resets it (no stale invoice binding)
    async with common.SessionLocal() as session:
        await _track_user(session, message.from_user)
        await _sync_command_menu(bot, session, message.from_user)
        await common._gate_or_menu(message.answer, bot, session, message.from_user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with common.SessionLocal() as session:
        await _sync_command_menu(bot, session, message.from_user)
        await common._send_menu(message.answer, session, message.from_user, bot=bot)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    async with common.SessionLocal() as session:
        if await common._is_owner_user(session, message.from_user):
            await message.answer(
                "📖 راهنمای مدیر\n\n"
                "/menu — منوی مدیریت\n"
                "/stats — آمار کلی\n"
                "/health — سلامت سامانه\n"
                "/payments — پرداخت‌های در انتظار\n"
                "/debtors — بدهکاران\n"
                "/test — کانفیگ تست (می‌توانید نام هم بدهید: /test نامِ مشتری)\n"
                "/search — جستجوی نماینده\n"
                "/broadcast — پیام همگانی به نمایندگان\n"
                "/sync — همگام‌سازی پنل‌ها\n"
                "/backup — پشتیبان‌گیری اکنون\n"
                "/cancel — لغو عملیات جاری\n\n"
                "• ثبت کانال/گروه: یک پیام از آن را برای ربات فوروارد کنید.\n"
                "• بازیابی: فایل پشتیبان (zip) را برای ربات بفرستید.\n"
                "• پاسخ به پشتیبانی: روی پیام کاربر گزینهٔ «پاسخ» (Reply) را بزنید."
            )
        else:
            await message.answer(
                "📖 راهنما\n\n"
                "/menu — منوی اصلی\n"
                "/invoices — فاکتورهای پرداخت‌نشده\n"
                "/pay — پرداخت فاکتور (می‌توانید همهٔ بدهی را یکجا پرداخت کنید)\n"
                "/interim — فاکتور علی‌الحساب (ماه جاری)\n"
                "/panels — پنل‌های من\n"
                "/subs — مدیریت زیرمجموعه‌ها\n"
                "/newuser — ساخت سرویس (نماینده‌های اصلی)\n"
                "/storefront — راه‌اندازی ربات فروشگاهی\n"
                "/portal — ورود به پنلِ تحتِ وب\n"
                "/register — ثبت لینک پنل من\n"
                "/support — پیام به پشتیبانی\n"
                "/removelink — حذف لینک‌ها\n"
                "/cancel — لغو عملیات جاری\n\n"
                "برای ثبت‌نام، کافی است لینک پنل خود را همین‌جا ارسال کنید.\n"
                "پرداخت از طریق رمزارز (تتر، گرام، اوالانچ)، کارت‌به‌کارت یا ارسال تصویر رسید انجام می‌شود "
                "(روش‌های فعال هنگام پرداخت نمایش داده می‌شوند)."
            )


@router.message(Command("invoices"))
async def cmd_invoices(message: Message, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_invoices(message.answer, message.from_user.id, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("pay"))
async def cmd_pay(message: Message, state: FSMContext) -> None:
    await state.clear()  # re-opening the pay list resets any prior invoice selection
    async with common.SessionLocal() as s:
        await _send_pay(message.answer, message.from_user.id, s)
        # No menu re-show: the result is a payable-invoice picker; a trailing menu would bury it.


@router.message(Command("panels"))
async def cmd_panels(message: Message, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_panels(message.answer, message.from_user.id, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("portal"))
async def cmd_portal(message: Message, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_portal_link(message.answer, message.from_user.id, s)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("interim"))
async def cmd_interim(message: Message, bot: Bot, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_self_interim(message.answer, message.from_user.id, s, bot=bot)
        await _reshow_menu(message, s, message.from_user)


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportState.waiting)
    await message.answer(
        "پیام خود را برای پشتیبانی بنویسید:", reply_markup=keyboards.flow_cancel_kb()
    )


@router.message(Command("removelink"))
async def cmd_removelink(message: Message, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_removelink(message.answer, message.from_user.id, s)
        # No menu re-show: the result is a link picker (tap to remove) — a menu would bury it.


@router.message(Command("subs"))
async def cmd_subs(message: Message, state: FSMContext) -> None:
    await common.clear_stale_flow(state)
    async with common.SessionLocal() as s:
        await _send_sub_panels(message.answer, message.from_user.id, s)
        # No menu re-show: the result is a sub-panel picker — a menu would bury it.


@router.message(Command("storefront"))
async def cmd_storefront(message: Message, state: FSMContext) -> None:
    """Start the storefront-bot setup (self-gated: only top-level resellers with the capability)."""
    async with common.SessionLocal() as s:
        await _begin_storefront_setup(message.answer, message.from_user.id, s, state)


@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    """Prompt for the panel link (same as the «🔗 ثبت لینک پنل من» menu action)."""
    await common.clear_stale_flow(state)
    await message.answer(
        "لطفاً لینک پنل خود را ارسال کنید (شامل دامنه و شناسه).",
        reply_markup=keyboards.cancel_keyboard("« بازگشت به منو"),
    )
