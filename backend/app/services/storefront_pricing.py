"""The single source of truth for «a storefront plan must never be sold below the reseller's
own cost».

A reseller buys quota from us at `reseller.price_per_gb` (or the global `default_price_per_gb`)
and resells it through their storefront bot. Nothing used to check that the retail price covered
that cost, and the overwhelmingly common failure was a UNIT error: the reseller typed `50` meaning
50,000 Toman, so a 10 GB plan sold for 50 T against a 20,000 T cost. One shop lost ~1.6M T in a
month that way; 34 shops were affected in total.

Deliberately a standalone module rather than a check inside `storefront_admin._validate_plan`:

* `_validate_plan` is pure/sync/session-free and encodes STATIC shape bounds (a 422 that never
  changes). This is live economic policy that needs a session and can change the moment the owner
  edits `default_price_per_gb`.
* `storefront_admin.set_plan_enabled` never calls `_validate_plan` at all, yet must be gated.
* `storefront_subscription` and `storefront_autorenew` need the same rule (a plan disabled for
  being below cost still renewed forever otherwise) without importing the admin command layer.
"""
from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reseller
from app.services import pricing

Action = Literal["save", "enable"]


def cost_per_gb_with_default(reseller: Reseller, default_price_per_gb: int) -> int:
    """The pure rule, for callers that already hold the global default.

    Split out so a list endpoint can read the default ONCE and still apply the identical rule to
    every shop, instead of re-deriving it per row or duplicating the expression.
    """
    return int(reseller.price_per_gb or default_price_per_gb)


async def cost_per_gb(session: AsyncSession, reseller: Reseller) -> int:
    """What one GB costs THIS reseller — their own buy price from us.

    Mirrors the established `effective_price_per_gb` idiom (`app/api/resellers.py`,
    `app/bot/handlers/owner.py`, `app/services/invoice_engine.py`), INCLUDING its known quirk that
    a `price_per_gb` of 0 falls through to the global default. Kept identical on purpose: the guard
    must agree with what the reseller is actually invoiced, quirk and all.
    """
    return cost_per_gb_with_default(reseller, await pricing.get_default_price_per_gb(session))


def price_floor(cost: int, gb: int) -> int:
    """The minimum legal retail price for a `gb` plan: what that quota costs the reseller.

    This is a conservative LOWER bound on their true cost — `min_sale_toman` (a whole-invoice
    monthly floor) can push the real figure higher, but that is not attributable per plan.
    """
    return max(0, int(cost)) * max(0, int(gb))


def is_below_cost(*, cost: int, gb: int, price_toman: int) -> bool:
    """True when this plan would be sold at a loss. `price == floor` is ALLOWED — selling at zero
    margin is the reseller's call. A zero floor disables the guard entirely (no cost configured)."""
    floor = price_floor(cost, gb)
    return floor > 0 and int(price_toman) < floor


def suggested_price(*, cost: int, gb: int, price_toman: int) -> int | None:
    """The number the reseller probably MEANT, when the entered price looks like a ×1000 unit slip
    (typed 50 for 50,000). None when ×1000 still would not clear the floor, so we never suggest a
    number that would just be rejected again."""
    floor = price_floor(cost, gb)
    price = int(price_toman)
    if price > 0 and price * 1000 >= floor:
        return price * 1000
    return None


def below_cost_message_fa(
    *, cost: int, gb: int, price_toman: int, action: Action = "save"
) -> str:
    """The Persian rejection shown on BOTH surfaces (bot text and portal 422 detail).

    Money figures use grouped Latin digits like the rest of the bot (`handlers._toman`), but the
    numbers we want the reseller to TYPE are bare Latin digits so they can be copy-pasted straight
    into the prompt. `rtl()` bidi-isolates all of it.
    """
    floor = price_floor(cost, gb)
    head = (
        "⛔️ این پلن با قیمتِ فعلی زیرِ هزینهٔ خودِ شماست و فعال نشد. ابتدا قیمت را اصلاح کنید."
        if action == "enable"
        else "⛔️ این قیمت از هزینهٔ خودِ شما کمتر است و ذخیره نشد."
    )
    suggestion = suggested_price(cost=cost, gb=gb, price_toman=price_toman)
    hint = (
        f"اگر منظورتان {price_toman} هزار تومان بوده، عددِ درست این است: {suggestion}"
        if suggestion is not None
        else f"عددی برابر یا بیشتر از {floor} بنویسید."
    )
    return "\n".join([
        head,
        "",
        f"پلن: {gb} گیگابایت",
        f"هزینهٔ هر گیگابایت برای شما: {cost:,} تومان",
        f"کفِ قیمتِ مجاز برای این پلن: {floor:,} تومان",
        f"قیمتی که نوشتید: {int(price_toman):,} تومان",
        "",
        "⚠️ قیمت به تومان است، نه هزار تومان. اگر منظورتان ۵۰ هزار تومان بود بنویسید: 50000",
        hint,
    ])


def below_cost_body(*, cost: int, gb: int, price_toman: int) -> dict:
    """The machine-readable 422 body for the portal, so the SPA can render the field error itself."""
    return {
        "error": "below_cost",
        "cost_per_gb": int(cost),
        "gb": int(gb),
        "price_toman": int(price_toman),
        "min_price_toman": price_floor(cost, gb),
        "suggested_price_toman": suggested_price(cost=cost, gb=gb, price_toman=price_toman),
    }


def price_prompt_hint_fa(*, cost: int, gb: int) -> str:
    """The two advisory lines appended to the bot's «قیمت به تومان» prompt, so the unit is
    unmistakable BEFORE the reseller types anything."""
    floor = price_floor(cost, gb)
    lines = ["⚠️ به تومان بنویسید، نه هزار تومان. مثال: برای ۵۰ هزار تومان بنویسید 50000"]
    if floor > 0:
        lines.append(
            f"💡 این پلن {gb} گیگابایت است و برای شما {floor:,} تومان تمام می‌شود؛ "
            "قیمتِ فروش باید از این عدد کمتر نباشد."
        )
    return "\n".join(lines)
