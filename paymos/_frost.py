"""FROST client-role signing — the SDK's half of a 2-of-2 co-sign.

Three thin wrappers over the native core (``paymos._core.mpc_call``): a JSON op in,
a JSON op out. The SDK holds the client key package (id 1); the Vault API server
holds the co-signer (id 2). Neither side alone can produce a signature — each
movement is signed by the client committing + signing here and the server
countersigning + aggregating on its side.

Only two ops run client-side in production:

- :func:`commit` — round 1: fresh per-session nonces + the public commitment the
  server needs to build the signing package. Call once PER movement source (each
  source is an independent signing session with its own nonces).
- :func:`sign` — round 2: the client's signature share for one signing package.

The blobs (``key_package`` / ``commitments`` / ``nonces`` / ``signing_package`` /
``signature_share``) are OPAQUE JSON objects — passed through verbatim, never
inspected or reconstructed. ``build_signing_package`` and ``aggregate`` are
SERVER-side ops; the SDK never calls them.

The key package NEVER leaves this module onto the wire — it only ever feeds
``_core.mpc_call`` locally.
"""

from __future__ import annotations

import json
from typing import Any

from . import _core
from .errors import PaymosError


def _call(req: dict[str, Any]) -> dict[str, Any]:
    """Run one native MPC op. Raises :class:`PaymosError` on an ``ok:false`` result
    (or unparseable output); returns the result dict on success."""
    raw = _core.mpc_call(json.dumps(req))
    try:
        r = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise PaymosError(f"mpc call returned non-JSON: {e}") from e
    if not isinstance(r, dict) or not r.get("ok"):
        msg = r.get("error") if isinstance(r, dict) else None
        raise PaymosError(msg or "mpc call failed")
    return r


def commit(key_package: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Round 1 for one signing session. Returns ``(commitments, nonces)`` — send the
    ``commitments`` to the server's ``sign/begin``; keep the ``nonces`` private for
    the matching :func:`sign`. Generate fresh per source."""
    r = _call({"op": "commit", "key_package": key_package})
    return r["commitments"], r["nonces"]


def sign(
    signing_package: dict[str, Any],
    nonces: dict[str, Any],
    key_package: dict[str, Any],
) -> dict[str, Any]:
    """Round 2: the client's signature share for ``signing_package`` (from the
    server) using the ``nonces`` from the matching :func:`commit` and the client
    ``key_package``. The server aggregates this share with its own."""
    r = _call({
        "op": "sign",
        "signing_package": signing_package,
        "nonces": nonces,
        "key_package": key_package,
    })
    return r["signature_share"]
