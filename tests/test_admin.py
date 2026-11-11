import secrets

import httpx
import pytest
from argon2 import PasswordHasher
from bs4 import BeautifulSoup
from sqlalchemy import select

from vagasunb.admin import create_app
from vagasunb.config import Settings
from vagasunb.db import Admin, Recipient


@pytest.fixture
def settings(test_url, vault, tmp_path):
    return Settings("demo", test_url, tmp_path / "secrets", "testserver")


async def login(client, password):
    page = await client.get("/login")
    nonce = BeautifulSoup(page.text, "html.parser").select_one('[name="nonce"]')["value"]
    return await client.post(
        "/login",
        data={"password": password, "nonce": nonce},
        headers={"Origin": "http://testserver"},
    )


async def test_dashboard_auth_csrf_host_demo_and_no_secrets(db, settings, vault):
    password = secrets.token_urlsafe(20)
    async with db.transaction() as s:
        s.add(Admin(id=1, password=PasswordHasher().hash(password)))
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        assert (await client.get("/")).status_code == 303
        assert (await client.get("/demo")).status_code == 401
        assert (await client.post("/operations", data={"action": "cleanup"})).status_code == 401
        assert (await login(client, password)).status_code == 303
        page = await client.get("/")
        assert page.status_code == 200 and "Catálogo local" in page.text
        assert password not in page.text and settings.database_url not in page.text
        assert (await client.get("/", headers={"host": "evil.example"})).status_code == 400
        assert (
            await client.post(
                "/operations", data={"action": "cleanup"}, headers={"Origin": "http://testserver"}
            )
        ).status_code == 403
        csrf = BeautifulSoup(page.text, "html.parser").select_one('[name="csrf"]')["value"]
        assert (
            await client.post(
                "/operations",
                data={"csrf": csrf, "action": "cleanup"},
                headers={"Origin": "https://evil.example"},
            )
        ).status_code == 403
        headers = {"Origin": "http://testserver"}
        assert (
            await client.post(
                "/operations", data={"csrf": csrf, "action": "cleanup"}, headers=headers
            )
        ).status_code == 303
        demo = await client.get("/demo")
        assert demo.status_code == 200
        result = await client.post(
            "/demo/interact",
            data={"csrf": csrf, "recipient": "1", "text": "/start"},
            headers=headers,
        )
        assert result.status_code == 200 and result.json()["buttons"]
        async with db.transaction() as s:
            r = await s.scalar(select(Recipient))
            dashboard = await client.get("/")
            assert r.lookup not in dashboard.text and r.cipher not in dashboard.text
        assert (
            await client.post("/logout", data={"csrf": csrf}, headers=headers)
        ).status_code == 303
        assert (await client.get("/demo")).status_code == 401


async def test_production_has_no_demo_endpoints(settings):
    settings.mode = "public"
    app = create_app(settings)
    assert not any(r.path.startswith("/demo") for r in app.routes)
    await app.state.db.close()


async def test_login_rate_limit(db, settings):
    async with db.transaction() as s:
        s.add(Admin(id=1, password=PasswordHasher().hash(secrets.token_urlsafe(20))))
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for _ in range(8):
            assert (await login(client, "wrong")).status_code == 401
        assert (await login(client, "wrong")).status_code == 429
    await app.state.db.close()


def test_demo_refuses_production_database(monkeypatch):
    monkeypatch.setenv("VAGAS_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://placeholder@localhost/production")
    with pytest.raises(ValueError, match="sufixo"):
        Settings.load()
