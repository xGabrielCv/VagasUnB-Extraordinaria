#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${1:-status}" in
  start) docker compose up -d --build admin worker cloudbeaver ;;
  stop) docker compose stop ;;
  status) docker compose ps ;;
  setup)
    test "$(id -u)" != 0 || { echo 'Execute setup como usuário sem root.'; exit 1; }
    uv sync --frozen --python 3.12
    uv run python -m vagasunb.cli init-files
    chmod 700 .private .private/secrets
    chmod 600 .private/secrets/*
    printf 'APP_UID=%s\nAPP_GID=%s\n' "$(id -u)" "$(id -g)" > .env
    mkdir -p .private/exports
    docker compose build
    docker compose up -d postgres
    docker compose run --rm migrate
    echo "Abra o painel e cadastre sua senha no primeiro acesso." ;;
  export)
    docker compose stop admin worker bot
    docker compose run --rm -v "$PWD/.private/exports:/exports" -v "$PWD/.private/secrets/backup_key:/run/backup_key:ro" admin python -m vagasunb.cli export --directory /exports --key /run/backup_key ;;
  restore)
    test -n "${2:-}" || { echo 'Informe o pacote privado'; exit 1; }
    docker compose stop admin worker bot
    docker compose up -d postgres
    docker compose run --rm --no-deps -v "$PWD/.private/exports:/exports:ro" -v "$PWD/.private/secrets/backup_key:/run/backup_key:ro" admin python -m vagasunb.cli restore --package "/exports/$2" --key /run/backup_key ;;
  setup-public)
    test "$(id -u)" != 0 || { echo 'Execute setup como usuário sem root.'; exit 1; }
    command -v docker >/dev/null 2>&1 || { echo 'Docker não encontrado. Instale Docker e Compose v2.'; exit 1; }
    docker compose version >/dev/null 2>&1 || { echo 'Docker Compose v2 não encontrado.'; exit 1; }
    if command -v uv >/dev/null 2>&1; then
      uv sync --frozen --python 3.12
      VAGAS_PRIVATE=.private/public uv run python -m vagasunb.cli init-files
    else
      docker compose -f compose.public.yaml build admin
      docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/.private/public:/out" -e VAGAS_PRIVATE=/out vagasunb-extraordinaria:local python -m vagasunb.cli init-files
    fi
    touch .private/public/secrets/telegram_token
    chmod 700 .private .private/public .private/public/secrets
    chmod 600 .private/public/secrets/*
    test -f .env || printf 'APP_UID=%s\nAPP_GID=%s\nVAGAS_TELEGRAM_ENABLE=0\n' "$(id -u)" "$(id -g)" > .env
    mkdir -p .private/public/exports
    docker compose -f compose.public.yaml build
    docker compose -f compose.public.yaml up -d postgres
    docker compose -f compose.public.yaml run --rm migrate
    echo "Instância pública preparada ($(uname -m)). Use start-public para iniciar ou telegram para conectar o bot." ;;
  start-public)
    services="admin worker"
    test "${2:-}" = "--no-cloudbeaver" || services="$services cloudbeaver"
    if grep -q '^VAGAS_TELEGRAM_ENABLE=1$' .env 2>/dev/null; then
      docker compose -f compose.public.yaml --profile telegram up -d --build $services bot
    else
      docker compose -f compose.public.yaml up -d --build $services
    fi ;;
  stop-public) docker compose -f compose.public.yaml --profile telegram stop ;;
  status-public) docker compose -f compose.public.yaml --profile telegram ps ;;
  logs-public) docker compose -f compose.public.yaml --profile telegram logs --tail=80 "${2:-}" ;;
  telegram)
    printf 'Token do BotFather: ' >&2
    stty -echo 2>/dev/null || true
    IFS= read -r token
    stty echo 2>/dev/null || true
    printf '\n' >&2
    printf '%s' "$token" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]{20,}$' || { echo 'Formato de token inválido.'; exit 1; }
    if command -v curl >/dev/null 2>&1; then
      me=$(curl -fsS --max-time 8 "https://api.telegram.org/bot${token}/getMe" 2>/dev/null || true)
      if printf '%s' "$me" | grep -q '"ok":true'; then
        bot_user=$(printf '%s' "$me" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')
        test -n "$bot_user" && echo "Bot validado no Telegram: https://t.me/$bot_user"
      else
        echo 'Aviso: não foi possível validar getMe agora; o token será salvo mesmo assim.' >&2
      fi
    fi
    mkdir -p .private/public/secrets
    chmod 700 .private .private/public .private/public/secrets
    printf '%s' "$token" > .private/public/secrets/telegram_token
    chmod 600 .private/public/secrets/telegram_token
    unset token
    printf 'APP_UID=%s\nAPP_GID=%s\nVAGAS_TELEGRAM_ENABLE=1\n' "$(id -u)" "$(id -g)" > .env
    docker compose -f compose.public.yaml --profile telegram up -d --build --force-recreate admin worker cloudbeaver bot
    echo "Telegram conectado no modo público (worker + bot ativos)." ;;
  *) echo 'Uso: linux.sh setup|start|stop|status|export|restore|setup-public|start-public [--no-cloudbeaver]|stop-public|status-public|logs-public [svc]|telegram'; exit 1 ;;
esac
