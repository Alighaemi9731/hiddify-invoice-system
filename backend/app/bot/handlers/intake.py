"""Registration-link + payment-proof intake helpers (no handler registration).

The tail helper block of the original monolithic module: panel-link registration
matching, the owner-facing payment-review HTML (also imported by the web portal),
and the shared TXID / screenshot payment recording used by the locked pay flow.
"""
from __future__ import annotations

import datetime as dt
import io
import os

from aiogram.types import Message
from sqlalchemy import func, select

from app.bot import keyboards, texts
from app.bot.handlers.common import (
    _is_top_level_reseller,
    _iso,
    _resellers_for_chat,
    _reshow_menu,
    log,
)
from app.bot.matching import normalize_host, normalize_path
from app.bot.rtl import rtl
from app.core.codes import payment_code
from app.models import Invoice, Panel, Reseller
from app.models.enums import PaymentMethod
from app.services import owner_notify, settings_service


async def _handle_link(message: Message, session, parsed) -> None:
    reseller = await _registration_candidate(session, parsed)
    # A registration identity is the normalized host + complete proxy path + UUID. Missing,
    # mismatched, or ambiguous identities are rejected rather than selecting an arbitrary row.
    if reseller is None:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Must be a real, non-owner reseller that came from one of the registered panels.
    if reseller.is_owner:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Only TOP-LEVEL resellers may register. A sub-reseller is handled by its parent (the
    # parent issues its invoices + manages it from «مدیریت زیرمجموعه‌ها»), so block it.
    if not await _is_top_level_reseller(session, reseller):
        await message.answer(
            "این لینک متعلق به یک زیرمجموعه است.\n"
            "زیرمجموعه‌ها مستقیماً در ربات ثبت نمی‌شوند؛ مدیریت و صدور فاکتورِ شما از طریق "
            "نمایندهٔ بالادستی‌تان انجام می‌شود. لطفاً با ایشان هماهنگ کنید."
        )
        return
    # Prevent duplicate / takeover: if bound to another account, refuse.
    if reseller.bot_chat_id and reseller.bot_chat_id != message.from_user.id:
        await message.answer("این نماینده قبلاً توسط حساب دیگری ثبت شده است.")
        return
    already = reseller.bot_chat_id == message.from_user.id
    reseller.bot_chat_id = message.from_user.id
    reseller.link_tag = parsed.tag or reseller.link_tag
    reseller.registered_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()
    if already:
        await message.answer("این لینک قبلاً ثبت شده بود و اطلاعاتش به‌روزرسانی شد.")
    else:
        await message.answer(await texts.render(session, "tpl_link_matched", name=reseller.name))
        panel = await session.get(Panel, reseller.panel_id)
        await owner_notify.notify_owner(
            session, f"🔗 نمایندهٔ جدید در ربات ثبت شد: «{reseller.name}»"
            + (f" (پنل {panel.key})" if panel else ""))
    # Now a registered reseller → keep the menu at hand.
    await _reshow_menu(message, session, message.from_user)


async def _registration_candidate(session, parsed) -> Reseller | None:
    """Return the unique reseller matching normalized host + proxy path + UUID."""
    expected_host = normalize_host(parsed.host)
    expected_path = normalize_path(parsed.path)
    if not expected_host or not expected_path:
        return None
    candidates = (
        await session.execute(
            select(Reseller).where(func.lower(Reseller.admin_uuid) == parsed.uuid.lower())
        )
    ).scalars().all()
    matches: list[Reseller] = []
    for candidate in candidates:
        panel = await session.get(Panel, candidate.panel_id)
        if panel is None:
            continue
        # The link's host may be the panel's CURRENT host or one of its old/alternate hosts
        # (after a domain move). Path + UUID identity is otherwise unchanged.
        host_ok = expected_host == normalize_host(panel.host) or expected_host in {
            normalize_host(a) for a in panel.host_alias_list
        }
        if host_ok and normalize_path(panel.proxy_path) == expected_path:
            matches.append(candidate)
    # Fail-closed: a unique match or nothing. Never guess / never take the first of several.
    return matches[0] if len(matches) == 1 else None


async def _invoice_amount_for_chain(session, invoice, chain: str) -> str:
    """The invoice amount in Toman plus its equivalent in the PAID currency: TON for a TON
    payment, USDT otherwise — so a TON payment's owner message doesn't show a USDT figure."""
    if invoice is None:
        return "نامشخص"
    toman = f"{float(invoice.amount_toman):,.0f} تومان"
    if chain == "ton":
        from app.services import rates
        rate = await rates.get_ton_toman(session)
        if rate:
            return f"{toman} (≈ {float(invoice.amount_toman) / rate:,.2f} GRAM)"
        return toman
    if chain == "avax":
        from app.services import rates
        rate = await rates.get_avax_toman(session)
        if rate:
            return f"{toman} (≈ {float(invoice.amount_toman) / rate:,.4f} AVAX)"
        return toman
    return f"{toman} ({float(invoice.amount_usdt):,.2f} USDT)"


_PAYMENT_METHOD_FA = {
    "usdt_txid": "تتر (USDT)", "ton_txid": "گرام (GRAM)", "avax_txid": "اوالانچ (AVAX)",
    "screenshot": "رسید تصویری", "manual": "دستی",
}


def _explorer_link(chain: str, txid: str) -> str:
    """An HTML link to the matching block explorer so the owner can open the tx in one tap."""
    if chain == "ton":
        return f"<a href='https://tonscan.org/tx/{txid}'>مشاهده در tonscan</a>"
    if chain == "avax":
        return f"<a href='https://snowtrace.io/tx/{txid}'>مشاهده در snowtrace</a>"
    return f"<a href='https://bscscan.com/tx/{txid}'>مشاهده در bscscan</a>"


def _network_status_fa(chk: dict) -> str:
    """One-line on-chain status from a `deposit_check` result, for any chain (TON/AVAX/USDT)."""
    if not chk.get("available"):
        return "⚪️ از زنجیره خوانده نشد — تراکنش را از روی لینک بررسی کنید."
    if chk.get("kind") == "ton":
        recv = f"{chk['received_ton']} GRAM ≈ {chk['received_toman']:,.0f} تومان"
        tol = f"±{chk['tolerance_pct']:.0f}٪"
    elif chk.get("kind") == "avax":
        recv = f"{chk['received_avax']} AVAX ≈ {chk['received_toman']:,.0f} تومان"
        conf = chk.get("confirmations")
        if conf is not None:
            recv += f" ({conf} تأیید)"
        tol = f"±{chk['tolerance_pct']:.0f}٪"
    else:  # usdt
        recv = f"{chk['received_usdt']:,.2f} USDT"
        conf = chk.get("confirmations")
        if conf is not None:
            recv += f" ({conf} تأیید)"
        tol = f"±{chk['tolerance_usdt']:g} USDT"
    if chk.get("match") is True:
        return f"✅ واریزی یافت شد: {recv} — مطابق فاکتور ({tol})"
    if chk.get("match") is False:
        return f"⚠️ واریزی یافت شد: {recv} — مغایر با فاکتور (خارج از {tol})"
    return f"🟢 واریزی یافت شد: {recv}"


async def _reseller_username(session, reseller) -> str | None:
    """The reseller's current Telegram @username (BotUser joined on bot_chat_id) — used to build the
    more-reliable t.me/<username> profile link. None when not registered/unknown."""
    if not reseller or not getattr(reseller, "bot_chat_id", None):
        return None
    from app.models import BotUser

    bu = (await session.execute(
        select(BotUser).where(BotUser.telegram_id == reseller.bot_chat_id)
    )).scalars().first()
    return bu.username if bu else None


async def _payment_review_html(session, pay, *, reseller=None, inv=None, invs=None) -> str:
    """Rich, owner-facing HTML summary of a pending payment — shared by the submit notification
    and the «پرداخت‌های در انتظار» detail view so both are complete and identical. Includes the
    tracking number, a CLICKABLE reseller name (opens their Telegram profile), method, EVERY
    invoice the payment covers (a payment may settle several) with its paid-currency equivalent
    plus a grand total, a clickable explorer link, and — for TON — a best-effort on-chain deposit
    read (actual received ≈ Toman, matched against the total) so the owner can decide."""
    from app.services import payments as _payments

    if reseller is None:
        reseller = await session.get(Reseller, pay.reseller_id)
    # The full set this payment covers. Prefer an explicitly passed list; else load from the
    # payment's stored set (settled_invoice_ids → invoice_id fallback).
    invoices = list(invs) if invs else ([inv] if inv else [])
    if not invoices:
        set_ids = _payments._settled_ids(pay)
        if set_ids:
            invoices = list(
                (await session.execute(select(Invoice).where(Invoice.id.in_(set_ids))))
                .scalars().all()
            )
    chain = pay.chain or ("ton" if pay.method == PaymentMethod.ton_txid else "bsc")
    name_link = (
        owner_notify.user_link(reseller, username=await _reseller_username(session, reseller))
        if reseller else "—"
    )
    lines = [
        f"💳 پرداخت — شمارهٔ پیگیری #{payment_code(pay.id)}",
        f"👤 نماینده: {name_link}",
        f"📤 روش: {_PAYMENT_METHOD_FA.get(pay.method.value, pay.method.value)}",
    ]
    if not invoices:
        lines.append("🧾 فاکتور: —")
    elif len(invoices) == 1:
        only = invoices[0]
        lines.append(f"🧾 فاکتور: دورهٔ {only.period_label}")
        lines.append(f"💰 مبلغ فاکتور: {await _invoice_amount_for_chain(session, only, chain)}")
    else:
        lines.append(f"🧾 فاکتورها ({len(invoices)}):")
        for one in invoices:
            amt = await _invoice_amount_for_chain(session, one, chain)
            lines.append(_iso(f"• دورهٔ {one.period_label}: {amt}"))
        total_toman = float(sum(float(i.amount_toman or 0) for i in invoices))
        lines.append(f"💰 مبلغ کل: {total_toman:,.0f} تومان")
    if pay.txid:
        lines.append(f"🔗 تراکنش: {_explorer_link(chain, pay.txid)}")
        lines.append(_iso(f"TXID: {pay.txid}"))
        # Best-effort on-chain read (free): TON via toncenter, USDT via a public BSC RPC node.
        chk = await _payments.deposit_check(session, pay)
        lines.append(f"🔍 وضعیت شبکه: {_network_status_fa(chk)}")
    return "\n".join(lines)


# Telegram's hard cap for photo captions. A «پرداخت همهٔ بدهی» review with many invoices
# (plus rtl()'s invisible bidi marks, which count) can exceed it — send_photo then raises
# and, before H04, the owner received NOTHING for a pending payment.
_TG_CAPTION_MAX = 1024
_TG_MESSAGE_MAX = 4096


def _split_caption(full: str) -> tuple[str, str | None]:
    """Fit `full` into a photo caption: returns (caption, follow_up_text_or_None). Cuts at a
    line boundary (rtl() isolates and the review's HTML tags never span lines, so a newline
    cut can't break either), with a defensive unbalanced-<a> strip."""
    if len(full) <= _TG_CAPTION_MAX:
        return full, None
    cut = full.rfind("\n", 0, _TG_CAPTION_MAX - 2)
    if cut < 100:  # no usable newline — hard cut (defensive; reviews are line-structured)
        cut = _TG_CAPTION_MAX - 2
    caption = full[:cut].rstrip()
    if caption.count("<a") > caption.count("</a>"):
        caption = caption[: caption.rfind("<a")].rstrip()
    return caption + "\n…", full[:_TG_MESSAGE_MAX]


async def send_owner_review(
    session,
    bot,
    *,
    intro: str,
    review_html: str,
    photo=None,  # Telegram file_id (str) or FSInputFile; None → text only
    reply_markup=None,
) -> bool:
    """The ONE delivery path for owner payment reviews (new proof intake + the in-bot view).

    Guarantees the owner can never miss a pending payment (a pending payment also freezes
    dunning for its invoices): photo captions are truncated to Telegram's 1024-char cap at a
    line boundary — with the FULL review following as a second message — and ANY photo-send
    failure falls back to the text review (the old code keyed that fallback on the DISK-save
    flag, so a saved-but-unforwardable photo notified nobody). Returns True if anything was
    delivered."""
    owner_chat = str(await settings_service.get(session, "owner_chat_id", "") or "").strip()
    if not owner_chat:
        return False
    chat_id = int(owner_chat)
    full = rtl((intro + "\n\n" if intro else "") + review_html)
    if photo is not None:
        caption, follow_up = _split_caption(full)
        try:
            await bot.send_photo(
                chat_id, photo, caption=caption, parse_mode="HTML", reply_markup=reply_markup,
            )
            if follow_up is not None:
                await bot.send_message(
                    chat_id, follow_up, parse_mode="HTML", disable_web_page_preview=True,
                )
            return True
        except Exception:  # noqa: BLE001 — the text fallback below must always fire
            log.warning("owner review photo send failed; falling back to text", exc_info=True)
            full = (full + "\n\n" + rtl("(ارسال تصویرِ رسید ناموفق بود؛ آن را در پنل ببینید.)"))[
                :_TG_MESSAGE_MAX
            ]
    try:
        await bot.send_message(
            chat_id, full[:_TG_MESSAGE_MAX], parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=reply_markup,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("owner review text send failed", exc_info=True)
        return False


async def _handle_txid(
    message: Message,
    session,
    txid: str,
    *,
    invoices: list[Invoice] | None,
    chain: str = "bsc",
    from_user=None,  # noqa: ANN001 — when called from a callback, cb.message.from_user is the bot
) -> None:
    """Record a submitted tx hash (USDT/BSC, TON, or AVAX) as a PENDING payment for MANUAL review —
    no on-chain auto-verify. The owner opens the clickable explorer link in the panel and
    confirms/rejects. A payment may cover several invoices (one transfer for several debts)."""
    from app.services import payments

    actor = from_user or message.from_user
    resellers = await _resellers_for_chat(session, actor.id)
    if not resellers:
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Shared validation + creation (identical rules on the bot and the web portal).
    result = await payments.submit_reseller_payment(
        session, reseller_ids={r.id for r in resellers},
        invoice_ids=[i.id for i in (invoices or [])], txid=txid, chain=chain,
    )
    await message.answer(rtl(result.user_message))
    if result.notify and result.payment is not None:
        # Attribute the review to the reseller row the payment actually belongs to — a
        # multi-panel customer's resellers[0] can be a DIFFERENT row (wrong name shown).
        primary = next(
            (r for r in resellers if r.id == result.payment.reseller_id), resellers[0])
        review = await _payment_review_html(
            session, result.payment, reseller=primary, invs=result.invoices)
        await owner_notify.notify_owner(
            session, result.owner_intro + "\n\n" + review,
            html=True, reply_markup=keyboards.owner_payment_detail_keyboard(result.payment.id))


async def _handle_payment_proof(
    message: Message, session, *, invoices: list[Invoice] | None
) -> None:
    """A reseller sent a deposit screenshot as proof of payment. Store it, link it to the
    exact invoice(s) chosen in «پرداخت فاکتور» as a PENDING payment, and forward it to the
    owner for manual confirmation. A payment may cover several invoices."""
    from app.services import payments

    resellers = await _resellers_for_chat(session, message.from_user.id)
    if not resellers:
        # Not a registered reseller → can't attribute the payment.
        await message.answer(await texts.render(session, "tpl_link_not_found"))
        return
    # Fetch the image BEFORE creating the payment, mirroring the portal path: a pending row with
    # no proof blocks the reseller's own retry (one pending payment per invoice), so a download
    # failure must not leave one behind. Held in memory — Telegram photos are a few hundred KB.
    photo = message.photo[-1]
    buf = io.BytesIO()
    try:
        await message.bot.download(photo, destination=buf)
    except Exception:  # noqa: BLE001 — network/Telegram hiccup; nothing recorded, retry is clean
        log.warning("failed to download payment proof photo", exc_info=True)
        await message.answer(rtl(
            "دریافت تصویرِ رسید ناموفق بود؛ پرداختی ثبت نشد. لطفاً دوباره ارسال کنید."
        ))
        return

    # Shared validation + creation (identical rules on the bot and the web portal).
    result = await payments.submit_reseller_payment(
        session, reseller_ids={r.id for r in resellers},
        invoice_ids=[i.id for i in (invoices or [])], screenshot=True,
    )
    if result.status != "ok" or result.payment is None:
        await message.answer(rtl(result.user_message))
        return
    payment = result.payment
    result_invoices = result.invoices

    # Persist the already-downloaded bytes. If even this fails the owner still gets the photo
    # below (Telegram hosts it by file_id), so the payment stays reviewable and we do NOT ask for
    # a resend — the row is intact and legitimate, only the local copy is missing.
    proof_dir = "data/payment_proofs"
    proof_path = f"{proof_dir}/payment_{payment.id}.jpg"
    try:
        os.makedirs(proof_dir, exist_ok=True)
        with open(proof_path, "wb") as fh:
            fh.write(buf.getvalue())
        payment.proof_path = proof_path
        await session.commit()
    except OSError:
        log.warning("failed to save payment proof for payment %s", payment.id, exc_info=True)
        await session.rollback()

    await message.answer(rtl(result.user_message))

    # Forward the screenshot to the owner so they can confirm from Telegram + the panel.
    # send_owner_review handles the >1024-char caption case and ALWAYS falls back to the
    # text review when the photo can't be sent (the old inline code only fell back when the
    # DISK save had failed — a saved file + failed forward notified nobody).
    primary = next((r for r in resellers if r.id == payment.reseller_id), resellers[0])
    review = await _payment_review_html(
        session, payment, reseller=primary, invs=result_invoices)
    await send_owner_review(
        session, message.bot,
        intro="🧾 رسید پرداخت جدید — منتظر تأیید شماست.",
        review_html=review,
        photo=photo.file_id,
        reply_markup=keyboards.owner_payment_detail_keyboard(payment.id),
    )
