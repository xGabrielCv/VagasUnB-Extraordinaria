import argparse
from vagasunb import run_async
import getpass
import json
import logging
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from sqlalchemy import delete, select

from vagasunb.config import Settings
from vagasunb.crypto import Vault
from vagasunb.db import Admin, Base, Database, Recipient, Setting


def bootstrap_files(root):
    root = Path(root)
    existing = any(
        (root / p).exists() for p in ("config.json", "native.json", "secrets/postgres_password")
    )
    if existing and not (root / "secrets" / "keys.json").exists():
        raise ValueError("Configuração existente sem chaves: restaure as chaves originais")
    vault = Vault.create(root / "secrets")
    for name, value in [
        ("postgres_password", secrets.token_urlsafe(32).encode()),
        ("backup_key", secrets.token_bytes(32)),
    ]:
        path = root / "secrets" / name
        if not path.exists():
            with path.open("xb") as f:
                f.write(value)
    return vault


async def command(args):
    cfg = Settings.load()
    db = Database(cfg.database_url)
    try:
        if args.action == "admin":
            password = getpass.getpass("Nova senha administrativa (mínimo 12 caracteres): ")
            confirm = getpass.getpass("Repita a senha: ")
            if len(password) < 12 or password != confirm:
                raise ValueError("Senha curta ou confirmação diferente")
            async with db.transaction() as s:
                await s.merge(Admin(id=1, password=PasswordHasher().hash(password)))
                from vagasunb.db import AdminSession

                await s.execute(delete(AdminSession))
            print("Senha configurada. Sessões anteriores revogadas.")
        elif args.action == "seed":
            from vagasunb.demo import seed

            async with db.transaction() as s:
                await seed(s, cfg.mode)
            print("Catálogo demo preparado.")
        elif args.action == "reset-demo":
            if cfg.mode != "demo" or not args.confirm:
                raise ValueError("Reset exige modo demo e --confirm; produção recusada")
            async with db.transaction() as s:
                # Preserve administrator and instance keys; remove only application demo data.
                from vagasunb.db import Classroom

                if await s.scalar(
                    select(Classroom.id).where(~Classroom.period.startswith("DEMO")).limit(1)
                ):
                    raise ValueError("Reset recusado: o catálogo contém dados públicos")
                keep = {"admins", "admin_sessions", "settings"}
                for table in reversed(Base.metadata.sorted_tables):
                    if table.name not in keep:
                        await s.execute(delete(table))
                await s.execute(delete(Setting).where(Setting.key.not_in(["instance"])))
                from vagasunb.demo import seed

                await seed(s, "demo")
            print("Dados demo recriados; chaves e senha preservadas.")
        elif args.action == "export":
            from vagasunb.backup import export

            print(export(cfg, args.directory, args.key))
        elif args.action == "restore":
            from vagasunb.backup import restore

            await restore(db, cfg, args.package, args.key)
            print("Restaurado em quarentena. Concilie exclusões antes de liberar pela CLI.")
        elif args.action == "release-quarantine":
            if not args.confirm:
                raise ValueError("Exige --confirm após conciliar exclusões de backups antigos")
            from vagasunb.services import change_options

            async with db.transaction() as s:
                await change_options(s, {"quarantine": False})
            print("Quarentena liberada. Coleta e despacho continuam pausados até ação explícita.")
        elif args.action == "rotate-keys":
            # New keyring must include old encryption keys + a new active key and new HMAC key.
            old, new = Vault(cfg.secrets), Vault(Path(args.new_keys))
            async with db.transaction() as s:
                from vagasunb.db import lock

                await lock(s, "maintenance", "keys")
                await s.execute(
                    __import__("sqlalchemy").text("LOCK TABLE recipients IN ACCESS EXCLUSIVE MODE")
                )
                rows = (await s.scalars(select(Recipient))).all()
                for row in rows:
                    chat = old.decrypt(row.cipher)
                    row.cipher, row.lookup = new.encrypt(chat), new.index(chat)
                await s.merge(
                    Setting(
                        key="instance",
                        value={
                            "mode": cfg.mode,
                            "key_check": new.index("0"),
                            "cipher_check": new.encrypt("vagasunb-key-check"),
                        },
                    )
                )
            print(
                "Banco rotacionado. Com serviços parados, ative o novo diretório de chaves antes de reiniciar."
            )
        elif args.action == "diagnose":
            async with db.transaction() as s:
                from vagasunb.services import options, verify_instance

                await verify_instance(s, Vault(cfg.secrets), cfg.mode)

                ops = await options(s)
                print(
                    json.dumps(
                        {
                            "mode": cfg.mode,
                            "database": "PostgreSQL",
                            "quarantine": ops["quarantine"],
                            "keys": "valid",
                            "telegram": "disabled"
                            if cfg.mode != "telegram"
                            else "requires explicit enable",
                        }
                    )
                )
    finally:
        await db.close()


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    for name in ("httpx", "httpcore", "telegram", "sqlalchemy", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    parser = argparse.ArgumentParser(description="Operação VagasUnB (Extraordinária)")
    parser.add_argument(
        "action",
        choices=[
            "init-files",
            "admin",
            "seed",
            "reset-demo",
            "worker",
            "bot",
            "diagnose",
            "export",
            "restore",
            "release-quarantine",
            "rotate-keys",
        ],
    )
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument(
        "--directory", default=str(Path(os.getenv("VAGAS_PRIVATE", ".private")) / "exports")
    )
    parser.add_argument(
        "--key",
        default=str(
            Path(
                os.getenv(
                    "VAGAS_SECRETS", str(Path(os.getenv("VAGAS_PRIVATE", ".private")) / "secrets")
                )
            )
            / "backup_key"
        ),
    )
    parser.add_argument("--package")
    parser.add_argument("--new-keys")
    args = parser.parse_args()
    try:
        if args.action == "init-files":
            bootstrap_files(os.getenv("VAGAS_PRIVATE", ".private"))
            print("Segredos preparados; arquivos existentes preservados.")
        elif args.action == "worker":
            from vagasunb.worker import run

            run_async(run())
        elif args.action == "bot":
            from vagasunb.telegram import run_bot

            run_bot(Settings.load())
        else:
            run_async(command(args))
    except (Exception, KeyboardInterrupt) as exc:
        from vagasunb.operations import error_code

        print(
            f"Falha operacional: {error_code(exc)}. Verifique status/logs e pré-requisitos; dados e chaves não foram redefinidos."
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
