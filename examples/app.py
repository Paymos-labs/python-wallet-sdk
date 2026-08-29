"""Paymos SDK demo — a small LOCAL web app that points at PROD.

Run it on YOUR machine, open the page, paste a ``vs_live_`` vault secret, and click
to load balances, price a quote (with the full fee breakdown — the headline), and,
gated behind a confirm box, do a real swap. This mirrors the first integration
client's stack (FastAPI). The backend is a thin shim over ``from paymos import Wallet``.

    cd sdk/python
    pip install maturin fastapi uvicorn && maturin develop   # (or install the wheel)
    uvicorn examples.app:app --reload                          # open http://127.0.0.1:8000

Defaults to prod (``https://wallet.paymos.io``) — no env needed. The read + quote
path is live TODAY (a read-scope or full ``vs_live_`` → balances + fee breakdown).

SECURITY — run this LOCALLY. Each request builds a fresh ``Wallet`` from the posted
secret; the secret is NEVER persisted, logged, or echoed back. A FULL secret carries
SPEND POWER inside this process, so never host this app publicly and never paste a
full secret into a remote copy. A read-scope key is identity-only (safe to click
around with).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from paymos import Wallet
from paymos.errors import (
    AuthError,
    Conflict,
    CrossAssetWithdrawNotAllowed,
    Forbidden,
    InsufficientFunds,
    PaymosError,
    QuoteExpired,
    RateLimited,
    RouteUnavailable,
    SlippageExceeded,
)

# Prod by default — the demo needs no environment to run against live prod. Override
# with PAYMOS_BASE_URL to aim at a staging host.
BASE_URL = os.environ.get("PAYMOS_BASE_URL", "https://wallet.paymos.io")

# The clickable page lives next to this file; GET / serves it verbatim (no build step).
_INDEX = Path(__file__).with_name("index.html")

app = FastAPI(title="Paymos SDK demo", docs_url=None, redoc_url=None)


# --- error mapping -----------------------------------------------------------

# A default HTTP status per business-error type, used only when the SDK error did
# not already carry one (``PaymosError.status`` is authoritative when present). We
# never turn a business error into a 500 — the client gets a clean 4xx it can show.
_STATUS_BY_TYPE: dict[type[PaymosError], int] = {
    AuthError: 401,
    Forbidden: 403,
    Conflict: 409,
    RateLimited: 429,
    InsufficientFunds: 400,
    QuoteExpired: 400,
    SlippageExceeded: 400,
    CrossAssetWithdrawNotAllowed: 400,
    RouteUnavailable: 400,
}


def _error_response(exc: PaymosError) -> JSONResponse:
    """Render a ``PaymosError`` (or subclass) as ``{"error", "type"}`` JSON with a
    sensible 4xx. Never a 500: a business error is the client's to display, not a
    server fault. The message is the server's already-rail-free reason."""
    status = exc.status or _STATUS_BY_TYPE.get(type(exc), 400)
    body: dict[str, Any] = {"error": exc.message, "type": type(exc).__name__}
    if isinstance(exc, RateLimited) and exc.retry_after is not None:
        body["retry_after"] = exc.retry_after
    return JSONResponse(body, status_code=status)


@app.exception_handler(PaymosError)
async def _paymos_error_handler(request: Request, exc: PaymosError) -> JSONResponse:
    """Any ``PaymosError`` anywhere in a request — whether from the input helpers
    below or from the SDK call itself — becomes a clean 4xx JSON, never a 500. This
    is the single place that keeps a business error off the 500 path even when it is
    raised before a handler's own ``try`` block (e.g. a missing secret)."""
    return _error_response(exc)


def _to_json(obj: Any) -> Any:
    """SDK dataclasses (and lists/tuples of them) → plain JSON-able structures.

    ``dataclasses.asdict`` recurses through the nested ``Amount`` / ``Receive`` /
    ``Fees`` / ``RouteFee`` models, so the whole ``Quote`` — including the full fee
    breakdown — round-trips to JSON with no field-by-field wiring here."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_json(x) for x in obj]
    return obj


async def _read_json(request: Request) -> dict[str, Any]:
    """Parse the JSON body as an object, or raise a clean 400 (never a 500)."""
    try:
        data = await request.json()
    except Exception:
        raise PaymosError("request body must be JSON")
    if not isinstance(data, dict):
        raise PaymosError("request body must be a JSON object")
    return data


def _wallet_from(body: dict[str, Any]) -> Wallet:
    """Build a per-request :class:`Wallet` from the posted secret.

    Missing/blank secret → clean 400. A malformed ``vs_live_`` makes the constructor
    (``VaultSecret.parse``) raise ``ValueError``; map that to a ``PaymosError`` so a
    bad paste is a clean 4xx, not a 500. The secret lives only for this request — it
    is never stored, logged, or placed in any response."""
    secret = body.get("secret")
    if not isinstance(secret, str) or not secret.strip():
        raise PaymosError("paste your vault secret (vs_live_…)")
    try:
        return Wallet(secret=secret.strip(), base_url=BASE_URL)
    except ValueError:
        # Don't echo the (malformed) secret back — just say the shape is wrong.
        raise PaymosError("that doesn't look like a vault secret (expects vs_live_…)")


# --- routes ------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    """Serve the self-contained clickable page (vanilla JS, no build step)."""
    return FileResponse(_INDEX, media_type="text/html")


@app.post("/api/balances")
async def api_balances(request: Request) -> JSONResponse:
    """Body ``{secret}`` → the vault's per-asset balances (raw amounts + USD).

    Works with a read-scope OR a full ``vs_live_``. Live against prod today. Any
    ``PaymosError`` (bad secret, auth, transport) routes to the global handler → 4xx."""
    body = await _read_json(request)
    wallet = _wallet_from(body)
    try:
        balances = await wallet.balances()
        return JSONResponse(_to_json(balances))
    finally:
        await wallet.aclose()


@app.post("/api/quote")
async def api_quote(request: Request) -> JSONResponse:
    """Body ``{secret, kind:"withdraw"|"swap", …}`` → a DRY (preview) ``Quote``.

    ``kind="withdraw"``: ``asset``, ``amount``, ``to`` (+ optional ``mode``).
    ``kind="swap"``: ``send``, ``receive``, ``amount`` (+ optional ``slippage_bps``).
    Both call the SDK's ``quote_*`` methods, which ALWAYS send ``dry=true`` — a pure
    fee-breakdown preview that moves no money and works for read + full keys alike."""
    body = await _read_json(request)
    kind = (body.get("kind") or "withdraw").strip().lower()
    wallet = _wallet_from(body)
    try:
        if kind == "swap":
            quote = await wallet.quote_swap(
                send=str(body["send"]),
                receive=str(body["receive"]),
                amount=str(body["amount"]),
                slippage_bps=int(body.get("slippage_bps", 50)),
            )
        elif kind == "withdraw":
            quote = await wallet.quote_withdraw(
                asset=str(body["asset"]),
                amount=str(body["amount"]),
                to=str(body["to"]),
                mode=str(body.get("mode", "exact_out")),
            )
        else:
            raise PaymosError(f"unknown quote kind {kind!r} (use 'withdraw' or 'swap')")
        return JSONResponse(_to_json(quote))
    except KeyError as exc:
        # A missing required field for the chosen kind → clean 400, not a 500. Re-raise
        # as PaymosError so the global handler formats it like every other business error.
        raise PaymosError(f"missing field: {exc.args[0]}")
    except ValueError as exc:
        # A malformed human amount (empty/non-numeric/negative/too many fractional
        # digits) makes parse_units raise ValueError; a non-int slippage_bps makes
        # int() raise ValueError. Both are input errors → clean 400, not a 500. The
        # message is about the amount/slippage only — it never carries the secret.
        raise PaymosError(f"invalid input: {exc}") from exc
    finally:
        await wallet.aclose()


@app.post("/api/swap")
async def api_swap(request: Request) -> JSONResponse:
    """Body ``{secret, send, receive, amount, slippage_bps, confirm:true}`` → REAL money.

    WARNING: this moves REAL funds when the secret is a funded FULL key. It is gated
    twice — the UI's confirm checkbox AND this ``confirm===true`` guard (else 400).
    A read-only secret raises ``PaymosError`` (no share to co-sign) → surfaced as a
    clean 4xx. On success returns the created ``Movement``; ``wait()`` polls it to a
    terminal status so the caller sees the settled result."""
    body = await _read_json(request)
    if body.get("confirm") is not True:
        raise PaymosError("confirm must be true to move real funds")
    wallet = _wallet_from(body)
    try:
        movement = await wallet.swap(
            send=str(body["send"]),
            receive=str(body["receive"]),
            amount=str(body["amount"]),
            slippage_bps=int(body.get("slippage_bps", 50)),
        )
        # Poll to a terminal status so the demo shows the settled movement, not a
        # pending stub. Non-terminal within the window still returns what we have.
        try:
            movement = await wallet.wait(movement.id)
        except PaymosError:
            pass
        return JSONResponse(_to_json(movement))
    except KeyError as exc:
        raise PaymosError(f"missing field: {exc.args[0]}")
    except ValueError as exc:
        # Malformed amount (parse_units) or non-int slippage_bps (int()) → clean 400,
        # not a 500. The message is about the amount/slippage, never the secret.
        raise PaymosError(f"invalid input: {exc}") from exc
    finally:
        await wallet.aclose()
