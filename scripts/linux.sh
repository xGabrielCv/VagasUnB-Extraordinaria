#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${1:-status}" in
  start) docker compose up -d --build admin worker ;;
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
  *) echo 'Uso: linux.sh setup|start|stop|status|export|restore pacote.enc'; exit 1 ;;
esac
