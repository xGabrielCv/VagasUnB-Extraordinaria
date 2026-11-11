import base64
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Vault:
    def __init__(self, directory: Path):
        try:
            data = json.loads((directory / "keys.json").read_text())
            self.active = data["active"]
            self.keys = {k: base64.b64decode(v, validate=True) for k, v in data["keys"].items()}
            self.index_key = base64.b64decode(data["index"], validate=True)
            assert len(self.index_key) == 32 and all(len(v) == 32 for v in self.keys.values())
            assert self.active in self.keys
        except Exception:
            raise ValueError("Chaves ausentes ou inválidas; operações bloqueadas") from None

    @staticmethod
    def create(directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "keys.json"
        if not path.exists():
            data = {
                "active": "k1",
                "keys": {"k1": base64.b64encode(secrets.token_bytes(32)).decode()},
                "index": base64.b64encode(secrets.token_bytes(32)).decode(),
            }
            with path.open("x") as f:
                json.dump(data, f)
        return Vault(directory)

    def encrypt(self, value: str) -> str:
        nonce = secrets.token_bytes(12)
        aad = f"v1:{self.active}".encode()
        body = AESGCM(self.keys[self.active]).encrypt(nonce, value.encode(), aad)
        return f"v1:{self.active}:" + base64.b64encode(nonce + body).decode()

    def decrypt(self, value: str) -> str:
        try:
            version, kid, body = value.split(":", 2)
            assert version == "v1"
            raw = base64.b64decode(body, validate=True)
            return AESGCM(self.keys[kid]).decrypt(raw[:12], raw[12:], f"v1:{kid}".encode()).decode()
        except Exception:
            raise ValueError("Falha de integridade ou chave incompatível") from None

    def index(self, chat_id: int | str) -> str:
        normalized = str(int(chat_id))
        return hmac.new(self.index_key, normalized.encode(), hashlib.sha256).hexdigest()
