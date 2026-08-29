"""Async HTTP transport for the Paymos SDK.

Thin wrapper over an ``httpx.AsyncClient`` that (a) attaches ``Authorization: Bearer
<api_key>`` to every request, (b) returns parsed JSON on a 2xx, and (c) maps any other
status onto a typed :mod:`paymos.errors` exception via the server's error envelope.

The server's contract: business errors are ``{"error": "<msg>"}`` and the sign flow
answers ``{"ok":false,"status":"failed","error":"<msg>"}`` — both carry the message
under ``error``. Messages arrive already client-safe, so this layer maps but never
rewrites them, and it never logs or echoes request bodies.

Transport-level failures (connect/read timeout, DNS, reset) are wrapped into a typed
:class:`~paymos.errors.PaymosError` (``status=None``) so a caller on the money path never
sees a raw ``httpx`` exception. The read timeout is generous by default because the
co-sign is a multi-round exchange that can outlast httpx's 5s default.

The client is created lazily; tests override it by assigning ``self._client`` a client
backed by an ``httpx.MockTransport``.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from . import errors

# Generous default: the sign/aggregate round can trigger a relay that takes a while.
_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _message(body: Any, resp: httpx.Response) -> str:
    """Pull the client-safe message out of the response.

    Prefers ``error`` (the real key on both ``ApiError`` and ``SignResultDto``), then a
    defensive ``detail`` fallback (this server never sends it). If the body isn't a JSON
    object with either key, fall back to the raw response text — still client-safe.
    """
    if isinstance(body, Mapping):
        msg = body.get("error") or body.get("detail")
        if isinstance(msg, str) and msg:
            return msg
    text = (resp.text or "").strip()
    return text or f"request failed with status {resp.status_code}"


def _classify_400(message: str) -> type[errors.PaymosError]:
    """Map a 400's message to a type, first-match-wins in this exact order."""
    m = message.lower()
    if "cross-asset" in m:
        return errors.CrossAssetWithdrawNotAllowed
    if "slippage" in m:
        return errors.SlippageExceeded
    if "expired" in m:
        return errors.QuoteExpired
    if any(t in m for t in ("insufficient", "not enough balance", "exceeds the vault", "available balance")):
        return errors.InsufficientFunds
    # The server's route/liquidity rejections come back as transient-sounding phrases
    # ("No liquidity available … try again in a moment", "Failed to get quote … try again
    # in a moment", "route can't be quoted"). Map that family to RouteUnavailable.
    if any(t in m for t in ("route", "can't be quoted", "liquidity", "try again in a moment")):
        return errors.RouteUnavailable
    return errors.PaymosError


def _raise_for(resp: httpx.Response) -> None:
    """Raise the typed error mapped from a non-2xx response. Never returns."""
    try:
        body: Any = resp.json()
    except Exception:
        body = None
    message = _message(body, resp)
    status = resp.status_code

    if status == 401:
        raise errors.AuthError(message, status)
    if status == 403:
        raise errors.Forbidden(message, status)
    if status == 409:
        raise errors.Conflict(message, status)
    if status == 429:
        retry_after = None
        raw = resp.headers.get("Retry-After")
        if raw is not None:
            try:
                retry_after = int(raw)
            except ValueError:
                retry_after = None
        raise errors.RateLimited(message, status, retry_after)
    if status == 400:
        raise _classify_400(message)(message, status)
    # 404 (no NotFound type), every other 4xx, and all 5xx → base error.
    raise errors.PaymosError(message, status)


class Http:
    """Authenticated async transport. One instance per configured client key."""

    def __init__(self, base_url: str, api_key: str, *, timeout: Any = None) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        # Lazily created on first use; tests override before any request.
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._client

    def _auth(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _ok(resp: httpx.Response) -> Any:
        if 200 <= resp.status_code < 300:
            return resp.json()
        _raise_for(resp)  # raises

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            resp = await self.client.get(path, params=params, headers=self._auth())
        except httpx.HTTPError as e:
            raise errors.PaymosError(f"transport error: {e}") from e
        return self._ok(resp)

    async def post(
        self,
        path: str,
        json_body: Any,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            resp = await self.client.post(path, json=json_body, headers=self._auth(headers))
        except httpx.HTTPError as e:
            raise errors.PaymosError(f"transport error: {e}") from e
        return self._ok(resp)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            # Null it so a later call transparently re-opens instead of using a closed client.
            self._client = None
