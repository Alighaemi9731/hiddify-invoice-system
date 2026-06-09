"""
Single source of truth for legal invoice status transitions (B03).

Every place that changes an invoice's status — the panel API, dunning, payments — must go
through these guards instead of assigning `inv.status` after its own ad-hoc check. That
keeps the state machine consistent: a paid invoice can't be silently canceled/deferred/
edited, a draft can't be marked paid, a canceled invoice can't be deferred, etc.

Status meanings (see `app.models.enums.InvoiceStatus`):
  draft     — generated, not delivered (not in the financial ledger)
  sent      — delivered, owed
  overdue   — delivered, owed, past due
  enforced  — delivered, owed, panel access suspended
  paid      — settled
  canceled  — voided

`OWED` = the delivered-but-unpaid states money is collected against.
"""
from __future__ import annotations

from app.models.enums import InvoiceStatus as S

# The states an invoice is owed money in.
OWED: tuple[S, ...] = (S.sent, S.overdue, S.enforced)

# Allowed target states from each current state. Self-transitions (status unchanged, e.g.
# an edit or recompute that keeps the status) are always allowed and not listed here.
_TRANSITIONS: dict[S, set[S]] = {
    S.draft:    {S.sent, S.canceled},
    S.sent:     {S.paid, S.overdue, S.enforced, S.canceled, S.draft},
    S.overdue:  {S.paid, S.sent, S.enforced, S.canceled, S.draft},
    S.enforced: {S.paid, S.sent, S.canceled, S.draft},
    # paid leaves only by an explicit "unmark paid" (→ sent/draft); never directly to
    # canceled/overdue/enforced.
    S.paid:     {S.sent, S.draft},
    # canceled is terminal except for an explicit revert-to-draft.
    S.canceled: {S.draft},
}


class InvoiceStateError(ValueError):
    """An attempted invoice operation/transition is not legal from the current state.
    Carries a Persian, user-facing message (the API maps it to HTTP 400)."""


def can_transition(frm: S, to: S) -> bool:
    if frm == to:
        return True
    return to in _TRANSITIONS.get(frm, set())


def ensure_transition(frm: S, to: S, *, msg: str | None = None) -> None:
    if not can_transition(frm, to):
        raise InvoiceStateError(
            msg or f"تغییر وضعیت فاکتور از «{frm.value}» به «{to.value}» مجاز نیست."
        )


# ------------------------------- operation guards -------------------------------
def ensure_can_mark_paid(status: S) -> None:
    if status not in OWED:
        if status == S.paid:
            raise InvoiceStateError("این فاکتور قبلاً پرداخت‌شده است.")
        if status == S.draft:
            raise InvoiceStateError("فاکتور پیش‌نویس را نمی‌توان پرداخت‌شده کرد؛ ابتدا آن را صادر/ارسال کنید.")
        raise InvoiceStateError("فاکتور لغوشده را نمی‌توان پرداخت‌شده کرد.")


def ensure_can_cancel(status: S) -> None:
    if status == S.paid:
        raise InvoiceStateError("فاکتور پرداخت‌شده را نمی‌توان لغو کرد؛ ابتدا «لغو پرداخت» را بزنید.")


def ensure_can_defer(status: S) -> None:
    if status not in OWED:
        raise InvoiceStateError(
            "مهلت پرداخت فقط برای فاکتورهای صادرشده و پرداخت‌نشده قابل تنظیم است "
            "(پیش‌نویس/پرداخت‌شده/لغوشده مجاز نیست)."
        )


def ensure_can_edit(status: S) -> None:
    if status in (S.paid, S.canceled):
        raise InvoiceStateError(
            "فاکتور پرداخت‌شده یا لغوشده را نمی‌توان ویرایش کرد؛ ابتدا «لغو پرداخت» یا «بازگردانی به پیش‌نویس» را بزنید."
        )


def ensure_can_unmark_paid(status: S) -> None:
    if status != S.paid:
        raise InvoiceStateError("این فاکتور در وضعیت پرداخت‌شده نیست.")
