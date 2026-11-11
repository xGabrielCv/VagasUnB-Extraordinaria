import asyncio
import secrets
from datetime import timedelta

import httpx
import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from vagasunb.admin import create_app
from vagasunb.db import Setting
from vagasunb.collection import collect_catalog, next_delay
from vagasunb.config import Settings
from vagasunb.db import Admin, Classroom, Outbox, now
from vagasunb.demo import fixtures
from vagasunb.distribution import SendBudget, dispatch_batch
from vagasunb.monitor import observe
from vagasunb.router import View
from vagasunb.services import change_options, options, register, subscribe


async def nonce(client, path):
    response = await client.get(path)
    return BeautifulSoup(response.text, "html.parser").select_one('[name="nonce"]')["value"]


async def test_bootstrap_concurrency_origin_change_and_partial_config(
    db, test_url, vault, tmp_path
):
    app = create_app(Settings("demo", test_url, tmp_path / "secrets", "testserver"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        assert (await client.get("/login")).headers["location"] == "/setup"
        headers = {"Origin": "http://testserver"}
        token = await nonce(client, "/setup")
        password = secrets.token_urlsafe(20)
        for value, confirm in [("short", "short"), ("x" * 257, "x" * 257), (password, "different")]:
            r = await client.post(
                "/setup",
                data={"nonce": token, "password": value, "confirm": confirm},
                headers=headers,
            )
            assert r.status_code == 400 and 'role="alert"' in r.text
            token = BeautifulSoup(r.text, "html.parser").select_one('[name="nonce"]')["value"]
        for origin in ["null", "http://localhost", "https://evil.example"]:
            r = await client.post(
                "/setup",
                data={"nonce": token, "password": password, "confirm": password},
                headers={"Origin": origin},
            )
            assert r.status_code == 403
            token = BeautifulSoup(r.text, "html.parser").select_one('[name="nonce"]')["value"]
        responses = await asyncio.gather(
            *(
                client.post(
                    "/setup",
                    data={"nonce": token, "password": password, "confirm": password},
                    headers=headers,
                )
                for _ in range(2)
            )
        )
        assert all(r.status_code == 303 for r in responses)
        assert (await client.get("/setup")).headers["location"] == "/login"
        token = await nonce(client, "/login")
        assert (
            await client.post(
                "/login", data={"nonce": token, "password": password}, headers=headers
            )
        ).status_code == 303
        page = await client.get("/")
        assert page.headers["referrer-policy"] == "same-origin"
        csrf = BeautifulSoup(page.text, "html.parser").select_one('[name="csrf"]')["value"]
        async with db.transaction() as s:
            scopes = (await options(s))["scopes"]
        assert (
            await client.post(
                "/operations",
                data={"csrf": csrf, "action": "config", "interval": "5"},
                headers=headers,
            )
        ).status_code == 303
        async with db.transaction() as s:
            cfg = await options(s)
            assert cfg["scopes"] == scopes and cfg["interval"] == 5
            assert await s.scalar(select(func.count()).select_from(Admin)) == 1
        assert (
            await client.post(
                "/password",
                data={
                    "csrf": csrf,
                    "current": password,
                    "password": password + "new",
                    "confirm": password + "new",
                },
                headers=headers,
            )
        ).status_code == 303
        assert (await client.get("/metrics")).status_code == 401
        token = await nonce(client, "/login")
        assert (
            await client.post(
                "/login", data={"nonce": token, "password": password}, headers=headers
            )
        ).status_code == 401
    # New app process/state does not reopen bootstrap.
    app = create_app(Settings("demo", test_url, tmp_path / "secrets", "testserver"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/setup")).status_code == 303
    await app.state.db.close()


class ManyScopes:
    complete = True
    kind = "demo"
    calls = 0
    discoveries = 0
    bad = None

    async def discover(self):
        self.discoveries += 1
        return {
            "units": [{"id": u, "name": u} for u in ["A", "B", "Empty"]],
            "levels": [{"id": "G", "name": "Graduação"}, {"id": "M", "name": "Mestrado"}],
        }

    async def collect(self, unit, period, level):
        self.calls += 1
        if unit == self.bad:
            raise ValueError("synthetic error")
        if unit == "Empty":
            return []
        # A and B overlap: one canonical identity, across units and levels.
        return [dict(fixtures()[0], units=[unit], period=period)]


async def test_complete_catalog_empty_overlap_partial_and_stable_identity(db):
    source = ManyScopes()
    async with db.transaction() as s:
        before = {c.code + ":" + c.number: c.id for c in (await s.scalars(select(Classroom))).all()}
    first = await collect_catalog(db, source, "demo")
    assert first["status"] == "complete" and first["expected"] == 6 and first["empty"] == 2
    assert first["valid"] == 6 and source.calls == 6
    second = await collect_catalog(db, source, "demo")
    assert second["status"] == "complete"
    async with db.transaction() as s:
        rows = (await s.scalars(select(Classroom))).all()
        assert {c.code + ":" + c.number: c.id for c in rows} == before
        c = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        assert c.missing == 0 and {"A", "B"}.issubset(c.units)
        assert await s.scalar(select(func.count()).select_from(Outbox)) == 0
    source.bad = "B"
    partial = await collect_catalog(db, source, "demo")
    assert partial["status"] == "partial" and partial["failed"] == 2


@pytest.mark.parametrize("desired", [5, 10, 60])
def test_start_intervals_no_backlog_and_source_backoff(desired):
    assert next_delay(100, desired, clock=lambda: 101) == desired - 1
    assert next_delay(100, desired, clock=lambda: 200) == 0
    assert next_delay(100, desired, backoff=230, clock=lambda: 200) == 30


async def test_empty_discovery_is_degraded_and_global_lock(db):
    class Empty:
        async def discover(self):
            return {"units": [], "levels": []}

    assert (await collect_catalog(db, Empty(), "demo"))["status"] == "degraded"
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(ManyScopes):
        async def discover(self):
            entered.set()
            await release.wait()
            return await super().discover()

    task = asyncio.create_task(collect_catalog(db, Slow(), "demo"))
    await entered.wait()
    assert (await collect_catalog(db, ManyScopes(), "demo"))["status"] == "already_running"
    release.set()
    await task


async def test_batch_100_limited_fair_shared_budget_and_two_leaders(db, vault):
    async with db.transaction() as s:
        await change_options(s, {"group_delay": 0, "debounce": 0})
        for i in range(100):
            r = await register(s, vault, 100000 + i)
            await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
    t = now() - timedelta(seconds=2)
    rows = [dict(fixtures()[0], occupied=29)]
    # Baseline observations were created now; establish a fresh zero then opening.
    async with db.transaction() as s:
        c = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        c.observed = t - timedelta(seconds=1)
        await observe(s, rows, "demo", t)
    async with db.transaction() as s:
        assert await observe(s, rows, "demo", t + timedelta(seconds=1)) == 100

    class Clock:
        value = 1000.0

        def now(self):
            return self.value

        async def sleep(self, seconds):
            self.value += seconds
            await asyncio.sleep(0)

    clock = Clock()

    class Transport:
        active = maximum = 0
        sent = []

        async def send(self, chat, view):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.sent.append((chat, clock.now()))
            await asyncio.sleep(0.1)
            self.active -= 1

    transport = Transport()
    budget = SendBudget(db, clock.now, clock.sleep)
    counts = await asyncio.gather(
        dispatch_batch(db, vault, transport, "demo", budget=budget),
        dispatch_batch(db, vault, transport, "demo", budget=budget),
    )
    assert sum(counts) == 100 and len({chat for chat, _ in transport.sent}) == 100
    assert 1 < transport.maximum <= 6
    order = [int(chat) for chat, _ in transport.sent]
    assert order != sorted(order)
    # The shared virtual clock advances while other consumers publish claims.
    # Wire times can lag grants by the bounded in-flight cohort; the real-clock
    # measurement separately verifies the complete 100-recipient delivery path.
    times = sorted(t for _, t in transport.sent)
    assert clock.value >= 1000 + 99 / 25 - 0.01
    assert times[-1] - times[0] >= (100 - 6) / 25 - 0.01


def test_long_html_preserves_entities_and_tags():
    view = View("<b>" + ("&lt;&amp;" * 3000) + "</b>")
    assert len(view.text) < 4096 and "<b>" not in view.text
    assert not view.text.endswith("&")


async def test_changed_scope_confirmation_is_shared_not_per_recipient(db, vault):
    async with db.transaction() as s:
        await change_options(s, {"debounce": 1, "group_delay": 0})
        for i in range(100):
            r = await register(s, vault, 700000 + i)
            await subscribe(s, r.id, "DEMO0002", "DEMO.1", [])

    class Source(ManyScopes):
        async def collect(self, unit, period, level):
            self.calls += 1
            if unit == "Empty":
                return [dict(fixtures()[2], units=[unit], occupied=29)] if level == "M" else []
            return [dict(fixtures()[0], units=[unit])]

    source = Source()
    result = await collect_catalog(db, source, "demo")
    assert result["status"] == "complete"
    assert result["confirmations"] == 1 and result["eligible"] == 100
    assert source.calls == 7  # Six discovered scopes, one shared confirmation.


async def test_slow_catalog_cadence_keeps_short_delivery_ttl(db, vault):
    async with db.transaction() as s:
        r = await register(s, vault, 730001)
        await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
        s.add(
            Setting(
                key="catalog_cycle_public",
                value={"period": "DEMO.1", "valid": 100, "processed": 100, "duration_s": 600},
            )
        )
        c = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        c.observed = now() - timedelta(seconds=600)
    rows = [dict(fixtures()[0], occupied=29)]
    at = now()
    async with db.transaction() as s:
        await observe(s, rows, "demo", at)
    async with db.transaction() as s:
        assert await observe(s, rows, "demo", at + timedelta(seconds=11)) == 1
