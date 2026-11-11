import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from sqlalchemy import func, select

from vagasunb.backup import export, pg_environment, restore
from vagasunb.config import Settings
from vagasunb.db import Classroom, Database, Recipient
from vagasunb.services import options, register


async def test_migration_reapply_keeps_data(db, test_url):
    cmd = (
        str(Path(sys.executable).with_name("alembic.exe")) if sys.platform == "win32" else "alembic"
    )
    result = subprocess.run(
        [cmd, "upgrade", "head"], env={**os.environ, "DATABASE_URL": test_url}, capture_output=True
    )
    assert result.returncode == 0
    async with db.transaction() as s:
        assert await s.scalar(select(func.count()).select_from(Classroom)) == 24


@pytest.mark.parametrize("schema", ["head", "0002"])
async def test_encrypted_export_restore_and_retention(
    db, test_url, vault, tmp_path, monkeypatch, schema
):
    # Use the explicitly configured portable PostgreSQL tools, never a reference executable.
    native = Path(".private/native.json")
    if native.exists():
        monkeypatch.setenv(
            "VAGAS_PG_BIN", json.loads(native.read_text(encoding="utf-8-sig"))["bin"]
        )
    async with db.transaction() as s:
        await register(s, vault, 99123456789)
    key = tmp_path / "export.key"
    key.write_bytes(secrets.token_bytes(32))
    cfg = Settings("demo", test_url, tmp_path / "secrets")
    output = tmp_path / "exports"
    from alembic import command
    from alembic.config import Config

    def migrate(connection, direction, revision):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        getattr(command, direction)(config, revision)

    if schema != "head":
        # Construct a legacy fixture only inside the random isolated test database.
        # Production intentionally refuses downgrading the synthetic delivery fence.
        from sqlalchemy import text

        async with db.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE recipients DROP COLUMN origin"))
            await conn.execute(text("DROP INDEX ix_classroom_code_key"))
            await conn.execute(text("DROP INDEX ix_classroom_name_trgm"))
            await conn.execute(text("UPDATE alembic_version SET version_num='0002'"))
    for _ in range(4):
        package = export(cfg, output, key)
    if schema != "head":
        async with db.engine.begin() as conn:
            await conn.run_sync(migrate, "upgrade", "head")
    assert len(list(output.glob("*.enc"))) == 3
    assert b"99123456789" not in package.read_bytes()
    assert b"PGDMP" not in package.read_bytes()
    env = pg_environment(test_url)
    name = "vagas_restore_" + uuid.uuid4().hex[:12] + "_demo"
    kwargs = dict(
        host=env["PGHOST"],
        port=env["PGPORT"],
        user=env["PGUSER"],
        password=env["PGPASSWORD"],
        dbname="postgres",
        autocommit=True,
    )
    with psycopg.connect(**kwargs) as conn:
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
    target_url = test_url.rsplit("/", 1)[0] + "/" + name
    target = Database(target_url)
    try:
        await restore(target, Settings("demo", target_url, tmp_path / "secrets"), package, key)
        async with target.transaction() as s:
            ops = await options(s)
            assert ops["quarantine"] and ops["collect_paused"] and ops["dispatch_paused"]
            assert await s.scalar(select(func.count()).select_from(Classroom)) == 24
            r = await s.scalar(select(Recipient))
            assert vault.decrypt(r.cipher) == "99123456789"
            assert r.origin == "demo"
            from sqlalchemy import text

            assert await s.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
        with pytest.raises(ValueError, match="vazio"):
            await restore(target, cfg, package, key)
    finally:
        await target.close()
        with psycopg.connect(**kwargs) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    psycopg.sql.Identifier(name)
                )
            )


def test_compose_isolation_and_build_allowlist():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    assert not compose["services"]["postgres"].get("ports")
    assert compose["services"]["admin"]["ports"] == ["127.0.0.1:8765:8765"]
    assert compose["services"]["bot"]["profiles"] == ["telegram-future"]
    assert (
        compose["services"]["admin"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert Path(".dockerignore").read_text().startswith("**\n")
    for service in compose["services"].values():
        assert "docker.sock" not in json.dumps(service)
