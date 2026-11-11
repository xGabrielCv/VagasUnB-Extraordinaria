from datetime import datetime, timedelta, timezone
from pathlib import Path


def error_code(error):
    from sqlalchemy.exc import OperationalError, IntegrityError

    if isinstance(error, OperationalError):
        return "database_unavailable"
    if isinstance(error, IntegrityError):
        return "data_conflict"
    if getattr(error, "code", None) in {
        "rate_limit",
        "access_restricted",
        "changed_form",
        "session_expired",
        "http_unavailable",
        "network_unavailable",
        "identity_conflict",
        "circuit_open",
    }:
        return error.code
    if isinstance(error, ValueError):
        if any(word in str(error).lower() for word in ("chave", "integridade")):
            return "key_missing_or_incompatible"
        return "configuration_or_key_invalid"
    if isinstance(error, FileNotFoundError):
        return "runtime_file_missing"
    return "internal_failure"


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
