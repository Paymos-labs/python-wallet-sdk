"""Read/quote surface: assets / balances / quote_swap / quote_withdraw.

A `MockTransport` stubs the four `/vault/v1` endpoints with the EXACT snake_case
wire shapes the server emits (`JsonNamingPolicy.SnakeCaseLower`), including
forward-compat keys the SDK must ignore. Every request body the SDK POSTs is
captured so we can assert the human->raw amount conversion, the `slippage_bps`
passthrough, and — the money-safety invariant — that a public quote is always a
`dry=true` preview.
"""

import httpx
import pytest

from paymos._secret import VaultSecret
from paymos._wallet import Wallet
from paymos._types import Asset, Balance, Quote, Amount, Receive, Fees, RouteFee
from paymos.errors import PaymosError


# --- canned wire payloads (snake_case, as the C# DTOs serialize) -------------

# GET /assets — bare array; min_* are always null today and MUST be ignored.
ASSETS = [
    {"asset": "USDC@base", "symbol": "USDC", "chain": "base", "decimals": 6,
     "min_deposit_raw": None, "min_withdraw_raw": None},
    {"asset": "ETH@arb", "symbol": "ETH", "chain": "arb", "decimals": 18,
     "min_deposit_raw": None, "min_withdraw_raw": None},
]

# GET /balances — bare array; usd nullable.
BALANCES = [
    {"asset": "USDC@base", "symbol": "USDC", "chain": "base", "decimals": 6,
     "amount_raw": "12500000", "usd": "12.50"},
    {"asset": "ETH@arb", "symbol": "ETH", "chain": "arb", "decimals": 18,
     "amount_raw": "1000000000000000000", "usd": None},
]

# POST /quote/withdraw — same-asset payout; route is exact (estimate:false).
WITHDRAW_QUOTE = {
    "movement_id": None,
    "mode": "exact_out",
    "send": {"amount": "10000000", "asset": "USDC@base"},
    "debit": {"amount": "10012000", "asset": "USDC@base"},
    "receive": {"amount": "10000000", "min": "10000000", "asset": "USDC@base"},
    "fees": {
        "asset": "USDC@base",
        "platform": "10000",
        "network": "2000",
        "route": {"amount": "0", "estimate": False},
        "total": "12000",
        "usd": {"platform": "0.01", "network": "0.002", "route": "0", "total": "0.012"},
    },
    "expires_at": "2026-07-06T12:00:00+00:00",
}

# POST /quote/swap — cross-asset; route is a rate-derived estimate (estimate:true).
SWAP_QUOTE = {
    "movement_id": None,
    "mode": "exact_in",
    "send": {"amount": "5000000", "asset": "USDC@base"},
    "debit": {"amount": "5012000", "asset": "USDC@base"},
    "receive": {"amount": "1600000000000000", "min": "1580000000000000", "asset": "ETH@arb"},
    "fees": {
        "asset": "USDC@base",
        "platform": "5000",
        "network": "2000",
        "route": {"amount": "3000", "estimate": True},
        "total": "10000",
        "usd": {"platform": "0.005", "network": "0.002", "route": "0.003", "total": "0.01"},
    },
    "expires_at": "2026-07-06T12:05:00+00:00",
}


def _secret(share=None):
    """A vs_live_ secret (read key by default; pass a share for a full key)."""
    return VaultSecret.pack("vk_live_test", 1, share)


class _Capture:
    """Collects every POST body keyed by path so a test can inspect it."""

    def __init__(self):
        self.posts = {}

    def wallet(self, secret_share=None):
        """A Wallet whose Http transport is a MockTransport over the canned server."""
        import json as _json

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.headers["authorization"] == "Bearer vk_live_test"
            path = req.url.path
            if req.method == "POST":
                self.posts[path] = _json.loads(req.content.decode() or "{}")
            table = {
                "/vault/v1/assets": ASSETS,
                "/vault/v1/balances": BALANCES,
                "/vault/v1/quote/withdraw": WITHDRAW_QUOTE,
                "/vault/v1/quote/swap": SWAP_QUOTE,
            }
            return httpx.Response(200, json=table[path])

        w = Wallet(_secret(secret_share), base_url="https://api.test")
        w._http._client = httpx.AsyncClient(
            base_url="https://api.test", transport=httpx.MockTransport(handler)
        )
        return w


# --- assets ------------------------------------------------------------------

async def test_assets_maps_and_ignores_unknown_keys():
    w = _Capture().wallet()
    got = await w.assets()
    assert got[0] == Asset("USDC@base", "USDC", "base", 6)
    assert got[1] == Asset("ETH@arb", "ETH", "arb", 18)
    # min_deposit_raw / min_withdraw_raw are dropped, not modeled.
    assert not hasattr(got[0], "min_deposit_raw")


# --- balances ----------------------------------------------------------------

async def test_balances_maps_amount_raw_and_usd():
    w = _Capture().wallet()
    got = await w.balances()
    assert got[0] == Balance("USDC@base", "USDC", "base", 6, "12500000", "12.50")
    # usd is a nullable string and stays raw when null.
    assert got[1].usd is None
    assert got[1].amount_raw == "1000000000000000000"
    assert isinstance(got[0].amount_raw, str)


# --- quote_withdraw ----------------------------------------------------------

async def test_quote_withdraw_converts_amount_and_is_dry_preview():
    cap = _Capture()
    w = cap.wallet()
    q = await w.quote_withdraw(asset="USDC@base", amount="10", to="0xabc")

    body = cap.posts["/vault/v1/quote/withdraw"]
    # human "10" goes on the wire AS-IS ("10"); the server scales to raw (never a float).
    assert body["amount"] == "10"
    assert isinstance(body["amount"], str)
    assert body["asset"] == "USDC@base"
    assert body["to"] == "0xabc"
    assert body["mode"] == "exact_out"
    # Money-safe: a public quote NEVER creates a movement.
    assert body["dry"] is True

    assert isinstance(q, Quote)
    assert q.movement_id is None
    assert q.mode == "exact_out"
    assert isinstance(q.send, Amount) and q.send.amount == "10000000"
    assert isinstance(q.receive, Receive)
    assert q.receive.min == "10000000"
    # expires_at stays a raw ISO string (not parsed to datetime).
    assert q.expires_at == "2026-07-06T12:00:00+00:00"
    assert isinstance(q.expires_at, str)


async def test_quote_withdraw_fee_breakdown_shapes():
    w = _Capture().wallet()
    q = await w.quote_withdraw(asset="USDC@base", amount="10", to="0xabc")
    assert isinstance(q.fees, Fees)
    # All amount legs are RAW integer strings.
    assert q.fees.platform == "10000" and isinstance(q.fees.platform, str)
    assert q.fees.network == "2000" and isinstance(q.fees.network, str)
    assert q.fees.total == "12000"
    assert q.fees.asset == "USDC@base"
    # route is a RouteFee with a bool estimate.
    assert isinstance(q.fees.route, RouteFee)
    assert q.fees.route.amount == "0"
    assert q.fees.route.estimate is False
    # usd is a nullable dict of nullable strings.
    assert q.fees.usd["platform"] == "0.01"


async def test_quote_withdraw_dry_true_even_for_full_key():
    # A full key (has a share) must ALSO send dry=true on a public quote — a mere
    # fee-check must never reserve/debit the vault.
    cap = _Capture()
    w = cap.wallet(secret_share='{"kp":"x"}')
    assert w._secret.has_share
    await w.quote_withdraw(asset="USDC@base", amount="1", to="0xabc")
    assert cap.posts["/vault/v1/quote/withdraw"]["dry"] is True


# --- quote_swap --------------------------------------------------------------

async def test_quote_swap_sends_slippage_and_converts_send_amount():
    cap = _Capture()
    w = cap.wallet()
    q = await w.quote_swap(send="USDC@base", receive="ETH@arb", amount="5", slippage_bps=125)

    body = cap.posts["/vault/v1/quote/swap"]
    assert body["send"] == "USDC@base"
    assert body["receive"] == "ETH@arb"
    # amount is a HUMAN decimal in the SEND asset; it goes on the wire AS-IS ("5"),
    # and the server scales it to raw using the asset's decimals.
    assert body["amount"] == "5"
    assert body["slippage_bps"] == 125
    assert body["mode"] == "exact_in"  # explicit, as the recorded request carries it
    assert body["dry"] is True

    assert isinstance(q, Quote)
    assert q.mode == "exact_in"
    assert q.receive.asset == "ETH@arb"
    # cross-asset -> route.estimate is True.
    assert q.fees.route.estimate is True


async def test_quote_swap_default_slippage_is_50():
    cap = _Capture()
    w = cap.wallet()
    await w.quote_swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert cap.posts["/vault/v1/quote/swap"]["slippage_bps"] == 50


# --- unknown asset -----------------------------------------------------------

async def test_unknown_asset_raises_paymos_error():
    w = _Capture().wallet()
    with pytest.raises(PaymosError):
        await w.quote_withdraw(asset="DOGE@base", amount="1", to="0xabc")
    with pytest.raises(PaymosError):
        await w.quote_swap(send="DOGE@base", receive="ETH@arb", amount="1")


# --- catalog caching ---------------------------------------------------------

async def test_catalog_is_cached_after_first_load():
    cap = _Capture()
    w = cap.wallet()
    calls = {"n": 0}
    inner = w._http._client
    orig_get = inner.get

    async def counting_get(url, *a, **k):
        if str(url).endswith("/assets"):
            calls["n"] += 1
        return await orig_get(url, *a, **k)

    inner.get = counting_get
    await w.assets()
    # A second decimals lookup must not refetch the catalog.
    await w.quote_withdraw(asset="USDC@base", amount="1", to="0xabc")
    assert calls["n"] == 1
