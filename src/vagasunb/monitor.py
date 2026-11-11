from datetime import datetime, timedelta
import time

from sqlalchemy import select

from vagasunb.catalog import normalize
from vagasunb.db import (
    Classroom,
    Cooldown,
    Event,
    Outbox,
    Recipient,
    Run,
    Setting,
    Subscription,
    lock,
    now,
    uid,
)
from vagasunb.services import options, detection_window


async def observe(s, observations, mode, at=None, stats=None):
    at = at or now()
    cfg = await options(s)
    if cfg["quarantine"]:
        return 0
    count = 0
    observations = list(observations)
    if not observations:
        return 0
    transition_ttl = await detection_window(s, cfg, observations[0]["period"])
    await lock(s, "catalog", "publish")
    existing = (
        await s.scalars(
            select(Classroom)
            .where(
                Classroom.code.in_({r["code"] for r in observations}),
                Classroom.period.in_({r["period"] for r in observations}),
            )
            .with_for_update()
        )
    ).all()
    by_identity = {(c.code, c.number, c.period): c for c in existing}
    for data in observations:
        identity = (data["code"], data["number"], data["period"])
        c = by_identity.get(identity)
        if stats is not None and (
            c is None
            or (c.offered, c.occupied, c.name, c.teachers, c.schedule, c.location)
            != (
                data.get("offered"),
                data.get("occupied"),
                data["name"],
                data.get("teachers"),
                data.get("schedule"),
                data.get("location"),
            )
        ):
            stats.setdefault("changed", set()).add(identity)
        offered, occupied = data.get("offered"), data.get("occupied")
        valid = type(offered) is int and type(occupied) is int and 0 <= occupied <= offered
        if not c:
            c = Classroom(
                id=uid(),
                cycle=0,
                missing=0,
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
            by_identity[identity] = c
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
            c.state, c.candidate = "UNKNOWN", None
            s.add(Event(category="collection", code="unknown_count", values={"count": 1}))
            continue
        c.name, c.search = data["name"], normalize(data["name"])
        c.teachers, c.schedule, c.location = (
            data.get("teachers"),
            data.get("schedule"),
            data.get("location"),
        )
        fresh_before = c.observed and (at - c.observed).total_seconds() <= transition_ttl
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
            if stats is not None:
                stats["confirmation_needed"] = True
            continue
        if (at - c.candidate).total_seconds() < cfg["debounce"]:
            continue
        c.state, c.candidate = "AVAILABLE", None
        c.cycle += 1
        if stats is not None:
            stats["events"] = stats.get("events", 0) + 1
        s.add(Event(category="detection", code="opening", values={"count": 1}))
        # Set-based fan-out: all eligible recipients enter the same release window.
        from sqlalchemy import and_, or_, cast, func, literal, String, case
        from sqlalchemy.dialects.postgresql import insert, JSONB

        await s.flush()
        fields = [
            "id",
            "recipient_id",
            "subscription_id",
            "classroom_id",
            "cycle",
            "revision",
            "mode",
            "status",
            "created",
            "due",
            "attempts",
        ]
        recipients = (
            select(
                func.replace(cast(func.gen_random_uuid(), String), "-", ""),
                Recipient.id,
                Subscription.id,
                literal(c.id),
                literal(c.cycle),
                Subscription.revision,
                case((Recipient.origin == "demo", "demo"), else_="public"),
                literal("pending"),
                literal(at),
                literal(at + timedelta(seconds=cfg["group_delay"])),
                literal(0),
            )
            .join(Recipient, Recipient.id == Subscription.recipient_id)
            .outerjoin(
                Cooldown, and_(Cooldown.recipient_id == Recipient.id, Cooldown.classroom_id == c.id)
            )
            .where(
                Subscription.code == c.code,
                Subscription.period == c.period,
                Subscription.status == "active",
                Recipient.paused.is_(False),
                (Recipient.origin == "demo") if mode == "demo" else literal(True),
                or_(
                    func.json_array_length(Subscription.numbers) == 0,
                    cast(Subscription.numbers, JSONB).contains([c.number]),
                ),
                or_(Cooldown.until.is_(None), Cooldown.until <= at),
            )
        )
        result = await s.execute(
            insert(Outbox)
            .from_select(fields, recipients)
            .on_conflict_do_nothing(index_elements=["recipient_id", "classroom_id", "cycle"])
            .returning(literal(1))
        )
        count += len(result.all())
    await s.flush()
    return count


async def collect_scope(
    db, source, scope, mode, *, period=None, level=None, seen=None, stats=None, recheck=False
):
    from sqlalchemy import text, cast
    from sqlalchemy.dialects.postgresql import JSONB

    started = now()
    async with db.transaction() as s:
        cfg = await options(s)
    period = period or cfg["period"]
    key = f"scope:{period}:{level or 'G'}:{scope}"
    async with db.engine.connect() as connection:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"), {"key": key}
        )
        await connection.commit()
        if not acquired:
            return False
        try:
            from vagasunb.schedule import allowed

            if not allowed(cfg, "collect", now()):
                return False
            # Network work is outside any database writing transaction.
            rows = (
                await source.collect(scope, period)
                if level is None
                else await source.collect(scope, period, level)
            )
            observed = now()
            if any(r["period"] != period for r in rows):
                raise ValueError("period_mismatch")
            if seen is not None:
                for r in rows:
                    identity = (r["code"], r["number"], r["period"])
                    fingerprint = (r.get("offered"), r.get("occupied"), r.get("source_id"))
                    if (
                        not recheck
                        and identity in seen
                        and seen[identity][2]
                        and fingerprint[2]
                        and seen[identity][2] != fingerprint[2]
                    ):
                        from vagasunb.sigaa_public import SourceError

                        raise SourceError("identity_conflict")
            write_start = time.perf_counter()
            local_stats = {}
            async with db.transaction() as s:
                old = (
                    await s.scalars(
                        select(Classroom).where(
                            Classroom.period == period,
                            cast(Classroom.units, JSONB).contains([scope]),
                        )
                    )
                ).all()
                if old and len(rows) < len(old) * 0.5 and not getattr(source, "complete", False):
                    raise ValueError("suspicious_coverage")
                observation_mode = "demo" if getattr(source, "kind", None) == "demo" else mode
                eligible_count = await observe(
                    s, rows, observation_mode, at=observed, stats=local_stats
                )
                found = {(r["code"], r["number"]) for r in rows}
                for c in old:
                    if seen is None and (c.code, c.number) not in found:
                        c.missing += 1
                s.add(
                    Run(
                        scope=key,
                        started=started,
                        finished=now(),
                        duration_ms=int((now() - started).total_seconds() * 1000),
                        count=len(rows),
                        valid=True,
                    )
                )
                await s.merge(
                    Setting(
                        key=key,
                        value={
                            "valid": True,
                            "observed": observed.isoformat(),
                            "count": len(rows),
                            "empty": not rows,
                        },
                    )
                )
            if stats is not None:
                stats.setdefault("changed", set()).update(local_stats.get("changed", set()))
                stats["events"] = stats.get("events", 0) + local_stats.get("events", 0)
                stats["eligible"] = stats.get("eligible", 0) + eligible_count
                stats["process_ms"] = (
                    stats.get("process_ms", 0) + (time.perf_counter() - write_start) * 1000
                )
                if local_stats.get("confirmation_needed"):
                    stats.setdefault("confirmations", {})[(scope, level)] = (
                        # Windows scheduling clocks can have a coarser tick than UTC.
                        time.monotonic() + cfg["debounce"] + 0.05
                    )
            if seen is not None:
                for r in rows:
                    seen[(r["code"], r["number"], r["period"])] = (
                        r.get("offered"),
                        r.get("occupied"),
                        r.get("source_id"),
                    )
            return True
        except Exception as exc:
            code = getattr(exc, "code", "invalid_or_unavailable")
            async with db.transaction() as s:
                s.add(
                    Run(
                        scope=key,
                        started=started,
                        finished=now(),
                        duration_ms=int((now() - started).total_seconds() * 1000),
                        count=0,
                        valid=False,
                        error=code,
                    )
                )
                prior = await s.get(Setting, key)
                await s.merge(
                    Setting(
                        key=key,
                        value={
                            **(prior.value if prior else {}),
                            "valid": False,
                            "error": code,
                            "attempt": now().isoformat(),
                        },
                    )
                )
            return False
        finally:
            from vagasunb.db import release_session_lock

            await release_session_lock(connection, key)


async def eligible(s, item, at, mode, cfg):
    sub = await s.get(Subscription, item.subscription_id)
    r = await s.get(Recipient, item.recipient_id)
    c = await s.get(Classroom, item.classroom_id)
    cd = await s.get(Cooldown, (item.recipient_id, item.classroom_id))
    from vagasunb.schedule import allowed

    return bool(
        allowed(cfg, "dispatch", at)
        and item.mode == mode
        and sub
        and r
        and c
        and not r.paused
        and (
            (mode == "demo" and r.origin == "demo") or (mode == "public" and r.origin == "private")
        )
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
        and c.missing == 0
        and (at - item.created).total_seconds() <= cfg["stale"]
        and (not cd or cd.until <= at)
        and at < datetime.fromisoformat(cfg["window_end"])
    )


async def dispatch_once(db, vault, transport, mode, at=None, candidate_pair=None, budget=None):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    fixed_time = at is not None
    at = at or now()
    if mode == "demo" and getattr(transport, "real_enabled", False):
        return 0
    if budget is None and getattr(transport, "real_enabled", False):
        from vagasunb.distribution import SendBudget

        budget = SendBudget(db)
    async with db.transaction() as s:
        cfg = await options(s)
        if (
            cfg["dispatch_paused"]
            or cfg["quarantine"]
            or (mode != "demo" and not getattr(transport, "real_enabled", False))
        ):
            return 0
        for item in (
            await s.scalars(
                select(Outbox)
                .where(Outbox.status == "sending", Outbox.lease < at)
                .with_for_update(skip_locked=True)
            )
        ).all():
            item.status, item.error = "uncertain", "lease_expired"
        candidate = (
            None
            if candidate_pair
            else await s.scalar(
                select(Outbox)
                .where(
                    Outbox.status.in_(["pending", "retry"]), Outbox.due <= at, Outbox.mode == mode
                )
                .order_by(Outbox.due)
                .limit(1)
            )
        )
        if not candidate and not candidate_pair:
            return 0
        rid, sid = candidate_pair or (candidate.recipient_id, candidate.subscription_id)
    # Wait without a claim, recipient lock, transaction or checked-out connection.
    # A deleted recipient cannot be recreated by this path.
    if budget:
        if await budget.wait(rid) is False:
            return 0
    if not fixed_time:
        at = now()
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
                                Outbox.mode == mode,
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
                cfg = await options(s)
                send_at = at if fixed_time else now()
                if cfg["dispatch_paused"] or cfg["quarantine"]:
                    for item in valid:
                        item.status, item.lease = "pending", None
                    return 0
                for item in list(valid):
                    if not await eligible(s, item, send_at, mode, cfg):
                        item.status = "cancelled"
                        valid.remove(item)
                if not valid:
                    return 0
                classes = [await s.get(Classroom, i.classroom_id) for i in valid]
                from vagasunb.router import alert_view

                view = await alert_view(s, rid, sid, classes)
                r = await s.get(Recipient, rid)
                from vagasunb.distribution import send_permit

                chat = vault.decrypt(r.cipher)
                permit = send_permit.set((rid, vault.index(chat)) if budget else None)
                try:
                    await transport.send(chat, view)
                except Exception as exc:
                    code = getattr(exc, "code", "uncertain")
                    if code == "forbidden":
                        r.paused = True
                    if code == "rate_limit" and budget:
                        await budget.defer(max(1, getattr(exc, "retry_after", 60)))
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
                finally:
                    send_permit.reset(permit)
                sent_at = at if fixed_time else now()
                for item in valid:
                    item.status, item.sent = (
                        ("simulated" if mode == "demo" else "sent"),
                        sent_at,
                    )
                    cd = await s.get(Cooldown, (rid, item.classroom_id))
                    if cd:
                        cd.until = sent_at + timedelta(seconds=cfg["cooldown"])
                    else:
                        s.add(
                            Cooldown(
                                recipient_id=rid,
                                classroom_id=item.classroom_id,
                                until=sent_at + timedelta(seconds=cfg["cooldown"]),
                            )
                        )
                return len(valid)
        finally:
            from vagasunb.db import release_session_lock

            await release_session_lock(conn, "recipient:" + rid)
