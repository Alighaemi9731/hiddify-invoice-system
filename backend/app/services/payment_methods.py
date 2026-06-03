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


def instructions_text(opts: PaymentOptions, *, amount_usdt: str | None = None) -> str:
    """Multi-line payment instructions for the enabled methods (Telegram / PDF caption)."""
    blocks: list[str] = []
    if opts.usdt:
        b = ["💳 پرداخت با USDT (شبکه BEP-20):"]
        if amount_usdt:
            b.append(f"مبلغ: {amount_usdt} USDT")
        b.append(f"آدرس کیف پول:\n{opts.wallet}")
        b.append("پس از واریز، شناسهٔ تراکنش (TXID) را همین‌جا ارسال کنید.")
        blocks.append("\n".join(b))
    if opts.card:
        b = ["🏦 کارت‌به‌کارت:", f"شماره کارت:\n{opts.card_number}"]
        if opts.card_holder:
            b.append(f"به نام: {opts.card_holder}")
        b.append("پس از واریز، تصویر رسید را همین‌جا ارسال کنید.")
        blocks.append("\n".join(b))
    elif opts.screenshot:
        # Screenshot as a standalone option only matters when card isn't already asking
        # for a receipt photo; otherwise it's implied above.
        blocks.append("🧾 یا تصویر رسید واریز خود را همین‌جا ارسال کنید تا بررسی شود.")
    if not blocks:
        return "برای هماهنگی پرداخت با پشتیبانی در تماس باشید."
    return "\n\n".join(blocks)
