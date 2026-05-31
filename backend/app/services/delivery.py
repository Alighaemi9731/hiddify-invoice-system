"""Build and deliver invoice / reminder messages to resellers via the bot."""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.telegram import build_bot
from app.models import DeliveryLog, Invoice, Reseller
from app.models.enums import DeliveryKind, DeliveryStatus, InvoiceStatus
from app.services import invoice_pdf, notifier, settings_service

# Telegram caption hard limit.
_CAPTION_MAX = 1024

log = logging.getLogger("delivery")


async def build_invoice_text(session: AsyncSession, inv: Invoice, reseller: Reseller) -> str:
    wallet = await settings_service.get(session, "usdt_bep20_address", "") or "(تنظیم نشده)"
    text = await texts.render(
        session, "tpl_invoice",
        name=reseller.name, period=inv.period_label,
        usage_gb=f"{float(inv.usage_gb):,.0f}",
        amount_toman=f"{float(inv.amount_toman):,.0f}",
        amount_usdt=f"{float(inv.amount_usdt):,.2f}",
        wallet_address=wallet,
    )
    # When the minimum-sale floor was applied, explain it transparently.
    if getattr(inv, "floor_applied", False):
        base = float(inv.base_amount_toman or 0)
        text += (
            f"\n\nℹ️ توجه: مبلغ فروش واقعی شما در این دوره {base:,.0f} تومان بود که کمتر از "
            f"حداقل مجاز است؛ بنابراین حداقل مبلغ ({float(inv.amount_toman):,.0f} تومان) "
            f"به‌عنوان فاکتور برای شما صادر شد."
        )
    return text


async def send_invoice(
    session: AsyncSession, invoice_id: int, *, bot: Bot | None = None
) -> DeliveryLog:
    """Deliver the invoice as the PDF document with the full text as its caption
    (one message). Falls back to text-only if the PDF/document send fails."""
    inv = await session.get(Invoice, invoice_id)
    reseller = await session.get(Reseller, inv.reseller_id)
    text = await build_invoice_text(session, inv, reseller)

    # Not registered with the bot → log unmatched, nothing to send.
    if reseller.bot_chat_id is None:
        return await notifier.send_to_reseller(
            session, reseller, text, kind=DeliveryKind.invoice, invoice_id=inv.id
        )

    own_bot = False
    if bot is None:
        bot = await build_bot(session)
        own_bot = True

    # Resend cleanup: remove the previously delivered message for THIS invoice so the
    # reseller's chat shows only the latest (corrected) version.
    if bot is not None:
        prior = (
            await session.execute(
                select(DeliveryLog)
                .where(
                    DeliveryLog.invoice_id == inv.id,
                    DeliveryLog.kind == DeliveryKind.invoice,
                    DeliveryLog.tg_message_id.is_not(None),
                )
                .order_by(DeliveryLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior and prior.tg_message_id:
            try:
                await bot.delete_message(reseller.bot_chat_id, prior.tg_message_id)
            except Exception:  # noqa: BLE001 — old message may be deleted/too old to remove
                pass

    status = DeliveryStatus.sent
    error: str | None = None
    msg_id: int | None = None
    try:
        if bot is None:
            raise RuntimeError("no telegram bot token configured")
        path, _ = await invoice_pdf.render_invoice_pdf(session, inv)
        # Caption carries the invoice text; overflow goes as a follow-up message.
        caption = text if len(text) <= _CAPTION_MAX else None
        sent = await bot.send_document(reseller.bot_chat_id, FSInputFile(path), caption=caption)
        msg_id = sent.message_id
        if caption is None:
            await bot.send_message(reseller.bot_chat_id, text)
    except TelegramForbiddenError:
        status, error = DeliveryStatus.blocked, "reseller blocked the bot"
    except Exception as exc:  # noqa: BLE001 — fall back to text-only
        log.warning("PDF delivery failed for invoice %s: %s", inv.id, exc)
        try:
            sent = await bot.send_message(reseller.bot_chat_id, text)
            msg_id = sent.message_id
        except Exception as exc2:  # noqa: BLE001
            status, error = DeliveryStatus.failed, str(exc2)[:500]

    dl = DeliveryLog(
        reseller_id=reseller.id, invoice_id=inv.id, kind=DeliveryKind.invoice,
        status=status, error=error, message_preview=text[:200], tg_message_id=msg_id,
    )
    session.add(dl)

    notify_abuse = False
    if status == DeliveryStatus.sent and inv.status == InvoiceStatus.draft:
        inv.status = InvoiceStatus.sent
        inv.sent_at = dt.datetime.now(dt.timezone.utc)
        # Now it's a real, delivered invoice → enter the durable financial ledger.
        from app.services import financial_archive

        await financial_archive.record(session, inv, reseller=reseller)
        notify_abuse = True
    await session.commit()

    # On first delivery, if the invoice includes abuse-metered extra, explain it to the
    # reseller and ping the owner. Best-effort; never blocks delivery.
    if notify_abuse:
        from app.services import metering

        await metering.notify_abuse_if_any(session, inv, reseller, bot=bot)

    if own_bot and bot is not None:
        await bot.session.close()
    return dl


async def send_period(session: AsyncSession, period_label: str) -> dict:
    """Send every draft invoice for a period. Reuses one Bot for all sends."""
    invoices = (
        await session.execute(
            select(Invoice).where(
                Invoice.period_label == period_label, Invoice.status == InvoiceStatus.draft
            )
        )
    ).scalars().all()
    if not invoices:
        return {"period": period_label, "sent": 0, "failed": 0, "unmatched": 0, "total": 0}

    bot = await build_bot(session)
    counts = {"sent": 0, "failed": 0, "unmatched": 0, "blocked": 0}
    try:
        for inv in invoices:
            dl = await send_invoice(session, inv.id, bot=bot)
            counts[dl.status.value] = counts.get(dl.status.value, 0) + 1
    finally:
        if bot is not None:
            await bot.session.close()
    return {"period": period_label, "total": len(invoices), **counts}
