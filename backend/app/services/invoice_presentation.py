"""One familiar invoice status in the UI, without changing its collection clock."""
from sqlalchemy import String, and_, case, cast, func, select

from app.models import EnforcementAction, Invoice, Reseller
from app.models.enums import EnforcementActionType, EnforcementState, InvoiceStatus


def restriction_expression():  # noqa: ANN201
    """Original live suspension cause; reassertions are only maintenance of that cause."""
    return (
        select(case(
            (EnforcementAction.invoice_id.is_not(None), "financial_blocked"),
            (EnforcementAction.snapshot["restriction_reason"].as_string() == "registration",
             "registration_blocked"),
            else_="enforced",
        ))
        .where(
            EnforcementAction.reseller_id == Reseller.id,
            EnforcementAction.action == EnforcementActionType.disable_users,
            EnforcementAction.dry_run.is_(False),
            func.coalesce(EnforcementAction.snapshot["reassert"].as_boolean(), False).is_(False),
        )
        .order_by(EnforcementAction.id.desc()).limit(1)
        .correlate(Reseller).scalar_subquery()
    )


def status_expression():  # noqa: ANN201
    """Shared projection for rows, filters, totals and CSV (SQLite and PostgreSQL)."""
    source = restriction_expression()
    return case(
        (Invoice.status.in_([InvoiceStatus.paid, InvoiceStatus.canceled]), cast(Invoice.status, String)),
        (and_(Invoice.status == InvoiceStatus.enforced,
              Reseller.enforcement_state == EnforcementState.enforced),
         func.coalesce(source, case(
             (Reseller.registration_blocked.is_(True), "registration_blocked"),
             else_="enforced",
         ))),
        (and_(Reseller.enforcement_state == EnforcementState.enforced,
              Reseller.registration_blocked.is_(True)), "registration_blocked"),
        (Invoice.sent_at.is_(None), "draft"),
        else_=cast(Invoice.status, String),
    )


async def restriction_reasons(session, reseller_ids: list[int]) -> dict[int, str]:  # noqa: ANN001
    if not reseller_ids:
        return {}
    rows = (await session.execute(select(Reseller.id, restriction_expression()).where(
        Reseller.id.in_(reseller_ids), Reseller.enforcement_state == EnforcementState.enforced,
    ))).all()
    return {rid: reason for rid, reason in rows if reason}
