"""Typed models for the Paymos read/quote surface.

Every model is built from the server's snake_case JSON via a ``from_dict`` that
reads ONLY the fields it models and ignores the rest — forward-compat, so a new
server field (or the assets' always-null ``min_deposit_raw`` / ``min_withdraw_raw``)
never breaks a caller on an older SDK.

Money discipline: every amount in these read models is a RAW integer **string** — the
SDK never turns an amount into a float. (Request amounts you pass to ``Wallet`` methods
are HUMAN decimal strings; the server scales them to raw — see :mod:`paymos._wallet`.)
Timestamps (``expires_at`` and ``created_at`` / ``completed_at``) stay raw ISO-8601
strings; the SDK does not parse them to ``datetime``.

The headline model is :class:`Fees` — the platform / network / route breakdown a
caller previews before signing. ``platform`` / ``network`` / ``total`` are raw
amount strings, ``route`` is a :class:`RouteFee` (or ``None`` when no route leg
applies), and ``route.estimate`` is a bool: ``False`` for an exact same-asset
spread, ``True`` for a rate-derived cross-asset one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _s(v: Any) -> str | None:
    """A JSON value as ``str`` (or ``None``). Amounts are strings on the wire; this
    only coerces defensively and never touches ``None``."""
    return v if v is None else str(v)


@dataclass(frozen=True)
class Asset:
    """A catalog entry: the white-label ``SYMBOL@chain`` id, its symbol, chain, and
    on-chain decimals (used to convert human amounts to raw)."""

    asset: str
    symbol: str
    chain: str
    decimals: int

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Asset":
        return cls(
            asset=d["asset"],
            symbol=d["symbol"],
            chain=d["chain"],
            decimals=int(d["decimals"]),
        )


@dataclass(frozen=True)
class Balance:
    """A single asset's balance in the caller's vault. ``amount_raw`` is a raw
    integer string; ``usd`` is a nullable string (``None`` when unpriced)."""

    asset: str
    symbol: str
    chain: str
    decimals: int
    amount_raw: str
    usd: str | None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Balance":
        return cls(
            asset=d["asset"],
            symbol=d["symbol"],
            chain=d["chain"],
            decimals=int(d["decimals"]),
            amount_raw=str(d["amount_raw"]),
            usd=_s(d.get("usd")),
        )


@dataclass(frozen=True)
class RouteFee:
    """The route (spread) leg of a fee breakdown. ``amount`` is a raw integer
    string; ``estimate`` is ``True`` when the amount is rate-derived (cross-asset),
    ``False`` when it's an exact same-asset spread."""

    amount: str
    estimate: bool

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RouteFee":
        return cls(amount=str(d["amount"]), estimate=bool(d["estimate"]))


@dataclass(frozen=True)
class Fees:
    """The platform / network / route fee breakdown — the SDK's headline preview.

    ``asset`` is the ``SYMBOL@chain`` the fees are denominated in (the send asset).
    ``platform`` / ``network`` / ``total`` are raw integer strings. ``route`` is a
    :class:`RouteFee` or ``None`` (no route leg). ``usd`` is a nullable dict of
    nullable strings (keys: ``platform`` / ``network`` / ``route`` / ``total``);
    kept as a plain dict since every value is an independently-nullable string."""

    asset: str
    platform: str
    network: str
    route: RouteFee | None
    total: str
    usd: dict[str, str | None] | None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Fees":
        route = d.get("route")
        usd = d.get("usd")
        return cls(
            asset=d["asset"],
            platform=str(d["platform"]),
            network=str(d["network"]),
            route=RouteFee.from_dict(route) if route is not None else None,
            total=str(d["total"]),
            usd={k: _s(v) for k, v in usd.items()} if usd is not None else None,
        )


@dataclass(frozen=True)
class Amount:
    """A raw amount paired with the asset it's denominated in."""

    amount: str
    asset: str

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Amount":
        return cls(amount=str(d["amount"]), asset=d["asset"])


@dataclass(frozen=True)
class Receive:
    """The receive leg of a quote: expected ``amount`` and guaranteed ``min`` (both
    raw integer strings) of ``asset``."""

    amount: str
    min: str
    asset: str

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Receive":
        return cls(amount=str(d["amount"]), min=str(d["min"]), asset=d["asset"])


@dataclass(frozen=True)
class Quote:
    """A priced quote (preview or a created movement).

    ``movement_id`` is ``None`` for a dry preview (nothing persisted). ``send`` is
    the amount fed to the route; ``debit`` is the full vault debit (send + fees);
    ``receive`` is the expected / guaranteed out. ``fees`` is the breakdown.
    ``expires_at`` is a raw ISO-8601 string.

    ``sources`` is the number of vault source rows a real (non-dry) quote will need
    ONE FROST commitment for at ``sign/begin`` — the exact co-sign session count.
    The server omits it on a dry preview (nothing to sign) → ``None``; a non-dry
    quote always carries ``sources >= 1``."""

    movement_id: str | None
    mode: str
    send: Amount
    debit: Amount
    receive: Receive
    fees: Fees
    expires_at: str
    sources: int | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Quote":
        sources = d.get("sources")
        return cls(
            movement_id=_s(d.get("movement_id")),
            mode=d["mode"],
            send=Amount.from_dict(d["send"]),
            debit=Amount.from_dict(d["debit"]),
            receive=Receive.from_dict(d["receive"]),
            fees=Fees.from_dict(d["fees"]),
            expires_at=str(d["expires_at"]),
            sources=int(sources) if sources is not None else None,
        )


@dataclass(frozen=True)
class Movement:
    """A movement (a swap or withdraw) — returned by ``swap`` / ``withdraw`` / ``movement`` / ``movements`` / ``wait``.

    Summary rows carry no ``fees`` (``None``); the single-movement detail carries
    the full breakdown. ``dest_chain_tx_hash`` / ``dest_chain_explorer_url`` are the
    destination-chain settlement only. Timestamps stay raw ISO strings."""

    id: str
    type: str
    status: str
    send: Amount
    receive: Receive
    fees: Fees | None
    dest_chain_tx_hash: str | None
    dest_chain_explorer_url: str | None
    created_at: str
    completed_at: str | None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Movement":
        fees = d.get("fees")
        return cls(
            id=d["id"],
            type=d["type"],
            status=d["status"],
            send=Amount.from_dict(d["send"]),
            receive=Receive.from_dict(d["receive"]),
            fees=Fees.from_dict(fees) if fees is not None else None,
            dest_chain_tx_hash=_s(d.get("dest_chain_tx_hash")),
            dest_chain_explorer_url=_s(d.get("dest_chain_explorer_url")),
            created_at=str(d["created_at"]),
            completed_at=_s(d.get("completed_at")),
        )
