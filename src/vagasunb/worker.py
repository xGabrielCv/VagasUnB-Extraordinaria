import asyncio
import os
import signal
import time
from pathlib import Path

from vagasunb.config import Settings
from vagasunb.crypto import Vault
from sqlalchemy import select
from vagasunb.db import Classroom, Database, Event, Setting, Subscription, now
from vagasunb.demo import DemoSource
from vagasunb.monitor import collect_scope, dispatch_once
from vagasunb.services import cleanup, options, verify_instance
from vagasunb.sigaa_public import PublicSource
from vagasunb.telegram import FakeTransport


async def run(settings=None):
    cfg = settings or Settings.load()
    db, vault = Database(cfg.database_url), Vault(cfg.secrets)
    async with db.transaction() as s:
        await verify_instance(s, vault, cfg.mode)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:
        pass
    source = DemoSource(db) if cfg.mode == "demo" else PublicSource()
    health = {"collect": "starting", "dispatch": "starting", "cleanup": "starting"}
    real_bot = None
    transport = FakeTransport()
    if cfg.mode == "telegram" and os.getenv("VAGAS_TELEGRAM_ENABLE") == "1":
        from telegram import Bot
        from vagasunb.telegram import TelegramTransport

        real_bot = Bot((cfg.secrets / "telegram_token").read_text().strip())
        await real_bot.initialize()
        transport = TelegramTransport(real_bot)

    last_structural = 0.0

    async def collector():
        nonlocal last_structural
        async with db.transaction() as s:
            ops = await options(s)
            req = await s.get(Setting, "collect_request")
            requested = req.value["scope"] if req else None
            if req:
                await s.delete(req)
            monitored_codes = set(
                (
                    await s.scalars(
                        select(Subscription.code).where(Subscription.period == ops["period"])
                    )
                ).all()
            )
            classes = (
                await s.scalars(
                    select(Classroom).where(
                        Classroom.period == ops["period"], Classroom.code.in_(monitored_codes)
                    )
                )
            ).all()
            monitored_units = {unit for c in classes for unit in c.units}
        structural_due = time.monotonic() - last_structural >= ops["structural_interval"]
        if requested:
            scopes = [requested]
        elif structural_due:
            scopes = ops["scopes"]
            last_structural = time.monotonic()
        else:
            scopes = [unit for unit in ops["scopes"] if unit in monitored_units]
        results = []
        for scope in scopes:
            if stop.is_set():
                break
            results.append(await collect_scope(db, source, scope, cfg.mode))
        return max(1, ops["interval"]), all(results)

    async def dispatcher():
        if cfg.mode == "demo":
            await dispatch_once(db, vault, FakeTransport(), "demo")
        elif real_bot:
            await dispatch_once(db, vault, transport, "telegram")
        return 1

    async def cleaner():
        async with db.transaction() as s:
            await cleanup(s)
        from vagasunb.operations import maintain_logs

        maintain_logs(os.getenv("VAGAS_PRIVATE", ".private"))
        return 3600

    async def supervise(name, action):
        while not stop.is_set():
            delay = 5
            try:
                delay = await action()
                if isinstance(delay, tuple):
                    delay, valid = delay
                    health[name] = "ok" if valid else "degraded"
                else:
                    health[name] = "ok"
            except Exception:
                health[name] = "degraded"
                try:
                    async with db.transaction() as s:
                        s.add(
                            Event(category="worker", code="task_recovered", values={"task": name})
                        )
                except Exception:
                    pass  # DB outage must not kill the supervisor itself.
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    tasks = [
        asyncio.create_task(supervise(name, fn))
        for name, fn in [("collect", collector), ("dispatch", dispatcher), ("cleanup", cleaner)]
    ]
    try:
        while not stop.is_set():
            if Path(".private/shutdown").exists():
                stop.set()
                break
            if any(task.done() for task in tasks):
                raise RuntimeError("Tarefa supervisionada terminou inesperadamente")
            try:
                async with db.transaction() as s:
                    await s.merge(
                        Setting(
                            key="worker_health",
                            value={
                                **health,
                                "at": now().isoformat(),
                                "circuit_failures": getattr(source, "failures", 0),
                                "circuit_wait_s": round(
                                    max(0, getattr(source, "next_attempt", 0) - time.monotonic())
                                ),
                            },
                        )
                    )
            except Exception:
                # No heartbeat is written while database is unreachable: health degrades visibly.
                await asyncio.sleep(2)
                continue
            heartbeat_dir = Path(os.getenv("VAGAS_HEARTBEAT_DIR", ".private"))
            heartbeat_dir.mkdir(exist_ok=True)
            (heartbeat_dir / "worker.heartbeat").write_text(str(time.time()))
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if real_bot:
            await real_bot.shutdown()
        await db.close()
