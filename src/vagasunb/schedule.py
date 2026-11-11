"""Small shared operation gate. Persist instants in UTC, evaluate weekly hours locally."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_instant(value, timezone):
    if not value:
        return None
    value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        raise ValueError("Use data e hora locais no campo de Brasília")
    return value.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC).isoformat()


def validate(rule, timezone):
    try:
        ZoneInfo(timezone)
        start, end = [
            datetime.fromisoformat(rule[k]) if rule.get(k) else None for k in ("start", "end")
        ]
        if any(v and v.tzinfo is None for v in (start, end)) or (start and end and start >= end):
            raise ValueError()
        if type(rule.get("enabled", False)) is not bool:
            raise ValueError()
        if any(type(d) is not int or d not in range(7) for d in rule.get("days", [])):
            raise ValueError()
        hours = [time.fromisoformat(rule[k]) if rule.get(k) else None for k in ("from", "to")]
        if bool(hours[0]) != bool(hours[1]) or (hours[0] and hours[0] == hours[1]):
            raise ValueError()
    except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
        raise ValueError("Agenda inválida: confira datas, dias, faixa horária e fuso") from None


def within(rule, at, timezone):
    if not rule.get("enabled"):
        return True
    if rule.get("start") and at < datetime.fromisoformat(rule["start"]):
        return False
    if rule.get("end") and at >= datetime.fromisoformat(rule["end"]):
        return False
    local = at.astimezone(ZoneInfo(timezone))
    day, hour = local.weekday(), local.time().replace(tzinfo=None)
    if rule.get("from"):
        begin, end = time.fromisoformat(rule["from"]), time.fromisoformat(rule["to"])
        if begin < end:
            if not begin <= hour < end:
                return False
        else:
            if end <= hour < begin:
                return False
            # An overnight window belongs to the weekday on which it opens.
            if hour < end:
                day = (day - 1) % 7
    return not rule.get("days") or day in rule["days"]


def allowed(cfg, task, at):
    requested = cfg["registrations_open"] if task == "register" else not cfg[task + "_paused"]
    return bool(
        requested and not cfg["quarantine"] and within(cfg["schedule_" + task], at, cfg["timezone"])
    )


def state(cfg, task, at):
    requested = cfg["registrations_open"] if task == "register" else not cfg[task + "_paused"]
    active = allowed(cfg, task, at)
    reason = (
        "quarantine"
        if cfg["quarantine"]
        else "paused"
        if not requested
        else "ok"
        if active
        else "scheduled"
    )
    rule = cfg["schedule_" + task]
    candidates = []
    for key in ("start", "end"):
        if rule.get(key):
            candidates.append(datetime.fromisoformat(rule[key]))
    local = at.astimezone(ZoneInfo(cfg["timezone"]))
    anchors = {local.date()}
    if rule.get("start"):
        anchors.add(
            datetime.fromisoformat(rule["start"]).astimezone(ZoneInfo(cfg["timezone"])).date()
        )
    for day in {anchor + timedelta(days=offset) for anchor in anchors for offset in range(9)}:
        for hour in ("00:00", rule.get("from"), rule.get("to")):
            if hour:
                candidates.append(
                    datetime.combine(
                        day, time.fromisoformat(hour), ZoneInfo(cfg["timezone"])
                    ).astimezone(UTC)
                )
    upcoming = (
        next(
            (
                t
                for t in sorted(set(candidates))
                if t > at
                and within(rule, t, cfg["timezone"])
                != within(rule, t - timedelta(microseconds=1), cfg["timezone"])
            ),
            None,
        )
        if rule.get("enabled")
        else None
    )
    return {
        "requested": requested,
        "active": active,
        "reason": reason,
        "next": upcoming.isoformat() if upcoming else None,
        "next_open": within(rule, upcoming, cfg["timezone"]) if upcoming else None,
    }
