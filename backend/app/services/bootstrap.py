"""First-boot bootstrap: migrate schema, seed the owner login and default settings."""
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
        await settings_service.seed_defaults(session)

        count = (await session.execute(select(func.count(AppUser.id)))).scalar_one()
        # An explicit (non-placeholder) ADMIN_PASSWORD = scripted install → seed the
        # owner and skip the setup wizard. Otherwise the first visit runs the wizard.
        explicit_pw = bool(boot.admin_password) and boot.admin_password not in ("", "change-me-now")

        if count == 0 and explicit_pw:
            session.add(AppUser(
                username=boot.admin_username,
                password_hash=hash_password(boot.admin_password),
                role="owner",
            ))
            await settings_service.set_value(session, "setup_done", True)
            await session.commit()
            log.info("Seeded owner '%s' from env; setup marked done.", boot.admin_username)
        elif count > 0:
            # Existing owner (fresh seed long ago, or restored backup) → setup done.
            await settings_service.set_value(session, "setup_done", True)
            await session.commit()
        else:
            log.info("No owner yet — first visit will show the setup wizard.")
        log.info("Settings defaults ensured.")
