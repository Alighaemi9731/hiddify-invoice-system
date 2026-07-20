"""Build and deliver invoice / reminder messages to resellers via the bot."""
from __future__ import annotations

import datetime as dt
import logging
from html import escape as _html_escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.rtl import rtl
from app.bot.telegram import build_bot
from app.models import DeliveryLog, Invoice, Reseller
from app.models.enums import DeliveryKind, DeliveryStatus, InvoiceStatus
from app.services import invoice_pdf, notifier

log = logging.getLogger("delivery")


# Call to action at the bottom of the sent invoice. The invoice text itself is kept lean — the
# wallet/card/USDT/TON details are NOT embedded (they'd duplicate the pay view AND a TON amount
# printed at send time goes stale by the time the customer pays). Paying is done ONLY from the
# menu «💳 پرداخت فاکتور», whose FIRST option settles all outstanding debt in one transfer — so a
# customer with several invoices doesn't accidentally pay just the one under a single message.
_PAY_CTA = (
    "برای پرداخت، از منو گزینهٔ «💳 پرداخت فاکتور» را انتخاب کنید؛ "
    "گزینهٔ نخست، پرداختِ یکجای کلِ بدهی است (یا می‌توانید هر فاکتور را جداگانه انتخاب و پرداخت کنید)."
)


async def build_invoice_text(session: AsyncSession, inv: Invoice, reseller: Reseller) -> str:
    """Build the payable invoice text as **HTML**. Lean by design: amount + a CTA to the menu
    pay flow — NO embedded wallet/card/USDT/TON (those live on the «💳 پرداخت فاکتور» path, with a
    live TON rate). Dynamic free text (the reseller name) is HTML-escaped."""
    import html as _html

    from app.core.codes import invoice_code

    text = await texts.render(
        session, "tpl_invoice",
        name=_html.escape(reseller.name or ""), period=inv.period_label,
        usage_gb=f"{float(inv.usage_gb):,.0f}",
        amount_toman=f"{float(inv.amount_toman):,.0f}",
        pay_cta=_PAY_CTA,
    )
    # When the minimum-sale floor was applied, explain it transparently: real sale, the floor
    # threshold, and the final billed amount — so it's clear WHY the invoice exceeds the usage.
    if getattr(inv, "floor_applied", False):
        base = float(inv.base_amount_toman or 0)
        floor = float(inv.min_sale_toman or 0)
        final = float(inv.amount_toman or 0)
        text += (
            "\n\n"
            "ℹ️ <b>حداقل فروش ماهانه</b> برای شما فعال است.\n"
            f"• فروش واقعی این دوره: {base:,.0f} تومان\n"
            f"• حداقل مبلغ قابل قبول: {floor:,.0f} تومان\n"
            f"از آنجا که فروش این دوره کمتر از حداقل بود، مبلغِ فاکتور برابرِ حداقل "
            f"({final:,.0f} تومان) برای شما صادر شد. جزئیاتِ دقیقِ مصرفِ واقعیِ شما در فایلِ PDF آمده است."
        )
    # Prepend the public 8-digit invoice number (tap-to-copy) at the top.
    return f"🔢 شمارهٔ فاکتور: <code>{invoice_code(inv.id)}</code>\n{text}"


# Unicode First-Strong Isolate (U+2068 … U+2069): wraps a possibly-English value so it keeps
# its own direction and does NOT reorder the surrounding RTL text (e.g. the GB after a name).
def _iso(value) -> str:
    return f"⁨{value}⁩"


def _breakdown_lines(bd: dict) -> list[str]:
    """Per-node usage breakdown (own + each sub), matching the «فاکتور علی‌الحساب» format.
    GB only — the payable total + payment instructions live in the main invoice text. The
    (sub-)reseller name is isolated so an English name doesn't push the GB to the wrong side."""
    lines = [
        "➖➖➖➖➖➖➖➖",
        "🧾 ریز مصرف این دوره:",
        f"🟦 مصرف خودتان: حجم {bd['own']['gb']:g} گیگابایت ({bd['own']['users']} سرویس)",
    ]
    if bd["subs"]:
        lines.append("🟨 زیرمجموعه‌های شما:")
        for s in bd["subs"]:
            # message is sent as HTML → escape the name (then isolate it for RTL).
            nm = _iso(_html_escape(s["name"]))
            lines.append(f"• نماینده {nm}: حجم {s['gb']:g} گیگابایت ({s['users']} سرویس)")
    return lines


async def _render_invoice_pdfs(
    session: AsyncSession, inv: Invoice, reseller: Reseller
) -> list[tuple[str, str]]:
    """Build the per-node, volume-only PDFs for an invoice — ONE for the reseller's own users and
    ONE per sub-reseller — straight from the invoice's PERSISTED lines (so they always match the
    locked invoice total; a user deleted from the panel after issue no longer shrinks the PDF).
    Falls back to a single whole-bundle PDF if the split yields nothing. Returns [(path, caption)]."""
    title = f"فاکتور دورهٔ {inv.period_label}"
    docs: list[tuple[str, str]] = []
    try:
        docs = await invoice_pdf.render_invoice_node_pdfs(session, inv, reseller)
    except Exception:  # noqa: BLE001
        log.warning("per-node invoice PDF render failed for invoice %s", inv.id, exc_info=True)
    if not docs:
        # Defensive fallback: the classic single bundle PDF so the reseller still gets a doc.
        try:
            path, _ = await invoice_pdf.render_invoice_pdf(session, inv)
            docs.append((path, f"📄 {title}"))
        except Exception:  # noqa: BLE001
            log.warning("fallback invoice PDF render failed for invoice %s", inv.id, exc_info=True)
    return docs


async def send_invoice_content(
    session: AsyncSession, bot: Bot, chat_id: int, inv: Invoice, reseller: Reseller,
    *, text: str | None = None,
) -> list[int]:
    """Send the invoice content to `chat_id`: the payable text + per-node usage breakdown
    (HTML, so wallet/card are tap-to-copy), then a volume-only PDF for the reseller's own
    users and one per sub-reseller. Returns the sent message ids. No DB side effects — the
    delivery wrapper (send_invoice) adds status/log/cleanup; the «فاکتورهای من» view reuses
    this raw to re-show an invoice on demand."""
    if text is None:
        text = await build_invoice_text(session, inv, reseller)
    # Breakdown comes from the PERSISTED invoice lines (authoritative — sums to the locked total),
    # NOT a live recompute, so it matches the amount shown in the text even after panel changes.
    bd: dict | None = None
    try:
        b = await invoice_pdf.invoice_node_breakdown(session, inv, reseller)
        bd = b if b and b.get("total_gb", 0) > 0 else None
    except Exception:  # noqa: BLE001
        bd = None

    full_text = text + ("\n\n" + "\n".join(_breakdown_lines(bd)) if bd else "")
    # No pay button under the invoice on purpose: paying is ONLY via the menu «💳 پرداخت فاکتور»,
    # whose first option settles ALL outstanding debt at once — so a customer with several invoices
    # doesn't pay just this one. A cold txid/photo sent outside that flow is ignored.
    sent_ids: list[int] = []
    msg = await bot.send_message(chat_id, rtl(full_text), parse_mode="HTML")
    sent_ids.append(msg.message_id)
    for path, caption in await _render_invoice_pdfs(session, inv, reseller):
        try:
            doc = await bot.send_document(chat_id, FSInputFile(path), caption=caption)
            sent_ids.append(doc.message_id)
        except Exception:  # noqa: BLE001 — a single PDF failure shouldn't fail the invoice
            log.warning("invoice PDF send failed for invoice %s", inv.id, exc_info=True)
    return sent_ids


async def send_invoice(
    session: AsyncSession, invoice_id: int, *, bot: Bot | None = None
) -> DeliveryLog:
    """Deliver the invoice like the «فاکتور علی‌الحساب»: a text message (payable amount +
    payment instructions + a per-node usage breakdown), then a SEPARATE volume-only PDF for
    the reseller's own users and one per sub-reseller. Falls back to text-only on failure."""
    inv = await session.get(Invoice, invoice_id)
    if inv is None:
        raise ValueError(f"invoice {invoice_id} not found")
    reseller = await session.get(Reseller, inv.reseller_id)
    if reseller is None:
        raise ValueError(f"reseller {inv.reseller_id} not found for invoice {invoice_id}")
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

    # Resend cleanup: collect ALL message ids of the prior delivery (text + every PDF) and
    # delete them only AFTER the new pieces land, so a failed resend never wipes the chat.
    prior_ids: list[int] = []
    prior = (
        await session.execute(
            select(DeliveryLog)
            .where(
                DeliveryLog.invoice_id == inv.id,
                DeliveryLog.kind == DeliveryKind.invoice,
            )
            .order_by(DeliveryLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior:
        if prior.tg_message_ids:
            prior_ids = [int(x) for x in prior.tg_message_ids.split(",") if x.strip().isdigit()]
        elif prior.tg_message_id:
            prior_ids = [prior.tg_message_id]

    status = DeliveryStatus.sent
    error: str | None = None
    sent_ids: list[int] = []
    try:
        if bot is None:
            raise RuntimeError("no telegram bot token configured")
        sent_ids = await send_invoice_content(
            session, bot, reseller.bot_chat_id, inv, reseller, text=text
        )
    except TelegramForbiddenError:
        status, error = DeliveryStatus.blocked, "reseller blocked the bot"
    except Exception as exc:  # noqa: BLE001 — fall back to plain text only
        log.warning("invoice delivery failed for invoice %s: %s", inv.id, exc)
        try:
            if bot is None:
                raise RuntimeError("no telegram bot token configured")
            msg = await bot.send_message(reseller.bot_chat_id, rtl(text), parse_mode="HTML")
            sent_ids = [msg.message_id]
        except Exception as exc2:  # noqa: BLE001
            status, error = DeliveryStatus.failed, str(exc2)[:500]

    # New pieces are out → now it's safe to remove the previous delivery's messages.
    if status == DeliveryStatus.sent and prior_ids and bot is not None:
        for mid in prior_ids:
            try:
                await bot.delete_message(reseller.bot_chat_id, mid)
            except Exception:  # noqa: BLE001 — old message may already be gone / too old
                pass

    dl = DeliveryLog(
        reseller_id=reseller.id, invoice_id=inv.id, kind=DeliveryKind.invoice,
        status=status, error=error, message_preview=text[:200],
        tg_message_id=sent_ids[0] if sent_ids else None,
        tg_message_ids=",".join(str(i) for i in sent_ids) or None,
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
