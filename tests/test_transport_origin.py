from datetime import timedelta

from sqlalchemy import select

from vagasunb.db import Classroom, Outbox, now
from vagasunb.demo import fixtures
from vagasunb.monitor import observe, dispatch_once
from vagasunb.services import register, subscribe, change_options, verify_instance
from vagasunb.telegram import FakeTransport


async def test_transport_switch_preserves_catalog_and_fences_demo(db, vault):
    async with db.transaction() as s:
        await verify_instance(s, vault, "demo")
        await verify_instance(s, vault, "public")
        await change_options(s, {"debounce": 0, "group_delay": 0})
        demo = await register(s, vault, -900001)
        private = await register(s, vault, 123456, synthetic=False)
        for r in [demo, private]:
            await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
        c = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        before = c.id
        t = now() - timedelta(seconds=2)
        c.observed = t - timedelta(seconds=1)
        await observe(s, [dict(fixtures()[0], occupied=29)], "public", t)
    async with db.transaction() as s:
        assert (
            await observe(s, [dict(fixtures()[0], occupied=29)], "public", t + timedelta(seconds=1))
            == 2
        )
        assert (await s.get(Classroom, before)).id == before
        assert {r.mode for r in (await s.scalars(select(Outbox))).all()} == {"demo", "public"}
    # A public catalog with sending disabled leaves real pending items intact.
    assert await dispatch_once(db, vault, FakeTransport(), "public") == 0
    assert await dispatch_once(db, vault, FakeTransport(), "demo") == 1
    async with db.transaction() as s:
        assert (await s.scalar(select(Outbox).where(Outbox.mode == "public"))).status == "pending"

    class SimulatedFutureTransport(FakeTransport):
        real_enabled = True

    future = SimulatedFutureTransport()
    assert await dispatch_once(db, vault, future, "demo") == 0
    assert await dispatch_once(db, vault, future, "public") == 1
    assert len(future.views) == 1
