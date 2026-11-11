from datetime import datetime, timedelta

from sqlalchemy import delete, select

from vagasunb.db import (
    Callback,
    Classroom,
    Cooldown,
    Event,
    Outbox,
    Recipient,
    Setting,
    Subscription,
    lock,
    now,
)

DEFAULTS = {
    "timezone": "America/Sao_Paulo",
    "registrations_open": True,
    "closed_message": "Novos monitoramentos estão fechados. Você pode consultar, cancelar e apagar seus dados.",
    "collection_auto": True,
    "screen_interval": 3,
    "schedule_register": {},
    "schedule_collect": {},
    "schedule_dispatch": {},
    "period": "DEMO.1",
    "source": "demo",
    "send_rate": 25,
    "send_concurrency": 6,
    "window_end": "2099-12-31T23:59:59+00:00",
    "interval": 60,
    "structural_interval": 86400,
    "debounce": 10,
    "cooldown": 900,
    "group_delay": 3,
    "stale": 180,
    "collect_paused": False,
    "dispatch_paused": False,
    "quarantine": False,
    "scopes": [],
    "limit": 10,
    "outbox_days": 7,
    "events_days": 30,
    "recipient_days": 30,
}


async def verify_instance(s, vault, mode):
    await lock(s, "instance", "bootstrap")
    marker = await s.get(Setting, "instance")
    proof = vault.index("0")
    if marker:
        if marker.value["key_check"] != proof:
            raise ValueError("Modo ou chave incompatível com o banco")
        if (
            marker.value.get("cipher_check")
            and vault.decrypt(marker.value["cipher_check"]) != "vagasunb-key-check"
        ):
            raise ValueError("Chave de cifra incompatível")
    r = await s.scalar(select(Recipient).limit(1))
    if r:
        vault.decrypt(r.cipher)
    if not marker:
        s.add(
            Setting(
                key="instance",
                value={
                    "mode": mode,
                    "key_check": proof,
                    "cipher_check": vault.encrypt("vagasunb-key-check"),
                },
            )
        )
    elif not marker.value.get("cipher_check"):
        marker.value = {**marker.value, "cipher_check": vault.encrypt("vagasunb-key-check")}


async def options(s):
    row = await s.get(Setting, "operations")
    return {**DEFAULTS, **(row.value if row else {})}


async def detection_window(s, cfg, period):
    """Compare across a measured catalog cadence; delivery keeps its short TTL."""
    cycle = await s.get(Setting, "catalog_cycle_public")
    if not cycle or cycle.value.get("period") != period:
        return cfg["stale"]
    value = cycle.value
    if value.get("valid", 0) < 0.9 * max(1, value.get("processed", 0)):
        return cfg["stale"]
    return max(
        cfg["stale"],
        2 * max(cfg["interval"], value.get("last_duration_s", 0), value.get("duration_s", 0)),
    )


async def change_options(s, values):
    await lock(s, "operations", "config")
    cfg = await options(s)
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError("Configuração não permitida")
        if key in {
            "interval",
            "structural_interval",
            "stale",
            "limit",
            "outbox_days",
            "events_days",
            "recipient_days",
        } and (type(value) is not int or not 1 <= value <= 604800):
            raise ValueError("Valor fora do limite")
        if key in {"debounce", "cooldown", "group_delay"} and (
            type(value) is not int or not 0 <= value <= 86400
        ):
            raise ValueError("Valor fora do limite")
        if key == "source" and value not in {"demo", "public"}:
            raise ValueError("Fonte inválida")
        if key in {"send_rate", "send_concurrency"} and (
            type(value) is not int or not 1 <= value <= (29 if key == "send_rate" else 12)
        ):
            raise ValueError("Limite de distribuição inválido")
        if key == "screen_interval" and (type(value) is not int or not 3 <= value <= 60):
            raise ValueError("Atualização da tela deve ficar entre 3 e 60 segundos")
        cfg[key] = value
    from vagasunb.schedule import validate

    for key in ("schedule_register", "schedule_collect", "schedule_dispatch"):
        validate(cfg[key], cfg["timezone"])
    for key in (
        "registrations_open",
        "collection_auto",
        "collect_paused",
        "dispatch_paused",
        "quarantine",
    ):
        if type(cfg[key]) is not bool:
            raise ValueError("Opção ligada/desligada inválida")
    if not isinstance(cfg["closed_message"], str) or not 1 <= len(cfg["closed_message"]) <= 240:
        raise ValueError("Mensagem de fechamento deve ter de 1 a 240 caracteres")
    row = await s.get(Setting, "operations")
    if row:
        row.value = cfg
    else:
        s.add(Setting(key="operations", value=cfg))
    s.add(Event(category="config", code="updated", values={"fields": sorted(values)}))


async def register(s, vault, chat_id, *, synthetic=True):
    await lock(s, "lookup", vault.index(chat_id))
    r = await s.scalar(select(Recipient).where(Recipient.lookup == vault.index(chat_id)))
    if r:
        return r
    r = Recipient(
        cipher=vault.encrypt(str(int(chat_id))),
        lookup=vault.index(chat_id),
        origin="demo" if synthetic else "private",
    )
    s.add(r)
    await s.flush()
    return r


async def subscribe(s, rid, code, period, numbers):
    await lock(s, "recipient", rid)
    r = await s.get(Recipient, rid)
    if not r:
        raise ValueError("Abra /start conscientemente para iniciar")
    cfg = await options(s)
    from vagasunb.schedule import allowed

    if not allowed(cfg, "register", now()):
        raise ValueError(cfg["closed_message"])
    if now() >= datetime.fromisoformat(cfg["window_end"]):
        raise ValueError("Janela de monitoramento encerrada")
    if cfg["quarantine"] or period != cfg["period"]:
        raise ValueError("Período indisponível")
    classes = list(
        (
            await s.scalars(
                select(Classroom).where(Classroom.code == code, Classroom.period == period)
            )
        ).all()
    )
    if not classes or not set(numbers).issubset({c.number for c in classes}):
        raise ValueError("Turma inexistente")
    sub = await s.scalar(
        select(Subscription).where(
            Subscription.recipient_id == rid,
            Subscription.code == code,
            Subscription.period == period,
        )
    )
    if sub:
        if sub.numbers == sorted(set(numbers)):
            return sub, False
        sub.numbers = sorted(set(numbers))
        sub.revision += 1
        await cancel_pending(s, sub.id)
        return sub, False
    count = len(
        (await s.scalars(select(Subscription.id).where(Subscription.recipient_id == rid))).all()
    )
    if count >= min(cfg["limit"], 10):
        raise ValueError("Limite de 10 disciplinas alcançado; pausadas também contam")
    sub = Subscription(recipient_id=rid, code=code, period=period, numbers=sorted(set(numbers)))
    s.add(sub)
    await s.flush()
    return sub, True


async def cancel_pending(s, sid):
    for item in (
        await s.scalars(
            select(Outbox).where(
                Outbox.subscription_id == sid, Outbox.status.in_(["pending", "retry"])
            )
        )
    ).all():
        item.status = "cancelled"


async def subscription_action(s, rid, sid, action, revision=None):
    await lock(s, "recipient", rid)
    sub = await s.get(Subscription, sid)
    if not sub or sub.recipient_id != rid or (revision is not None and sub.revision != revision):
        raise ValueError("Este botão expirou ou o monitoramento foi alterado")
    if action == "continue":
        return
    if action == "pause":
        sub.status = "paused"
    elif action == "resume":
        sub.status = "active"
    elif action in {"stop", "complete"}:
        await s.delete(sub)
        s.add(Event(category="subscriptions", code=action, values={"count": 1}))
        return
    else:
        raise ValueError("Ação inválida")
    sub.revision += 1
    await cancel_pending(s, sid)


async def erase(s, rid):
    # Dispatcher holds this lock across the send. Confirmation waits for any in-flight call.
    await lock(s, "recipient", rid)
    await lock(s, "send", "budget")
    budget = await s.get(Setting, "send_budget")
    if budget and rid in budget.value.get("chats", {}):
        value = dict(budget.value)
        value["chats"] = {k: v for k, v in value["chats"].items() if k != rid}
        budget.value = value
    r = await s.get(Recipient, rid)
    if r:
        await s.delete(r)
        await s.flush()


async def cleanup(s, at=None):
    at = at or now()
    cfg = await options(s)
    # Closing a window stops eligibility; subscriptions remain manageable.
    await s.execute(delete(Callback).where(Callback.expires < at))
    await s.execute(
        delete(Outbox).where(
            Outbox.created < at - timedelta(days=cfg["outbox_days"]),
            Outbox.status.not_in(["pending", "retry", "sending"]),
        )
    )
    await s.execute(delete(Event).where(Event.created < at - timedelta(days=cfg["events_days"])))
    from vagasunb.db import AdminSession, Run

    await s.execute(delete(AdminSession).where(AdminSession.expires < at))
    await s.execute(delete(Run).where(Run.started < at - timedelta(days=30)))
    await s.execute(delete(Cooldown).where(Cooldown.until < at))
    await s.execute(
        delete(Recipient).where(
            Recipient.active_at < at - timedelta(days=cfg["recipient_days"]),
            ~Recipient.id.in_(select(Subscription.recipient_id)),
        )
    )
    # One aggregate snapshot/day, no recipient identifiers or click histories.
    from sqlalchemy import func

    daily = await s.get(Setting, "metrics_daily")
    values = dict(daily.value) if daily else {}
    values[at.date().isoformat()] = {
        "recipients": await s.scalar(select(func.count()).select_from(Recipient)),
        "subscriptions": await s.scalar(select(func.count()).select_from(Subscription)),
        "database_bytes": await s.scalar(
            __import__("sqlalchemy").text("SELECT pg_database_size(current_database())")
        ),
    }
    values = {
        day: counters
        for day, counters in values.items()
        if day >= (at - timedelta(days=90)).date().isoformat()
    }
    if daily:
        daily.value = values
    else:
        s.add(Setting(key="metrics_daily", value=values))
    s.add(Event(category="maintenance", code="cleanup", values={}))
