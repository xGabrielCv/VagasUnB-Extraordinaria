import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import delete

from vagasunb.backup import pg_environment
from vagasunb.config import Settings
from vagasunb.crypto import Vault
from vagasunb.db import Base, Database
from vagasunb.demo import seed


@pytest.fixture(scope="session")
def test_url():
    # Only CREATE/DROP a random database owned by this test session; never truncate demo.
    cfg = Settings.load()
    name = "vagas_test_" + uuid.uuid4().hex[:12] + "_demo"
    env = pg_environment(cfg.database_url)
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
    url = cfg.database_url.rsplit("/", 1)[0] + "/" + name
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("alembic.exe"))
            if sys.platform == "win32"
            else "alembic",
            "upgrade",
            "head",
        ],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
    )
    assert result.returncode == 0, (
        "Isolated PostgreSQL migration failed (private output suppressed)"
    )
    yield url
    with psycopg.connect(**kwargs) as conn:
        conn.execute(
            psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(psycopg.sql.Identifier(name))
        )


@pytest_asyncio.fixture
async def db(test_url):
    db = Database(test_url)
    async with db.transaction() as s:
        for table in reversed(Base.metadata.sorted_tables):
            await s.execute(delete(table))
        await seed(s, "demo")
    yield db
    await db.close()


@pytest.fixture
def vault(tmp_path):
    return Vault.create(tmp_path / "secrets")
