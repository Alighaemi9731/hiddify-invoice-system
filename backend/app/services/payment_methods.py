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
    "pay_usdt_enabled", "pay_screenshot_enabled", "pay_card_enabled", "pay_ton_enabled",
    "pay_avax_enabled", "usdt_bep20_address", "card_number", "card_holder_name",
    "ton_wallet_address", "avax_address",
]


@dataclass
class PaymentOptions:
    usdt: bool
    screenshot: bool
    card: bool
    ton: bool
    avax: bool
    wallet: str
    card_number: str
    card_holder: str
    ton_address: str
    avax_address: str

    @property
    def any_enabled(self) -> bool:
        return self.usdt or self.screenshot or self.card or self.ton or self.avax


async def load_options(session: AsyncSession) -> PaymentOptions:
    cfg = await settings_service.get_many(session, _KEYS)
    wallet = (cfg.get("usdt_bep20_address") or "").strip()
    card = (cfg.get("card_number") or "").strip()
    ton = (cfg.get("ton_wallet_address") or "").strip()
    avax = (cfg.get("avax_address") or "").strip()
    return PaymentOptions(
        # A method is only really available if its data is present (a wallet / a card
        # number / a TON or AVAX address), so an enabled-but-unconfigured method is silently
        # skipped rather than telling the reseller to pay to nowhere.
        usdt=bool(cfg.get("pay_usdt_enabled")) and bool(wallet),
        screenshot=bool(cfg.get("pay_screenshot_enabled")),
        card=bool(cfg.get("pay_card_enabled")) and bool(card),
        ton=bool(cfg.get("pay_ton_enabled")) and bool(ton),
        avax=bool(cfg.get("pay_avax_enabled")) and bool(avax),
        wallet=wallet,
        card_number=card,
        card_holder=(cfg.get("card_holder_name") or "").strip(),
        ton_address=ton,
        avax_address=avax,
    )


def instructions_text(
    opts: PaymentOptions, *, amount_usdt: str | None = None, amount_toman: str | None = None,
    amount_ton: str | None = None, amount_avax: str | None = None, html: bool = False,
) -> str:
    """Multi-line payment instructions for the enabled methods.

    `amount_usdt` / `amount_toman` are pre-formatted figures. USDT payers see the USDT amount
    (with the Toman equivalent in parentheses); card-to-card payers see the **Toman** amount —
    Iranian customers pay and think in Toman, so the card block leads with it.

    The payable amount is shown ONCE in the invoice header (Toman). Each method block here adds
    only what's specific to it — its OWN-unit amount (USDT/TON), the address/card, and a network
    warning — so the Toman figure isn't repeated per method. Card pays the header Toman amount,
    so its block carries no amount.

    `html=True` wraps copyable values (wallet/card/TON address) in <code>…</code> for tap-to-copy
    (send with parse_mode="HTML"). `html=False` prepends an RLM (‏ U+200F) so a value starting
    with a Latin/hex/digit char keeps its line right-aligned."""
    import html as _html

    rtl = "‏"

    def copyable(value: str) -> str:
        # 📋 cues "tap to copy"; the <code>/value itself is what gets copied.
        return f"📋 <code>{_html.escape(str(value))}</code>" if html else f"📋 {rtl}{value}"

    blocks: list[str] = []
    if opts.usdt:
        b = ["🟢 USDT (شبکهٔ BEP-20):"]
        if amount_usdt:
            b.append(f"💵 مبلغ: {amount_usdt} USDT")
        b.append(copyable(opts.wallet))
        b.append("⚠️ لطفاً فقط از طریق شبکهٔ BEP-20 (BSC) واریز کنید؛ واریز از شبکه‌های دیگر موجب از دست رفتن وجه می‌شود.")
        blocks.append("\n".join(b))
    if opts.card:
        b = ["🏦 کارت‌به‌کارت (همین مبلغ به تومان):"]
        b.append(copyable(opts.card_number))
        if opts.card_holder:
            holder = _html.escape(opts.card_holder) if html else f"{rtl}{opts.card_holder}"
            b.append(f"👤 به نام: {holder}")
        blocks.append("\n".join(b))
    if opts.ton:
        # GRAM (ex-Toncoin). The coin/ticker is Gram now; the network is still TON.
        b = ["💎 گرام (GRAM):"]
        if amount_ton:
            b.append(f"💎 مبلغ: {amount_ton} GRAM")
        elif amount_toman:
            # No live Gram rate → tell them to send the Toman equivalent (header amount).
            b.append(f"💎 معادلِ {amount_toman} تومان به GRAM")
        b.append(copyable(opts.ton_address))
        b.append("⚠️ لطفاً فقط روی شبکهٔ TON واریز کنید.")
        blocks.append("\n".join(b))
    if opts.avax:
        b = ["❄️ اوالانچ (AVAX):"]
        if amount_avax:
            b.append(f"❄️ مبلغ: {amount_avax} AVAX")
        elif amount_toman:
            # No live AVAX rate → tell them to send the Toman equivalent (header amount).
            b.append(f"❄️ معادلِ {amount_toman} تومان به AVAX")
        b.append(copyable(opts.avax_address))
        b.append("⚠️ لطفاً فقط روی شبکهٔ Avalanche C-Chain واریز کنید.")
        blocks.append("\n".join(b))
    if opts.screenshot and not (opts.card or opts.ton or opts.avax):
        # Standalone "send a receipt photo" note — useful next to USDT, redundant when card/TON
        # already ask for a photo.
        blocks.append("🧾 یا می‌توانید تصویر رسید واریز را همین‌جا ارسال کنید.")
    if not blocks:
        return "لطفاً برای هماهنگی پرداخت با پشتیبانی در تماس باشید."

    out = ["💳 لطفاً از یکی از روش‌های زیر پرداخت کنید:", "\n\n".join(blocks)]
    # Only ever shown AFTER the customer taps the «💳 پرداخت فاکتور» button (the locked pay flow);
    # the sent invoice no longer embeds payment instructions. → tell them to submit the proof now.
    txid_m = opts.usdt or opts.ton or opts.avax
    photo_m = opts.card or opts.screenshot
    if txid_m and photo_m:
        out.append("📩 پس از واریز، برای رمزارز «شناسهٔ تراکنش (TXID)» و برای کارت‌به‌کارت «تصویر رسید» را همین‌جا ارسال کنید.")
    elif txid_m:
        out.append("📩 پس از واریز، «شناسهٔ تراکنش (TXID)» را همین‌جا ارسال کنید (لینکِ تراکنش نیز پذیرفته می‌شود).")
    else:
        out.append("📩 پس از واریز، «تصویر رسید» را همین‌جا ارسال کنید.")
    if html and (opts.usdt or opts.card or opts.ton or opts.avax):
        out.append("👆 برای کپی، روی آدرس یا شمارهٔ کارت ضربه بزنید.")
    return "\n\n".join(out)
