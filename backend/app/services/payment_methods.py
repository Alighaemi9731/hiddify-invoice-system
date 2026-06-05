"""
Which payment methods are offered to resellers, and the human text for them.

Each method has an on/off setting; the owner controls which appear on the invoice
message/PDF and in the bot's «پرداخت» view. One source of truth so the bot, the invoice
caption, and the PDF stay consistent.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service

_KEYS = [
    "pay_usdt_enabled", "pay_screenshot_enabled", "pay_card_enabled",
    "usdt_bep20_address", "card_number", "card_holder_name",
]


@dataclass
class PaymentOptions:
    usdt: bool
    screenshot: bool
    card: bool
    wallet: str
    card_number: str
    card_holder: str

    @property
    def any_enabled(self) -> bool:
        return self.usdt or self.screenshot or self.card


async def load_options(session: AsyncSession) -> PaymentOptions:
    cfg = await settings_service.get_many(session, _KEYS)
    wallet = (cfg.get("usdt_bep20_address") or "").strip()
    card = (cfg.get("card_number") or "").strip()
    return PaymentOptions(
        # A method is only really available if its data is present (a wallet / a card
        # number), so an enabled-but-unconfigured method is silently skipped rather than
        # telling the reseller to pay to nowhere.
        usdt=bool(cfg.get("pay_usdt_enabled")) and bool(wallet),
        screenshot=bool(cfg.get("pay_screenshot_enabled")),
        card=bool(cfg.get("pay_card_enabled")) and bool(card),
        wallet=wallet,
        card_number=card,
        card_holder=(cfg.get("card_holder_name") or "").strip(),
    )


def instructions_text(
    opts: PaymentOptions, *, amount_usdt: str | None = None, amount_toman: str | None = None,
    html: bool = False,
) -> str:
    """Multi-line payment instructions for the enabled methods.

    `amount_usdt` / `amount_toman` are pre-formatted figures. USDT payers see the USDT amount
    (with the Toman equivalent in parentheses); card-to-card payers see the **Toman** amount —
    Iranian customers pay and think in Toman, so the card block leads with it.

    `html=True` renders the wallet address / card number inside <code>…</code> so the reseller
    can TAP to copy them (the message must then be sent with parse_mode="HTML"). `html=False`
    keeps the plain form: a leading right-to-left mark (‏ U+200F) so a value that starts with a
    Latin/hex/digit char still right-aligns its line instead of jumbling the message."""
    import html as _html

    rtl = "‏"

    def copyable(value: str) -> str:
        return f"<code>{_html.escape(str(value))}</code>" if html else f"{rtl}{value}"

    blocks: list[str] = []
    if opts.usdt:
        b = ["💳 پرداخت با USDT (فقط شبکهٔ BEP-20):"]
        if amount_usdt:
            line = f"مبلغ: {amount_usdt} USDT"
            if amount_toman:
                line += f" (≈ {amount_toman} تومان)"
            b.append(line)
        b.append(f"آدرس کیف پول:\n{copyable(opts.wallet)}")
        b.append(
            "⚠️ توجه: فقط در شبکهٔ BEP-20 (BSC) واریز کنید. ارسال از شبکه‌های دیگر "
            "(مثل TRC20 یا ERC20) باعث از‌دست‌رفتن وجه می‌شود."
        )
        b.append("پس از واریز، شناسهٔ تراکنش (TXID) را همین‌جا ارسال کنید.")
        blocks.append("\n".join(b))
    if opts.card:
        b = ["🏦 کارت‌به‌کارت (پرداخت تومانی):"]
        if amount_toman:
            b.append(f"مبلغ: {amount_toman} تومان")
        b.append(f"شماره کارت:\n{copyable(opts.card_number)}")
        if opts.card_holder:
            holder = _html.escape(opts.card_holder) if html else f"{rtl}{opts.card_holder}"
            b.append(f"به نام: {holder}")
        b.append("پس از واریز، تصویر رسید را همین‌جا ارسال کنید.")
        blocks.append("\n".join(b))
    elif opts.screenshot:
        # Screenshot as a standalone option only matters when card isn't already asking
        # for a receipt photo; otherwise it's implied above.
        blocks.append("🧾 یا تصویر رسید واریز خود را همین‌جا ارسال کنید تا بررسی شود.")
    if not blocks:
        return "برای هماهنگی پرداخت با پشتیبانی در تماس باشید."
    tail = "\n\n👆 برای کپی، روی آدرس کیف پول یا شمارهٔ کارت ضربه بزنید." if html and (opts.usdt or opts.card) else ""
    return "\n\n".join(blocks) + tail
