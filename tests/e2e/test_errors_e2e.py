"""Error taxonomy, proven against LIVE prod — every rejection maps to the right type.

What each test proves about the production Vault API + SDK 0.1.1 mapping:

- ``test_bogus_api_key_is_auth_error_401`` — a structurally valid ``vs_live_`` secret
  carrying an unknown ``vk_live_`` key parses fine client-side, and the SERVER rejects
  the first read with 401 -> :class:`AuthError` ("invalid or missing API key").
- ``test_cross_asset_withdraw_maps_to_typed_error`` — a withdraw quote whose ``receive``
  differs from ``asset`` is a policy rejection: 400 "cross-asset withdraw…" ->
  :class:`CrossAssetWithdrawNotAllowed` (a distinct type, not a bare 400).
- ``test_same_asset_swap_is_a_plain_400`` — a swap quote with send == receive is 400
  "source and destination assets must differ" and maps to the BASE :class:`PaymosError`
  (no subclass claims that message).
- ``test_unknown_asset_is_rejected_locally`` — an asset id missing from the live catalog
  never reaches the quote endpoint: the SDK raises :class:`PaymosError` itself
  (``status is None`` — not an HTTP response).
- ``test_non_decimal_amount_is_rejected_locally`` / ``test_too_many_decimals_is_rejected_locally``
  — malformed amounts (negative, junk, more fractional digits than the live asset's
  decimals) are refused by the SDK's client-side validation BEFORE any quote POST
  (``status is None`` again).
- ``test_unknown_movement_is_a_plain_404`` — an unknown movement id is 404
  "movement not found" and surfaces as the BASE :class:`PaymosError` with
  ``status == 404`` (the taxonomy deliberately has no ``NotFound`` subclass).

Money safety: everything here is a read, a ``dry=true`` quote preview, or a local
rejection that never even hits the network. Nothing creates or signs a movement.
"""

from __future__ import annotations

import os

import pytest

from paymos import AuthError, CrossAssetWithdrawNotAllowed, PaymosError, Wallet
from paymos._secret import VaultSecret

from _helpers import (
    ERR_CROSS_ASSET_WITHDRAW,
    ERR_MOVEMENT_NOT_FOUND,
    ERR_NO_AUTH,
    ERR_SAME_ASSET_SWAP,
    EVM_ADDRESS,
    richest_balance,
)

# Same resolution as conftest: the bogus-key probe must hit the same live target the
# rest of the suite runs against.
BASE_URL = os.environ.get("PAYMOS_BASE_URL", "https://wallet.paymos.io")

# Verified catalog members (contract characterization 2026-07-07). Both live on EVM
# chains so the recipient-address shape can never preempt the cross-asset rejection.
ASSET_A = "USDC@base"
ASSET_B = "USDT@bsc"


# --- 401 -> AuthError ---------------------------------------------------------------


async def test_bogus_api_key_is_auth_error_401(wallet):
    # `wallet` is taken only as the suite's gate: without PAYMOS_VAULT_SECRET every e2e
    # test skips, and this probe must not hit prod from a credential-less CI either.
    bogus = VaultSecret.pack("vk_live_deadbeefbogus", 1, None)
    async with Wallet(bogus, base_url=BASE_URL) as w:
        with pytest.raises(AuthError) as exc:
            await w.balances()
    assert exc.value.status == 401
    assert ERR_NO_AUTH in str(exc.value)


# --- 400 "cross-asset withdraw…" -> CrossAssetWithdrawNotAllowed ---------------------


async def test_cross_asset_withdraw_maps_to_typed_error(wallet):
    # The public quote_withdraw() can't even express a differing `receive`, so drive the
    # raw quote body through the SDK's own (dry -> money-safe) plumbing.
    body = {
        "asset": ASSET_A,
        "amount": "0.1",
        "to": EVM_ADDRESS,
        "receive": ASSET_B,
        "mode": "exact_out",
    }
    with pytest.raises(CrossAssetWithdrawNotAllowed) as exc:
        await wallet._quote("/vault/v1/quote/withdraw", body, dry=True)
    assert exc.value.status == 400
    assert ERR_CROSS_ASSET_WITHDRAW in str(exc.value).lower()


# --- 400 same-asset swap -> base PaymosError ------------------------------------------


async def test_same_asset_swap_is_a_plain_400(wallet):
    held = await richest_balance(wallet)
    if held is None:
        pytest.skip("test vault holds nothing spendable")
    # Keep the rejection server-side: a 0-decimal asset would fail "0.1" locally.
    amount = "0.1" if held.decimals >= 1 else "1"
    with pytest.raises(PaymosError) as exc:
        await wallet.quote_swap(held.asset, held.asset, amount)
    # Exactly the base type: no subclass claims this message in the 400 classifier.
    assert type(exc.value) is PaymosError
    assert exc.value.status == 400
    assert ERR_SAME_ASSET_SWAP in str(exc.value).lower()


# --- local (pre-network) rejections: status is None -----------------------------------


async def test_unknown_asset_is_rejected_locally(wallet):
    # quote_swap validates the SEND asset against the live catalog before any quote
    # POST; an id the catalog doesn't know raises in the SDK, not on the server.
    with pytest.raises(PaymosError) as exc:
        await wallet.quote_swap("FOO@bar", ASSET_A, "1")
    assert exc.value.status is None  # no HTTP response produced this error
    assert "FOO@bar" in str(exc.value)


@pytest.mark.parametrize("bad_amount", ["-1", "abc"], ids=["negative", "junk"])
async def test_non_decimal_amount_is_rejected_locally(wallet, bad_amount):
    asset = (await wallet.assets())[0].asset  # any real catalog asset
    with pytest.raises(PaymosError) as exc:
        await wallet.quote_withdraw(asset, bad_amount, EVM_ADDRESS)
    assert exc.value.status is None  # SDK-side parse_units rejection, no quote POST


async def test_too_many_decimals_is_rejected_locally(wallet):
    a = (await wallet.assets())[0]
    overflow = "0." + "1" * (a.decimals + 1)  # one fractional digit too many, per LIVE decimals
    with pytest.raises(PaymosError) as exc:
        await wallet.quote_withdraw(a.asset, overflow, EVM_ADDRESS)
    assert exc.value.status is None


# --- 404 -> base PaymosError (deliberately no NotFound subclass) -----------------------


async def test_unknown_movement_is_a_plain_404(wallet):
    with pytest.raises(PaymosError) as exc:
        await wallet.movement("mov_does_not_exist")
    assert type(exc.value) is PaymosError
    assert exc.value.status == 404
    assert ERR_MOVEMENT_NOT_FOUND in str(exc.value).lower()
