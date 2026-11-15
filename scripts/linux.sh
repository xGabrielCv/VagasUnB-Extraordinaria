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
    uv sync --frozen --python 3.12
    VAGAS_PRIVATE=.private/public uv run python -m vagasunb.cli init-files
    touch .private/public/secrets/telegram_token
    chmod 700 .private .private/public .private/public/secrets
    chmod 600 .private/public/secrets/*
    printf 'APP_UID=%s\nAPP_GID=%s\nVAGAS_TELEGRAM_ENABLE=0\n' "$(id -u)" "$(id -g)" > .env
    mkdir -p .private/public/exports
    docker compose -f compose.public.yaml build
    docker compose -f compose.public.yaml up -d postgres
    docker compose -f compose.public.yaml run --rm migrate
    echo "Instância pública preparada. Use start-public para iniciar ou telegram para conectar o bot." ;;
  start-public)
    if grep -q '^VAGAS_TELEGRAM_ENABLE=1$' .env 2>/dev/null; then
      docker compose -f compose.public.yaml --profile telegram up -d --build admin worker cloudbeaver bot
    else
      docker compose -f compose.public.yaml up -d --build admin worker cloudbeaver
    fi ;;
  stop-public) docker compose -f compose.public.yaml --profile telegram stop ;;
  status-public) docker compose -f compose.public.yaml --profile telegram ps ;;
  telegram)
    printf 'Token do BotFather: ' >&2
    stty -echo 2>/dev/null || true
    IFS= read -r token
    stty echo 2>/dev/null || true
    printf '\n' >&2
    printf '%s' "$token" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]{20,}$' || { echo 'Formato de token inválido.'; exit 1; }
    mkdir -p .private/public/secrets
    chmod 700 .private .private/public .private/public/secrets
    printf '%s' "$token" > .private/public/secrets/telegram_token
    chmod 600 .private/public/secrets/telegram_token
    unset token
    printf 'APP_UID=%s\nAPP_GID=%s\nVAGAS_TELEGRAM_ENABLE=1\n' "$(id -u)" "$(id -g)" > .env
    docker compose -f compose.public.yaml --profile telegram up -d --build --force-recreate admin worker bot
    echo "Telegram conectado no modo público (worker + bot ativos)." ;;
  *) echo 'Uso: linux.sh setup|start|stop|status|export|restore|setup-public|start-public|stop-public|status-public|telegram'; exit 1 ;;
esac
