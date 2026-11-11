"""Brazilian presentation shared by templates, simulator, bot and local metrics UI."""

from datetime import datetime
from zoneinfo import ZoneInfo

COUNT_HELP = {
    "destinatários": "Cadastros que ainda existem no banco. Não equivalem a pessoas únicas; nenhuma identificação individual é mostrada.",
    "inscrições": "Monitoramentos em todos os períodos, incluindo pausados. Cada disciplina conta uma vez por destinatário.",
    "turmas": "Turmas canônicas de todos os períodos no banco, incluindo demonstração e catálogo público. A tabela abaixo usa o período selecionado.",
    "eventos": "Registros sanitizados ainda conservados pela política de retenção. Não são conversas ou atualizações completas do Telegram.",
    "disciplinas": "Códigos distintos no catálogo de todos os períodos. Várias turmas do mesmo código contam uma disciplina.",
    "com inscrições": "Cadastros com pelo menos um monitoramento, em qualquer período. Não expõe a lista de destinatários.",
    "pausados": "Destinatários que pausaram todos os seus avisos. Não significa que a atualização do catálogo está pausada.",
    "disciplinas monitoradas": "Códigos distintos com monitoramentos no banco. Cem inscrições no mesmo código compartilham as consultas ao SIGAA.",
}
LABELS = {
    "waiting": "Aguardando o próximo ciclo",
    "collection": "Atualização do catálogo",
    "detection": "Detecção de vagas",
    "subscriptions": "Monitoramentos",
    "maintenance": "Manutenção",
    "admin": "Administração",
    "stop": "Monitoramento encerrado",
    "opening": "Abertura confirmada",
    "unknown_count": "Quantidade de vagas não confirmada",
    "web_setup_completed": "Senha inicial cadastrada",
    "password_changed": "Senha alterada",
    "network_unavailable": "Fonte sem resposta",
    "rate_limit": "Fonte pediu uma pausa",
    "http_unavailable": "Fonte indisponível",
    "access_restricted": "Acesso público restringido",
    "circuit_open": "Pausa automática antes de tentar novamente",
    "search_rejected": "Consulta recusada pela fonte",
    "manual": "Aguardando atualização manual",
    "NO_VACANCY": "Sem vagas",
    "demo": "Demonstração",
    "public": "Catálogo público",
    "running": "Em andamento",
    "complete": "Concluída",
    "partial": "Parcialmente atualizada",
    "degraded": "Fonte indisponível",
    "interrupted": "Interrompida",
    "not_started": "Aguardando primeira atualização",
    "starting": "Iniciando",
    "ok": "Funcionando",
    "paused": "Pausado manualmente",
    "scheduled": "Aguardando horário programado",
    "quarantine": "Proteção após restauração",
    "simulated": "Simulado",
    "disabled": "Desligado",
    "enabled": "Habilitado",
    "pending": "Aguardando envio",
    "retry": "Nova tentativa programada",
    "sending": "Enviando",
    "sent": "Enviado",
    "failed": "Não enviado",
    "cancelled": "Cancelado",
    "uncertain": "Envio com resultado não confirmado",
    "active": "Ativo",
    "stopped": "Encerrado",
    "completed": "Concluído",
    "achieved": "Vaga obtida informada pelo usuário",
    "AVAILABLE": "Vaga confirmada",
    "FULL": "Sem vagas",
    "UNKNOWN": "Disponibilidade não confirmada",
    "STALE": "Informação antiga",
    "CANDIDATE": "Aguardando confirmação",
    "already_running": "Atualização já em andamento",
    "config": "Configuração",
    "updated": "Configuração atualizada",
    "worker": "Serviço em segundo plano",
    "dispatch": "Envio de avisos",
    "collect": "Atualização do catálogo",
    "cleanup": "Limpeza de dados vencidos",
    "identity_conflict": "Identidade ambígua na resposta",
    "changed_header": "Formato da fonte mudou",
    "empty_or_invalid_discovery": "Não foi possível descobrir unidades e níveis",
    "database_unavailable": "Banco indisponível",
    "retry_requested": "Reavaliação solicitada",
    "local_credential_reset_once": "Acesso inicial preparado",
    "baseline": "Primeira leitura de referência",
}


def label(value):
    return LABELS.get(str(value), "Estado não reconhecido; consulte o diagnóstico")


def date_br(value, timezone="America/Sao_Paulo"):
    if not value:
        return "Ainda não informado"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(ZoneInfo(timezone)).strftime("%d/%m/%Y às %H:%M:%S")


def input_date(value, timezone="America/Sao_Paulo"):
    if not value:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%dT%H:%M")


def duration(value):
    if value is None:
        return "Ainda não medido"
    if 0 < float(value) < 1:
        return "menos de 1 s"
    seconds = max(0, round(float(value)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return " ".join(
        s
        for s in (
            f"{hours} h" if hours else "",
            f"{minutes} min" if minutes else "",
            f"{seconds} s" if seconds or not (hours or minutes) else "",
        )
        if s
    )
