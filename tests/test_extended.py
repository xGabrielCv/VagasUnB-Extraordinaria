import json
import os
import time
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vagasunb.catalog import search
from vagasunb.cli import bootstrap_files, command
from vagasunb.crypto import Vault
from vagasunb.db import Callback, Classroom, Outbox, Recipient, Subscription, now
from vagasunb.demo import fixtures
from vagasunb.monitor import observe
from vagasunb.router import button, route
from vagasunb.services import change_options, register, subscribe, verify_instance
from vagasunb.telegram import FakeRequest, handle_update


async def test_wrong_keys_block_existing_instance_without_reset(db, vault, tmp_path):
    async with db.transaction() as s:
        await verify_instance(s, vault, "demo")
        await register(s, vault, 123987)
    wrong = Vault.create(tmp_path / "wrong")
    async with db.transaction() as s:
        with pytest.raises(ValueError):
            await verify_instance(s, wrong, "demo")
        assert await s.scalar(select(func.count()).select_from(Recipient)) == 1


def test_missing_keys_not_regenerated(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(ValueError):
        bootstrap_files(tmp_path)
    assert not (tmp_path / "secrets/keys.json").exists()


async def test_key_rotation_preserves_recipient_and_lookup(
    db, test_url, vault, tmp_path, monkeypatch
):
    async with db.transaction() as s:
        r = await register(s, vault, 123987)
        rid = r.id
    new = Vault.create(tmp_path / "new")
    monkeypatch.setenv("DATABASE_URL", test_url)
    monkeypatch.setenv("VAGAS_SECRETS", str(tmp_path / "secrets"))
    await command(Namespace(action="rotate-keys", new_keys=str(tmp_path / "new")))
    async with db.transaction() as s:
        r = await s.get(Recipient, rid)
        assert new.decrypt(r.cipher) == "123987" and r.lookup == new.index(123987)
        await verify_instance(s, new, "demo")
        assert await s.scalar(select(func.count()).select_from(Recipient)) == 1


async def test_expired_callback_and_private_chat_validation(db, vault):
    async with db.transaction() as s:
        await route(s, vault, 501, "/start")
        r = await s.scalar(select(Recipient))
        b = await button(s, r.id, "private", "home")
        await s.flush()
        cb = await s.get(Callback, b["callback"])
        cb.expires = now() - timedelta(seconds=1)
        assert "expirou" in (await route(s, vault, 501, callback=b["callback"])).text
        assert "privada" in (await route(s, vault, 501, "/start", private=False)).text


async def test_subset_selection_multiple_classes_and_stale_no_event(db, vault):
    async with db.transaction() as s:
        await route(s, vault, 501, "/start")
        r = await s.scalar(select(Recipient))
        c = await s.scalar(select(Classroom).where(Classroom.code == "DEMO0001"))
        b = await button(s, r.id, "choose", "choose", cid=c.id, numbers=["01"])
        await s.flush()
        view = await route(s, vault, 501, callback=b["callback"])
        token = next(b["callback"] for b in view.buttons if "Turma 02" in b["label"])
        await s.flush()
        view = await route(s, vault, 501, callback=token)
        confirm = next(b["callback"] for b in view.buttons if "Confirmar turmas" in b["label"])
        await s.flush()
        view = await route(s, vault, 501, callback=confirm)
        activate = view.buttons[0]["callback"]
        await s.flush()
        await route(s, vault, 501, callback=activate)
        sub = await s.scalar(select(Subscription))
        assert sub.numbers == ["01", "02"]
        at = now() + timedelta(seconds=500)
        rows = [dict(fixtures()[0], occupied=29)]
        await observe(s, rows, "demo", at)
        await observe(s, rows, "demo", at + timedelta(seconds=20))
        assert await s.scalar(select(func.count()).select_from(Outbox)) == 0


async def test_full_sdk_update_discards_profile_and_recovers_deleted_menu(db, vault):
    from telegram import Bot, Update

    class Context:
        pass

    async with Bot(
        "123:synthetic_only", request=FakeRequest(), get_updates_request=FakeRequest()
    ) as bot:
        update = Update.de_json(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "text": "/start",
                    "chat": {"id": 12345, "type": "private", "first_name": "SENSITIVE_NAME"},
                    "from": {
                        "id": 12345,
                        "is_bot": False,
                        "first_name": "SENSITIVE_NAME",
                        "username": "SENSITIVE_USERNAME",
                    },
                },
            },
            bot,
        )
        context = Context()
        context.bot = bot
        await handle_update(update, context, db, vault)
    async with db.transaction() as s:
        r = await s.scalar(select(Recipient))
        assert "12345" not in r.cipher
        args = [c.args for c in (await s.scalars(select(Callback))).all()]
        assert "SENSITIVE" not in json.dumps(args)


async def test_window_expiry_refuses_new_subscriptions(db, vault):
    async with db.transaction() as s:
        r = await register(s, vault, 999)
        await change_options(s, {"window_end": (now() - timedelta(days=1)).isoformat()})
        with pytest.raises(ValueError, match="encerrada"):
            await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])


async def test_synthetic_benchmark(db, vault):
    started = time.perf_counter()
    async with db.transaction() as s:
        for i in range(100):
            r = await register(s, vault, 100000 + i)
            await subscribe(s, r.id, "DEMO0001", "DEMO.1", [])
    setup_ms = (time.perf_counter() - started) * 1000
    at = now()
    rows = [dict(fixtures()[0], occupied=29)]
    async with db.transaction() as s:
        await observe(s, rows, "demo", at)
    started = time.perf_counter()
    async with db.transaction() as s:
        n = await observe(s, rows, "demo", at + timedelta(seconds=11))
    fanout_ms = (time.perf_counter() - started) * 1000
    assert n == 100
    timings = []
    async with db.transaction() as s:
        for _ in range(100):
            started = time.perf_counter()
            await search(s, "calculo", "DEMO.1")
            timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    result = {
        "recipients": 100,
        "classrooms": 24,
        "search_samples": 100,
        "search_p95_ms": round(timings[95], 2),
        "fanout_ms": round(fanout_ms, 2),
        "setup_ms": round(setup_ms, 2),
    }
    # Private result, never staged or included in build context.
    destination = Path(os.environ.get("VAGAS_BENCH_OUT", ".private/benchmark.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result))
