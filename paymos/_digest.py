"""Signed-message digest — the exact 32 bytes the vault's signature is computed over.

A byte-exact port of the server's signing digest, pinned by a cross-language golden
vector (the server's test ⇄ ``tests/test_digest.py``). The SDK uses it to VERIFY —
before a single FROST commitment — that the server disclosed the true message it is
asking the vault to sign (audit #5: never blind-sign).

Digest = ``sha256(`` a fixed-layout encoding of ``)``:
  ``u32   tag``               (domain separation; fixed, must match the server)
  ``string message``          (u32-LE length prefix + utf-8)
  ``bytes32 nonce``           (raw, fixed 32)
  ``string recipient``        (u32-LE length prefix + utf-8)
  ``option<string> callback`` (0x00 = none, else 0x01 + string)

The message + recipient are hashed as their EXACT utf-8 bytes — re-formatting the
payload would change the digest, which is exactly what the binding check catches.
"""

from __future__ import annotations

import hashlib
import struct

# Fixed domain-separation tag mixed into every signed-message digest. The value is part
# of the wire contract — it MUST byte-match the server, or the binding check never agrees.
_MSG_TAG = 2147484061


def _len_prefixed_utf8(s: str) -> bytes:
    """A little-endian u32 byte-length prefix, then the utf-8 bytes."""
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def payload_digest(message: str, nonce: bytes, recipient: str, callback_url: str | None = None) -> bytes:
    """The 32-byte sha256 digest the vault's ed25519 signature is over.

    ``nonce`` must be exactly 32 bytes (raw). Matches the server's digest so the client
    and server never disagree on what a given ``(message, nonce, recipient)`` hashes to."""
    if len(nonce) != 32:
        raise ValueError(f"nonce must be 32 bytes, got {len(nonce)}")
    w = struct.pack("<I", _MSG_TAG)
    w += _len_prefixed_utf8(message)
    w += nonce
    w += _len_prefixed_utf8(recipient)
    w += b"\x00" if callback_url is None else (b"\x01" + _len_prefixed_utf8(callback_url))
    return hashlib.sha256(w).digest()


def payload_digest_hex(message: str, nonce: bytes, recipient: str, callback_url: str | None = None) -> str:
    """:func:`payload_digest` as a lowercase hex string — the form the FROST signing
    package embeds as its ``message`` field, so the two can be compared directly."""
    return payload_digest(message, nonce, recipient, callback_url).hex()
