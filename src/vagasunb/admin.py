import hashlib
import secrets
import shutil
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

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
    app.mount("/assets", StaticFiles(directory=ASSETS / "assets"), name="assets")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
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
        origin = request.headers.get("origin")
        if origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origem inválida")

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
            return {"status": "ok", "component": "admin"}
        except Exception:
            return JSONResponse({"status": "degraded", "component": "admin"}, status_code=503)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        token = secrets.token_urlsafe(32)
        response = templates.TemplateResponse(
            request=request, name="login.html", context={"nonce": token, "error": ""}
        )
        response.set_cookie(
            "login_nonce", token, httponly=True, samesite="strict", secure=cfg.secure, max_age=600
        )
        return response

    @app.post("/login")
    async def login(request: Request):
        check_origin(request)
        form = await request.form()
        nonce = request.cookies.get("login_nonce", "")
        if not nonce or not secrets.compare_digest(nonce, str(form.get("nonce", ""))):
            raise HTTPException(403, "CSRF inválido")
        t = time.monotonic()
        while login_attempts and login_attempts[0] < t - 60:
            login_attempts.popleft()
        if len(login_attempts) >= 8:
            raise HTTPException(429, "Aguarde um minuto antes de tentar novamente")
        login_attempts.append(t)
        async with db.transaction() as s:
            admin = await s.get(Admin, 1)
            try:
                if not admin or len(str(form.get("password", ""))) > 256:
                    raise ValueError()
                PasswordHasher().verify(admin.password, str(form.get("password", "")))
            except (VerificationError, ValueError):
                raise HTTPException(401, "Credencial inválida") from None
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
            durations = sorted(r.duration_ms for r in runs)
            process = psutil.Process()
            disk = shutil.disk_usage(cfg.secrets.resolve().parent)
            context = dict(
                mode=cfg.mode,
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
            ops = await options(s)
            if action in {"collect_paused", "dispatch_paused"}:
                await change_options(s, {action: not ops[action]})
            elif action == "cleanup":
                await cleanup(s)
            elif action == "collect":
                scope = str(form.get("scope", ""))
                if scope not in ops["scopes"]:
                    raise HTTPException(400, "Escopo inválido")
                await s.merge(
                    Setting(
                        key="collect_request",
                        value={"scope": scope, "requested": now().isoformat()},
                    )
                )
            elif action == "config":
                try:
                    from datetime import datetime

                    period = str(form.get("period", "")).strip()
                    end = datetime.fromisoformat(str(form.get("window_end", "")))
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
                        ]
                    }
                    scopes = [
                        v.strip() for v in str(form.get("scopes", "")).splitlines() if v.strip()
                    ]
                    if len(scopes) > 250 or any(len(v) > 100 for v in scopes):
                        raise ValueError()
                    if cfg.mode != "demo" and any(not v.isdigit() for v in scopes):
                        raise ValueError()
                    values.update(period=period, window_end=end.isoformat(), scopes=scopes)
                    if cfg.mode == "demo" and not period.startswith("DEMO"):
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
        return RedirectResponse("/", status_code=303)

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
                classes = (
                    await s.scalars(select(Classroom).order_by(Classroom.code, Classroom.number))
                ).all()
            return templates.TemplateResponse(
                request=request,
                name="demo.html",
                context={"csrf": session.csrf, "classes": classes, "mode": cfg.mode},
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
                "status": "Observação registrada. Para confirmar abertura, observe novamente após o debounce; o worker também coleta."
            }

        @app.post("/demo/dispatch")
        async def dispatch(request: Request):
            await mutation(request)
            from vagasunb.monitor import dispatch_once
            from vagasunb.telegram import FakeTransport

            transport = FakeTransport()
            await dispatch_once(db, vault, transport, "demo")
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
