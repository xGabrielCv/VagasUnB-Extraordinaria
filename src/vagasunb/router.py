import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape

from sqlalchemy import select

from vagasunb.catalog import search
from vagasunb.presentation import date_br, label as present
from vagasunb.db import Callback, Classroom, Recipient, Subscription, lock, now
from vagasunb.services import erase, options, register, subscribe, subscription_action

SOURCE = "https://github.com/xGabrielCv/VagasUnB-Extraordinaria"
SIGAA = "https://sigaa.unb.br/sigaa/public/turmas/listar.jsf"
COMMANDS = [
    "start",
    "menu",
    "buscar",
    "catalogo",
    "minhas",
    "status",
    "ajuda",
    "privacidade",
    "apagar_dados",
]


@dataclass
class View:
    text: str
    buttons: list = field(default_factory=list)

    def __post_init__(self):
        if len(self.text) > 3900:
            from bs4 import BeautifulSoup

            plain = BeautifulSoup(self.text, "html.parser").get_text()
            chunks = []
            length = 0
            for char in plain:
                encoded = escape(char)
                if length + len(encoded) > 3800:
                    break
                chunks.append(encoded)
                length += len(encoded)
            self.text = "".join(chunks) + "…\nConsulte os detalhes no catálogo."

    def as_dict(self):
        return {"text": self.text, "buttons": self.buttons}


async def button(s, rid, label, action, **args):
    token = secrets.token_urlsafe(16)
    assert len(token.encode()) <= 64
    s.add(
        Callback(
            token=token,
            recipient_id=rid,
            action=action,
            args=args,
            expires=now() + timedelta(minutes=15),
        )
    )
    return {"label": label, "callback": token}


async def alert_view(s, rid, sid, classes):
    sub = await s.get(Subscription, sid)
    lines = [
        "🚨 <b>VagasUnB (Extraordinária)</b>",
        "Apareceu uma vaga!",
        escape(classes[0].name),
        escape(classes[0].code),
    ]
    for c in classes:
        lines.append(
            f"Turma {escape(c.number)} · {c.available} vaga(s)\n{escape((c.teachers or 'Não informado')[:100])} · {escape((c.schedule or 'Não informado')[:100])}\nObservado: {date_br(c.observed)}"
        )
    lines.append("O alerta não garante disponibilidade. A matrícula é feita por você no SIGAA.")
    buttons = [{"label": "Abrir SIGAA", "url": SIGAA}]
    for label, action in [
        ("✅ Consegui minha vaga", "achieved"),
        ("Continuar monitorando", "continue"),
        ("Parar de monitorar", "stop"),
    ]:
        buttons.append(
            await button(s, rid, label, "sub_action", sid=sid, op=action, revision=sub.revision)
        )
    return View("\n\n".join(lines), buttons)


async def route(
    s, vault, chat_id, text="", callback=None, private=True, sender_matches=True, synthetic=True
):
    if not private or not sender_matches:
        return View("Use este serviço somente na sua conversa privada.")
    if len(text) > 120:
        return View("Entrada limitada a 120 caracteres.")
    r = await s.scalar(select(Recipient).where(Recipient.lookup == vault.index(chat_id)))
    if text == "/start":
        r = await register(s, vault, chat_id, synthetic=synthetic)
    if not r:
        return View("Seus dados não estão cadastrados. Envie /start para iniciar conscientemente.")
    await lock(s, "recipient", r.id)
    # Re-read after lock: erase may have won the race.
    await s.refresh(r)
    r.active_at = now()
    cfg = await options(s)
    action, args = "home", {}
    if callback:
        cb = await s.get(Callback, callback)
        if not cb or cb.recipient_id != r.id or cb.expires <= now():
            return View("⌛ Este menu expirou. Envie /menu.")
        action, args = cb.action, cb.args
        await s.delete(cb)  # All callbacks single-use, including sensitive confirmations.
    elif text.startswith("/"):
        action = {
            "/start": "home",
            "/menu": "home",
            "/buscar": "search",
            "/catalogo": "campus",
            "/minhas": "subscriptions",
            "/status": "status",
            "/ajuda": "about",
            "/privacidade": "privacy",
            "/apagar_dados": "delete",
        }.get(text, "home")
    elif text.strip():
        action, args = "results", {"query": text.strip()}
    rid = r.id

    async def b(label, action, **kwargs):
        return await button(s, rid, label, action, **kwargs)

    async def finish(content, buttons=None, back="home", **back_args):
        buttons = buttons or []
        buttons.extend([await b("◀ Voltar", back, **back_args), await b("⌂ Início", "home")])
        return View(content, buttons)

    if action == "home":
        subs = list(
            (await s.scalars(select(Subscription).where(Subscription.recipient_id == rid))).all()
        )
        return View(
            f"🎓 <b>VagasUnB (Extraordinária)</b>\n\nMonitoramentos: {len(subs)}/10\nPeríodo: {escape(cfg['period'])}\nAs vagas podem mudar; consulte a última observação.\n\nPor onde vamos começar?",
            [
                await b(label, act)
                for label, act in [
                    ("🔎 Buscar disciplina", "search"),
                    ("📚 Explorar catálogo", "campus"),
                    ("🔔 Meus monitoramentos", "subscriptions"),
                    ("🟢 Consultar vagas", "vacancies"),
                    ("Sobre e suporte", "about"),
                ]
            ],
        )
    if action in {"search", "vacancies"}:
        return await finish(
            "🔎 <b>Buscar disciplina</b>\n\nDigite código, nome ou código + turma. Exemplo sintético: DEMO0001 01.\nA busca usa somente o catálogo local; não ativa monitoramento.",
            [
                await b("Explorar catálogo", "campus"),
                await b("Turmas com vagas", "results", vacancies=True),
            ],
        )
    if action == "campus":
        campuses = sorted(
            {
                c or "?"
                for c in (
                    await s.scalars(
                        select(Classroom.campus).where(Classroom.period == cfg["period"]).distinct()
                    )
                ).all()
            }
        )
        return await finish(
            "🏫 <b>Explorar catálogo</b>\nEscolha o campus. Sem mapeamento validado, o campus permanece desconhecido.",
            [
                await b(c if c != "?" else "Campus não identificado", "units", campus=c)
                for c in campuses[:8]
            ],
        )
    if action == "units":
        rows, _, _ = await search(s, "", cfg["period"], campus=args.get("campus"))
        stmt = select(Classroom).where(Classroom.period == cfg["period"])
        stmt = (
            stmt.where(Classroom.campus.is_(None))
            if args.get("campus") == "?"
            else stmt.where(Classroom.campus == args.get("campus"))
        )
        rows = (await s.scalars(stmt)).all()
        units = sorted({u for c in rows for u in c.units})
        page = args.get("page", 0)
        buttons = [
            await b(u, "results", campus=args.get("campus"), unit=u)
            for u in units[page * 6 : page * 6 + 6]
        ]
        if len(units) > (page + 1) * 6:
            buttons.append(
                await b("Próxima página", "units", campus=args.get("campus"), page=page + 1)
            )
        return await finish(
            "🏢 <b>Unidades públicas</b>\nSelecione uma unidade.", buttons, "campus"
        )
    if action == "results":
        q = args.pop("query", "")  # Never persist free-text search in callback args.
        rows, total, approx = await search(
            s,
            q,
            cfg["period"],
            page=args.get("page", 0),
            campus=args.get("campus"),
            unit=args.get("unit"),
            vacancies=args.get("vacancies", False),
        )
        buttons = [
            await b(f"{c.code} · {c.name[:35]} · {c.number}", "details", cid=c.id) for c in rows
        ]
        if not q and total > (args.get("page", 0) + 1) * 6:
            buttons.append(
                await b("Próxima página", "results", **{**args, "page": args.get("page", 0) + 1})
            )
        title = (
            "Sugestões aproximadas — escolha explicitamente"
            if approx
            else f"Resultados: {total} turma(s)"
        )
        return await finish(
            f"📚 <b>{title}</b>\nPeríodo {escape(cfg['period'])}.\n"
            + (
                "Nenhuma disciplina encontrada. Tente outro código."
                if not rows
                else "Selecione uma turma para ver detalhes. Para refinar, digite uma nova busca."
            ),
            buttons,
        )
    if action == "choose":
        c = await s.get(Classroom, args.get("cid"))
        if not c or c.period != cfg["period"]:
            return await finish("Turma ou período indisponível.")
        selected = set(args.get("numbers", []))
        rows = (
            await s.scalars(
                select(Classroom)
                .where(Classroom.code == c.code, Classroom.period == c.period)
                .order_by(Classroom.number)
            )
        ).all()
        if not selected.issubset({row.number for row in rows}):
            return await finish("Seleção inválida.")
        page = args.get("page", 0)
        buttons = []
        for row in rows[page * 4 : page * 4 + 4]:
            values = selected ^ {row.number}
            buttons.append(
                await b(
                    ("☑ " if row.number in selected else "☐ ") + f"Turma {row.number}",
                    "choose",
                    cid=c.id,
                    numbers=sorted(values),
                    page=page,
                )
            )
        if selected:
            buttons.append(
                await b(
                    "Confirmar turmas selecionadas", "confirm", cid=c.id, numbers=sorted(selected)
                )
            )
        if (page + 1) * 4 < len(rows):
            buttons.append(
                await b(
                    "Próxima página", "choose", cid=c.id, numbers=sorted(selected), page=page + 1
                )
            )
        if page:
            buttons.append(
                await b(
                    "Página anterior", "choose", cid=c.id, numbers=sorted(selected), page=page - 1
                )
            )
        return await finish(
            "<b>Escolher turmas</b>\nMarque uma ou mais e confirme. Toda a seleção conta como uma disciplina.",
            buttons,
        )
    if action in {"details", "confirm", "activate"}:
        c = await s.get(Classroom, args.get("cid"))
        if not c or c.period != cfg["period"]:
            return await finish("Turma ou período indisponível.")
        if action == "details":
            freshness = (
                present("STALE")
                if not c.observed or (now() - c.observed).total_seconds() > cfg["stale"]
                else present(c.state)
            )
            lines = [
                f"📘 <b>{escape(c.name)}</b>",
                f"{escape(c.code)} · Turma {escape(c.number)} · {escape(c.period)}",
                f"Campus: {escape(c.campus or 'Campus não identificado')}\nUnidades: {escape(', '.join(c.units))}",
                f"Docentes: {escape(c.teachers or 'Não informado')}\nHorário: {escape(c.schedule or 'Não informado')}\nLocal: {escape(c.location or 'Não informado')}",
                f"Ofertadas: {c.offered if c.offered is not None else 'Não informado'} · Ocupadas: {c.occupied if c.occupied is not None else 'Não informado'}\nDisponibilidade: {c.available if c.available is not None else 'Não informado'} · {freshness}",
                f"Última observação válida: {date_br(c.observed) if c.observed else 'Não informado'}",
            ]
            return await finish(
                "\n\n".join(lines),
                [
                    await b("Monitorar esta turma", "confirm", cid=c.id, numbers=[c.number]),
                    await b("Monitorar disciplina inteira", "confirm", cid=c.id, numbers=[]),
                    await b("Escolher várias turmas", "choose", cid=c.id, numbers=[]),
                ],
            )
        if action == "confirm":
            return await finish(
                f"🔔 <b>Confirmar monitoramento</b>\n{escape(c.name)} · {escape(c.period)}\nTurmas: {escape(', '.join(args['numbers']) or 'Todas, incluindo novas turmas')}\nA disponibilidade atual não dispara alerta inicial.",
                [await b("✅ Ativar monitoramento", "activate", **args)],
            )
        try:
            sub, created = await subscribe(s, rid, c.code, c.period, args["numbers"])
            return await finish(
                "✅ Monitoramento ativado."
                if created
                else "Monitoramento existente; seleção atualizada sem duplicação.",
                [await b("Gerenciar monitoramento", "subscription", sid=sub.id)],
            )
        except ValueError as exc:
            return await finish(escape(str(exc)))
    if action == "subscriptions":
        subs = list(
            (
                await s.scalars(
                    select(Subscription)
                    .where(Subscription.recipient_id == rid)
                    .order_by(Subscription.code)
                )
            ).all()
        )
        buttons = [
            await b(
                f"{sub.code} · {present(sub.status)} · {', '.join(sub.numbers) or 'Todas'}",
                "subscription",
                sid=sub.id,
            )
            for sub in subs
        ]
        buttons.append(
            await b(
                "Retomar todos os alertas" if r.paused else "Pausar todos os alertas",
                "pause_all",
                paused=not r.paused,
            )
        )
        return await finish(
            f"🔔 <b>Meus monitoramentos</b>\n{len(subs)}/10 disciplinas; pausadas contam no limite.",
            buttons,
        )
    if action == "pause_all":
        r.paused = bool(args["paused"])
        if r.paused:
            from vagasunb.services import cancel_pending

            for sub in (
                await s.scalars(select(Subscription).where(Subscription.recipient_id == rid))
            ).all():
                await cancel_pending(s, sub.id)
        return await finish("Preferência de alertas atualizada.", back="subscriptions")
    if action in {"subscription", "sub_action"}:
        sub = await s.get(Subscription, args.get("sid"))
        if not sub or sub.recipient_id != rid:
            return await finish("Este monitoramento não está disponível.")
        if action == "sub_action":
            op = args["op"]
            if op == "achieved":
                return await finish(
                    "Você informou que conseguiu sua vaga. Deseja encerrar o monitoramento da disciplina? Não verificamos matrícula.",
                    [
                        await b(
                            "Sim, encerrar",
                            "sub_action",
                            sid=sub.id,
                            op="complete",
                            revision=sub.revision,
                        ),
                        await b(
                            "Continuar acompanhando",
                            "sub_action",
                            sid=sub.id,
                            op="continue",
                            revision=sub.revision,
                        ),
                    ],
                )
            try:
                await subscription_action(s, rid, sub.id, op, args.get("revision"))
                return await finish(
                    "Monitoramento encerrado."
                    if op in {"stop", "complete"}
                    else "Preferência atualizada. O mesmo evento não será repetido.",
                    back="subscriptions",
                )
            except ValueError as exc:
                return await finish(escape(str(exc)))
        rows = (
            await s.scalars(
                select(Classroom)
                .where(Classroom.code == sub.code, Classroom.period == sub.period)
                .order_by(Classroom.number)
            )
        ).all()
        buttons = [
            await b("Consultar estado atual", "subscription", sid=sub.id),
            await b(
                "Retomar" if sub.status == "paused" else "Pausar",
                "sub_action",
                sid=sub.id,
                op="resume" if sub.status == "paused" else "pause",
                revision=sub.revision,
            ),
            await b(
                "Parar de monitorar", "sub_action", sid=sub.id, op="stop", revision=sub.revision
            ),
        ]
        if rows:
            buttons.append(await b("Alterar turmas: todas", "confirm", cid=rows[0].id, numbers=[]))
            for c in rows[:4]:
                buttons.append(
                    await b(
                        f"Alterar: somente turma {c.number}",
                        "confirm",
                        cid=c.id,
                        numbers=[c.number],
                    )
                )
        states = "\n".join(
            f"Turma {escape(c.number)}: {c.available if c.available is not None else 'Não informado'} vaga(s) · {('STALE' if not c.observed or (now() - c.observed).total_seconds() > cfg['stale'] else c.state)}"
            for c in rows
        )
        return await finish(
            f"<b>{escape(sub.code)}</b> · {escape(sub.period)}\nEstado: {present(sub.status)}\nTurmas: {escape(', '.join(sub.numbers) or 'Todas')}\n\n{states}",
            buttons,
            "subscriptions",
        )
    if action == "delete":
        return await finish(
            "<b>Apagar meus dados?</b>\nRemove destinatário, inscrições, preferências, callbacks e alertas. Uma chamada já em voo pode terminar antes da confirmação. Backups anteriores expiram em até 7 dias; não são reescritos automaticamente.",
            [await b("Sim, apagar", "erase")],
        )
    if action == "erase":
        await erase(s, rid)
        return View(
            "Seus dados ativos foram removidos. Não iniciaremos novos alertas dessas inscrições. Mensagens já enviadas não podem ser recolhidas. Backups anteriores expiram em até 7 dias. Somente um novo /start cria cadastro."
        )
    if action == "status":
        return await finish(
            f"Período: {escape(cfg['period'])}\nColeta: {'pausada' if cfg['collect_paused'] else 'habilitada'}\nEnvio: {'pausado' if cfg['dispatch_paused'] else 'habilitado'}\nConsulte a data dos detalhes; indisponibilidade não apaga a última observação válida."
        )
    if action == "privacy":
        return await finish(
            "<b>Uso dos dados</b>\nGuardamos chat ID cifrado e índice HMAC, inscrições, preferências e controle operacional mínimo. São dados pseudonimizados; não anonimato absoluto. Não guardamos nome, username, telefone, matrícula, e-mail, texto de busca ou conversa. O Telegram tem seu próprio processamento.\n\nCallbacks: 15 min. Envios finalizados: 7 dias. Eventos públicos/coletas: 30 dias. Destinatário inativo sem inscrições: 30 dias. Backups privados, quando usados: 3 cópias e 7 dias. Restauração exige conciliar exclusões.\n\nFinalidade: enviar alertas solicitados. Sem publicidade ou analytics. Suporte: GitHub Issues, sem dados privados.",
            [await b("Apagar meus dados", "delete"), {"label": "Código-fonte", "url": SOURCE}],
        )
    if action == "issue_warning":
        return await finish(
            "<b>Cuidado com seus dados</b>\nRelatos no GitHub são públicos. Não inclua senha, matrícula, telefone, CPF ou dados privados.",
            [{"label": "Entendi, abrir Issues", "url": SOURCE + "/issues/new"}],
        )
    if action == "faq":
        return await finish(
            "<b>Como funciona?</b>\nBusque uma disciplina, escolha turmas e confirme. Até 10 disciplinas, inclusive pausadas. Novas turmas entram em monitoramento da disciplina inteira sem alerta inicial.\n\nA vaga é garantida? Não. As informações podem atrasar; a matrícula é feita por você no SIGAA.\n\nO bot é da UnB? Não, é gratuito e independente. Nunca pedimos credenciais acadêmicas.\n\nPor que não repete? Debounce exige nova coleta, cooldown evita spam; disponibilidade contínua não é novo evento."
        )
    return await finish(
        "<b>VagasUnB (Extraordinária)</b>\nGratuito e independente da Universidade de Brasília. Consulta catálogo público e alerta sobre vagas; não realiza nem verifica matrícula.\n\nCliente público adaptado do Followw UnB (MIT). Operação: reiniciar containers pelos scripts/Compose; o painel controla somente tarefas internas.",
        [
            await b("Como funciona / FAQ", "faq"),
            await b("Uso dos dados", "privacy"),
            await b("Suporte / sugestão", "issue_warning"),
            {"label": "Código-fonte", "url": SOURCE},
        ],
    )
