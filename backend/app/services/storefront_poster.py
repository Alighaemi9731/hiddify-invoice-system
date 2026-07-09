"""One-tap shareable shop poster (PNG): shop name + QR code + the t.me bot link.

The storefront admin taps «🖼 پوستر فروشگاه» and gets a clean branded image to post in a story,
a group, or print. Reuses `usercreate.qr_png` for the QR and the bundled Vazirmatn fonts + `pdf.rtl`
(PIL cannot shape Persian on its own) for the text. Pure/synchronous — the caller runs it off-loop.
"""
from __future__ import annotations

import io

from app.services import pdf as pdf_service
from app.services.usercreate import qr_png

# Brand RGB (mirrors the reportlab palette in app/services/pdf.py).
_PRIMARY = (31, 59, 115)
_INK = (15, 23, 42)
_SUBTLE = (205, 215, 238)
_LIGHT = (238, 242, 251)
_WHITE = (255, 255, 255)


def _font(size: int, *, bold: bool = True):  # noqa: ANN202
    from PIL import ImageFont

    name = "Vazirmatn-Bold.ttf" if bold else "Vazirmatn-Regular.ttf"
    try:
        return ImageFont.truetype(str(pdf_service.FONTS_DIR / name), size)
    except Exception:  # noqa: BLE001 — fall back to whatever PIL ships
        return ImageFont.load_default()


def _center(d, cx: float, y: float, text: str, font, fill) -> None:  # noqa: ANN001
    w = d.textlength(text, font=font)
    d.text((cx - w / 2, y), text, font=font, fill=fill)


def build_poster_png(shop_name: str, bot_username: str) -> bytes:
    """Compose the poster and return PNG bytes. `bot_username` (no @) drives both the QR target and
    the printed link."""
    from PIL import Image, ImageDraw

    W, H = 820, 1000
    band_h = 190
    username = (bot_username or "").lstrip("@")
    link = f"t.me/{username}"

    img = Image.new("RGB", (W, H), _WHITE)
    d = ImageDraw.Draw(img)

    # top brand band + shop name (Persian shaped for PIL)
    d.rectangle([0, 0, W, band_h], fill=_PRIMARY)
    _center(d, W / 2, 60, pdf_service.rtl(shop_name or "فروشگاه"), _font(46), _WHITE)
    _center(d, W / 2, 128, pdf_service.rtl("فروشگاهِ اینترنتِ پرسرعت"), _font(22, bold=False), _SUBTLE)

    # QR on a white rounded card
    qr = Image.open(io.BytesIO(qr_png(f"https://t.me/{username}"))).convert("RGB")
    qr_size = 470
    qr = qr.resize((qr_size, qr_size))
    pad = 28
    cx0 = (W - qr_size) / 2 - pad
    cy0 = band_h + 66
    d.rounded_rectangle(
        [cx0, cy0, cx0 + qr_size + 2 * pad, cy0 + qr_size + 2 * pad],
        radius=28, fill=_WHITE, outline=_LIGHT, width=3)
    img.paste(qr, (int((W - qr_size) / 2), int(cy0 + pad)))

    # caption + link pill
    cap_y = cy0 + qr_size + 2 * pad + 26
    _center(d, W / 2, cap_y, pdf_service.rtl("برای خرید، اسکن کنید"), _font(30), _INK)

    lf = _font(30)
    lw = d.textlength(link, font=lf)
    pill_w, pill_h = lw + 64, 64
    px0, py0 = (W - pill_w) / 2, cap_y + 54
    d.rounded_rectangle([px0, py0, px0 + pill_w, py0 + pill_h], radius=32, fill=_PRIMARY)
    d.text((px0 + 32, py0 + 15), link, font=lf, fill=_WHITE)  # LTR ascii — no shaping needed

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
