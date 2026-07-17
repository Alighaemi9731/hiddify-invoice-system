"""Shared bot views + flow-entry helpers (no handler registration).

Helper-only blocks moved out of the original monolithic module so that domain
modules imported EARLIER in the registration order can use them without forward
imports: the create-user / storefront-setup entry helpers, the owner action
dispatcher + owner report views, the shared reseller views, and the sub-reseller
management helpers. Nothing here registers on the router.
"""
from __future__ import annotations

import html

from aiogram.fsm.context import FSMContext
from sqlalchemy import func, select

from app.bot import keyboards, texts
from app.bot.handlers.common import (
    _OWED,
    _STATUS_FA,
    StorefrontSetupState,
    _is_top_level_reseller,
    _iso,
    _resellers_for_chat,
    _top_level_resellers,
    iso_html,
    log,
)
from app.bot.rtl import rtl
from app.core.codes import payment_code
from app.models import Invoice, Panel, Payment, Reseller
from app.models.enums import EnforcementState, PaymentStatus
from app.services import settings_service
from app.services.periods import today as tehran_today


# --------------------------- create user (top-level resellers) ---------------------------
async def _begin_create_user(answer, chat_id: int, session, state: FSMContext) -> None:
    """Entry point for «ساخت کاربر»: validate eligibility, then ask for the panel (if >1) or the mode."""
    from app.services import usercreate

    await state.clear()
    opts = await usercreate.load_options(session)
    if not opts.enabled:
        await answer("ساخت کاربر در حال حاضر غیرفعال است.")
        return
    if not (opts.gb and opts.days):
        await answer("گزینه‌های حجم/روز پیکربندی نشده‌اند؛ به پشتیبانی اطلاع دهید.")
        return
    roots = await _top_level_resellers(session, chat_id)
    if not roots:
        await answer("فقط نماینده‌های اصلی می‌توانند کاربر بسازند.")
        return
    if len(roots) == 1:
        await state.update_data(cu_reseller_id=roots[0].id)
        await answer(
            f"➕ ساخت کاربر روی پنلِ «{_iso(roots[0].name)}»\nنوعِ ساخت را انتخاب کنید:",
            reply_markup=keyboards.create_user_mode_keyboard(),
        )
        return
    items = []
    for r in roots:
        panel = await session.get(Panel, r.panel_id)
        items.append((r.id, _iso(f"{(panel.name or panel.key) if panel else '?'} — {r.name}")))
    await answer("➕ ساخت کاربر\nروی کدام پنل؟", reply_markup=keyboards.create_user_panels_keyboard(items))

_BOTFATHER_GUIDE = (
    "🏪 راه‌اندازی ربات فروشگاهی\n\n"
    "۱) در تلگرام به @BotFather بروید.\n"
    "۲) دستور /newbot را بفرستید؛ یک نام و سپس یک یوزرنیم (که به bot ختم می‌شود) انتخاب کنید.\n"
    "۳) توکنی که می‌دهد (مثلِ <code>123456789:AA...</code>) را کپی کنید.\n"
    "۴) همان توکن را همین‌جا بفرستید تا رباتِ فروشگاهیِ شما ساخته و فعال شود.\n\n"
    "برای لغو، «انصراف» را بزنید."
)


async def _storefront_target_line(session, r: Reseller) -> str:  # noqa: ANN001
    """A one-liner naming which account/panel this storefront bot will sell from — always shown so the
    owner knows the target before sending the token."""
    panel = await session.get(Panel, r.panel_id)
    # HTML-escaped: this line is sent with parse_mode="HTML"; an admin name containing `<`
    # would otherwise break entity parsing and the storefront-setup prompt never sends.
    return (f"🏪 رباتِ فروشگاهی برای حسابِ «{iso_html(r.name or '—')}» روی پنلِ "
            f"«{iso_html(panel.key if panel else '?')}» راه‌اندازی می‌شود.\n\n")


async def _begin_storefront_setup(answer, chat_id: int, session, state: FSMContext) -> None:  # noqa: ANN001
    from app.services import storefront

    # One storefront bot PER PANEL (per reseller row): a person who is top-level on several panels can
    # run a separate bot for each. Re-running setup on a panel that already has a bot just replaces its
    # token (data preserved). Only top-level resellers reach here.
    roots = [
        r for r in await _top_level_resellers(session, chat_id)
        if getattr(r, "storefront_enabled", False)
    ]
    if not roots:
        await answer(rtl("این قابلیت برای شما فعال نیست."))
        return
    if len(roots) == 1:
        r = roots[0]
        target = await _storefront_target_line(session, r)
        if await storefront.get_bot_for_reseller(session, r.id) is not None:
            target += ("این پنل هم‌اکنون یک ربات دارد — توکنِ جدید جایگزین می‌شود و همهٔ داده‌ها "
                       "(پلن‌ها/مشتری‌ها/سرویس‌ها/کیفِ پول) حفظ می‌شوند.\n\n")
        await state.set_state(StorefrontSetupState.token)
        await state.update_data(sf_reseller_id=r.id)
        await answer(rtl(target + _BOTFATHER_GUIDE), parse_mode="HTML",
                     reply_markup=keyboards.cancel_keyboard("« انصراف"))
        return
    items = []
    for r in roots:
        panel = await session.get(Panel, r.panel_id)
        bot = await storefront.get_bot_for_reseller(session, r.id)
        status = f"ربات فعلی: @{bot.bot_username or '—'}" if bot is not None else "بدون ربات"
        items.append((r.id, f"{r.name or '—'} — {panel.key if panel else '?'} ({status})"))
    await answer(rtl("🏪 برای کدام حساب/پنل؟ (هر پنل یک ربات جداگانه)"),
                 reply_markup=keyboards.storefront_setup_panels_keyboard(items))

async def _dispatch_owner(action: str, answer, session) -> None:
    """Run an owner action. Shared by the menu buttons (cb_owner) AND the owner `/` commands,
    so the slash-command list and the inline menu always do the exact same thing."""
    if action == "stats":
        await _owner_stats(answer, session)
    elif action == "health":
        from app.services import owner_report

        await answer(owner_report.render_health(await owner_report.health(session)))
    elif action == "payments":
        await _owner_pending_payments(answer, session)
    elif action == "debtors":
        await _owner_debtors(answer, session)
    elif action == "broadcast":
        await answer("📢 گیرندگان پیام همگانی را انتخاب کنید:",
                     reply_markup=keyboards.broadcast_audience_keyboard())
    elif action == "sync":
        from app.services import sync as sync_service

        await answer("⏳ در حال همگام‌سازی پنل‌ها…")
        res = await sync_service.sync_all(session)
        ok = sum(1 for r in res if r.status.value == "success")
        await answer(f"🔄 همگام‌سازی انجام شد: {ok}/{len(res)} پنل موفق.")
    elif action == "backup":
        from app.services import backup_delivery

        await answer("⏳ در حال تهیهٔ پشتیبان…")
        r = await backup_delivery.send_backup_to_owner(session)
        if r.get("status") == "sent":
            await answer("🗄 پشتیبان تهیه و برای شما ارسال شد.")
        elif r.get("status") in ("no_owner_chat", "no_bot"):
            await answer(f"⚠️ پشتیبان روی سرور ذخیره شد ولی ارسال نشد ({r.get('status')}).")
        else:
            await answer("❌ ارسال پشتیبان ناموفق بود.")
    elif action == "monthly":
        from app.services import delivery, invoicing
        from app.services import sync as sync_service
        from app.services.periods import previous_month

        await answer("⏳ در حال همگام‌سازی، صدور و ارسال ماه قبل...")
        await sync_service.sync_all(session)
        p = previous_month()
        g = await invoicing.generate_invoices(session, p)
        d = await delivery.send_period(session, p.label)
        await answer(
            f"✅ دوره {p.label}: {g.created} فاکتور ساخته شد، "
            f"{d.get('sent', 0)} ارسال موفق، {d.get('unmatched', 0)} بدون ربات."
        )

async def _owner_stats(answer, session, label: str | None = None) -> None:
    """Period KPI dashboard with a month switch + a per-panel breakdown button."""
    from app.services import owner_report

    label = label or owner_report.current_period_label()
    stats = await owner_report.period_stats(session, label)
    await answer(
        owner_report.render_period_stats(stats),
        reply_markup=keyboards.owner_stats_keyboard(label, owner_report.period_choices()),
    )


async def _owner_debtors(answer, session) -> None:
    from app.services import owner_report

    rows = await owner_report.top_debtors(session, limit=10)
    if not rows:
        await answer("بدهکاری وجود ندارد.")
        return
    # Each row starts with a right-to-left mark (‏) so a line that begins with an
    # English reseller name still renders right-aligned in Telegram, and links to the card.
    lines = ["💰 بدهکاران برتر — برای کارت/اقدام روی «🔎 جستجوی نماینده» بزنید:\n"]
    for i, d in enumerate(rows, 1):
        lines.append(f"‏{i}. {_iso(d.name)}: {float(d.total):,.0f} تومان")
    await answer("\n".join(lines))


async def _owner_pending_payments(answer, session) -> None:
    """List PENDING payments as buttons → tap to see proof + confirm/reject."""
    rows = (
        await session.execute(
            select(Payment.id, Reseller.name, Payment.amount_toman,
                   Invoice.period_label, Invoice.amount_toman)
            .join(Reseller, Payment.reseller_id == Reseller.id)
            .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
            .where(Payment.status == PaymentStatus.pending)
            .order_by(Payment.created_at)
            .limit(30)
        )
    ).all()
    if not rows:
        await answer("✅ پرداختِ در انتظارِ تأییدی وجود ندارد.")
        return
    items: list[tuple[int, str]] = []
    for pid, name, toman, period, inv_toman in rows:
        # A TON/screenshot payment rarely stores its own amount; fall back to the invoice amount
        # so the list never shows a bare «—».
        shown = float(toman or 0) or float(inv_toman or 0)
        amt = f"{shown:,.0f}ت" if shown else "—"
        items.append((pid, f"#{payment_code(pid)} · {(name or '—')[:18]} · {amt} · {period or '—'}"))
    await answer(
        f"💳 {len(rows)} پرداختِ در انتظارِ تأیید:",
        reply_markup=keyboards.owner_pending_payments_keyboard(items),
    )

# --------------------------- shared reseller views ---------------------------
async def _pending_payment_for_invoice(session, invoice_id: int | None):
    """The PENDING payment whose invoice SET contains this invoice (if any) — used to block a
    duplicate submission so one invoice never sits in several pending payments. Delegates to the
    service helper so the bot and the web portal share identical rules."""
    from app.services import payments

    return await payments._pending_payment_for_invoice(session, invoice_id)


async def _pending_invoice_ids(session, reseller_ids: list[int]) -> set[int]:
    """Invoice ids that already belong to a PENDING payment's set (awaiting the owner's review) —
    so the bot shows «در انتظار تأیید» and blocks a duplicate submission. Expands each pending
    payment's invoice SET (a payment may cover several invoices)."""
    from app.services import payments

    if not reseller_ids:
        return set()
    held: set[int] = set()
    for p in await payments._pending_payments_for_resellers(session, set(reseller_ids)):
        held.update(payments._settled_ids(p))
    return held


async def _send_invoices(answer, chat_id: int, session) -> None:
    """«فاکتورهای پرداخت‌نشده» — the reseller's UNPAID, already-issued invoices, each as a
    button; tapping re-sends the full invoice (text + PDFs). An invoice with a payment awaiting
    review is marked «در انتظار تأیید» so the customer knows not to pay again."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    ids = [r.id for r in resellers]
    invoices = (
        await session.execute(
            select(Invoice)
            .where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
            .order_by(Invoice.period_start.desc()).limit(12)
        )
    ).scalars().all()
    if not invoices:
        await answer("فاکتور پرداخت‌نشده‌ای ندارید. 🎉")
        return
    pending = await _pending_invoice_ids(session, ids)
    items = []
    for inv in invoices:
        toman = f"{float(inv.amount_toman):,.0f} تومان"
        if inv.id in pending:
            items.append((inv.id, f"⏳ دوره {inv.period_label} — {toman} (در انتظار تأیید)"))
        else:
            status = _STATUS_FA.get(inv.status.value, inv.status.value)
            items.append((inv.id, f"🧾 دوره {inv.period_label} — {toman} ({status})"))
    note = ""
    if pending:
        note = "\n⏳ فاکتورهای «در انتظار تأیید» را فرستاده‌اید؛ تا بررسیِ پشتیبانی منتظر بمانید."
    await answer(
        "🧾 فاکتورهای پرداخت‌نشدهٔ شما — برای دیدن کاملِ هر فاکتور (متن + PDF) روی آن بزنید.\n"
        "برای پرداخت، از «💳 پرداخت فاکتور» استفاده کنید." + note,
        reply_markup=keyboards.my_invoices_keyboard(items),
    )


async def _send_self_interim(answer, chat_id: int, session, *, bot=None) -> None:
    """A reseller's OWN interim invoice for the CURRENT month so far — same SCOPE as the real
    end-of-month invoice (their own users + all sub-resellers), but marked interim. Sends a
    text breakdown (own + each sub + Rial total) plus volume-only PDFs split per node: ONE PDF
    for the reseller's own users and ONE PDF per sub-reseller (its subtree), so each can be
    handed to the matching sub without exposing the others. The grand total stays text-only."""
    from app.services import invoice_pdf, reseller_report
    from app.services.periods import current_month

    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    period = current_month()
    sent_any = False
    for r in resellers:
        # Only TOP-LEVEL resellers get a bundled invoice (a sub is billed via its parent).
        if not await _is_top_level_reseller(session, r):
            continue
        bd = await reseller_report.interim_breakdown(session, r, period)
        if bd["total_gb"] <= 0:
            await answer(f"«{r.name}»: در دورهٔ جاری ({period.label}) هنوز مصرفی ثبت نشده است.")
            sent_any = True
            continue

        # --- text breakdown ---
        # Each line starts with a Persian word so Telegram renders the WHOLE line RTL even
        # when the (sub-)reseller's name is English — otherwise a line beginning with a
        # Latin name gets left-aligned and reads garbled.
        price = bd["price"]
        lines = [
            f"📄 فاکتور علی‌الحساب — «{r.name}»",
            f"دوره: {period.label} (تا امروز)",
            f"قیمت هر گیگ: {price:,} تومان",
            "",
            "🟦 مصرف خودتان:",
            f"• حجم {bd['own']['gb']:g} گیگ ({bd['own']['users']} سرویس) — {bd['own']['amount']:,} تومان",
        ]
        if bd["subs"]:
            lines.append("\n🟨 زیرمجموعه‌های شما:")
            for s in bd["subs"]:
                # Isolate the (possibly English) name so the GB/Toman after it don't reorder.
                lines.append(
                    f"• نماینده {_iso(s['name'])}: حجم {s['gb']:g} گیگ "
                    f"({s['users']} سرویس) — {s['amount']:,} تومان"
                )
        lines += [
            "",
            "➖➖➖➖➖➖➖➖",
            f"📊 مجموع حجم: {bd['total_gb']:g} گیگ ({bd['total_users']} سرویس)",
            f"💰 مجموع مبلغ: {bd['total_amount']:,} تومان",
            "",
            "ℹ️ این فاکتور علی‌الحساب است؛ اول ماه آینده فاکتور کامل و واقعیِ قابل پرداخت برایتان ارسال می‌شود.",
        ]
        if bd["subs"]:
            lines.append("\n📎 در ادامه، یک PDF جدا برای خودتان و هر زیرمجموعه ارسال می‌شود تا بتوانید به هرکدام بدهید.")
        text = "\n".join(lines)

        owner_name = await settings_service.get(session, "owner_name", "") or ""
        # Send the text breakdown first (it's the summary), then the PDFs.
        await answer(text)

        if bot is None:
            sent_any = True
            continue
        from aiogram.types import FSInputFile

        # (1) The admin's OWN invoice — ONLY their own users (not the subtree), exactly
        #     like each sub gets its own PDF; own + each sub cover everyone once.
        try:
            res = await invoice_pdf.render_own_usage_pdf(
                session, r, period, title="فاکتور علی الحساب", issuer_name=owner_name
            )
            if res:
                path, fname = res
                await bot.send_document(
                    chat_id, FSInputFile(path, filename=fname),
                    caption=f"📄 فاکتور علی‌الحساب شما «{r.name}» (فقط کاربران خودتان)",
                )
        except Exception:  # noqa: BLE001
            log.warning("interim own pdf failed", exc_info=True)

        # (2) A separate PDF per sub-reseller, so the admin can forward each to that sub.
        for s in bd["subs"]:
            sub = await session.get(Reseller, s["id"])
            if sub is None:
                continue
            try:
                sres = await invoice_pdf.render_node_usage_pdf(
                    session, sub, period, title="فاکتور علی الحساب", issuer_name=r.name
                )
                if sres:
                    spath, sfname = sres
                    await bot.send_document(
                        chat_id, FSInputFile(spath, filename=sfname),
                        caption=f"📄 فاکتور علی‌الحساب زیرمجموعه «{sub.name}» — {s['gb']:g} گیگ",
                    )
            except Exception:  # noqa: BLE001
                log.warning("interim sub pdf failed for %s", s.get("id"), exc_info=True)
        sent_any = True
    if not sent_any:
        await answer("شما نمایندهٔ اصلی نیستید؛ فاکتور شما از طریق نمایندهٔ بالادستی صادر می‌شود.")


async def _send_pay(answer, chat_id: int, session) -> None:
    """«پرداخت فاکتور» — list each UNPAID, due-now invoice as its OWN button, plus a «پرداخت
    همهٔ بدهی» button (when 2+ are payable) that settles them all with one transfer. Tapping a
    button starts the locked pay flow for that invoice (payinv:<id>) or all of them (payall)."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    ids = [r.id for r in resellers]
    owed = (
        await session.execute(
            select(Invoice).where(Invoice.reseller_id.in_(ids), Invoice.status.in_(_OWED))
            .order_by(Invoice.period_start.desc())
        )
    ).scalars().all()
    today = tehran_today()
    due = [i for i in owed if not (i.deferred_until and i.deferred_until > today)]
    deferred = [i for i in owed if i.deferred_until and i.deferred_until > today]
    # An invoice with a payment already awaiting review is NOT offered for payment again —
    # the customer is told it's under review (one pending payment per invoice).
    pending = await _pending_invoice_ids(session, ids)
    payable = [i for i in due if i.id not in pending]
    in_review = [i for i in due if i.id in pending]

    if not payable:
        if in_review:
            await answer(
                f"⏳ {len(in_review)} فاکتور فرستاده‌اید و در انتظار تأیید پشتیبانی است؛ "
                "لطفاً منتظر بمانید. (لازم نیست دوباره بفرستید.)"
            )
        elif deferred:
            await answer("فاکتورِ سررسیدشده‌ای برای پرداخت ندارید؛ فاکتورهای شما مهلت‌دار هستند. ⏳")
        else:
            await answer("بدهی فعالی برای پرداخت ندارید. 🎉")
        return

    items = [
        (i.id,
         f"💳 دوره {i.period_label} — {float(i.amount_toman):,.0f} تومان")
        for i in payable
    ]
    pay_all_label = None
    if len(payable) > 1:
        total_toman = float(sum(float(i.amount_toman or 0) for i in payable))
        pay_all_label = f"✅ پرداخت همهٔ بدهی ({len(payable)} فاکتور — {total_toman:,.0f} تومان)"
        msg = (
            "💳 می‌توانید همهٔ فاکتورها را یکجا پرداخت کنید (دکمهٔ بالا)، "
            "یا هر فاکتور را جداگانه با زدن روی آن:"
        )
    else:
        msg = "💳 کدام فاکتور را می‌خواهید پرداخت کنید؟ روی آن بزنید:"
    if in_review:
        msg += f"\n\n⏳ {len(in_review)} فاکتور دیگر در انتظار تأیید است (لازم نیست دوباره بفرستید)."
    if deferred:
        msg += f"\n\n📅 {len(deferred)} فاکتور مهلت‌دار فعلاً لازم نیست پرداخت شود."
    await answer(msg, reply_markup=keyboards.pay_invoices_keyboard(items, pay_all_label=pay_all_label))


async def _send_removelink(answer, chat_id: int, session) -> None:
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer("لینکی برای حذف ندارید.")
        return
    items = [(r.id, _iso(f"{r.name} (…{r.admin_uuid[-6:]})")) for r in resellers]
    await answer("لینک‌های ثبت‌شدهٔ شما — برای حذف انتخاب کنید:",
                 reply_markup=keyboards.remove_links_keyboard(items))


async def _send_panels(answer, chat_id: int, session) -> None:
    """Show a reseller the list of panels they're registered on (with sub-counts + their own
    tap-to-copy panel link). The link is built from the panel's CURRENT host, so it auto-updates
    after a domain move."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    lines = ["🖥 پنل‌های شما:"]
    for r in resellers:
        panel = await session.get(Panel, r.panel_id)
        subs = (
            await session.execute(
                select(func.count(Reseller.id)).where(
                    Reseller.panel_id == r.panel_id,
                    Reseller.parent_admin_uuid == r.admin_uuid,
                )
            )
        ).scalar_one()
        tag = f" (#{r.link_tag})" if r.link_tag else ""
        link = panel.admin_link(r.admin_uuid, tag=r.link_tag)
        lines.append("")  # blank line between panels for readability
        # HTML-escape the panel name/tag (a name with `<` would break entity parsing → the
        # whole «پنل‌های من» message fails to send and the view looks dead).
        lines.append(
            f"‏• پنل {iso_html((panel.name or panel.key) + tag)} — زیرمجموعه‌ها: {subs}")
        lines.append(f"‏🔗 <code>{html.escape(link)}</code>")
    await answer(rtl("\n".join(lines)), parse_mode="HTML")


async def _send_portal_link(answer, chat_id: int, session) -> None:
    """Give the reseller a one-time link that opens the standalone web portal already logged in
    (no password). The link carries a short-lived signed token the site exchanges for a session."""
    resellers = await _resellers_for_chat(session, chat_id)
    if not resellers:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    from app.bot.handlers.common import portal_login_url

    url = await portal_login_url(session, chat_id)
    if not url:
        await answer(rtl("🌐 پنلِ تحتِ وب هنوز پیکربندی نشده است؛ لطفاً به پشتیبانی اطلاع دهید."))
        return
    msg = (
        "🌐 ورود به پنلِ تحتِ وب\n\n"
        "برای دیدنِ فاکتورها، پرداخت، آمار و مدیریتِ زیرمجموعه‌ها در سایت، روی لینکِ زیر بزنید "
        "(این لینک تا ۱۵ دقیقه معتبر است):\n\n"
        f"<a href=\"{html.escape(url, quote=True)}\">باز کردنِ پنلِ من</a>\n\n"
        "یا این آدرس را کپی کنید:\n"
        f"<code>{html.escape(url)}</code>"
    )
    await answer(rtl(msg), parse_mode="HTML", disable_web_page_preview=True)


# --------------------------- sub-reseller management helpers ---------------------------
async def _owns_sub(session, chat_id: int, sub: Reseller) -> bool:
    """True if `sub` is a descendant of one of the chat's own resellers (same panel).
    Guards every management action so a reseller can only touch their own subtree."""
    from app.services.reseller_report import node_descendants

    mine = [r for r in await _resellers_for_chat(session, chat_id) if r.panel_id == sub.panel_id]
    for r in mine:
        if r.id == sub.id:
            continue
        if any(d.id == sub.id for d in await node_descendants(session, r)):
            return True
    return False


async def _send_sub_panels(answer, chat_id: int, session) -> None:
    mine = await _resellers_for_chat(session, chat_id)
    if not mine:
        await answer(await texts.render(session, "tpl_link_not_found"))
        return
    items: list[tuple[int, str]] = []
    for r in mine:
        subs = (
            await session.execute(
                select(func.count(Reseller.id)).where(
                    Reseller.panel_id == r.panel_id,
                    Reseller.parent_admin_uuid == r.admin_uuid,
                )
            )
        ).scalar_one()
        if subs > 0:
            panel = await session.get(Panel, r.panel_id)
            items.append((r.id, f"پنل {_iso(panel.name or panel.key)} — {_iso(r.name)} ({subs})"))
    if not items:
        await answer("شما زیرمجموعه‌ای ندارید.")
        return
    await answer(
        "👥 مدیریت زیرمجموعه‌ها\nیک پنل را انتخاب کنید:",
        reply_markup=keyboards.sub_panels_keyboard(items),
    )


async def _send_sub_list(answer, chat_id: int, parent_id: int, session) -> None:
    parent = await session.get(Reseller, parent_id)
    if not parent or parent.bot_chat_id != chat_id:
        await answer("دسترسی ندارید.")
        return
    subs = (
        await session.execute(
            select(Reseller)
            .where(
                Reseller.panel_id == parent.panel_id,
                Reseller.parent_admin_uuid == parent.admin_uuid,
            )
            .order_by(Reseller.name)
        )
    ).scalars().all()
    if not subs:
        await answer("زیرمجموعه‌ای ندارید.")
        return
    items = [
        (s.id, f"{'⛔️' if s.enforcement_state == EnforcementState.enforced else '🟢'} {s.name}")
        for s in subs
    ]
    await answer(
        f"زیرمجموعه‌های «{parent.name}» — یکی را برای مشاهده/مدیریت انتخاب کنید:",
        reply_markup=keyboards.sub_list_keyboard(items),
    )


async def _send_sub_detail(answer, chat_id: int, sub_id: int, session) -> None:
    sub = await session.get(Reseller, sub_id)
    if not sub or not await _owns_sub(session, chat_id, sub):
        await answer("دسترسی ندارید.")
        return
    from app.services import reseller_report

    rep = await reseller_report.node_report(session, sub, months=3)
    state = sub.enforcement_state.value
    status_txt = (
        "⛔️ مسدود" if state == "enforced"
        else "🚫 ساخت کاربر متوقف (کاربرانِ فعلی آنلاین)" if state == "frozen"
        else "🟢 فعال"
    )
    lines = [
        f"👤 زیرمجموعه: {rep['name']}",
        f"وضعیت: {status_txt}",
        f"تعداد کاربران: {rep['total_users']} (فعال: {rep['enabled_users']})",
    ]
    if rep["sub_count"]:
        lines.append(f"زیرمجموعه‌های این نماینده: {rep['sub_count']}")
    lines.append(f"قیمت هر گیگ: {rep['price_per_gb']:,} تومان")
    # Monthly GB-cap progress (the Hiddify-missing volume limit, simulated by us).
    cap = rep.get("gb_cap") or 0
    used = rep.get("current_gb") or 0
    if cap > 0:
        pct = rep.get("cap_pct") or 0
        bar = _cap_bar(pct)
        remaining = rep.get("cap_remaining_gb")
        status = "⛔️ به سقف رسید" if used >= cap else f"باقی‌مانده: {remaining:g} گیگ"
        lines.append(
            f"\n🎯 سقف حجم ماهانه ({rep['current_period']}):\n"
            f"{bar} {used:g}/{cap:g} گیگ ({pct}%) — {status}"
        )
    else:
        lines.append(
            f"\n🎯 سقف حجم ماهانه: تعیین نشده "
            f"(این ماه تا الان: {used:g} گیگ ساخته شده)"
        )
    lines.append("\n📊 فروش ماهانه (سهمیهٔ فروخته‌شده):")
    for m in rep["months"]:
        lines.append(
            f"• {m['label']}: {m['gb']:g} گیگ — {m['amount_toman']:,} تومان "
            f"({m['new_services']} سرویس جدید)"
        )
    lines.append("\n📄 برای دریافت فاکتور این زیرمجموعه (برای ارسال به خودش) دکمهٔ ماه را بزنید.")
    months = [m["label"] for m in rep["months"]]
    await answer(
        "\n".join(lines),
        reply_markup=keyboards.sub_detail_keyboard(sub.id, state, months, has_cap=cap > 0),
    )


def _cap_bar(pct: int, width: int = 10) -> str:
    """A small text progress bar for the GB cap (🟩 under 70%, 🟧 70–89%, 🟥 90%+)."""
    pct = max(0, min(100, int(pct)))
    filled = round(pct / 100 * width)
    block = "🟥" if pct >= 90 else ("🟧" if pct >= 70 else "🟩")
    return block * filled + "⬜️" * (width - filled)


async def _send_sub_invoice(answer, chat_id: int, sub_id: int, period_label: str, session, *, bot=None) -> None:
    """Generate + send a PDF invoice for ONE sub-reseller (for the reseller to bill it)."""
    sub = await session.get(Reseller, sub_id)
    if not sub or not await _owns_sub(session, chat_id, sub):
        await answer("دسترسی ندارید.")
        return
    from app.services import invoice_pdf
    from app.services.periods import parse_period

    try:
        period = parse_period(period_label)
    except Exception:  # noqa: BLE001
        await answer("دورهٔ نامعتبر.")
        return
    # The issuer is the chat's own reseller on this panel (the parent billing the sub).
    mine = [r for r in await _resellers_for_chat(session, chat_id) if r.panel_id == sub.panel_id]
    issuer = mine[0].name if mine else ""
    try:
        res = await invoice_pdf.render_sub_invoice_pdf(session, sub, period, issuer_name=issuer)
    except Exception:  # noqa: BLE001
        log.warning("sub invoice pdf failed", exc_info=True)
        res = None
    if res is None:
        await answer(f"«{sub.name}» در دوره {period_label} فروشی نداشته است.")
        return
    path, fname = res
    if bot is not None:
        from aiogram.types import FSInputFile

        await bot.send_document(chat_id, FSInputFile(path, filename=fname),
                                caption=f"📄 فاکتور «{sub.name}» — دوره {period_label}")
    else:
        await answer(f"فاکتور ساخته شد: {fname}")
