from __future__ import annotations
import base64, json
from dataclasses import dataclass

_PREFIX = "vs_live_"

def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def _mask(key: str) -> str:
    # Show only the non-secret prefix so logs/tracebacks can't carry a usable credential.
    return (key[:8] + "…") if key else "…"   # "vk_live_…"

@dataclass(frozen=True, repr=False)
class VaultSecret:
    api_key: str
    vault_id: int
    share: str | None

    def __repr__(self) -> str:
        # NEVER render the FROST share, and mask the api key — a stray print/log/
        # traceback of this object must not carry a usable credential or the share.
        return f"VaultSecret(api_key={_mask(self.api_key)!r}, vault_id={self.vault_id}, has_share={self.has_share})"

    @property
    def has_share(self) -> bool:
        return self.share is not None

    @staticmethod
    def pack(api_key: str, vault_id: int, share: str | None) -> str:
        payload = {"k": api_key, "v": int(vault_id), "s": share}
        return _PREFIX + _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())

    @staticmethod
    def parse(secret: str) -> "VaultSecret":
        if not isinstance(secret, str) or not secret.startswith(_PREFIX):
            raise ValueError("not a vs_live_ vault secret")
        try:
            o = json.loads(_b64url_decode(secret[len(_PREFIX):]))
            api_key = o["k"]; vault_id = int(o["v"]); share = o.get("s")
        except Exception as e:
            raise ValueError(f"malformed vault secret: {e}") from e
        if not isinstance(api_key, str) or not api_key.startswith("vk_live_"):
            raise ValueError("vault secret carries no valid api key")
        return VaultSecret(api_key=api_key, vault_id=vault_id, share=share if share is None else str(share))
