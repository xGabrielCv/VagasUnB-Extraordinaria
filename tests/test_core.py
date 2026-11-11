import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from vagasunb.catalog import search
from vagasunb.crypto import Vault
from vagasunb.db import Callback, Classroom, Event, Outbox, Recipient, Subscription, now
from vagasunb.demo import fixtures
from vagasunb.monitor import collect_scope, dispatch_once, observe
from vagasunb.router import button, route
from vagasunb.services import cleanup, erase, register, subscribe, subscription_action
from vagasunb.telegram import DeliveryError, FakeRequest, FakeTransport, TelegramTransport


async def prepare(db, vault, all_classes=True):
    async with db.transaction() as s:
        r = await register(s, vault, -987654321)
        sub, _ = await subscribe(s, r.id, "DEMO0001", "DEMO.1", [] if all_classes else ["01"])
        classes = (
            await s.scalars(
                select(Classroom).where(Classroom.code == "DEMO0001").order_by(Classroom.number)
            )
        ).all()
        return r.id, sub.id, [c.id for c in classes]


async def opening(db, at=None, numbers=("01",)):
    at = at or now()
    rows = [r for r in fixtures() if r["code"] == "DEMO0001" and r["number"] in numbers]
    for r in rows:
        r["occupied"] = 29
    async with db.transaction() as s:
        await observe(s, rows, "demo", at=at)
    async with db.transaction() as s:
        count = await observe(s, rows, "demo", at=at + timedelta(seconds=11))
    return at + timedelta(seconds=15), count


def test_crypto_integrity_and_separate_index(vault, tmp_path):
    a, b = vault.encrypt("123456789"), vault.encrypt("123456789")
    assert a != b and vault.decrypt(a) == "123456789"
    assert vault.index("000123") == vault.index(123)
    assert "123456789" not in a
    with pytest.raises(ValueError):
        vault.decrypt(a[:-4] + "AAAA")
    with pytest.raises(ValueError):
        Vault(tmp_path / "missing")
    with pytest.raises(ValueError):
        Vault.create(tmp_path / "wrong").decrypt(a)


@pytest.mark.parametrize("query", ["DEMO0001", "demo1", " CÁLCULO   experimental ", "DEMO0001 1"])
async def test_search_normalizes(db, query):
    async with db.transaction() as s:
        rows, total, _ = await search(s, query, "DEMO.1")
        assert rows and rows[0].code == "DEMO0001"
        if query.endswith(" 1"):
            assert total == 1 and rows[0].number == "01"


async def test_search_period_pagination_unknown_and_multiple_units(db):
    async with db.transaction() as s:
        first, total, _ = await search(s, "", "DEMO.1")
        second, _, _ = await search(s, "", "DEMO.1", page=1)
        assert total == 24 and not {c.id for c in first} & {c.id for c in second}
        unknown, _, _ = await search(s, "", "DEMO.1", campus="?")
        assert all(c.campus is None for c in unknown)
        assert not (await search(s, "", "OTHER"))[0]
        c = first[0]
        c.units = ["A", "B"]
        await s.flush()
        assert (await search(s, c.code, "DEMO.1", unit="B"))[0]


async def test_limit_ten_including_paused_and_concurrent_eleventh(db, vault):
    async with db.transaction() as s:
        r = await register(s, vault, 1001)
        rid = r.id
        for i in range(1, 10):
            sub, _ = await subscribe(s, rid, f"DEMO{i:04d}", "DEMO.1", [])
        await subscription_action(s, rid, sub.id, "pause")

    async def add(code):
        try:
            async with db.transaction() as s:
                await subscribe(s, rid, code, "DEMO.1", [])
            return True
        except ValueError:
            return False

    results = await asyncio.gather(add("DEMO0010"), add("DEMO0011"))
    assert results.count(True) == 1
    async with db.transaction() as s:
        assert await s.scalar(select(func.count()).select_from(Subscription)) == 10


async def test_subscription_update_idempotent(db, vault):
    rid, sid, _ = await prepare(db, vault)
    async with db.transaction() as s:
        sub, created = await subscribe(s, rid, "DEMO0001", "DEMO.1", [])
        assert sub.id == sid and not created
        sub, created = await subscribe(s, rid, "DEMO0001", "DEMO.1", ["01", "02"])
        assert sub.id == sid and sub.revision == 2


async def test_baseline_debounce_continuous_cooldown(db, vault):
    await prepare(db, vault)
    async with db.transaction() as s:
        assert await s.scalar(select(func.count()).select_from(Outbox)) == 0
    at, count = await opening(db)
    assert count == 1
    transport = FakeTransport()
    assert await dispatch_once(db, vault, transport, "demo", at) == 1
    rows = [r for r in fixtures() if r["code"] == "DEMO0001" and r["number"] == "01"]
    rows[0]["occupied"] = 28
    async with db.transaction() as s:
        assert await observe(s, rows, "demo", at + timedelta(seconds=1)) == 0
    rows[0]["occupied"] = 30
    async with db.transaction() as s:
        await observe(s, rows, "demo", at + timedelta(seconds=2))
    _, count = await opening(db, at + timedelta(seconds=3))
    assert count == 0
    assert len(transport.views) == 1


async def test_debounce_needs_new_observation(db, vault):
    await prepare(db, vault)
    rows = [dict(fixtures()[0], occupied=29)]
    async with db.transaction() as s:
        await observe(s, rows, "demo")
    assert (
        await dispatch_once(db, vault, FakeTransport(), "demo", now() + timedelta(seconds=30)) == 0
    )


async def test_partial_failure_preserves_state_and_unknown_not_zero(db, vault):
    await prepare(db, vault)
    at, _ = await opening(db)
    async with db.transaction() as s:
        before = await s.scalar(
            select(Classroom).where(Classroom.code == "DEMO0001", Classroom.number == "01")
        )
        assert before.available == 1
        observed = before.observed
        await observe(s, [dict(fixtures()[0], occupied=None)], "demo", at + timedelta(seconds=1))
        assert before.available == 1 and before.observed == observed

    class BadSource:
        async def collect(self, scope, period):
            return []

    assert not await collect_scope(db, BadSource(), "Unidade sintética A", "demo")
    async with db.transaction() as s:
        assert (await s.get(Classroom, before.id)).available == 1


async def test_stale_recovery_and_delayed_response(db, vault):
    _, _, ids = await prepare(db, vault)
    t = now()
    rows = [dict(fixtures()[0], occupied=29)]
    async with db.transaction() as s:
        await observe(s, rows, "demo", t + timedelta(seconds=500))
        assert await s.scalar(select(func.count()).select_from(Outbox)) == 0
        await observe(s, [dict(rows[0], occupied=30)], "demo", t - timedelta(seconds=10))
        assert (await s.get(Classroom, ids[0])).available == 1


async def test_whole_subject_new_class_baseline_and_grouping(db, vault):
    await prepare(db, vault)
    async with db.transaction() as s:
        await observe(s, [dict(fixtures()[0], number="03", occupied=28)], "demo")
        assert await s.scalar(select(func.count()).select_from(Outbox)) == 0
    at, n = await opening(db, numbers=("01", "02"))
    assert n == 2
    fake = FakeTransport()
    assert await dispatch_once(db, vault, fake, "demo", at) == 2
    assert (
        len(fake.views) == 1
        and "Turma 01" in fake.views[0].text
        and "Turma 02" in fake.views[0].text
    )


@pytest.mark.parametrize("action", ["pause", "stop", "complete", "erase"])
async def test_invalidates_pending(db, vault, action):
    rid, sid, _ = await prepare(db, vault)
    at, _ = await opening(db)
    async with db.transaction() as s:
        if action == "erase":
            await erase(s, rid)
        else:
            await subscription_action(s, rid, sid, action)
    assert await dispatch_once(db, vault, FakeTransport(), "demo", at) == 0


async def test_two_workers_send_once(db, vault):
    await prepare(db, vault)
    at, _ = await opening(db)
    fake = FakeTransport()
    results = await asyncio.gather(
        dispatch_once(db, vault, fake, "demo", at), dispatch_once(db, vault, fake, "demo", at)
    )
    assert sum(results) == 1 and len(fake.views) == 1


@pytest.mark.parametrize(
    "code,status",
    [
        ("rate_limit", "retry"),
        ("forbidden", "failed"),
        ("invalid_destination", "failed"),
        ("uncertain", "uncertain"),
        ("transient", "retry"),
    ],
)
async def test_delivery_errors(db, vault, code, status):
    await prepare(db, vault)
    at, _ = await opening(db)

    class Broken:
        async def send(self, chat, view):
            raise DeliveryError(code, retry_after=123)

    assert await dispatch_once(db, vault, Broken(), "demo", at) == 0
    async with db.transaction() as s:
        item = await s.scalar(select(Outbox))
        assert item.status == status
        if code == "rate_limit":
            assert item.due == at + timedelta(seconds=123)


async def test_expired_claim_is_uncertain(db, vault):
    await prepare(db, vault)
    at, _ = await opening(db)
    async with db.transaction() as s:
        item = await s.scalar(select(Outbox))
        item.status, item.lease = "sending", at - timedelta(seconds=1)
    assert await dispatch_once(db, vault, FakeTransport(), "demo", at) == 0
    async with db.transaction() as s:
        assert (await s.scalar(select(Outbox))).status == "uncertain"


async def test_deletion_waits_inflight_and_prevents_new_sends(db, vault):
    rid, _, _ = await prepare(db, vault)
    at, _ = await opening(db)
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow:
        async def send(self, chat, view):
            entered.set()
            await release.wait()

    send = asyncio.create_task(dispatch_once(db, vault, Slow(), "demo", at))
    await asyncio.wait_for(entered.wait(), 5)

    async def delete():
        async with db.transaction() as s:
            await erase(s, rid)

    erasing = asyncio.create_task(delete())
    await asyncio.sleep(0.05)
    assert not erasing.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(send, erasing), 5)
    assert await dispatch_once(db, vault, FakeTransport(), "demo", at) == 0


async def test_callbacks_bound_single_use_and_erase_no_recreate(db, vault):
    async with db.transaction() as s:
        view = await route(s, vault, 501, "/start")
        token = view.buttons[0]["callback"]
        assert len(token.encode()) <= 64
        await route(s, vault, 502, "/start")
        assert "expirou" in (await route(s, vault, 502, callback=token)).text
        assert "Buscar" in (await route(s, vault, 501, callback=token)).text
        assert "expirou" in (await route(s, vault, 501, callback=token)).text
        r = await s.scalar(select(Recipient).where(Recipient.lookup == vault.index(501)))
        b = await button(s, r.id, "erase", "erase")
        await s.flush()
        await route(s, vault, 501, callback=b["callback"])
        assert "não estão cadastrados" in (await route(s, vault, 501, "/menu")).text
        assert not await s.scalar(select(Recipient).where(Recipient.lookup == vault.index(501)))


async def test_sensitive_update_fields_discarded_and_html_escaped(db, vault):
    async with db.transaction() as s:
        await route(s, vault, -987654321, "/start")
        await route(s, vault, -987654321, "SENSITIVE_SEARCH_456")
        r = await s.scalar(select(Recipient))
        assert "987654321" not in json.dumps({"cipher": r.cipher, "lookup": r.lookup})
        callbacks = (await s.scalars(select(Callback))).all()
        assert "SENSITIVE_SEARCH_456" not in json.dumps([c.args for c in callbacks])
        c = await s.scalar(select(Classroom))
        c.name = "<script>unsafe</script>"
        b = await button(s, r.id, "details", "details", cid=c.id)
        await s.flush()
        view = await route(s, vault, -987654321, callback=b["callback"])
        assert "<script>" not in view.text and "&lt;script&gt;" in view.text


async def test_retention_and_mode_isolation(db, vault):
    rid, _, _ = await prepare(db, vault)
    at, _ = await opening(db)
    assert await dispatch_once(db, vault, FakeTransport(), "telegram", at) == 0
    async with db.transaction() as s:
        s.add(Event(category="test", code="expired", created=now() - timedelta(days=31), values={}))
        await cleanup(s)
        assert not await s.scalar(select(Event).where(Event.code == "expired"))


async def test_ptb_mock_transport_no_network():
    from telegram import Bot
    from vagasunb.router import View

    async with Bot(
        "123:synthetic_only", request=FakeRequest(), get_updates_request=FakeRequest()
    ) as bot:
        await TelegramTransport(bot).send(123, View("<b>Synthetic</b>", []))
