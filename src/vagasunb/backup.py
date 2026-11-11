import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text

from vagasunb.services import change_options


def pg_environment(url):
    parsed = urlsplit(url.replace("postgresql+psycopg:", "postgresql:"))
    return {
        **os.environ,
        "PGHOST": parsed.hostname or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": parsed.path.lstrip("/"),
        "PGCONNECT_TIMEOUT": "10",
    }


def program(name):
    directory = os.getenv("VAGAS_PG_BIN")
    return str(Path(directory) / name) if directory else name


def export(settings, destination, key_file, max_bytes=100 * 1024 * 1024):
    directory = Path(destination).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = Path(key_file).read_bytes()
    if len(key) != 32:
        raise ValueError("Chave de exportação deve conter 32 bytes")
    with tempfile.TemporaryDirectory(prefix="vagas-export-", dir=directory) as temp:
        dump = Path(temp) / "database.dump"
        result = subprocess.run(
            [program("pg_dump"), "--format=custom", "--no-owner", "--no-acl", "--file", str(dump)],
            env=pg_environment(settings.database_url),
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError("pg_dump falhou; saída privada não registrada")
        data = dump.read_bytes()
        manifest = json.dumps(
            {
                "format": 1,
                "schema": "0002",
                "mode": settings.mode,
                "created": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ).encode()
        raw = len(manifest).to_bytes(4, "big") + manifest + data
        nonce = secrets.token_bytes(12)
        package = b"VAGAS1" + nonce + AESGCM(key).encrypt(nonce, raw, b"VAGAS1")
        if len(package) > max_bytes:
            raise ValueError("Exportação excede limite de bytes")
        path = directory / (
            "vagas-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            + "-"
            + secrets.token_hex(3)
            + ".enc"
        )
        path.write_bytes(package)
    files = sorted(directory.glob("vagas-*.enc"), key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for index, item in enumerate(files):
        size = item.stat().st_size
        expired = datetime.fromtimestamp(item.stat().st_mtime, UTC) < datetime.now(UTC) - timedelta(
            days=7
        )
        if index >= 3 or expired or total + size > max_bytes:
            item.unlink()
        else:
            total += size
    if not path.exists():
        raise RuntimeError("Política de retenção não pôde preservar exportação")
    return path


async def restore(db, settings, package, key_file):
    # Only restore into a new empty database. Never overwrite an existing instance.
    async with db.transaction() as s:
        tables = await s.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        )
        if tables:
            raise ValueError("Restauração exige banco novo vazio, sem migrações prévias")
    key = Path(key_file).read_bytes()
    data = Path(package).read_bytes()
    if not data.startswith(b"VAGAS1"):
        raise ValueError("Formato inválido")
    raw = AESGCM(key).decrypt(data[6:18], data[18:], b"VAGAS1")
    length = int.from_bytes(raw[:4], "big")
    manifest = json.loads(raw[4 : 4 + length])
    dump = raw[4 + length :]
    if (
        manifest["format"] != 1
        or manifest["schema"] != "0002"
        or manifest["mode"] != settings.mode
        or hashlib.sha256(dump).hexdigest() != manifest["sha256"]
    ):
        raise ValueError("Manifesto ou checksum incompatível")
    with tempfile.TemporaryDirectory(prefix="vagas-restore-") as temp:
        path = Path(temp) / "database.dump"
        path.write_bytes(dump)
        result = subprocess.run(
            [
                program("pg_restore"),
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-acl",
                "--dbname",
                pg_environment(settings.database_url)["PGDATABASE"],
                str(path),
            ],
            env=pg_environment(settings.database_url),
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError("pg_restore falhou; banco destino deve permanecer isolado")
    async with db.transaction() as s:
        await change_options(
            s, {"quarantine": True, "collect_paused": True, "dispatch_paused": True}
        )
    # Commit quarantine BEFORE any key validation, so wrong keys cannot roll it back.
    async with db.transaction() as s:
        from sqlalchemy import delete

        from vagasunb.crypto import Vault
        from vagasunb.db import AdminSession, Callback, Outbox, Recipient

        vault = Vault(settings.secrets)
        for recipient in (await s.scalars(select(Recipient))).all():
            vault.decrypt(recipient.cipher)
        await s.execute(delete(AdminSession))
        await s.execute(delete(Callback))
        for item in (
            await s.scalars(
                select(Outbox).where(Outbox.status.in_(["pending", "retry", "sending"]))
            )
        ).all():
            item.status = "cancelled"
        # No token or key is part of the encrypted dump package.
