import json
import os
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


@dataclass
class Settings:
    mode: str
    database_url: str = field(repr=False)
    secrets: Path = field(repr=False)
    host: str = "127.0.0.1:8765"
    secure: bool = False

    @property
    def private_dir(self):
        return Path(os.getenv("VAGAS_PRIVATE", ".private")).resolve()

    @property
    def instance(self):
        return hashlib.sha256(str(self.private_dir).encode()).hexdigest()[:20]

    @classmethod
    def load(cls):
        root = Path(os.getenv("VAGAS_PRIVATE", ".private"))
        cfg = (
            json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
            if (root / "config.json").exists()
            else {}
        )
        mode = os.getenv("VAGAS_MODE", cfg.get("mode", "demo"))
        if mode == "telegram":
            mode = "public"  # Transport activation no longer changes the catalog/database mode.
        if mode not in {"demo", "public", "telegram"}:
            raise ValueError("Modo inválido")
        url = os.getenv("DATABASE_URL", cfg.get("database_url", ""))
        if not url and os.getenv("PGHOST"):
            password = Path(os.environ["PGPASSWORD_FILE"]).read_text().strip()
            url = f"postgresql+psycopg://vagas:{quote(password, safe='')}@{os.environ['PGHOST']}/vagas_{mode}"
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("Configure PostgreSQL; SQLite não é suportado")
        db = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if mode == "demo" and not db.endswith("_demo"):
            raise ValueError("Demo exige banco com sufixo _demo")
        return cls(
            mode,
            url,
            Path(os.getenv("VAGAS_SECRETS", str(root / "secrets"))),
            os.getenv("VAGAS_HOST", "127.0.0.1:8765"),
            os.getenv("VAGAS_HTTPS") == "1",
        )
