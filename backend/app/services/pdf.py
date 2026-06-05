"""
Persian / RTL invoice PDF generation (reportlab + arabic-reshaper + python-bidi).

Designed to be robust against odd reseller names (mixed Persian/Latin/digits, emoji,
zero-width chars) so nothing renders as tofu boxes or reversed text.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

FONT = "Vazir"
BOLD = "Vazir-Bold"
FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

# Brand palette
INK = colors.HexColor("#0f172a")
PRIMARY = colors.HexColor("#1f3b73")
PRIMARY_LT = colors.HexColor("#eef2fb")
ACCENT = colors.HexColor("#f29f05")
MUTED = colors.HexColor("#64748b")
LINE = colors.HexColor("#e2e8f0")
STRIPE = colors.HexColor("#f8fafc")
GREEN = colors.HexColor("#15803d")

_FONTS_READY: bool | None = None

# Strip emoji / pictographs the PDF font can't draw (keeps Persian, Latin, digits).
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U00002190-\U000021FF\U00002B00-\U00002BFF]+"
)
_ZW = re.compile("[​‌‍⁠﻿]")


def _register_fonts() -> bool:
    """Register Vazirmatn (preferred) or fall back to DejaVuSans / Helvetica."""
    global _FONTS_READY
    if _FONTS_READY is not None:
        return _FONTS_READY
    candidates = [
        (FONT, FONTS_DIR / "Vazirmatn-Regular.ttf", BOLD, FONTS_DIR / "Vazirmatn-Bold.ttf"),
    ]
    for reg_name, reg_path, bold_name, bold_path in candidates:
        if reg_path.exists():
            try:
                pdfmetrics.registerFont(TTFont(reg_name, str(reg_path)))
                pdfmetrics.registerFont(TTFont(bold_name, str(bold_path if bold_path.exists() else reg_path)))
                _FONTS_READY = True
                return True
            except Exception:  # noqa: BLE001
                pass
    # Fallback to the bundled DejaVuSans (Persian-capable) for both weights.
    dejavu = FONTS_DIR / "DejaVuSans.ttf"
    try:
        pdfmetrics.registerFont(TTFont(FONT, str(dejavu)))
        pdfmetrics.registerFont(TTFont(BOLD, str(dejavu)))
        _FONTS_READY = True
    except Exception:  # noqa: BLE001
        _FONTS_READY = False
    return _FONTS_READY


def _clean(text: object) -> str:
    s = str(text if text is not None else "")
    s = _EMOJI.sub("", s)
    s = _ZW.sub("", s)
    return s.strip()


def rtl(text: object) -> str:
    """Shape + bidi-reorder text for correct RTL rendering (safe on mixed scripts)."""
    s = _clean(text)
    if not s:
        return ""
    try:
        return get_display(arabic_reshaper.reshape(s))
    except Exception:  # noqa: BLE001
        return s


def _fa_digits(s: str) -> str:
    return s.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def money(n: float) -> str:
    return _fa_digits(f"{round(n):,}")


def gb(n: float) -> str:
    return _fa_digits(f"{round(n):,}")


def _font_or_default(name: str) -> str:
    return name if _register_fonts() else "Helvetica"


def build_invoice_pdf(
    output_path: str,
    *,
    reseller_name: str,
    panel_label: str,
    period_label: str,
    period_start: dt.date,
    period_end: dt.date,
    lines: list[dict],
    total_gb: float,
    price_per_gb: int,
    amount_toman: float,
    amount_usdt: float,
    usdt_rate: int,
    wallet_address: str = "",
    card_number: str = "",
    card_holder: str = "",
    base_amount_toman: float | None = None,
    min_sale_toman: int = 0,
    floor_applied: bool = False,
    owner_name: str = "",
    issued_at: dt.date | None = None,
    invoice_title: str = "فاکتور",
) -> str:
    # NOTE: the money args (price_per_gb, amount_toman, amount_usdt, usdt_rate,
    # wallet_address, card_number, card_holder, base_amount_toman, min_sale_toman,
    # floor_applied) are accepted for backward-compat but NO LONGER rendered — the PDF is a
    # volume-only usage report. Pricing/payment is shown in the bot's text message instead.
    reg = _font_or_default(FONT)
    bold = _font_or_default(BOLD)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    issued = issued_at or dt.date.today()

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=16 * mm, bottomMargin=16 * mm, leftMargin=14 * mm, rightMargin=14 * mm,
        title=f"factor-{period_label}",
    )

    P = lambda t, **k: ParagraphStyle("x", fontName=reg, **k)  # noqa: E731
    rightMuted = P("rm", fontSize=9, alignment=2, textColor=MUTED, leading=14)
    val = P("v", fontSize=10, alignment=2, textColor=INK, leading=16)
    cellC = ParagraphStyle("cc", fontName=reg, fontSize=9, alignment=1, textColor=INK, leading=14)
    th = ParagraphStyle("th", fontName=bold, fontSize=9.5, alignment=1, textColor=colors.white, leading=14)

    elems: list = []

    # ---------- header band ----------
    # Title + period as ONE right-aligned paragraph (two lines via <br/>) so they
    # never overlap. rtl() output is already shaped, so wrapping <font> tags is safe.
    title_style = ParagraphStyle("t", fontName=bold, fontSize=19, textColor=colors.white,
                                 alignment=2, leading=26)
    title_html = (
        f"{rtl(invoice_title or 'فاکتور')}"
        f"<br/><font size='10' color='#cdd7ee'>{rtl('دوره ' + _fa_digits(period_label))}</font>"
    )
    title = Paragraph(title_html, title_style)
    brand = Paragraph(rtl(owner_name or "سامانه مدیریت نمایندگان"),
                      ParagraphStyle("b", fontName=bold, fontSize=12, textColor=colors.white,
                                     alignment=0, leading=18))
    header = Table([[brand, title]], colWidths=[60 * mm, 122 * mm])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
    ]))
    elems += [header, Spacer(1, 12)]

    # ---------- meta grid (two columns of label/value) ----------
    # Dates are pure LTR — render them left-aligned WITHOUT bidi reshaping so the
    # second date never flips/garbles. The label stays RTL on the right.
    ltr_val = ParagraphStyle("lv", fontName=reg, fontSize=10, alignment=0, textColor=INK, leading=16)

    def kv(label, value, vstyle=val):
        return [Paragraph(rtl(value) if isinstance(value, str) else value, vstyle),
                Paragraph(rtl(label), rightMuted)]

    def kv_ltr(label, value):  # value rendered as raw LTR (dates)
        return [Paragraph(value, ltr_val), Paragraph(rtl(label), rightMuted)]

    date_range = _fa_digits(f"{period_start:%Y-%m-%d}  ←  {period_end:%Y-%m-%d}")
    meta = Table(
        [
            kv("نماینده", reseller_name, ParagraphStyle("vn", fontName=bold, fontSize=11, alignment=2, textColor=PRIMARY)) +
            kv("پنل", panel_label),
            kv_ltr("تاریخ صدور", _fa_digits(issued.strftime("%Y-%m-%d"))) +
            kv_ltr("بازه دوره", date_range),
        ],
        colWidths=[46 * mm, 45 * mm, 46 * mm, 45 * mm],
    )
    meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRIMARY_LT),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elems += [meta, Spacer(1, 14)]

    # ---------- line items ----------
    # Monospace style so the UUID slice has equal-width characters across rows.
    mono = ParagraphStyle("mono", fontName="Courier", fontSize=8, alignment=1,
                          textColor=MUTED, leading=12)
    show_sub = any((l.get("sub_reseller_name") or "") != reseller_name for l in lines)

    head = [Paragraph(rtl("ردیف"), th), Paragraph(rtl("نام سرویس"), th), Paragraph(rtl("شناسه"), th)]
    if show_sub:
        widths = [11 * mm, 40 * mm, 30 * mm, 38 * mm, 30 * mm, 22 * mm]
        head.append(Paragraph(rtl("زیرمجموعه"), th))
    else:
        widths = [12 * mm, 56 * mm, 32 * mm, 42 * mm, 28 * mm]
    head += [Paragraph(rtl("تاریخ ساخت"), th), Paragraph(rtl("حجم (گیگ)"), th)]

    def uuid_slice(u: str) -> str:
        # First 8 chars of the uuid — fixed width, enough to disambiguate same-name users.
        return (u or "").replace("-", "")[:8].upper() or "—"

    data = [head]
    for idx, l in enumerate(lines, 1):
        row = [Paragraph(_fa_digits(str(idx)), cellC),
               Paragraph(rtl(l.get("name", "")), cellC),
               Paragraph(uuid_slice(l.get("uuid", "")), mono)]
        if show_sub:
            row.append(Paragraph(rtl(l.get("sub_reseller_name", "")), cellC))
        sd = l.get("start_date")
        row += [Paragraph(_fa_digits(str(sd)) if sd else "—", cellC),
                Paragraph(gb(float(l.get("usage_gb", 0))), cellC)]
        data.append(row)

    if not lines:
        span = len(head)
        data.append([Paragraph(rtl("در این دوره سرویسی ثبت نشده است"), cellC)] + [""] * (span - 1))

    table = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    if not lines:
        style.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(style))
    elems += [table, Spacer(1, 12)]

    # ---------- total GB summary (volume only — NO prices/amounts on the PDF) ----------
    # The invoice PDF is a usage report: it shows ONLY how much volume was created and by
    # whom. Money (price, total Toman/USDT, payment instructions) lives in the bot's text
    # message, not on the document — so a reseller can hand the PDF to their sub-reseller
    # without exposing the prices configured in the owner's settings.
    grand = Table(
        [[Paragraph(rtl(f"{gb(total_gb)} گیگ"),
                    ParagraphStyle("g", fontName=bold, fontSize=15, alignment=2, textColor=colors.white)),
          Paragraph(rtl("مجموع حجم"),
                    ParagraphStyle("gl", fontName=bold, fontSize=11, alignment=2, textColor=colors.white))],
         [Paragraph(rtl(f"{_fa_digits(str(len(lines)))} سرویس"),
                    ParagraphStyle("gc", fontName=reg, fontSize=9.5, alignment=2, textColor=colors.HexColor("#dbe6fb"))),
          Paragraph(rtl("تعداد سرویس‌های جدید"),
                    ParagraphStyle("gcl", fontName=reg, fontSize=9, alignment=2, textColor=colors.HexColor("#dbe6fb")))]],
        colWidths=[60 * mm, 55 * mm],
    )
    grand.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
    ]))
    # Right-align the summary block on the page.
    summary_block = Table([["", grand]], colWidths=[72 * mm, 115 * mm])
    summary_block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elems += [summary_block, Spacer(1, 14)]

    elems.append(Paragraph(
        rtl(f"این گزارش به‌صورت خودکار در تاریخ {_fa_digits(issued.strftime('%Y-%m-%d'))} صادر شده است."),
        ParagraphStyle("f", fontName=reg, fontSize=8, alignment=1, textColor=MUTED)))

    doc.build(elems)
    return output_path
