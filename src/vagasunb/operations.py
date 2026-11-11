from datetime import datetime, timedelta, timezone
from pathlib import Path


def maintain_logs(directory, at=None, max_bytes=100 * 1024 * 1024):
    """Bound only this instance's named runtime logs, never arbitrary host logs."""
    directory = Path(directory).resolve()
    at = at or datetime.now(timezone.utc)
    files = [
        directory / name
        for name in (
            "admin.log",
            "admin-error.log",
            "worker.log",
            "worker-error.log",
            "postgres.log",
        )
    ]
    files = [p for p in files if p.is_file() and not p.is_symlink()]
    for path in files:
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < at - timedelta(days=7):
            with path.open("wb"):
                pass
    total = sum(p.stat().st_size for p in files)
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        if total <= max_bytes:
            break
        size = path.stat().st_size
        with path.open("wb"):
            pass
        total -= size
    if total > max_bytes:
        raise RuntimeError("Limite de logs não pôde ser cumprido")
