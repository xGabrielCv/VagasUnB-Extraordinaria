"""One central catalog cycle. Coverage is independent of subscriber count."""

import time
import asyncio

from sqlalchemy import text, select

from vagasunb.db import Classroom, Setting, now
from vagasunb.monitor import collect_scope
from vagasunb.services import options
from vagasunb.schedule import allowed


async def collect_catalog(db, source, mode, *, max_scopes=None, deadline=1800, period=None):
    started = now()
    clock_start = time.monotonic()
    async with db.engine.connect() as connection:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended('catalog:cycle',0))")
        )
        await connection.commit()
        if not acquired:
            return {"status": "already_running"}
        before = {
            key: getattr(source, key, 0)
            for key in ("requests", "bytes", "retries", "limited", "parse_ms", "sessions")
        }
        result = {
            "status": "running",
            "started": started.isoformat(),
            "period": None,
            "expected": 0,
            "valid": 0,
            "empty": 0,
            "failed": 0,
            "processed": 0,
            "source": getattr(source, "kind", mode),
        }
        try:
            async with db.transaction() as s:
                cfg = await options(s)
            configured_period = cfg["period"]
            if period is not None:
                cfg = {**cfg, "period": period}
            if not allowed(cfg, "collect", now()):
                return {"status": "paused"}
            async with db.transaction() as s:
                previous = await s.get(Setting, "catalog_cycle_" + result["source"])
                if (
                    previous
                    and previous.value.get("started")
                    and previous.value.get("period") == cfg["period"]
                ):
                    from datetime import datetime

                    result["previous_started"] = previous.value["started"]
                    result["last_duration_s"] = previous.value.get("duration_s", 0)
                    result["effective_s"] = (
                        started - datetime.fromisoformat(previous.value["started"])
                    ).total_seconds()
            metadata = await source.discover()
            async with db.transaction() as s:
                await s.merge(Setting(key="catalog_discovery", value=metadata))
                await s.merge(Setting(key="catalog_discovery_" + result["source"], value=metadata))
            units = list({u["id"]: u for u in metadata["units"]}.values())
            levels = list({v["id"]: v for v in metadata["levels"]}.values())
            scopes = [(u["id"], v["id"]) for u in units for v in levels]
            result.update(
                period=cfg["period"],
                expected=len(scopes),
                units=len(units),
                levels=levels,
            )
            if not scopes:
                raise ValueError("empty_discovery")
            seen = {}
            stats = {}
            valid_pairs = set()
            for unit, level in scopes[:max_scopes] if max_scopes else scopes:
                async with db.transaction() as s:
                    current = await options(s)
                if (
                    not allowed(current, "collect", now())
                    or current["source"] != cfg["source"]
                    or current["period"] != configured_period
                    or time.monotonic() - clock_start > deadline
                    or time.monotonic() < getattr(source, "next_attempt", 0)
                ):
                    break
                valid = await collect_scope(
                    db,
                    source,
                    unit,
                    mode,
                    period=cfg["period"],
                    level=level,
                    seen=seen,
                    stats=stats,
                )
                result["processed"] += 1
                result["valid" if valid else "failed"] += 1
                if valid:
                    valid_pairs.add((unit, level))
                # One follow-up per changed scope, shared by all its subscribers.
                # It uses the same source budget while full discovery keeps progressing.
                due = next(
                    (
                        scope
                        for scope, when in stats.get("confirmations", {}).items()
                        if when <= time.monotonic()
                    ),
                    None,
                )
                if due:
                    stats["confirmations"].pop(due)
                    confirmation = await collect_scope(
                        db,
                        source,
                        due[0],
                        mode,
                        period=cfg["period"],
                        level=due[1],
                        stats=stats,
                        seen=seen,
                        recheck=True,
                    )
                    result["confirmations"] = result.get("confirmations", 0) + 1
                    result["confirmation_failures"] = result.get("confirmation_failures", 0) + (
                        not confirmation
                    )
                result.update(
                    changed=len(stats.get("changed", set())),
                    events=stats.get("events", 0),
                    eligible=stats.get("eligible", 0),
                    processing_ms=round(stats.get("process_ms", 0), 3),
                )
                result.update(
                    {
                        key: round(getattr(source, key, 0) - value, 3)
                        for key, value in before.items()
                    }
                )
                async with db.transaction() as s:
                    state = await s.get(Setting, f"scope:{cfg['period']}:{level}:{unit}")
                    result["empty"] += bool(valid and state and state.value.get("empty"))
                    await s.merge(
                        Setting(
                            key="catalog_cycle",
                            value={
                                **result,
                                "current": f"{level}:{unit}",
                                "duration_s": round(time.monotonic() - clock_start, 3),
                            },
                        )
                    )
                    await s.merge(
                        Setting(
                            key="catalog_cycle_" + result["source"],
                            value={
                                **result,
                                "duration_s": round(time.monotonic() - clock_start, 3),
                            },
                        )
                    )
            for pending, when in list(stats.get("confirmations", {}).items()):
                if time.monotonic() - clock_start > deadline or time.monotonic() < getattr(
                    source, "next_attempt", 0
                ):
                    break
                await asyncio.sleep(max(0, when - time.monotonic()))
                stats["confirmations"].pop(pending, None)
                confirmation = await collect_scope(
                    db,
                    source,
                    pending[0],
                    mode,
                    period=cfg["period"],
                    level=pending[1],
                    stats=stats,
                    seen=seen,
                    recheck=True,
                )
                result["confirmations"] = result.get("confirmations", 0) + 1
                result["confirmation_failures"] = result.get("confirmation_failures", 0) + (
                    not confirmation
                )
            result.update(
                events=stats.get("events", 0),
                eligible=stats.get("eligible", 0),
                processing_ms=round(stats.get("process_ms", 0), 3),
            )
            result["status"] = "complete" if result["valid"] == len(scopes) else "partial"
            result["unattempted"] = len(scopes) - result["processed"]
            async with db.transaction() as s:
                levels = {v["id"] for v in metadata["levels"]}
                for c in (
                    await s.scalars(select(Classroom).where(Classroom.period == cfg["period"]))
                ).all():
                    if (c.code, c.number, c.period) in seen:
                        c.missing = 0
                    elif c.units and all(
                        (unit, level) in valid_pairs for unit in c.units for level in levels
                    ):
                        c.missing += 1
            result["classrooms_observed"] = len(seen)
            result["published"] = now().isoformat() if seen or result["valid"] else None
        except Exception as exc:
            result.update(
                status="degraded", error=getattr(exc, "code", "empty_or_invalid_discovery")
            )
        finally:
            if result["status"] == "running":
                result["status"] = "interrupted"
            result.update(
                finished=now().isoformat(), duration_s=round(time.monotonic() - clock_start, 3)
            )
            result.update(
                {key: round(getattr(source, key, 0) - value, 3) for key, value in before.items()}
            )
            try:
                async with db.transaction() as s:
                    await s.merge(Setting(key="catalog_cycle", value=result))
                    await s.merge(Setting(key="catalog_cycle_" + result["source"], value=result))
                    if result["status"] == "complete":
                        await s.merge(
                            Setting(key="catalog_complete_" + result["source"], value=result)
                        )
            finally:
                from vagasunb.db import release_session_lock

                await release_session_lock(connection, "catalog:cycle")
        return result


def next_delay(started, desired, backoff=0, clock=time.monotonic):
    """Desired interval between starts; no accumulated ticks or overlapping cycles."""
    return max(0, started + desired - clock(), backoff - clock())
