"""First-boot bootstrap: create tables, seed the owner login and default settings."""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.core.config import settings as boot
from app.core.db import SessionLocal, init_models
from app.core.security import hash_password
from app.models.app_user import AppUser
from app.services import settings_service

log = logging.getLogger("bootstrap")


async def run_bootstrap() -> None:
    await init_models()
    async with SessionLocal() as session:
        # Seed the initial owner account if none exists.
        count = (await session.execute(select(func.count(AppUser.id)))).scalar_one()
        if count == 0:
            session.add(
                AppUser(
                    username=boot.admin_username,
                    password_hash=hash_password(boot.admin_password),
                    role="owner",
                )
            )
            await session.commit()
            log.info("Seeded initial owner account '%s'.", boot.admin_username)

        # Seed runtime settings defaults.
        await settings_service.seed_defaults(session)
        log.info("Settings defaults ensured.")
