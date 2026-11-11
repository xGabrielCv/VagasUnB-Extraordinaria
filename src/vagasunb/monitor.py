from datetime import datetime, timedelta

from sqlalchemy import select

from vagasunb.catalog import normalize
from vagasunb.db import Classroom, Cooldown, Event, Outbox, Recipient, Run, Subscription, lock, now
from vagasunb.services import options


async def observe(s, observations, mode, at=None):
    at = at or now()
    cfg = await options(s)
    if cfg["quarantine"]:
        return 0
    count = 0
    for data in observations:
        identity = (data["code"], data["number"], data["period"])
        await lock(s, "classroom", ":".join(identity))
        c = await s.scalar(
            select(Classroom)
            .where(
                Classroom.code == identity[0],
                Classroom.number == identity[1],
                Classroom.period == identity[2],
            )
            .with_for_update()
        )
        offered, occupied = data.get("offered"), data.get("occupied")
        valid = type(offered) is int and type(occupied) is int and 0 <= occupied <= offered
        if not c:
            c = Classroom(
                source_id=data.get("source_id"),
                code=data["code"],
                name=data["name"],
                number=data["number"],
                period=data["period"],
                search=normalize(data["name"]),
                units=data["units"],
                campus=data.get("campus"),
                provenance=data.get("provenance", "SIGAA público; campus não validado"),
                teachers=data.get("teachers"),
                schedule=data.get("schedule"),
                location=data.get("location"),
                offered=offered,
                occupied=occupied,
                available=offered - occupied if valid else None,
                observed=at if valid else None,
                state=("AVAILABLE" if offered > occupied else "NO_VACANCY") if valid else "UNKNOWN",
            )
            s.add(c)
            await s.flush()
            continue  # New classroom always establishes baseline.
        if c.source_id and data.get("source_id") and c.source_id != data["source_id"]:
            raise ValueError("Conflito de identidade na origem")
        if data.get("source_id"):
            c.source_id = data["source_id"]
        if set(data["units"]) - set(c.units):
            # Multiple units are provenance, not a second class or availability event.
            if c.observed == at and (c.offered, c.occupied) != (offered, occupied):
                raise ValueError("Conflito entre unidades")
            c.units = sorted(set(c.units) | set(data["units"]))
        if c.observed and at <= c.observed:
            continue
        if not valid:
            s.add(Event(category="collection", code="unknown_count", values={"count": 1}))
            continue
        c.name, c.search = data["name"], normalize(data["name"])
        c.teachers, c.schedule, c.location = (
            data.get("teachers"),
            data.get("schedule"),
            data.get("location"),
        )
        fresh_before = c.observed and (at - c.observed).total_seconds() <= cfg["stale"]
        previous_state = c.state
        c.offered, c.occupied, c.available, c.observed = offered, occupied, offered - occupied, at
        c.missing = 0
        if c.available == 0:
            c.state, c.candidate = "NO_VACANCY", None
            continue
        if previous_state == "UNKNOWN":
            c.state = "AVAILABLE"
            continue
        if previous_state == "AVAILABLE":
            continue
        if not fresh_before:
            c.state, c.candidate = "AVAILABLE", None
            continue  # Recovery establishes a new baseline; no invented opening event.
        if c.candidate is None:
            c.candidate = at
            continue
        if (at - c.candidate).total_seconds() < cfg["debounce"]:
            continue
        c.state, c.candidate = "AVAILABLE", None
        c.cycle += 1
        s.add(Event(category="detection", code="opening", values={"count": 1}))
        for sub in (
            await s.scalars(
                select(Subscription).where(
                    Subscription.code == c.code,
                    Subscription.period == c.period,
                    Subscription.status == "active",
                )
            )
        ).all():
            r = await s.get(Recipient, sub.recipient_id)
            if not r or r.paused or (sub.numbers and c.number not in sub.numbers):
                continue
            cd = await s.get(Cooldown, (r.id, c.id))
            if cd and cd.until > at:
                continue
            s.add(
                Outbox(
                    recipient_id=r.id,
                    subscription_id=sub.id,
                    classroom_id=c.id,
                    cycle=c.cycle,
                    revision=sub.revision,
                    mode=mode,
                    created=at,
                    due=at + timedelta(seconds=cfg["group_delay"]),
                )
            )
            count += 1
    return count


async def collect_scope(db, source, scope, mode):
    started = now()
    requests_before = getattr(source, "requests", 0)
    try:
        async with db.transaction() as s:
            if mode != "demo" and not await lock(s, "public-source", "global", wait=False):
                return False
            if not await lock(s, "scope", scope, wait=False):
                return False
            cfg = await options(s)
            if cfg["collect_paused"] or cfg["quarantine"]:
                return False
            rows = await source.collect(scope, cfg["period"])
            if any(c["period"] != cfg["period"] for c in rows):
                raise ValueError("Período inesperado")
            known = list(
                (await s.scalars(select(Classroom).where(Classroom.period == cfg["period"]))).all()
            )
            old = [c for c in known if scope in c.units]
            if old and len(rows) < len(old) * 0.5:
                raise ValueError("Cobertura suspeita")
            await observe(s, rows, mode)
            s.add(
                Event(
                    category="collection",
                    code="valid_scope",
                    values={
                        "requests": getattr(source, "requests", 0) - requests_before,
                        "classrooms": len(rows),
                    },
                )
            )
            found = {(r["code"], r["number"]) for r in rows}
            for c in old:
                if (c.code, c.number) not in found:
                    c.missing += 1  # Preserve records; signal after repeated complete observations.
            s.add(
                Run(
                    scope=scope,
                    started=started,
                    finished=now(),
                    duration_ms=int((now() - started).total_seconds() * 1000),
                    count=len(rows),
                    valid=True,
                )
            )
            return True
    except Exception as exc:
        code = getattr(exc, "code", "invalid_or_unavailable")
        async with db.transaction() as s:
            s.add(
                Run(
                    scope=scope,
                    started=started,
                    finished=now(),
                    duration_ms=int((now() - started).total_seconds() * 1000),
                    count=0,
                    valid=False,
                    error=code,
                )
            )
        return False


async def eligible(s, item, at, mode, cfg):
    sub = await s.get(Subscription, item.subscription_id)
    r = await s.get(Recipient, item.recipient_id)
    c = await s.get(Classroom, item.classroom_id)
    cd = await s.get(Cooldown, (item.recipient_id, item.classroom_id))
    return bool(
        item.mode == mode
        and sub
        and r
        and c
        and not r.paused
        and sub.status == "active"
        and sub.revision == item.revision
        and sub.period == cfg["period"]
        and (not sub.numbers or c.number in sub.numbers)
        and c.available
        and c.available > 0
        and c.state == "AVAILABLE"
        and c.cycle == item.cycle
        and c.observed
        and (at - c.observed).total_seconds() <= cfg["stale"]
        and c.missing < 2
        and (at - item.created).total_seconds() <= cfg["stale"]
        and (not cd or cd.until <= at)
        and at < datetime.fromisoformat(cfg["window_end"])
    )


async def dispatch_once(db, vault, transport, mode, at=None):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    at = at or now()
    async with db.transaction() as s:
        cfg = await options(s)
        if cfg["dispatch_paused"] or cfg["quarantine"] or mode == "public":
            return 0
        for item in (
            await s.scalars(
                select(Outbox)
                .where(Outbox.status == "sending", Outbox.lease < at)
                .with_for_update(skip_locked=True)
            )
        ).all():
            item.status, item.error = "uncertain", "lease_expired"
        candidate = await s.scalar(
            select(Outbox)
            .where(Outbox.status.in_(["pending", "retry"]), Outbox.due <= at)
            .order_by(Outbox.due)
            .limit(1)
        )
        if not candidate:
            return 0
        rid, sid = candidate.recipient_id, candidate.subscription_id
    # Session advisory lock survives claim commit; erase/pause uses the same key.
    async with db.engine.connect() as conn:
        acquired = await conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
            {"key": "recipient:" + rid},
        )
        await conn.commit()
        if not acquired:
            return 0
        sessions = async_sessionmaker(conn, expire_on_commit=False)
        try:
            async with sessions.begin() as s:
                cfg = await options(s)
                if cfg["dispatch_paused"] or cfg["quarantine"]:
                    return 0
                items = list(
                    (
                        await s.scalars(
                            select(Outbox)
                            .where(
                                Outbox.recipient_id == rid,
                                Outbox.subscription_id == sid,
                                Outbox.status.in_(["pending", "retry"]),
                                Outbox.due <= at,
                            )
                            .order_by(Outbox.created)
                            .with_for_update(skip_locked=True)
                            .limit(8)
                        )
                    ).all()
                )
                ids = []
                for item in items:
                    if await eligible(s, item, at, mode, cfg):
                        item.status, item.lease, item.attempts = (
                            "sending",
                            at + timedelta(seconds=60),
                            item.attempts + 1,
                        )
                        ids.append(item.id)
                    else:
                        item.status = "cancelled"
            if not ids:
                return 0
            # Claim is durable. Crash after this point yields an expired lease => uncertain,
            # never blind re-send. No exactly-once promise across Telegram and PostgreSQL.
            async with sessions.begin() as s:
                valid = list(
                    (
                        await s.scalars(select(Outbox).where(Outbox.id.in_(ids)).with_for_update())
                    ).all()
                )
                classes = [await s.get(Classroom, i.classroom_id) for i in valid]
                from vagasunb.router import alert_view

                view = await alert_view(s, rid, sid, classes)
                r = await s.get(Recipient, rid)
                try:
                    await transport.send(vault.decrypt(r.cipher), view)
                except Exception as exc:
                    code = getattr(exc, "code", "uncertain")
                    if code == "forbidden":
                        r.paused = True
                    for item in valid:
                        item.error = code
                        if code in {"forbidden", "invalid_destination"}:
                            item.status = "failed"
                        elif code == "rate_limit" and item.attempts < 4:
                            item.status, item.due = (
                                "retry",
                                at + timedelta(seconds=max(1, getattr(exc, "retry_after", 60))),
                            )
                        elif code == "transient" and item.attempts < 4:
                            item.status, item.due = (
                                "retry",
                                at + timedelta(seconds=2**item.attempts),
                            )
                        else:
                            item.status = "uncertain"
                    return 0
                for item in valid:
                    item.status, item.sent = ("simulated" if mode == "demo" else "sent"), at
                    cd = await s.get(Cooldown, (rid, item.classroom_id))
                    if cd:
                        cd.until = at + timedelta(seconds=cfg["cooldown"])
                    else:
                        s.add(
                            Cooldown(
                                recipient_id=rid,
                                classroom_id=item.classroom_id,
                                until=at + timedelta(seconds=cfg["cooldown"]),
                            )
                        )
                return len(valid)
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": "recipient:" + rid},
            )
            await conn.commit()
