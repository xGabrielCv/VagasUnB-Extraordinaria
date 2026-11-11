"""A bounded dispatcher leader and PostgreSQL budget shared with bot replies."""

import asyncio
import time
from contextvars import ContextVar

from sqlalchemy import String, cast, func, select, text

from vagasunb.db import Outbox, Recipient, Setting, lock, now
from vagasunb.services import options
from vagasunb.schedule import allowed


# Task-local single-use permit shared with the Telegram HTTP adapter.
send_permit = ContextVar("send_permit", default=None)


class SendBudget:
    def __init__(self, db, clock=time.time, sleep=asyncio.sleep):
        self.db, self.clock, self.sleep = db, clock, sleep

    async def wait(self, recipient=None):
        while True:
            async with self.db.transaction() as s:
                await lock(s, "send", "budget")
                if recipient and not await s.get(Recipient, recipient):
                    return False
                cfg = await options(s)
                row = await s.get(Setting, "send_budget")
                value = dict(row.value) if row else {}
                clock = self.clock()
                chats = {k: v for k, v in value.get("chats", {}).items() if v > clock}
                delay = (
                    max(value.get("next", 0), value.get("blocked", 0), chats.get(recipient, 0))
                    - clock
                )
                if delay <= 0:
                    if recipient:
                        chats[recipient] = clock + 1
                    value.update(next=clock + 1 / cfg["send_rate"], chats=chats)
                    await s.merge(Setting(key="send_budget", value=value))
                    return True
            await self.sleep(delay)

    async def defer(self, seconds):
        async with self.db.transaction() as s:
            await lock(s, "send", "budget")
            row = await s.get(Setting, "send_budget")
            value = dict(row.value) if row else {}
            value["blocked"] = max(value.get("blocked", 0), self.clock() + seconds)
            await s.merge(Setting(key="send_budget", value=value))


async def dispatch_batch(db, vault, transport, mode, *, limit=100, budget=None):
    from vagasunb.monitor import dispatch_once

    budget = budget or SendBudget(db)
    async with db.engine.connect() as leader:
        acquired = await leader.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended('dispatch:leader',0))")
        )
        await leader.commit()
        if not acquired:
            return 0
        try:
            async with db.transaction() as s:
                cfg = await options(s)
                if not allowed(cfg, "dispatch", now()) or (
                    mode != "demo" and not getattr(transport, "real_enabled", False)
                ):
                    return 0
                # Interleave events, order each cohort by a cycle-dependent lottery.
                # No priority derived from registration date or an external identifier.
                rank = func.row_number().over(
                    partition_by=(Outbox.classroom_id, Outbox.cycle),
                    order_by=func.md5(Outbox.recipient_id + cast(Outbox.cycle, String)),
                )
                candidates = (
                    select(
                        Outbox.recipient_id,
                        Outbox.subscription_id,
                        Outbox.created,
                        rank.label("rank"),
                    )
                    .where(
                        Outbox.status.in_(["pending", "retry"]),
                        Outbox.due <= now(),
                        Outbox.mode == mode,
                    )
                    .subquery()
                )
                pairs = (
                    await s.execute(
                        select(candidates.c.recipient_id, candidates.c.subscription_id)
                        .order_by(candidates.c.rank, candidates.c.created)
                        .limit(limit)
                    )
                ).all()
            jobs = asyncio.Queue()
            for pair in dict.fromkeys(tuple(p) for p in pairs):
                jobs.put_nowait(pair)

            async def consumer():
                total = 0
                while not jobs.empty():
                    pair = jobs.get_nowait()
                    total += await dispatch_once(
                        db, vault, transport, mode, candidate_pair=pair, budget=budget
                    )
                return total

            return sum(
                await asyncio.gather(
                    *(consumer() for _ in range(min(cfg["send_concurrency"], jobs.qsize())))
                )
            )
        finally:
            from vagasunb.db import release_session_lock

            await release_session_lock(leader, "dispatch:leader")
