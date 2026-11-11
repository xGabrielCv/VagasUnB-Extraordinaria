import asyncio
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from vagasunb.db import Classroom, now
from vagasunb.demo import fixtures
from vagasunb.distribution import dispatch_batch
from vagasunb.monitor import observe
from vagasunb.services import change_options, register, subscribe


async def test_100_recipient_measured_distribution(db, vault, tmp_path):
    async with db.transaction() as s:
        await change_options(s, {"group_delay": 0, "debounce": 0})
        for i in range(100):
            r = await register(s, vault, 500000 + i)
            await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
        at = now() - timedelta(seconds=2)
        c = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        c.observed = at - timedelta(seconds=1)
        await observe(s, [dict(fixtures()[0], occupied=29)], "demo", at)
    fanout_start = time.perf_counter()
    async with db.transaction() as s:
        assert (
            await observe(s, [dict(fixtures()[0], occupied=29)], "demo", at + timedelta(seconds=1))
            == 100
        )
    fanout_ms = (time.perf_counter() - fanout_start) * 1000
    started = time.perf_counter()

    class FakeNetwork:
        active = maximum = 0
        sent = []

        async def send(self, chat, view):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.sent.append((chat, time.perf_counter() - started))
            await asyncio.sleep(0.15)
            self.active -= 1

    fake = FakeNetwork()
    assert await dispatch_batch(db, vault, fake, "demo") == 100
    assert len({chat for chat, _ in fake.sent}) == 100 and 1 < fake.maximum <= 6
    times = sorted(t for _, t in fake.sent)
    elapsed = time.perf_counter() - started
    assert times[-1] - times[0] >= 99 / 25 - 0.1
    assert elapsed < 20  # A regression to one-recipient-per-second would fail decisively.
    report = {
        "recipients": 100,
        "fake_network_s": 0.15,
        "max_concurrency": fake.maximum,
        "fanout_ms": round(fanout_ms, 3),
        "first_s": round(times[0], 3),
        "last_s": round(times[-1], 3),
        "spread_s": round(times[-1] - times[0], 3),
        "wall_s": round(elapsed, 3),
        "duplicates": 0,
        "failures": 0,
        "source": "synthetic transport, real PostgreSQL budget, local clock",
    }
    destination = Path(os.environ.get("VAGAS_DISTRIBUTION_OUT", tmp_path / "distribution.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report))
