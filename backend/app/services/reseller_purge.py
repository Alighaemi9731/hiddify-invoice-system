"""DB-side cascade purge of a reseller subtree — used after the panel-side cascade deletion of an
admin (see app.services.enforcement._process_delete_action). Deletes the reseller rows plus the
end-users / usage-meters / invoices / payments tied to them; the durable financial ledger
(`financial_records`, no FK) is intentionally KEPT so accounting history survives. SQLite (tests)
has no FK enforcement, so children are deleted before parents."""
from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EndUserSnapshot,
    Invoice,
    InvoiceLine,
    Payment,
    Reseller,
    UsageMeter,
)


async def purge_subtree(
    session: AsyncSession,
    *,
    panel_id: int,
    reseller_ids: list[int],
    admin_uuids: list[str],
    commit: bool = True,
) -> dict:
    """Delete the given reseller rows + the end-users those admins created (by `added_by_uuid`)
    on `panel_id`, plus their usage-meters / invoices / invoice-lines / payments. Keeps
    `financial_records`. Returns {resellers_deleted, users_deleted}."""
    del_ids = [int(i) for i in reseller_ids]
    del_uuids = [(u or "").lower() for u in admin_uuids if u]

    user_uuids: list[str] = []
    if del_uuids:
        user_uuids = list(
            (
                await session.execute(
                    select(EndUserSnapshot.user_uuid).where(
                        EndUserSnapshot.panel_id == panel_id,
                        func.lower(EndUserSnapshot.added_by_uuid).in_(del_uuids),
                    )
                )
            ).scalars().all()
        )
        if user_uuids:
            await session.execute(
                sa_delete(UsageMeter).where(
                    UsageMeter.panel_id == panel_id, UsageMeter.user_uuid.in_(user_uuids)
                )
            )
        await session.execute(
            sa_delete(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == panel_id,
                func.lower(EndUserSnapshot.added_by_uuid).in_(del_uuids),
            )
        )

    if del_ids:
        await session.execute(sa_delete(Payment).where(Payment.reseller_id.in_(del_ids)))
        inv_ids = list(
            (
                await session.execute(select(Invoice.id).where(Invoice.reseller_id.in_(del_ids)))
            ).scalars().all()
        )
        if inv_ids:
            await session.execute(sa_delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(inv_ids)))
            await session.execute(sa_delete(Invoice).where(Invoice.id.in_(inv_ids)))
        await session.execute(sa_delete(Reseller).where(Reseller.id.in_(del_ids)))

    if commit:
        await session.commit()
    return {"resellers_deleted": len(del_ids), "users_deleted": len(user_uuids)}
