import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from telegram import Bot
from telegram.request import HTTPXRequest

from vagasunb.collection import collect_catalog
from vagasunb.db import Classroom, Cooldown, Outbox, Recipient, Setting, now
from vagasunb.demo import fixtures
from vagasunb.distribution import SendBudget
from vagasunb.monitor import dispatch_once, observe
from vagasunb.presentation import date_br, input_date, label
from vagasunb.schedule import allowed, local_instant, state, validate, within
from vagasunb.services import DEFAULTS, change_options, erase, options, register, subscribe
from vagasunb.telegram import BudgetRequest, FakeTransport, TelegramTransport


def test_schedule_overnight_boundaries_dates_pause_and_timezone():
    rule = {"enabled": True, "days": [0], "from": "22:00", "to": "02:00"}
    validate(rule, "America/Sao_Paulo")
    assert within(rule, datetime(2026, 11, 10, 4, tzinfo=UTC), "America/Sao_Paulo")
    assert not within(rule, datetime(2026, 11, 10, 5, tzinfo=UTC), "America/Sao_Paulo")
    assert not within(rule, datetime(2026, 11, 11, 4, tzinfo=UTC), "America/Sao_Paulo")
    cfg = {**DEFAULTS, "schedule_register": rule}
    at = datetime(2026, 11, 10, 4, tzinfo=UTC)
    assert allowed(cfg, "register", at)
    cfg["registrations_open"] = False
    assert not allowed(cfg, "register", at)
    assert state(cfg, "register", at)["reason"] == "paused"
    assert state(cfg, "register", at)["next"] == "2026-11-10T05:00:00+00:00"
    for bad in [
        {"from": "22:00"},
        {"from": "10:00", "to": "10:00"},
        {"days": [7]},
        {"start": "2026-11-11"},
        {"start": "2026-11-12T00:00+00:00", "end": "2026-11-11T00:00+00:00"},
    ]:
        with pytest.raises(ValueError):
            validate(bad, "America/Sao_Paulo")
    with pytest.raises(ValueError):
        validate({}, "Invalid/Zone")
    assert local_instant("2026-11-10T22:00", "America/Sao_Paulo") == "2026-11-11T01:00:00+00:00"
    assert date_br("2026-11-11T01:00:00+00:00") == "10/11/2026 às 22:00:00"
    assert input_date("2026-11-11T01:00:00+00:00") == "2026-11-10T22:00"
    assert "não reconhecido" in label("new_internal_code")
    future = {
        **DEFAULTS,
        "schedule_register": {
            "enabled": True,
            "start": "2027-01-04T12:00:00+00:00",
            "days": [0],
            "from": "22:00",
            "to": "02:00",
        },
    }
    assert state(future, "register", at)["next"] == "2027-01-05T01:00:00+00:00"


async def pending(db, vault, real=False):
    async with db.transaction() as s:
        await change_options(s, {"debounce": 0, "group_delay": 0})
        r = await register(s, vault, 800001, synthetic=not real)
        await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
        c = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        at = now() - timedelta(seconds=3)
        c.observed = at - timedelta(seconds=1)
        await observe(s, [dict(fixtures()[0], occupied=29)], "public" if real else "demo", at)
    async with db.transaction() as s:
        await observe(
            s,
            [dict(fixtures()[0], occupied=29)],
            "public" if real else "demo",
            at + timedelta(seconds=1),
        )
    return r.id


@pytest.mark.parametrize("action", ["pause", "delete", "expire", "schedule"])
async def test_budget_wait_has_no_claim_lock_or_connection_and_revalidates(db, vault, action):
    rid = await pending(db, vault)
    entered, release = asyncio.Event(), asyncio.Event()
    baseline = db.engine.pool.checkedout()

    class Budget:
        async def wait(self, key):
            assert key == rid
            entered.set()
            await release.wait()

    fake = FakeTransport()
    task = asyncio.create_task(dispatch_once(db, vault, fake, "demo", budget=Budget()))
    await asyncio.wait_for(entered.wait(), 3)
    assert db.engine.pool.checkedout() == baseline
    async with db.transaction() as s:
        item = await s.scalar(select(Outbox))
        assert item.status == "pending" and item.lease is None
        if action == "pause":
            await change_options(s, {"dispatch_paused": True})
        elif action == "delete":
            await asyncio.wait_for(erase(s, rid), 2)
        elif action == "expire":
            item.created = now() - timedelta(seconds=181)
        else:
            await change_options(
                s,
                {
                    "schedule_dispatch": {
                        "enabled": True,
                        "start": (now() + timedelta(days=1)).isoformat(),
                    }
                },
            )
    release.set()
    assert await asyncio.wait_for(task, 3) == 0
    assert not fake.views
    if action == "delete":
        async with db.transaction() as s:
            assert await s.get(Recipient, rid) is None


async def test_composed_dispatcher_and_telegram_http_spend_once(db, vault, monkeypatch):
    rid = await pending(db, vault, real=True)
    calls = []

    async def http(self, url, method, request_data=None, **kwargs):
        api = url.rsplit("/", 1)[-1]
        result = (
            {"id": 1, "is_bot": True, "first_name": "Synthetic"}
            if api == "getMe"
            else {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 800001, "type": "private"},
                "text": "Synthetic",
            }
        )
        return 200, json.dumps({"ok": True, "result": result}).encode()

    monkeypatch.setattr(HTTPXRequest, "do_request", http)

    async def wait(self, key=None):
        calls.append(key)

    monkeypatch.setattr(SendBudget, "wait", wait)
    bot = Bot("123456:synthetic_test_only", request=BudgetRequest(db, vault))
    async with bot:
        started = now()
        assert (
            await dispatch_once(db, vault, TelegramTransport(bot), "public", budget=SendBudget(db))
            == 1
        )
        assert calls == [rid]
        # A normal bot reply consumes its own permit, independently of the alert.
        await bot.send_message(800001, "Synthetic")
        assert calls == [rid, rid]
    async with db.transaction() as s:
        item = await s.scalar(select(Outbox))
        cd = await s.get(Cooldown, (rid, item.classroom_id))
        assert cd.until >= started + timedelta(seconds=DEFAULTS["cooldown"])


async def test_global_lock_released_when_final_metrics_fail(db, monkeypatch):
    original = db.transaction

    class Source:
        kind = "demo"

        async def discover(self):
            # Failure occurs in final metric persistence; the lock must still leave the pool.
            def fail():
                raise RuntimeError("synthetic persistence loss")

            monkeypatch.setattr(db, "transaction", fail)
            raise ValueError("synthetic source failure")

    with pytest.raises(RuntimeError, match="persistence"):
        await collect_catalog(db, Source(), "demo")
    monkeypatch.setattr(db, "transaction", original)
    async with db.engine.connect() as conn:
        assert await conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended('catalog:cycle',0))")
        )
        await conn.execute(text("SELECT pg_advisory_unlock(hashtextextended('catalog:cycle',0))"))


async def test_registration_closed_preserves_management_and_partial_updates(db, vault):
    async with db.transaction() as s:
        r = await register(s, vault, 800002)
        await change_options(s, {"registrations_open": False})
        with pytest.raises(ValueError, match="fechados"):
            await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
    await asyncio.gather(
        *[save(db, key, value) for key, value in [("interval", 5), ("cooldown", 100)]]
    )
    async with db.transaction() as s:
        cfg = await options(s)
        assert cfg["interval"] == 5 and cfg["cooldown"] == 100 and not cfg["registrations_open"]
        await erase(s, r.id)


async def save(db, key, value):
    async with db.transaction() as s:
        await change_options(s, {key: value})


async def test_429_longer_than_lease_waits_without_reservation(db, vault):
    from vagasunb.telegram import DeliveryError

    await pending(db, vault)
    clock = [1000.0]
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        async with db.transaction() as s:
            item = await s.scalar(select(Outbox))
            assert item.status == "retry"
            assert db.engine.pool.checkedout() == 1
        clock[0] += seconds

    budget = SendBudget(db, lambda: clock[0], sleep)

    class RateLimited(FakeTransport):
        async def send(self, chat, view):
            raise DeliveryError("rate_limit", 120)

    assert await dispatch_once(db, vault, RateLimited(), "demo", budget=budget) == 0
    async with db.transaction() as s:
        item = await s.scalar(select(Outbox))
        item.due = now() - timedelta(seconds=1)
    assert await dispatch_once(db, vault, FakeTransport(), "demo", budget=budget) == 1
    assert waits == [120]


async def test_catalog_changing_occupancy_across_units_preserves_complete_and_ids(db):
    class Source:
        kind = "demo"
        complete = True

        async def discover(self):
            return {
                "units": [{"id": "A"}, {"id": "B"}],
                "levels": [{"id": "G", "name": "Graduação"}],
            }

        async def collect(self, unit, period, level):
            return [dict(fixtures()[0], units=[unit], occupied=29 if unit == "A" else 28)]

    assert (await collect_catalog(db, Source(), "demo"))["status"] == "complete"
    async with db.transaction() as s:
        last = dict((await s.get(Setting, "catalog_complete_demo")).value)
        c = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        # A shared confirmation may legitimately observe A once more after B.
        assert c.occupied in {28, 29} and {"A", "B"}.issubset(c.units)

    class Bad(Source):
        async def discover(self):
            raise ValueError("synthetic failure")

    assert (await collect_catalog(db, Bad(), "demo"))["status"] == "degraded"
    async with db.transaction() as s:
        assert (await s.get(Setting, "catalog_complete_demo")).value == last


async def test_twenty_waiters_do_not_exhaust_single_connection(db, vault, test_url):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from vagasunb.db import Database

    rid = await pending(db, vault)
    async with db.transaction() as s:
        sid = (await s.scalar(select(Outbox))).subscription_id
    limited = Database(test_url)
    await limited.close()
    limited.engine = create_async_engine(test_url, pool_size=1, max_overflow=0, pool_timeout=1)
    limited.sessions = async_sessionmaker(limited.engine, expire_on_commit=False)
    ready, release = asyncio.Event(), asyncio.Event()
    waiting = [0]

    class Budget:
        async def wait(self, key):
            waiting[0] += 1
            if waiting[0] == 20:
                ready.set()
            await release.wait()

    fake = FakeTransport()
    tasks = [
        asyncio.create_task(
            dispatch_once(limited, vault, fake, "demo", candidate_pair=(rid, sid), budget=Budget())
        )
        for _ in range(20)
    ]
    try:
        await asyncio.wait_for(ready.wait(), 5)
        assert limited.engine.pool.checkedout() == 0
        release.set()
        assert sum(await asyncio.wait_for(asyncio.gather(*tasks), 5)) == 1
        assert len(fake.views) == 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await limited.close()


async def test_cancelled_catalog_returns_unlocked_connection(db):
    started = asyncio.Event()

    class Source:
        async def discover(self):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(collect_catalog(db, Source(), "demo"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with db.engine.connect() as conn:
        assert await conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended('catalog:cycle',0))")
        )
        await conn.execute(text("SELECT pg_advisory_unlock(hashtextextended('catalog:cycle',0))"))


async def test_reset_demo_refuses_public_catalog(db, test_url, tmp_path, vault):
    import os
    import subprocess
    import sys

    async with db.transaction() as s:
        await observe(s, [dict(fixtures()[0], period="2026.2")], "demo")
    result = subprocess.run(
        [sys.executable, "-m", "vagasunb.cli", "reset-demo", "--confirm"],
        env={**os.environ, "DATABASE_URL": test_url, "VAGAS_SECRETS": str(tmp_path / "secrets")},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode != 0
    async with db.transaction() as s:
        assert await s.scalar(select(Classroom.id).where(Classroom.period == "2026.2"))


async def test_demo_source_cannot_generate_public_delivery_for_private_recipient(db, vault):
    from vagasunb.demo import DemoSource
    from vagasunb.monitor import collect_scope

    async with db.transaction() as s:
        r = await register(s, vault, 800005, synthetic=False)
        await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
        await change_options(s, {"debounce": 0, "group_delay": 0})
        row = await s.get(Setting, "demo_source")
        row.value = {"rows": [dict(fixtures()[0], occupied=29)]}
    source = DemoSource(db)
    scope = fixtures()[0]["units"][0]
    await collect_scope(db, source, scope, "public")
    await collect_scope(db, source, scope, "public")
    async with db.transaction() as s:
        assert not (await s.scalars(select(Outbox))).all()


async def test_deleted_recipient_is_not_reintroduced_in_budget_after_wait(db, vault):
    rid = await pending(db, vault)
    clock = [1000.0]

    async def sleep(delay):
        async with db.transaction() as s:
            await erase(s, rid)
        clock[0] += delay

    budget = SendBudget(db, lambda: clock[0], sleep)
    await budget.defer(120)
    assert await budget.wait(rid) is False
    async with db.transaction() as s:
        assert rid not in (await s.get(Setting, "send_budget")).value.get("chats", {})
