"""Client-side money-safety guards + secret redaction, proven against LIVE prod.

What each test proves about SDK 0.1.1 talking to https://wallet.paymos.io:

- ``test_read_only_swap_raises_before_any_network`` /
  ``test_read_only_withdraw_raises_before_any_network`` — a share-stripped
  (read-only) ``vs_live_`` secret can NEVER reach the wire through ``swap`` /
  ``withdraw``: the client guard raises ``PaymosError`` mentioning "read-only"
  with ``status=None`` (not mapped from any HTTP response) and a tripwire on the
  transport records zero requests — so a leaked read key can't even create a
  pending movement.

- ``test_swap_echo_wrong_receive_asset_refuses_to_sign`` /
  ``test_swap_echo_inflated_send_amount_refuses_to_sign`` /
  ``test_withdraw_echo_inflated_receive_amount_refuses_to_sign`` — the pre-sign
  echo check refuses to sign a quote that doesn't echo EXACTLY the assets/amount
  the caller approved: a swapped receive asset or a 10x-inflated amount raises
  ``PaymosError`` ("refusing to sign") and ``_cosign`` is never reached, so a
  lying / drifted / compromised server cannot obtain a signature for a movement
  the caller didn't approve. Also pins that the SDK's movement-create step is
  always non-dry WITH an auto-generated Idempotency-Key.

- ``test_swap_slippage_floor_raises_before_cosign`` — against a REAL (non-dry)
  quote created on prod, an absurd ``min_receive`` floor raises
  ``SlippageExceeded`` locally (``status=None`` — the client floor, not a server
  400) and nothing is signed. Money-safe: the one pending movement it creates is
  never co-signed and simply expires.

- ``test_repr_redacts_api_key_and_frost_share`` — ``repr(Wallet)`` and
  ``repr(VaultSecret)`` surface only ``vault_id`` / ``has_share``; the api-key
  body, the FROST share, and the packed secret payload never render, so a stray
  print / log / traceback can't leak a usable credential.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from paymos import InsufficientFunds, PaymosError, RouteUnavailable, SlippageExceeded
from paymos._secret import VaultSecret
from paymos._types import Amount, Fees, Quote, Receive

from _helpers import EVM_ADDRESS, richest_balance, usd_of

# --- local instrumentation ----------------------------------------------------------


def _tripwire(label: str, calls: list[Any]):
    """An async stand-in that records the call and fails hard if ever reached."""

    async def _trip(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        raise AssertionError(f"{label} must never be reached by this test")

    return _trip


def _quote_stub(*, send_asset: str, send_raw: int, receive_asset: str, receive_raw: int) -> Quote:
    """A structurally complete non-dry Quote with attacker-chosen legs. Only the
    fields the echo check reads (send/receive asset + raw amounts) matter here."""
    return Quote(
        movement_id="mv_e2e_tampered",
        mode="exact_in",
        send=Amount(amount=str(send_raw), asset=send_asset),
        debit=Amount(amount=str(send_raw), asset=send_asset),
        receive=Receive(amount=str(receive_raw), min=str(receive_raw), asset=receive_asset),
        fees=Fees(asset=send_asset, platform="0", network="0", route=None, total="0", usd=None),
        expires_at="2099-01-01T00:00:00Z",
        sources=1,
    )


def _intercept_quote(wallet: Any, tampered: Quote) -> list[tuple[str, bool, str | None]]:
    """Replace the wallet's private movement-create step with one that returns the
    tampered quote, recording ``(path, dry, idempotency_key)``. Nothing hits the wire."""
    calls: list[tuple[str, bool, str | None]] = []

    async def _fake_quote(
        path: str, body: dict[str, Any], *, dry: bool, idempotency_key: str | None = None
    ) -> Quote:
        calls.append((path, dry, idempotency_key))
        return tampered

    wallet._quote = _fake_quote
    return calls


# --- (1) read-only guard: raise BEFORE any network ----------------------------------


async def test_read_only_swap_raises_before_any_network(read_only_wallet):
    assert read_only_wallet._secret.has_share is False  # fixture precondition
    wire: list[Any] = []
    read_only_wallet._http.get = _tripwire("network (GET)", wire)
    read_only_wallet._http.post = _tripwire("network (POST)", wire)

    with pytest.raises(PaymosError) as exc:
        await read_only_wallet.swap("USDC@base", "USDT@tron", "0.01")

    msg = str(exc.value)
    assert "read-only" in msg
    assert "swap" in msg
    assert exc.value.status is None  # locally raised, not mapped from an HTTP response
    assert wire == []  # not even the asset catalog was fetched


async def test_read_only_withdraw_raises_before_any_network(read_only_wallet):
    wire: list[Any] = []
    read_only_wallet._http.get = _tripwire("network (GET)", wire)
    read_only_wallet._http.post = _tripwire("network (POST)", wire)

    with pytest.raises(PaymosError) as exc:
        await read_only_wallet.withdraw("USDC@base", "0.01", EVM_ADDRESS)

    msg = str(exc.value)
    assert "read-only" in msg
    assert "withdraw" in msg
    assert exc.value.status is None
    assert wire == []


# --- (2) echo-check guard: a tampered quote is never signed --------------------------


async def test_swap_echo_wrong_receive_asset_refuses_to_sign(wallet):
    assets = await wallet.assets()  # one live catalog GET; cached afterwards
    if len(assets) < 2:
        pytest.skip("catalog too small to pick a cross-asset pair")
    send, recv = assets[0], assets[1]
    raw_one = 10 ** send.decimals  # raw of the human "1" approved below

    # The "server" echoes the approved send leg but flips the receive to a DIFFERENT
    # asset (here: the send asset itself — a same-asset route we never approved).
    tampered = _quote_stub(
        send_asset=send.asset, send_raw=raw_one, receive_asset=send.asset, receive_raw=raw_one
    )
    quote_calls = _intercept_quote(wallet, tampered)
    cosign_calls: list[Any] = []
    wallet._cosign = _tripwire("co-sign", cosign_calls)

    with pytest.raises(PaymosError) as exc:
        await wallet.swap(send.asset, recv.asset, "1")

    msg = str(exc.value)
    assert "asset mismatch" in msg
    assert "refusing to sign" in msg
    assert cosign_calls == []  # nothing was ever committed, let alone signed
    # The guard sits AFTER the (intercepted) create step, which the SDK always sends
    # non-dry with an auto-generated Idempotency-Key.
    assert quote_calls and quote_calls[0][0].endswith("/quote/swap")
    assert quote_calls[0][1] is False and quote_calls[0][2]


async def test_swap_echo_inflated_send_amount_refuses_to_sign(wallet):
    assets = await wallet.assets()
    if len(assets) < 2:
        pytest.skip("catalog too small to pick a cross-asset pair")
    send, recv = assets[0], assets[1]
    raw_one = 10 ** send.decimals

    # Correct assets, but the debited send amount comes back inflated 10x.
    tampered = _quote_stub(
        send_asset=send.asset,
        send_raw=raw_one * 10,
        receive_asset=recv.asset,
        receive_raw=10 ** recv.decimals,
    )
    quote_calls = _intercept_quote(wallet, tampered)
    cosign_calls: list[Any] = []
    wallet._cosign = _tripwire("co-sign", cosign_calls)

    with pytest.raises(PaymosError) as exc:
        await wallet.swap(send.asset, recv.asset, "1")

    msg = str(exc.value)
    assert "amount mismatch" in msg
    assert "refusing to sign" in msg
    assert cosign_calls == []
    assert quote_calls and quote_calls[0][1] is False


async def test_withdraw_echo_inflated_receive_amount_refuses_to_sign(wallet):
    assets = await wallet.assets()
    a = assets[0]
    raw_one = 10 ** a.decimals

    # exact_out: the approved `amount` IS the guaranteed receive. Inflate it 10x.
    # (The destination address never reaches the wire — the create step is
    # intercepted — so a fixed EVM address is fine whatever chain `a` lives on.)
    tampered = _quote_stub(
        send_asset=a.asset, send_raw=raw_one * 10, receive_asset=a.asset, receive_raw=raw_one * 10
    )
    quote_calls = _intercept_quote(wallet, tampered)
    cosign_calls: list[Any] = []
    wallet._cosign = _tripwire("co-sign", cosign_calls)

    with pytest.raises(PaymosError) as exc:
        await wallet.withdraw(a.asset, "1", EVM_ADDRESS)

    msg = str(exc.value)
    assert "amount mismatch" in msg
    assert "refusing to sign" in msg
    assert cosign_calls == []
    assert quote_calls and quote_calls[0][0].endswith("/quote/withdraw")
    assert quote_calls[0][1] is False and quote_calls[0][2]


# --- (3) slippage floor on a REAL prod quote, without signing -------------------------


async def test_swap_slippage_floor_raises_before_cosign(wallet):
    src = await richest_balance(wallet)
    if src is None:
        pytest.skip("test vault holds nothing spendable")
    # A REAL (non-dry) quote debits send + fees; require >= 0.2 units of headroom
    # for the 0.05 we quote. Integer math only: amount_raw >= 0.2 * 10^decimals.
    if int(src.amount_raw) * 5 < 10 ** src.decimals:
        pytest.skip("richest balance below 0.2 units — not enough headroom for a real quote")

    funded_others = sorted(
        (b for b in await wallet.balances() if b.asset != src.asset),
        key=usd_of,
        reverse=True,
    )
    if funded_others:
        recv = funded_others[0].asset  # a lane this vault already holds — known-routable
    else:
        recv = next(a.asset for a in await wallet.assets() if a.asset != src.asset)

    cosign_calls: list[Any] = []
    wallet._cosign = _tripwire("co-sign", cosign_calls)

    try:
        with pytest.raises(SlippageExceeded) as exc:
            # 999999 whole units of the receive asset can never be guaranteed for a
            # 0.05 send — the client floor must trip on the genuine prod quote.
            await wallet.swap(src.asset, recv, "0.05", min_receive="999999")
    except (RouteUnavailable, InsufficientFunds) as e:
        pytest.skip(f"live route/balance hiccup — floor not exercisable right now: {e}")

    assert "below the requested minimum" in str(exc.value)
    assert exc.value.status is None  # the CLIENT floor tripped, not a server-side 400
    assert cosign_calls == []  # the pending movement was left unsigned (it expires)


# --- (4) secret redaction -------------------------------------------------------------


async def test_repr_redacts_api_key_and_frost_share(wallet):
    secret = os.environ["PAYMOS_VAULT_SECRET"]  # fixture already skipped if unset
    parsed = VaultSecret.parse(secret)
    assert parsed.share, "e2e secret must be full-scope (carry the FROST share)"
    api_key_body = parsed.api_key[len("vk_live_") :]
    assert api_key_body, "api key has no body — redaction check would be vacuous"

    for r in (repr(wallet), repr(parsed)):
        assert f"vault_id={parsed.vault_id}" in r
        assert "has_share=True" in r
        assert api_key_body not in r  # the credential body never renders
        assert parsed.share not in r  # the FROST share never renders
        assert secret[len("vs_live_") :] not in r  # nor the packed secret payload
