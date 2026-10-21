"""A public HTTP fresh install provisions TLS before accepting owner credentials."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import setup
from app.core.db import Base
from app.core.security import hash_password, setup_tls_token
from app.models import AppUser
from app.services import domain_setup, settings_service


def test_http_to_https_setup_preserves_token_and_owner_gate(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        calls = []

        async def apply_domain(session, domain, email):
            calls.append(domain)
            return {"ok": True, "url": "https://panel.example.com", "domain": domain}

        monkeypatch.setattr(domain_setup, 'set_domain', apply_domain)
        http = SimpleNamespace(client=SimpleNamespace(host='203.0.113.20'),
                               headers={}, url=SimpleNamespace(scheme='http'))
        https = SimpleNamespace(client=http.client, headers={'x-forwarded-proto': 'https'}, url=http.url)
        try:
            async with factory() as s:
                await settings_service.set_value(s, 'setup_done', False)
                await settings_service.set_value(s, 'setup_bootstrap_token_hash', hash_password('install-token'))
                await settings_service.set_value(s, 'setup_tls_token_hash', hash_password(setup_tls_token('install-token')))
                for token in ('', 'wrong', 'install-token'):
                    with pytest.raises(HTTPException) as exc:
                        await setup.prepare_domain(setup.SetupDomainRequest(domain='panel.example.com', token=token), http, s)
                    assert exc.value.status_code == 403
                assert not calls
                result = await setup.prepare_domain(
                    setup.SetupDomainRequest(domain='panel.example.com', token=setup_tls_token('install-token')), http, s)
                assert result['url'] == 'https://panel.example.com'
                assert (await s.execute(select(func.count(AppUser.id)))).scalar() == 0
                assert await settings_service.get(s, 'setup_bootstrap_token_hash')
                body = setup.SetupRequest(username='owner', password='password123', token='install-token')
                with pytest.raises(HTTPException) as wrong_purpose:
                    await setup.do_setup(setup.SetupRequest(
                        username='owner', password='password123', token=setup_tls_token('install-token')
                    ), https, s)
                assert wrong_purpose.value.status_code == 403
                with pytest.raises(HTTPException) as exc:
                    await setup.do_setup(body, http, s)
                assert exc.value.status_code == 426
                assert (await setup.do_setup(body, https, s))['setup_done']
                assert not await settings_service.get(s, 'setup_bootstrap_token_hash')
                assert not await settings_service.get(s, 'setup_tls_token_hash')
                with pytest.raises(HTTPException) as exc:
                    await setup.prepare_domain(setup.SetupDomainRequest(domain='other.example.com', token='install-token'), http, s)
                assert exc.value.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_http_domain_bootstrap_failure_is_retryable(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def fail(*args):
            return {'ok': False, 'error': 'Invalid domain'}

        monkeypatch.setattr(domain_setup, 'set_domain', fail)
        request = SimpleNamespace(client=SimpleNamespace(host='203.0.113.21'))
        try:
            async with factory() as s:
                await settings_service.set_value(s, 'setup_bootstrap_token_hash', hash_password('install-token'))
                await settings_service.set_value(s, 'setup_tls_token_hash', hash_password(setup_tls_token('install-token')))
                with pytest.raises(HTTPException) as exc:
                    await setup.prepare_domain(setup.SetupDomainRequest(domain='bad', token=setup_tls_token('install-token')), request, s)
                assert exc.value.status_code == 400
                assert not await setup._is_done(s)
                assert await settings_service.get(s, 'setup_bootstrap_token_hash')
        finally:
            await engine.dispose()

    asyncio.run(run())
