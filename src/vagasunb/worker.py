import asyncio
import os
import signal
import time
from pathlib import Path

from vagasunb.collection import collect_catalog, next_delay
from vagasunb.config import Settings
from vagasunb.crypto import Vault
from vagasunb.db import Database, Event, Setting, now
from vagasunb.demo import DemoSource
from vagasunb.distribution import SendBudget, dispatch_batch
from vagasunb.services import cleanup, options, verify_instance
from vagasunb.schedule import allowed, state
from vagasunb.sigaa_public import PublicSource
from vagasunb.telegram import FakeTransport


async def run(settings=None):
    cfg = settings or Settings.load()
    db, vault = Database(cfg.database_url), Vault(cfg.secrets)
    async with db.transaction() as s:
        await verify_instance(s, vault, cfg.mode)
    stop = asyncio.Event()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    except NotImplementedError:
        pass
    health = {"collect": "starting", "dispatch": "starting", "cleanup": "starting"}
    demo, public = DemoSource(db), PublicSource()
    fake = FakeTransport(record=False)
    budget = SendBudget(db)
    last_start = 0.0
    real_bot = None
    if cfg.mode == "public" and os.getenv("VAGAS_TELEGRAM_ENABLE") == "1":
        from telegram import Bot
        from vagasunb.telegram import BudgetRequest, TelegramTransport

        real_bot = Bot(
            (cfg.secrets / "telegram_token").read_text().strip(), request=BudgetRequest(db, vault)
        )
        await real_bot.initialize()
        transport = TelegramTransport(real_bot)
    else:
        transport = fake

    async def collector():
        nonlocal last_start
        while not stop.is_set():
            async with db.transaction() as s:
                ops = await options(s)
                request = await s.get(Setting, "collect_request")
                requested = bool(request)
            source = public if ops["source"] == "public" else demo
            delay = next_delay(
                last_start, ops["interval"], source.next_attempt if source is public else 0
            )
            if (
                allowed(ops, "collect", now())
                and (requested or (ops["collection_auto"] and delay <= 0))
                and time.monotonic() >= getattr(source, "next_attempt", 0)
            ):
                if requested:
                    async with db.transaction() as s:
                        request = await s.get(Setting, "collect_request")
                        if request:
                            await s.delete(request)
                last_start = time.monotonic()
                health["collect"] = "running"
                result = await collect_catalog(db, source, cfg.mode)
                health["collect"] = "ok" if result["status"] == "complete" else result["status"]
            elif not allowed(ops, "collect", now()):
                health["collect"] = state(ops, "collect", now())["reason"]
            elif not ops["collection_auto"]:
                health["collect"] = "manual"
            elif health["collect"] in {"paused", "scheduled", "manual", "starting"}:
                health["collect"] = "waiting"
            await asyncio.sleep(0.5)

    async def dispatcher():
        while not stop.is_set():
            health["dispatch"] = "running"
            if cfg.mode == "demo" or real_bot:
                await dispatch_batch(
                    db,
                    vault,
                    transport,
                    "demo" if cfg.mode == "demo" else "public",
                    budget=budget,
                )
            async with db.transaction() as s:
                ops = await options(s)
            health["dispatch"] = (
                state(ops, "dispatch", now())["reason"]
                if not allowed(ops, "dispatch", now())
                else ("simulated" if cfg.mode == "demo" else "enabled" if real_bot else "disabled")
            )
            await asyncio.sleep(0.1)

    async def cleaner():
        while not stop.is_set():
            async with db.transaction() as s:
                await cleanup(s)
            from vagasunb.operations import maintain_logs

            maintain_logs(cfg.private_dir)
            health["cleanup"] = "ok"
            await asyncio.sleep(3600)

    async def supervised(name, action):
        while not stop.is_set():
            try:
                await action()
            except Exception as exc:
                from vagasunb.operations import error_code

                health[name] = error_code(exc)
                try:
                    async with db.transaction() as s:
                        s.add(Event(category="worker", code=error_code(exc), values={"task": name}))
                except Exception:
                    pass
                await asyncio.sleep(3)

    tasks = [
        asyncio.create_task(supervised(name, fn))
        for name, fn in [("collect", collector), ("dispatch", dispatcher), ("cleanup", cleaner)]
    ]
    try:
        while not stop.is_set():
            if (cfg.private_dir / "shutdown").exists():
                break
            if any(t.done() for t in tasks):
                raise RuntimeError("worker_task_ended")
            try:
                async with db.transaction() as s:
                    ops = await options(s)
                    delay = next_delay(last_start, ops["interval"], public.next_attempt)
                    await s.merge(
                        Setting(
                            key="worker_health",
                            value={
                                **health,
                                "pid": os.getpid(),
                                "at": now().isoformat(),
                                "next_attempt_s": round(delay, 2),
                                "desired_s": ops["interval"],
                                "backoff_s": round(
                                    max(0, public.next_attempt - time.monotonic()), 2
                                ),
                            },
                        )
                    )
                directory = Path(os.getenv("VAGAS_HEARTBEAT_DIR", str(cfg.private_dir)))
                directory.mkdir(exist_ok=True)
                (directory / "worker.heartbeat").write_text(str(time.time()))
            except Exception:
                pass  # Stale DB and file heartbeats visibly degrade health.
            await asyncio.sleep(2)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await public.aclose()
        if real_bot:
            await real_bot.shutdown()
        await db.close()
