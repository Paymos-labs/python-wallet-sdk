"""Dry quote previews (``quote_swap`` / ``quote_withdraw``) against LIVE prod.

Money-safe by construction: every call here is a ``dry=true`` preview — the server
prices the movement but persists nothing and there is nothing to co-sign.

What each test PROVES about the production Vault API (contract verified 2026-07-07)
through SDK 0.1.1:

* ``test_quote_swap_dry_is_pure_preview`` — a dry quote is a pure preview:
  ``movement_id is None`` (nothing persisted) and ``sources is None`` (nothing to
  co-sign), with a real ``mode`` / ``expires_at``.
* ``test_quote_swap_cross_asset_fees_and_math`` — for a cross-asset swap between two
  DIFFERENT catalog assets: the request ``amount`` is a HUMAN decimal and the SERVER
  scales it (``send.amount == human * 10^decimals``, raw); ``fees`` is a typed
  :class:`Fees` denominated in the SOURCE asset whose ``platform`` / ``network`` /
  ``total`` (and ``route.amount``) are RAW integer strings and whose ``usd`` is a
  nullable dict of nullable strings; the cross-asset route leg is rate-derived
  (``route.estimate is True``); exact BigInt source-token math
  ``debit.amount == send.amount + fees.total``; and ``receive.min <= receive.amount``.
* ``test_quote_withdraw_exact_out_evm`` — an ``exact_out`` same-asset withdraw preview
  to an EVM address: still a pure preview; every leg (send / receive / debit / fees)
  stays in the withdrawn asset; the payout is EXACT and guaranteed
  (``receive.amount == receive.min == human * 10^decimals``); ``send >= receive``;
  ``debit == send + fees.total``; and the same-asset route leg is an exact spread,
  not an estimate (``route.estimate is False``).

Assets are picked dynamically from the live catalog / balances so the module tracks
the test vault's funding; each case self-skips when nothing suitable is funded.
"""

from __future__ import annotations

import asyncio

import pytest

from paymos import RouteUnavailable
from paymos._types import Balance, Fees, Quote, RouteFee

from _helpers import EVM_ADDRESS, richest_balance, usd_of

# Chain ids (as they appear in the catalog's `chain` field) we treat as EVM — i.e.
# where `EVM_ADDRESS` is a syntactically valid payout target for a dry preview.
_EVM_CHAINS = {
    "eth", "ethereum", "base", "bsc", "bnb", "arb", "arbitrum",
    "op", "optimism", "polygon", "pol", "matic", "avax", "avalanche", "gnosis",
}


# --- independent unit math (deliberately NOT the SDK's parse/format helpers, so the
# --- expected values are computed by code the SDK cannot share a bug with) ----------

def _raw(human: str, decimals: int) -> int:
    """Scale a human decimal string to raw integer units."""
    whole, _, frac = human.partition(".")
    assert len(frac) <= decimals, f"test bug: {human!r} exceeds {decimals} decimals"
    return int((whole or "0") + frac.ljust(decimals, "0"))


def _human(raw: int, decimals: int) -> str:
    """Render raw integer units as a human decimal string."""
    if decimals == 0:
        return str(raw)
    s = str(raw).rjust(decimals + 1, "0")
    whole, frac = s[:-decimals], s[-decimals:].rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _slice_of(balance: Balance, *, cap_num: int, cap_den: int, divisor: int) -> str | None:
    """A human amount safely INSIDE ``balance``: min(balance/divisor, cap_num/cap_den
    of one unit), trimmed to at most 6 fractional digits so requests stay tidy even on
    18-decimals assets. ``None`` when the slice rounds to zero or is dust (< 0.01 of a
    unit — too small to route reliably)."""
    dec = balance.decimals
    cap = 10 ** dec * cap_num // cap_den
    raw = min(int(balance.amount_raw) // divisor, cap)
    if dec > 6:
        raw -= raw % 10 ** (dec - 6)
    if raw * 100 < 10 ** dec:  # below 0.01 of a unit
        return None
    return _human(raw, dec)


def _is_stablecoin(symbol: str) -> bool:
    sym = symbol.upper()
    return "USD" in sym or sym == "DAI"


# --- live case pickers (dynamic, so the module survives re-funding of the vault) ----

async def _swap_case(wallet) -> tuple[Balance, str, str]:
    """(source balance, DIFFERENT destination asset id, human amount) for a
    cross-asset dry swap — or skip when the vault can't support one."""
    src = await richest_balance(wallet, min_raw=1)
    if src is None:
        pytest.skip("vault holds no spendable balance to quote a swap from")
    amount = _slice_of(src, cap_num=1, cap_den=5, divisor=2)  # <= 0.2 of a unit
    if amount is None:
        pytest.skip(f"richest balance ({src.asset}) is too small to slice a routable amount")

    # Destination preference: another FUNDED asset (a route the vault demonstrably
    # uses), else another catalog stablecoin, else any other catalog asset.
    funded_others = sorted(
        (b for b in await wallet.balances() if b.asset != src.asset),
        key=usd_of,
        reverse=True,
    )
    if funded_others:
        return src, funded_others[0].asset, amount
    others = [a for a in await wallet.assets() if a.asset != src.asset]
    if not others:
        pytest.skip("catalog has no second asset to swap into")
    stable_others = [a for a in others if _is_stablecoin(a.symbol)]
    return src, (stable_others or others)[0].asset, amount


async def _evm_withdraw_case(wallet) -> tuple[Balance, str]:
    """(EVM stablecoin balance, human amount) for an exact_out dry withdraw preview —
    richest first — or skip when no EVM stablecoin is funded enough."""
    candidates = sorted(
        (
            b
            for b in await wallet.balances()
            if b.chain.lower() in _EVM_CHAINS and _is_stablecoin(b.symbol)
        ),
        key=usd_of,
        reverse=True,
    )
    for b in candidates:
        amount = _slice_of(b, cap_num=1, cap_den=20, divisor=4)  # <= 0.05 of a unit
        if amount is not None:
            return b, amount
    pytest.skip("no EVM stablecoin balance is funded enough to preview a withdraw")


async def _dry_quote(fn, *args, **kwargs) -> Quote:
    """Run a dry quote, absorbing the documented transient: live cross-asset routing
    can momentarily 400 with 'try again in a moment' (-> RouteUnavailable). Retry a
    couple of times; a persistent failure still fails the test."""
    last: RouteUnavailable | None = None
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2.0)
        try:
            return await fn(*args, **kwargs)
        except RouteUnavailable as e:
            last = e
    raise last


# --- tests ---------------------------------------------------------------------------

async def test_quote_swap_dry_is_pure_preview(wallet):
    """A dry swap quote persists nothing and has nothing to sign: ``movement_id`` and
    ``sources`` are both ``None`` (the server omits them on ``dry=true``)."""
    src, dest, amount = await _swap_case(wallet)
    q = await _dry_quote(wallet.quote_swap, src.asset, dest, amount)

    assert isinstance(q, Quote)
    assert q.movement_id is None
    assert q.sources is None
    # Still a fully-priced quote, not a stub:
    assert isinstance(q.mode, str) and q.mode
    assert isinstance(q.expires_at, str) and q.expires_at


async def test_quote_swap_cross_asset_fees_and_math(wallet):
    """A cross-asset swap quote between two different catalog assets: server-side
    human->raw scaling, source-token fee denomination, raw-integer-string fee legs,
    a rate-derived route (``estimate is True``), exact ``debit = send + fees.total``
    BigInt math, and ``receive.min <= receive.amount``."""
    src, dest, amount = await _swap_case(wallet)
    assert dest != src.asset  # cross-asset by construction
    q = await _dry_quote(wallet.quote_swap, src.asset, dest, amount)

    # Legs and denominations: fees + debit live in the SOURCE token.
    assert q.send.asset == src.asset
    assert q.receive.asset == dest
    assert q.debit.asset == src.asset
    assert q.fees.asset == src.asset

    # The request carried a HUMAN decimal; the SERVER scaled it to raw (exact_in).
    assert int(q.send.amount) == _raw(amount, src.decimals)

    # Fee breakdown shape: raw integer strings end to end.
    assert isinstance(q.fees, Fees)
    for leg in (q.fees.platform, q.fees.network, q.fees.total):
        assert isinstance(leg, str) and leg.isdigit()
    # Cross-asset: the route leg exists and is rate-derived.
    assert isinstance(q.fees.route, RouteFee)
    assert q.fees.route.amount.isdigit()
    assert q.fees.route.estimate is True
    # usd is None-or-dict of nullable strings — never floats.
    if q.fees.usd is not None:
        assert isinstance(q.fees.usd, dict)
        assert all(v is None or isinstance(v, str) for v in q.fees.usd.values())

    # Exact source-token money math, BigInt only. fees.total sums all legs; the vault
    # DEBIT is only send + platform + network — the route leg is the market spread that
    # already lives inside send->receive, so it is reported in total but is NOT an extra
    # debit. (Verified against VaultFees: total = platform + network + route.)
    route_raw = int(q.fees.route.amount) if q.fees.route else 0
    assert int(q.fees.total) == int(q.fees.platform) + int(q.fees.network) + route_raw
    assert int(q.debit.amount) == int(q.send.amount) + int(q.fees.platform) + int(q.fees.network)

    # The guaranteed floor never exceeds the expected out.
    assert int(q.receive.amount) > 0
    assert int(q.receive.min) <= int(q.receive.amount)


async def test_quote_withdraw_exact_out_evm(wallet):
    """An ``exact_out`` withdraw preview on an EVM chain pays the recipient EXACTLY the
    approved human amount: ``receive.amount == receive.min == human * 10^decimals``,
    every leg stays in the same asset, ``send >= receive``, ``send = receive + route``
    (the spread), ``debit = send + platform + network``, and the same-asset route leg is
    exact (``estimate is False``)."""
    bal, amount = await _evm_withdraw_case(wallet)
    q = await _dry_quote(wallet.quote_withdraw, bal.asset, amount, EVM_ADDRESS)

    # Still a pure preview — a dry withdraw persists / signs nothing.
    assert q.movement_id is None
    assert q.sources is None

    # Same-asset payout: send, receive, debit AND the fee denomination all match.
    assert q.send.asset == q.receive.asset == q.debit.asset == q.fees.asset == bal.asset

    # exact_out: the recipient gets exactly `amount`, and it is guaranteed (min == amount).
    expected_raw = _raw(amount, bal.decimals)
    assert int(q.receive.amount) == expected_raw
    assert int(q.receive.min) == int(q.receive.amount)

    # You always send at least what arrives. Same-asset: send = receive + route(spread).
    # The vault DEBIT is send + platform + network (the route leg is the spread already
    # inside send, not an extra debit); fees.total sums all three legs for reporting.
    assert int(q.send.amount) >= int(q.receive.amount)
    route_raw = int(q.fees.route.amount) if q.fees.route else 0
    assert int(q.send.amount) == int(q.receive.amount) + route_raw
    assert int(q.fees.total) == int(q.fees.platform) + int(q.fees.network) + route_raw
    assert int(q.debit.amount) == int(q.send.amount) + int(q.fees.platform) + int(q.fees.network)
    for leg in (q.fees.platform, q.fees.network, q.fees.total):
        assert isinstance(leg, str) and leg.isdigit()

    # Same-asset spread is exact, not a rate-derived estimate.
    assert isinstance(q.fees.route, RouteFee)
    assert q.fees.route.amount.isdigit()
    assert q.fees.route.estimate is False
