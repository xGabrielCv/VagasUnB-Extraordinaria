import hashlib
import hmac
import asyncio
import os
import secrets
import shutil
import time
import json
from collections import deque
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from vagasunb.config import Settings
from vagasunb.crypto import Vault
from vagasunb.db import (
    Admin,
    AdminSession,
    Classroom,
    Database,
    Event,
    Outbox,
    Recipient,
    Run,
    Setting,
    Subscription,
    lock,
    now,
)
from vagasunb.demo import seed
from vagasunb.services import change_options, cleanup, options

ASSETS = Path(__file__).parent


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(settings=None):
    cfg = settings or Settings.load()
    db = Database(cfg.database_url)
    vault = Vault(cfg.secrets)
    started = time.monotonic()
    login_attempts = deque(maxlen=50)
    nonce_key = secrets.token_bytes(32)

    @asynccontextmanager
    async def lifespan(app):
        # Verify mode and keys before serving. Never silently replace mismatched keys.
        async with db.transaction() as s:
            from vagasunb.services import verify_instance

            await verify_instance(s, vault, cfg.mode)
            await seed(s, cfg.mode)
        yield
        await db.close()

    app = FastAPI(
        title="VagasUnB (Extraordinária)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.db, app.state.vault, app.state.settings = db, vault, cfg
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[cfg.host.split(":")[0], "localhost", "127.0.0.1", "testserver"],
    )
    templates = Jinja2Templates(directory=ASSETS / "templates")
    from vagasunb.presentation import COUNT_HELP, LABELS, date_br, duration, input_date, label
    from vagasunb.schedule import state

    templates.env.filters.update(
        brdate=date_br, duration=duration, inputdate=input_date, label=label
    )
    templates.env.globals.update(labels=LABELS, count_help=COUNT_HELP)
    app.mount("/assets", StaticFiles(directory=ASSETS / "assets"), name="assets")

    @app.exception_handler(Exception)
    async def sanitized_failure(request, error):
        import logging
        from vagasunb.operations import error_code

        code = error_code(error)
        logging.getLogger("vagasunb").error("category=http code=%s", code)
        return JSONResponse(
            {
                "detail": "Falha operacional. Consulte o estado e os registros sanitizados.",
                "code": code,
            },
            status_code=503 if code == "database_unavailable" else 500,
        )

    @app.middleware("http")
    async def headers(request, call_next):
        if (
            request.method == "GET"
            and request.url.hostname == "localhost"
            and cfg.host.startswith("127.0.0.1")
        ):
            return RedirectResponse(
                f"{'https' if cfg.secure else 'http'}://{cfg.host}{request.url.path}"
                + (f"?{request.url.query}" if request.url.query else ""),
                status_code=307,
            )
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'",
            }
        )
        return response

    async def authenticated(request):
        token = request.cookies.get("session", "")
        async with db.transaction() as s:
            session = await s.get(AdminSession, digest(token)) if token else None
            if not session or session.expires <= now():
                raise HTTPException(401, "Autenticação necessária")
            return session

    def check_origin(request):
        def normalized(value):
            try:
                u = urlsplit(value)
                if (
                    u.scheme not in {"http", "https"}
                    or not u.hostname
                    or u.username
                    or u.path not in {"", "/"}
                    or u.query
                    or u.fragment
                ):
                    return None
                return u.scheme, u.hostname.lower(), u.port or (443 if u.scheme == "https" else 80)
            except ValueError:
                return None

        expected = normalized(f"{'https' if cfg.secure else 'http'}://{cfg.host}")
        if (
            normalized(request.headers.get("origin", "")) != expected
            or normalized(str(request.base_url)) != expected
        ):
            raise HTTPException(403, "Origem inválida")

    def auth_page(request, kind="login", error="", status=200):
        token = secrets.token_urlsafe(32)
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"nonce": token, "error": error, "kind": kind},
            status_code=status,
        )
        signature = hmac.new(nonce_key, token.encode(), hashlib.sha256).hexdigest()
        response.set_cookie(
            "login_nonce",
            token + "." + signature,
            httponly=True,
            samesite="strict",
            secure=cfg.secure,
            max_age=600,
        )
        return response

    async def auth_form(request):
        check_origin(request)
        form = await request.form()
        cookie = request.cookies.get("login_nonce", "")
        nonce, _, signature = cookie.partition(".")
        expected = hmac.new(nonce_key, nonce.encode(), hashlib.sha256).hexdigest()
        if (
            not nonce
            or not secrets.compare_digest(nonce, str(form.get("nonce", "")))
            or not secrets.compare_digest(signature, expected)
        ):
            raise HTTPException(403, "Formulário expirado. Tente novamente.")
        return form

    def validate_password(form):
        password = str(form.get("password", ""))
        if not 12 <= len(password) <= 256:
            raise ValueError("Use uma senha entre 12 e 256 caracteres.")
        if password != form.get("confirm"):
            raise ValueError("A confirmação da senha não confere.")
        return password

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        async with db.transaction() as s:
            if await s.get(Admin, 1):
                return RedirectResponse("/login", status_code=303)
        if cfg.host.split(":")[0] not in {"127.0.0.1", "localhost", "testserver"} and not (
            cfg.secure and os.getenv("VAGAS_PRIVATE_ACCESS") == "1"
        ):
            raise HTTPException(403, "Cadastro inicial exige acesso local ou privado protegido.")
        return auth_page(request, "setup")

    @app.post("/setup", response_class=HTMLResponse)
    async def setup(request: Request):
        try:
            form = await auth_form(request)
            password = validate_password(form)
            hashed = await asyncio.to_thread(PasswordHasher().hash, password)
            async with db.transaction() as s:
                await lock(s, "admin", "credential")
                if await s.get(Admin, 1):
                    return RedirectResponse("/login", status_code=303)
                if cfg.host.split(":")[0] not in {"127.0.0.1", "localhost", "testserver"} and not (
                    cfg.secure and os.getenv("VAGAS_PRIVATE_ACCESS") == "1"
                ):
                    raise HTTPException(403, "Use acesso local ou privado protegido.")
                s.add(Admin(id=1, password=hashed))
                s.add(Event(category="admin", code="web_setup_completed", values={}))
            return RedirectResponse("/login", status_code=303)
        except (ValueError, HTTPException) as exc:
            return auth_page(
                request,
                "setup",
                str(exc) if isinstance(exc, ValueError) else exc.detail,
                exc.status_code if isinstance(exc, HTTPException) else 400,
            )

    async def mutation(request):
        session = await authenticated(request)
        check_origin(request)
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), session.csrf):
            raise HTTPException(403, "CSRF inválido")
        return form

    @app.get("/health")
    async def health():
        try:
            async with db.transaction() as s:
                await s.execute(text("SELECT 1"))
                heartbeat = await s.get(Setting, "worker_health")
            from datetime import datetime

            value = heartbeat.value if heartbeat else {}
            age = (
                (now() - datetime.fromisoformat(value["at"])).total_seconds()
                if value.get("at")
                else None
            )
            alive = age is not None and age < 15
            return JSONResponse(
                {
                    "status": "ok" if alive else "degraded",
                    "component": "admin",
                    "service": "vagasunb-extraordinaria",
                    "instance": cfg.instance,
                    "pid": os.getpid(),
                    "database": "ok",
                    "worker": {
                        "alive": alive,
                        "tasks_healthy": alive
                        and value.get("collect")
                        in {"ok", "running", "paused", "scheduled", "manual", "waiting"}
                        and value.get("dispatch")
                        in {"simulated", "disabled", "enabled", "running", "paused", "scheduled"},
                        "age_s": round(age, 1) if age is not None else None,
                        **value,
                    },
                },
                status_code=200 if alive else 503,
            )
        except Exception:
            return JSONResponse({"status": "degraded", "component": "admin"}, status_code=503)

    @app.get("/metrics")
    async def metrics(request: Request):
        await authenticated(request)
        async with db.transaction() as s:
            worker = await s.get(Setting, "worker_health")
            ops = await options(s)
            cycle = await s.get(Setting, "catalog_cycle_" + ops["source"])
            complete = await s.get(Setting, "catalog_complete_" + ops["source"])
            states = dict(
                (await s.execute(select(Outbox.status, func.count()).group_by(Outbox.status))).all()
            )
            batches = (
                await s.execute(
                    select(
                        Classroom.code,
                        Classroom.number,
                        Outbox.cycle,
                        func.count(),
                        func.min(Outbox.sent),
                        func.max(Outbox.sent),
                        func.min(Outbox.created),
                    )
                    .join(Classroom, Classroom.id == Outbox.classroom_id)
                    .group_by(Classroom.code, Classroom.number, Outbox.cycle)
                    .order_by(func.max(Outbox.created).desc())
                    .limit(8)
                )
            ).all()
            version = await s.scalar(text("SELECT version_num FROM alembic_version"))
            from vagasunb.services import detection_window

            transition_window = await detection_window(s, ops, ops["period"])
            return {
                "cycle": cycle.value
                if cycle and cycle.value.get("period") == ops["period"]
                else {"status": "not_started", "period": ops["period"], "source": ops["source"]},
                "complete": complete.value if complete else {},
                "source": ops["source"],
                "gates": {
                    task: state(ops, task, now()) for task in ("register", "collect", "dispatch")
                },
                "worker": worker.value if worker else {"status": "not_started"},
                "desired_s": ops["interval"],
                "screen_s": ops["screen_interval"],
                "outbox": states,
                "migration": version,
                "transition_window_s": round(transition_window, 1),
                "alert_ttl_s": ops["stale"],
                "sending": {
                    "pool": ops["send_concurrency"],
                    "global_per_s": ops["send_rate"],
                    "chat_interval_s": 1,
                },
                "batches": [
                    {
                        "code": r[0],
                        "classroom": r[1],
                        "cycle": r[2],
                        "eligible": r[3],
                        "first": r[4].isoformat() if r[4] else None,
                        "last": r[5].isoformat() if r[5] else None,
                        "spread_s": (r[5] - r[4]).total_seconds() if r[4] and r[5] else None,
                        "first_latency_s": (r[4] - r[6]).total_seconds() if r[4] else None,
                    }
                    for r in batches
                ],
            }

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        async with db.transaction() as s:
            if not await s.get(Admin, 1):
                return RedirectResponse("/setup", status_code=303)
        return auth_page(request)

    @app.post("/login")
    async def login(request: Request):
        try:
            form = await auth_form(request)
        except HTTPException as exc:
            return auth_page(request, error=exc.detail, status=exc.status_code)
        t = time.monotonic()
        while login_attempts and login_attempts[0] < t - 60:
            login_attempts.popleft()
        if len(login_attempts) >= 8:
            return auth_page(
                request, error="Aguarde um minuto antes de tentar novamente.", status=429
            )
        login_attempts.append(t)
        async with db.transaction() as s:
            await lock(s, "admin", "credential")
            admin = await s.get(Admin, 1)
            try:
                if not admin or len(str(form.get("password", ""))) > 256:
                    raise ValueError()
                await asyncio.to_thread(
                    PasswordHasher().verify, admin.password, str(form.get("password", ""))
                )
            except (VerificationError, ValueError):
                return auth_page(request, error="Senha inválida. Tente novamente.", status=401)
            token = secrets.token_urlsafe(32)
            s.add(
                AdminSession(
                    digest=digest(token),
                    csrf=secrets.token_urlsafe(32),
                    expires=now() + timedelta(hours=8),
                )
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "session", token, httponly=True, samesite="strict", secure=cfg.secure, max_age=28800
        )
        response.delete_cookie("login_nonce")
        return response

    @app.post("/password")
    async def change_password(request: Request):
        form = await mutation(request)
        try:
            password = validate_password(form)
            async with db.transaction() as s:
                await lock(s, "admin", "credential")
                admin = await s.get(Admin, 1)
                await asyncio.to_thread(
                    PasswordHasher().verify, admin.password, str(form.get("current", ""))[:257]
                )
                admin.password = await asyncio.to_thread(PasswordHasher().hash, password)
                from sqlalchemy import delete

                await s.execute(delete(AdminSession))
                s.add(Event(category="admin", code="password_changed", values={}))
            return RedirectResponse("/login", status_code=303)
        except (ValueError, VerificationError):
            return auth_page(
                request,
                error="Senha atual ou confirmação inválida. A senha não foi alterada.",
                status=400,
            )

    @app.post("/logout")
    async def logout(request: Request):
        await mutation(request)
        async with db.transaction() as s:
            session = await s.get(AdminSession, digest(request.cookies["session"]))
            await s.delete(session)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("session")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        try:
            session = await authenticated(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        async with db.transaction() as s:
            ops = await options(s)
            counts = {}
            for name, model in [
                ("destinatários", Recipient),
                ("inscrições", Subscription),
                ("turmas", Classroom),
                ("eventos", Event),
            ]:
                counts[name] = await s.scalar(select(func.count()).select_from(model))
            counts["disciplinas"] = await s.scalar(
                select(func.count(func.distinct(Classroom.code)))
            )
            counts["com inscrições"] = await s.scalar(
                select(func.count(func.distinct(Subscription.recipient_id)))
            )
            counts["pausados"] = await s.scalar(
                select(func.count()).select_from(Recipient).where(Recipient.paused)
            )
            counts["disciplinas monitoradas"] = await s.scalar(
                select(func.count(func.distinct(Subscription.code)))
            )
            selected_count = await s.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(func.json_array_length(Subscription.numbers) > 0)
            )
            all_count = counts["inscrições"] - selected_count
            series = await s.get(Setting, "metrics_daily")
            sub_status = dict(
                (
                    await s.execute(
                        select(Subscription.status, func.count()).group_by(Subscription.status)
                    )
                ).all()
            )
            out_status = dict(
                (await s.execute(select(Outbox.status, func.count()).group_by(Outbox.status))).all()
            )
            oldest = await s.scalar(
                select(func.min(Outbox.created)).where(Outbox.status.in_(["pending", "retry"]))
            )
            query = request.query_params.get("q", "")[:120]
            page = max(
                0,
                min(
                    10000,
                    int(request.query_params.get("page", "0"))
                    if request.query_params.get("page", "0").isdigit()
                    else 0,
                ),
            )
            from vagasunb.catalog import search

            catalog, total, approximate = await search(s, query, ops["period"], page)
            runs = (await s.scalars(select(Run).order_by(Run.started.desc()).limit(100))).all()
            events = (await s.scalars(select(Event).order_by(Event.created.desc()).limit(60))).all()
            heartbeat = await s.get(Setting, "worker_health")
            database_size = await s.scalar(text("SELECT pg_database_size(current_database())"))
            tables = (
                (
                    await s.execute(
                        text(
                            "SELECT relname, pg_total_relation_size(relid) AS bytes FROM pg_catalog.pg_statio_user_tables ORDER BY bytes DESC LIMIT 5"
                        )
                    )
                )
                .mappings()
                .all()
            )
            migration = await s.scalar(text("SELECT version_num FROM alembic_version"))
            durations = sorted(r.duration_ms for r in runs)
            process = psutil.Process()
            disk = shutil.disk_usage(cfg.secrets.resolve().parent)
            context = dict(
                mode=cfg.mode,
                config_version=digest(json.dumps(ops, sort_keys=True)),
                gates={
                    task: state(ops, task, now()) for task in ("register", "collect", "dispatch")
                },
                ops=ops,
                counts=counts,
                csrf=session.csrf,
                catalog=catalog,
                total=total,
                approximate=approximate,
                q=query,
                page=page,
                runs=runs[:20],
                events=events,
                out_status=out_status,
                sub_status=sub_status,
                oldest=int((now() - oldest).total_seconds()) if oldest else None,
                database_size=database_size,
                tables=tables,
                uptime=int(time.monotonic() - started),
                memory=process.memory_info().rss,
                cpu=round(process.cpu_times().user + process.cpu_times().system, 2),
                disk_free=disk.free,
                heartbeat=heartbeat.value if heartbeat else None,
                mean_ms=round(sum(durations) / len(durations), 1) if durations else None,
                p95_ms=durations[min(len(durations) - 1, int(len(durations) * 0.95))]
                if durations
                else None,
                now=now(),
                title="Visão geral",
                migration=migration,
                catalog_source=ops["source"],
                series=sorted((series.value if series else {}).items())[-30:],
                all_count=all_count,
                selected_count=selected_count,
            )
        return templates.TemplateResponse(request=request, name="dashboard.html", context=context)

    @app.post("/operations")
    async def operations(request: Request):
        form = await mutation(request)
        action = str(form.get("action", ""))
        async with db.transaction() as s:
            await lock(s, "operations", "config")
            ops = await options(s)
            if action in {"collect_paused", "dispatch_paused", "registrations_open"}:
                value = str(form.get("value", str(not ops[action]).lower())) == "true"
                await change_options(s, {action: value})
            elif action == "cleanup":
                await cleanup(s)
            elif action == "collect":
                await s.merge(
                    Setting(
                        key="collect_request",
                        value={"requested": now().isoformat()},
                    )
                )
            elif action == "config":
                if form.get("config_version") and not hmac.compare_digest(
                    str(form["config_version"]), digest(json.dumps(ops, sort_keys=True))
                ):
                    raise HTTPException(
                        409,
                        "Outra aba alterou as opções. Recarregue antes de salvar; seus valores continuam nesta tela.",
                    )
                try:
                    from datetime import datetime

                    period = str(form.get("period", ops["period"])).strip()
                    end = datetime.fromisoformat(str(form.get("window_end", ops["window_end"])))
                    if not period or len(period) > 20 or not end.tzinfo:
                        raise ValueError()
                    values = {
                        key: int(str(form[key]))
                        for key in [
                            "interval",
                            "debounce",
                            "cooldown",
                            "stale",
                            "group_delay",
                            "outbox_days",
                            "events_days",
                            "recipient_days",
                            "send_rate",
                            "send_concurrency",
                            "screen_interval",
                        ]
                        if key in form
                    }
                    from vagasunb.schedule import local_instant

                    timezone = str(form.get("timezone", ops["timezone"]))
                    if "timezone" in form:
                        values["timezone"] = timezone
                    if "window_end_local" in form:
                        end = datetime.fromisoformat(
                            local_instant(str(form["window_end_local"]), timezone)
                        )
                    for key in ("collection_auto", "registrations_open"):
                        if key in form:
                            if form[key] not in {"true", "false"}:
                                raise ValueError()
                            values[key] = form[key] == "true"
                    if "closed_message" in form:
                        values["closed_message"] = str(form["closed_message"]).strip()
                    for task in ("register", "collect", "dispatch"):
                        key = "schedule_" + task
                        if key + "_enabled" in form:
                            values[key] = {
                                "enabled": form[key + "_enabled"] == "true",
                                "start": local_instant(str(form.get(key + "_start", "")), timezone),
                                "end": local_instant(str(form.get(key + "_end", "")), timezone),
                                "days": [int(v) for v in form.getlist(key + "_days")],
                                "from": str(form.get(key + "_from", "")),
                                "to": str(form.get(key + "_to", "")),
                            }
                    scopes = (
                        [v.strip() for v in str(form["scopes"]).splitlines() if v.strip()]
                        if "scopes" in form
                        else ops["scopes"]
                    )
                    if len(scopes) > 250 or any(len(v) > 100 for v in scopes):
                        raise ValueError()
                    if cfg.mode != "demo" and any(not v.isdigit() for v in scopes):
                        raise ValueError()
                    values.update(period=period, window_end=end.isoformat(), scopes=scopes)
                    source = str(form.get("source", ops["source"]))
                    if source not in {"demo", "public"}:
                        raise ValueError()
                    if source == "public":
                        import re

                        if not re.fullmatch(r"\d{4}\.\d", period):
                            raise ValueError()
                    values["source"] = source
                    if source == "demo" and not period.startswith("DEMO"):
                        raise ValueError()
                    await change_options(s, values)
                except (ValueError, KeyError):
                    raise HTTPException(400, "Configuração inválida") from None
            elif action == "retry":
                # Only safe unsent retry states; never retry uncertain acceptance automatically.
                for item in (
                    await s.scalars(
                        select(Outbox)
                        .where(Outbox.status == "retry", Outbox.attempts < 4)
                        .limit(50)
                    )
                ).all():
                    item.due = now()
                s.add(Event(category="dispatch", code="retry_requested", values={}))
            else:
                raise HTTPException(400, "Ação desconhecida")
        return RedirectResponse("/?saved=1#settings", status_code=303)

    @app.get("/events.csv")
    async def export_events(request: Request):
        await authenticated(request)
        async with db.transaction() as s:
            rows = (await s.scalars(select(Event).order_by(Event.created.desc()).limit(500))).all()
        return Response(
            "date,category,code\n"
            + "\n".join(f"{r.created.isoformat()},{r.category},{r.code}" for r in rows),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=events.csv"},
        )

    if cfg.mode == "demo":

        @app.get("/demo", response_class=HTMLResponse)
        async def demo_page(request: Request):
            session = await authenticated(request)
            async with db.transaction() as s:
                source = (await options(s))["source"]
                classes = (
                    await s.scalars(select(Classroom).order_by(Classroom.code, Classroom.number))
                ).all()
            return templates.TemplateResponse(
                request=request,
                name="demo.html",
                context={
                    "csrf": session.csrf,
                    "classes": classes,
                    "mode": cfg.mode,
                    "source": source,
                },
            )

        @app.post("/demo/interact")
        async def interact(request: Request):
            form = await mutation(request)
            from vagasunb.router import route

            recipient = str(form.get("recipient", "1"))
            if recipient not in {"1", "2", "3"}:
                raise HTTPException(400, "Destinatário sintético inválido")
            async with db.transaction() as s:
                view = await route(
                    s,
                    vault,
                    -900000 - int(recipient),
                    text=str(form.get("text", "")),
                    callback=str(form.get("callback")) if form.get("callback") else None,
                )
            return view.as_dict()

        @app.post("/demo/vacancy")
        async def vacancy(request: Request):
            form = await mutation(request)
            try:
                available = int(str(form.get("available", "")))
                if not 0 <= available <= 30:
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, "Vagas entre 0 e 30") from None
            async with db.transaction() as s:
                if (await options(s))["source"] != "demo":
                    raise HTTPException(403, "Alteração de vagas só é permitida na fonte demo.")
                c = await s.get(Classroom, str(form.get("cid", "")))
                source = await s.get(Setting, "demo_source")
                if not c or not source:
                    raise HTTPException(400, "Turma inválida")
                rows = [dict(r) for r in source.value["rows"]]
                for row in rows:
                    if (row["code"], row["number"], row["period"]) == (c.code, c.number, c.period):
                        row["occupied"] = 30 - available
                source.value = {"rows": rows}
                scope = c.units[0]
            from vagasunb.demo import DemoSource
            from vagasunb.monitor import collect_scope

            await collect_scope(db, DemoSource(db), scope, "demo")
            return {
                "status": "Observação registrada. Para confirmar a abertura, observe novamente após o tempo de confirmação; o serviço também segue as atualizações configuradas."
            }

        @app.post("/demo/dispatch")
        async def dispatch(request: Request):
            await mutation(request)
            from vagasunb.distribution import dispatch_batch
            from vagasunb.telegram import FakeTransport

            transport = FakeTransport()
            await dispatch_batch(db, vault, transport, "demo")
            return {"sent": len(transport.views)}

        @app.post("/demo/alerts")
        async def simulated_alerts(request: Request):
            form = await mutation(request)
            recipient = str(form.get("recipient", "1"))
            if recipient not in {"1", "2", "3"}:
                raise HTTPException(400, "Destinatário sintético inválido")
            seen = str(form.get("seen", ""))[:1200].split(",")
            from vagasunb.router import alert_view

            views = []
            async with db.transaction() as s:
                r = await s.scalar(
                    select(Recipient).where(
                        Recipient.lookup == vault.index(-900000 - int(recipient))
                    )
                )
                if not r:
                    return {"alerts": []}
                rows = (
                    await s.scalars(
                        select(Outbox)
                        .where(
                            Outbox.recipient_id == r.id,
                            Outbox.mode == "demo",
                            Outbox.status == "simulated",
                            Outbox.id.not_in(seen),
                        )
                        .order_by(Outbox.sent.desc())
                        .limit(8)
                    )
                ).all()
                groups = {}
                for row in rows:
                    groups.setdefault((row.subscription_id, row.sent), []).append(row)
                for (sid, _), items in groups.items():
                    if not await s.get(Subscription, sid):
                        continue
                    classes = [await s.get(Classroom, item.classroom_id) for item in items]
                    view = await alert_view(s, r.id, sid, classes)
                    views.append({"ids": [i.id for i in items], **view.as_dict()})
            return {"alerts": views}

    return app
