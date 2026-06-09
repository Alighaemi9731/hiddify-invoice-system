from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth, setup
from app.core import crypto, loginsec
from app.core.db import Base
from app.core.security import (
    create_access_token,
    get_current_subject,
    hash_password,
    validate_new_password,
)
from app.models.app_user import AppUser
from app.models.setting import Setting
from app.schemas.auth import TotpEnable


async def _session_factory(tmp_path, name: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_current_subject_requires_live_owner_role_and_epoch(tmp_path):
    engine, Session = await _session_factory(tmp_path, "jwt.db")
    async with Session() as session:
        session.add(
            AppUser(
                username="owner",
                password_hash=hash_password("password123"),
                role="owner",
                token_epoch=4,
            )
        )
        await session.commit()

        valid = create_access_token("owner", {"role": "owner", "epoch": 4})
        assert await get_current_subject(token=valid, session=session) == "owner"

        invalid_tokens = [
            create_access_token("owner", {"role": "owner"}),  # legacy/missing epoch
            create_access_token("owner", {"epoch": 4}),  # missing role
            create_access_token("owner", {"role": "staff", "epoch": 4}),
            create_access_token("owner", {"role": "owner", "epoch": 3}),
        ]
        for token in invalid_tokens:
            with pytest.raises(HTTPException) as exc:
                await get_current_subject(token=token, session=session)
            assert exc.value.status_code == 401

        user = (
            await session.execute(select(AppUser).where(AppUser.username == "owner"))
        ).scalar_one()
        user.is_active = False
        await session.commit()
        with pytest.raises(HTTPException) as exc:
            await get_current_subject(token=valid, session=session)
        assert exc.value.status_code == 401

        user.is_active = True
        user.role = "staff"
        await session.commit()
        with pytest.raises(HTTPException) as exc:
            await get_current_subject(token=valid, session=session)
        assert exc.value.status_code == 401
    await engine.dispose()


@pytest.mark.asyncio
async def test_current_subject_fails_closed_when_database_is_unavailable():
    class BrokenSession:
        async def execute(self, _statement):
            raise RuntimeError("database unavailable")

    token = create_access_token("owner", {"role": "owner", "epoch": 0})
    with pytest.raises(HTTPException) as exc:
        await get_current_subject(token=token, session=BrokenSession())
    assert exc.value.status_code == 503


def test_password_validation_enforces_bcrypt_byte_limit():
    validate_new_password("12345678")
    validate_new_password("رمزعبورامن")

    with pytest.raises(ValueError, match="حداقل"):
        validate_new_password("short")
    with pytest.raises(ValueError, match="72"):
        validate_new_password("é" * 37)  # 74 UTF-8 bytes
    with pytest.raises(ValueError, match="72"):
        hash_password("a" * 73)


@pytest.mark.asyncio
async def test_totp_replacement_stays_pending_until_confirmed(tmp_path, monkeypatch):
    engine, Session = await _session_factory(tmp_path, "totp.db")
    old_secret = "OLDSECRET"
    new_secret = "NEWSECRET"

    async with Session() as session:
        session.add(
            AppUser(
                username="owner",
                password_hash=hash_password("password123"),
                role="owner",
                totp_enabled=True,
                totp_secret_enc=crypto.encrypt(old_secret),
            )
        )
        await session.commit()

        monkeypatch.setattr(loginsec, "new_totp_secret", lambda: new_secret)
        await auth.totp_setup(subject="owner", session=session)

        user = (
            await session.execute(select(AppUser).where(AppUser.username == "owner"))
        ).scalar_one()
        assert user.totp_enabled is True
        assert crypto.decrypt(user.totp_secret_enc) == old_secret
        assert crypto.decrypt(user.totp_pending_secret_enc) == new_secret

        monkeypatch.setattr(loginsec, "verify_totp", lambda _secret, _code: False)
        with pytest.raises(HTTPException) as exc:
            await auth.totp_enable(
                TotpEnable(code="000000"),
                subject="owner",
                session=session,
            )
        assert exc.value.status_code == 400
        assert crypto.decrypt(user.totp_secret_enc) == old_secret
        assert crypto.decrypt(user.totp_pending_secret_enc) == new_secret

        monkeypatch.setattr(
            loginsec,
            "verify_totp",
            lambda secret, code: secret == new_secret and code == "123456",
        )
        await auth.totp_enable(
            TotpEnable(code="123456"),
            subject="owner",
            session=session,
        )

        assert user.totp_enabled is True
        assert crypto.decrypt(user.totp_secret_enc) == new_secret
        assert user.totp_pending_secret_enc is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_setup_creates_exactly_one_owner(tmp_path):
    engine, Session = await _session_factory(tmp_path, "setup.db")
    async with Session() as session:
        session.add(Setting(key="setup_done", value=False, is_secret=False))
        await session.commit()

    async def attempt(username: str):
        async with Session() as session:
            return await setup.do_setup(
                setup.SetupRequest(username=username, password="password123"),
                session=session,
            )

    results = await asyncio.gather(
        attempt("owner-one"),
        attempt("owner-two"),
        return_exceptions=True,
    )
    successes = [result for result in results if isinstance(result, dict)]
    conflicts = [
        result
        for result in results
        if isinstance(result, HTTPException) and result.status_code == 409
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1

    async with Session() as session:
        owner_count = (
            await session.execute(
                select(func.count(AppUser.id)).where(AppUser.role == "owner")
            )
        ).scalar_one()
        assert owner_count == 1
        assert await session.get(Setting, "setup_done") is not None
        assert (await session.get(Setting, "setup_done")).value is True
    await engine.dispose()
