import re
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


async def test_dashboard_vacancies_filter_levels_mode_and_telegram_status(db, settings, monkeypatch):
    async with db.transaction() as s:
        s.add(Admin(id=1, password=PasswordHasher().hash("long-test-password")))
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await login(client, "long-test-password")).status_code == 303
        page = await client.get("/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

        m1 = await client.get("/metrics")
        assert m1.status_code == 200
        payload = m1.json()
        assert payload["telegram_enabled"] is False
        assert "period_stats" in payload and payload["period_stats"]["total"] > 0

        filtered = await client.get("/?vacancies=1")
        assert filtered.status_code == 200
        assert "checked" in filtered.text

        res = await client.post(
            "/operations",
            data={
                "csrf": csrf,
                "action": "config",
                "source": "demo",
                "period": "DEMO.1",
                "collection_auto": "true",
                "interval": "60",
                "levels_mode": "G",
            },
            headers={"Origin": "http://testserver"},
        )
        assert res.status_code == 303
    await app.state.db.close()

    from vagasunb.services import options

    async with db.transaction() as s:
        assert (await options(s))["levels"] == ["G"]

    monkeypatch.setenv("VAGAS_TELEGRAM_ENABLE", "1")
    settings.mode = "public"
    (settings.secrets / "telegram_token").write_text("123456:SyntheticTokenForUnitTestOnly")
    app_pub = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_pub), base_url="http://testserver"
    ) as client:
        assert (await login(client, "long-test-password")).status_code == 303
        m2 = (await client.get("/metrics")).json()
        assert m2["telegram_enabled"] is True and m2["telegram_token"] is True
    await app_pub.state.db.close()


async def test_cloudbeaver_db_gate_additional_auth_and_proxy(db, settings):
    async with db.transaction() as s:
        s.add(Admin(id=1, password=PasswordHasher().hash("admin-panel-password-1")))
    app = create_app(settings)
    seen_cookies = []

    def fake_cb(request):
        seen_cookies.append(request.headers.get("cookie", ""))
        if request.url.path == "/cloudbeaver/status":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, text="cloudbeaver-ui")

    app.state.cb_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake_cb), base_url="http://cloudbeaver.local"
    )
    h = {"Origin": "http://testserver"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Without admin login -> blocked from /db and /cloudbeaver/
        assert (await client.get("/db")).status_code == 303
        assert (await client.get("/cloudbeaver/")).status_code == 303
        assert (await client.post("/cloudbeaver/api")).status_code == 401

        assert (await login(client, "admin-panel-password-1")).status_code == 303
        db_page = await client.get("/db")
        assert db_page.status_code == 200 and "Online" in db_page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', db_page.text).group(1)

        # Admin logged in, but additional DB gate NOT unlocked yet -> blocked
        assert (await client.get("/cloudbeaver/")).status_code == 303
        assert (await client.post("/cloudbeaver/api")).status_code == 401
        assert (
            await client.post(
                "/db/query", data={"csrf": csrf, "sql": "SELECT 1"}, headers=h
            )
        ).status_code == 303

        # Reject reusing panel password as additional DB password
        same = await client.post(
            "/db/setup",
            data={
                "csrf": csrf,
                "password": "admin-panel-password-1",
                "confirm": "admin-panel-password-1",
            },
            headers=h,
        )
        assert same.status_code == 400 and "diferente" in same.text

        # Configure distinct DB password -> unlocks gate and proxies cleanly
        ok = await client.post(
            "/db/setup",
            data={
                "csrf": csrf,
                "password": "db-second-layer-password",
                "confirm": "db-second-layer-password",
            },
            headers=h,
        )
        assert ok.status_code == 303
        proxied = await client.get("/cloudbeaver/")
        assert proxied.status_code == 200 and proxied.text == "cloudbeaver-ui"
        # Ensure admin session and db gate cookies are never leaked upstream
        assert all("session=" not in c and "vagas_db_gate=" not in c for c in seen_cookies)

        # Lock DB gate -> blocks /cloudbeaver/ again
        assert (await client.post("/db/lock", data={"csrf": csrf}, headers=h)).status_code == 303
        assert (await client.get("/cloudbeaver/")).status_code == 303

        # Wrong password rejected (401), rate limited (429), valid password unlocks
        bad = await client.post(
            "/db/unlock", data={"csrf": csrf, "password": "wrong-db-password"}, headers=h
        )
        assert bad.status_code == 401
        reopen = await client.post(
            "/db/unlock",
            data={"csrf": csrf, "password": "db-second-layer-password"},
            headers=h,
        )
        assert reopen.status_code == 303
        assert (await client.get("/cloudbeaver/")).status_code == 200

        # Integrated table viewer and SQL console on /db after additional auth
        explorer = await client.get("/db?table=classrooms")
        assert explorer.status_code == 200 and "DEMO0001" in explorer.text
        q_ok = await client.post(
            "/db/query",
            data={"csrf": csrf, "sql": "SELECT code, number FROM classrooms LIMIT 3"},
            headers=h,
        )
        assert q_ok.status_code == 200 and "DEMO0001" in q_ok.text
        q_bad = await client.post(
            "/db/query",
            data={"csrf": csrf, "sql": "SELECT * FROM nonexistent_table_xyz"},
            headers=h,
        )
        assert q_bad.status_code == 400 and "Erro SQL:" in q_bad.text

        # Password change requires valid current DB password and shares rate limit
        bad_pw = await client.post(
            "/db/password",
            data={
                "csrf": csrf,
                "current": "wrong-current-pass",
                "password": "new-second-layer-password",
                "confirm": "new-second-layer-password",
            },
            headers=h,
        )
        assert bad_pw.status_code == 400
    await app.state.cb_client.aclose()
    await app.state.db.close()
