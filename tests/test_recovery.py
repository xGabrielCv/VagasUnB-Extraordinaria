import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy import select

from vagasunb.db import Outbox, now
from vagasunb.monitor import collect_scope, dispatch_once
from vagasunb.operations import maintain_logs
from vagasunb.telegram import FakeTransport
from test_core import opening, prepare


async def test_claim_survives_crash_after_acceptance_before_ack(db, vault):
    await prepare(db, vault)
    at, _ = await opening(db)

    class Crash(BaseException):
        pass

    class CrashAfterAccepted:
        async def send(self, chat, view):
            raise Crash()

    with pytest.raises(Crash):
        await dispatch_once(db, vault, CrashAfterAccepted(), "demo", at)
    async with db.transaction() as s:
        assert (await s.scalar(select(Outbox))).status == "sending"
    assert await dispatch_once(db, vault, FakeTransport(), "demo", at + timedelta(seconds=61)) == 0
    async with db.transaction() as s:
        assert (await s.scalar(select(Outbox))).status == "uncertain"


async def test_scope_lock_blocks_overlapping_collection(db):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow:
        calls = 0

        async def collect(self, scope, period):
            self.calls += 1
            entered.set()
            await release.wait()
            from vagasunb.demo import fixtures

            return [r for r in fixtures() if scope in r["units"]]

    source = Slow()
    first = asyncio.create_task(collect_scope(db, source, "Unidade sintética A", "demo"))
    await asyncio.wait_for(entered.wait(), 5)
    assert not await collect_scope(db, source, "Unidade sintética A", "demo")
    release.set()
    assert await asyncio.wait_for(first, 5)
    assert source.calls == 1


def test_log_retention_bounds_only_named_files(tmp_path):
    old = tmp_path / "admin.log"
    old.write_text("old")
    expired = (now() - timedelta(days=8)).timestamp()
    os.utime(old, (expired, expired))
    huge = tmp_path / "worker.log"
    with huge.open("wb") as f:
        f.truncate(1024)
    unrelated = tmp_path / "owner-file.txt"
    unrelated.write_text("preserve")
    maintain_logs(tmp_path, max_bytes=100)
    assert old.stat().st_size == 0 and huge.stat().st_size == 0
    assert unrelated.read_text() == "preserve"
