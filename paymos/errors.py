"""Typed exception hierarchy for the Paymos SDK.

Every read / quote / sign call routes through :mod:`paymos._http`, which turns the
server's error envelope (``{"error": "…"}`` — or the sign flow's
``{"ok":false,"status":"failed","error":"…"}``) into one of these. The base
:class:`PaymosError` carries the server's already client-safe ``message`` and the HTTP
``status`` that produced it; subclasses name a specific, recoverable condition so a
caller can branch on the type instead of string-matching a message.

The class set is fixed by the SDK spec — deliberately no ``NotFound`` (a 404 surfaces
as the base ``PaymosError``). Messages arrive already client-safe from the server;
nothing here re-parses, reformats, or rewrites them further.
"""

from __future__ import annotations


class PaymosError(Exception):
    """Base for every Paymos SDK error.

    ``message`` is the server's client-safe reason; ``status`` is the HTTP status
    code that produced it (``None`` if the error did not originate from a response).
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class AuthError(PaymosError):
    """401 — the API key is missing, malformed, or not recognized."""


class Forbidden(PaymosError):
    """403 — the key is valid but lacks the scope this call requires."""


class InsufficientFunds(PaymosError):
    """400 — the vault's available balance can't cover the amount (plus fees)."""


class QuoteExpired(PaymosError):
    """400 — the referenced quote has expired; fetch a fresh one and retry."""


class SlippageExceeded(PaymosError):
    """400 — the delivered amount fell outside the accepted slippage bound."""


class CrossAssetWithdrawNotAllowed(PaymosError):
    """400 — a withdraw changed the asset; that's a swap and is rejected here."""


class RouteUnavailable(PaymosError):
    """400 — no route could be quoted for this pair right now; retry shortly."""


class RateLimited(PaymosError):
    """429 — too many requests.

    ``retry_after`` is the ``Retry-After`` header as an int when the server sent one,
    else ``None``.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, status)
        self.retry_after = retry_after


class Conflict(PaymosError):
    """409 — idempotency-key conflict (reused with a different request, or a quote
    for the key is still being created)."""
