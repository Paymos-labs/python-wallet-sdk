"""REAL-MONEY co-sign e2e — the ONE place in the suite that actually signs.

Every test here depends on the ``real_money`` gate fixture (skipped unless
``PAYMOS_E2E_REAL_MONEY=1``). The module performs EXACTLY ONE 2-of-2 FROST co-sign
against live prod: a tiny in-vault cross-stable swap. It never withdraws, so no
funds ever leave the vault; a full run costs a few cents of fees.

Prod behavior proven here (wallet.paymos.io, contract characterized 2026-07-07):

- ``test_one_real_swap_cosigns_completes_and_moves_balances`` — ``Wallet.swap``
  end-to-end on prod: non-dry quote → client echo check → FROST round 1
  (``sign/begin``) → round 2 (``sign/aggregate``) → relay. The returned
  ``Movement`` has a ``mov_`` id and is already past pending (``processing`` or
  ``completed``); ``Wallet.wait`` then polls it to the terminal white-label status
  ``completed``. Money moved the right way: the SOURCE stable balance decreased
  and the DESTINATION stable balance increased (raw BigInt compares — no floats),
  the vault was debited no more than the approved ``send + fees.total``, and the
  destination was credited at least the movement's guaranteed ``receive.min``.
  Also proves the human→raw amount rule: the request carried a HUMAN decimal and
  the movement echoes it scaled by the asset's decimals exactly once.
- ``test_completed_cosign_swap_reads_back_in_detail_and_history`` — the completed
  movement reads back consistently without any further signing: the detail
  (``GET /movements/{id}``) carries the fee breakdown denominated in the send
  asset (``total >= platform + network``; the route leg is never negative), and
  the first history page lists the same movement with ``fees`` omitted.

Deliberately NOT asserted: ``Movement.type`` — the live server stores a vault-API
swap via the withdraw money primitive and passes the internal row type through,
so its value is not part of the verified white-label contract.

Sizing: source and destination are both picked from the route-proven stable trio
(the characterization swap was USDT@bsc → USDC@base), the amount is capped at
0.05 of a ~$1 stable and at ~20% of the source balance, and a dry pre-flight
quote guards the single co-sign against a transiently unroutable pair or an
underfunded vault (skip, don't fail). The fixture wallet's default 60s HTTP
timeout was sufficient for the proven prod run of this exact flow.
"""

from __future__ import annotations

import asyncio
from decimal import ROUND_DOWN, Decimal

import pytest

from paymos import RouteUnavailable
from paymos._types import Balance, Movement

from _helpers import balances_by_asset, richest_balance

# Cross-stable assets proven routable on prod (the characterization swap completed
# USDT@bsc -> USDC@base). BOTH legs of the single co-sign are picked from this trio
# so the one real signature is never burned on an exotic route.
_ROUTE_STABLES = ("USDC@base", "USDT@bsc", "USDT0@arb")

# Never swap more than 0.05 of a ~$1 stable, never more than ~20% of the source
# balance, and skip below a 0.10 source so the debit (send + fees) has headroom.
_MAX_AMOUNT = Decimal("0.05")
_BALANCE_FRACTION = Decimal("0.20")
_MIN_SOURCE_BALANCE = Decimal("0.10")

# Handoff from the swap test to the read-back test: the completed Movement of the
# module's single co-sign. Written only after prod reports "completed"; the
# read-back test skips when absent (swap skipped/failed) instead of double-failing.
_outcome: dict[str, Movement] = {}


def _human(balance: Balance) -> Decimal:
    """A Balance's raw amount in exact human units (Decimal — never a float)."""
    return Decimal(int(balance.amount_raw)) / (Decimal(10) ** balance.decimals)


def _pick_pair(before: dict[str, Balance], catalog_ids: set[str]):
    """``((source Balance, dest asset id), None)`` among the route-proven stables,
    or ``(None, reason)`` when the vault can't fund a tiny swap right now.

    The source is the LARGEST trio balance by human units (they are all ~$1
    stables, so human units rank like USD without depending on the price feed).
    """
    candidates = [
        b
        for asset, b in before.items()
        if asset in _ROUTE_STABLES and asset in catalog_ids and _human(b) >= _MIN_SOURCE_BALANCE
    ]
    if not candidates:
        return None, (
            f"no route-proven stable balance >= {_MIN_SOURCE_BALANCE} among "
            f"{_ROUTE_STABLES} — top up the test vault"
        )
    src = max(candidates, key=_human)
    dst = next((a for a in _ROUTE_STABLES if a != src.asset and a in catalog_ids), None)
    if dst is None:  # catalog drift: nothing left to swap into
        return None, "the asset catalog no longer carries a second route-proven stable"
    return (src, dst), None


def _tiny_amount(src: Balance) -> Decimal:
    """A few cents at most: min(0.05, 20% of the source balance floored to 2dp)."""
    fraction = (_human(src) * _BALANCE_FRACTION).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return min(_MAX_AMOUNT, fraction)


async def _wait_for_deltas(
    wallet, src_asset: str, dst_asset: str, src_before: int, dst_before: int
) -> tuple[int, int]:
    """Post-settlement raw balances of both legs (absent-from-response = 0).

    A live balance read can lag the movement's terminal status by a beat, so this
    re-polls briefly (~20s) until both deltas are visible, then returns the LAST
    observation either way — the caller hard-asserts on it, so nothing is masked.
    """
    src_now, dst_now = src_before, dst_before
    for attempt in range(8):
        if attempt:
            await asyncio.sleep(2.5)
        now = await balances_by_asset(wallet)
        src_now = int(now[src_asset].amount_raw) if src_asset in now else 0
        dst_now = int(now[dst_asset].amount_raw) if dst_asset in now else 0
        if src_now < src_before and dst_now > dst_before:
            break
    return src_now, dst_now


async def test_one_real_swap_cosigns_completes_and_moves_balances(real_money, wallet):
    """THE single real co-sign of the suite: a tiny cross-stable in-vault swap on
    live prod signs (2-of-2 FROST), relays, reaches ``completed``, and moves both
    balances correctly — debited at most the approved ``send + fees.total``,
    credited at least the guaranteed ``receive.min``."""
    if await richest_balance(wallet, min_raw=1) is None:
        pytest.skip("the test vault holds nothing — no source to co-sign a swap from")

    catalog_ids = {a.asset for a in await wallet.assets()}
    before = await balances_by_asset(wallet)
    pair, reason = _pick_pair(before, catalog_ids)
    if pair is None:
        pytest.skip(reason)
    src, dst = pair

    amount = _tiny_amount(src)
    amount_str = f"{amount:f}"  # HUMAN decimal on the wire — the server scales it
    expected_send_raw = int(amount.scaleb(src.decimals))  # exact: 2dp into a >=6dp asset

    # Dry pre-flight (money-safe): don't burn the one co-sign on a transiently
    # unroutable pair, and prove the full debit fits the balance first.
    try:
        preview = await wallet.quote_swap(src.asset, dst, amount_str, slippage_bps=100)
    except RouteUnavailable as exc:
        pytest.skip(f"no live route {src.asset}->{dst} right now: {exc}")
    if int(preview.debit.amount) > int(src.amount_raw):
        pytest.skip(
            f"vault can't cover the quoted debit {preview.debit.amount} raw of "
            f"{src.asset} (balance {src.amount_raw}) — top up the test vault"
        )

    # === the one real co-sign ===
    mv = await wallet.swap(
        send=src.asset, receive=dst, amount=amount_str, slippage_bps=100, min_receive=None
    )
    assert isinstance(mv, Movement)
    assert mv.id.startswith("mov_"), f"unexpected movement id shape: {mv.id!r}"
    # swap() only returns after sign/aggregate succeeded, so the movement is already
    # in the relay pipeline (processing) or settled inline (completed) — never pending.
    assert mv.status in {"processing", "completed"}, f"{mv.id} came back {mv.status!r}"
    assert (mv.send.asset, mv.receive.asset) == (src.asset, dst)
    # The human amount was scaled to raw exactly once, server-side.
    assert int(mv.send.amount) == expected_send_raw

    final = await wallet.wait(mv.id, timeout=180, poll=3)
    assert final.id == mv.id
    assert final.status == "completed", f"{mv.id} ended {final.status!r}, not completed"
    _outcome["movement"] = final  # unlock the read-back test even if a delta assert trips

    # wait() returns the detail projection: the fee breakdown must be present and
    # denominated in the send asset.
    assert final.fees is not None
    assert final.fees.asset == src.asset
    guaranteed_out = int(final.receive.min)
    assert guaranteed_out > 0

    src_before = int(src.amount_raw)
    dst_before = int(before[dst].amount_raw) if dst in before else 0
    src_after, dst_after = await _wait_for_deltas(wallet, src.asset, dst, src_before, dst_before)

    # The headline claim: money moved the right way (raw BigInt compares).
    assert src_after < src_before, f"{mv.id}: source {src.asset} balance did not decrease"
    assert dst_after > dst_before, f"{mv.id}: destination {dst} balance did not increase"

    # Debit discipline: at least the principal left the vault, and never more than
    # the approved send + fees.total (the route leg only shrinks the output side;
    # platform + network are the only debits on top of the principal).
    debited = src_before - src_after
    assert debited >= expected_send_raw, (
        f"{mv.id}: vault debited {debited} raw, below the principal {expected_send_raw}"
    )
    assert debited <= expected_send_raw + int(final.fees.total), (
        f"{mv.id}: vault debited {debited} raw, above the approved "
        f"{expected_send_raw} + fees.total {final.fees.total}"
    )
    # Delivery floor: credited at least the movement's guaranteed receive.min.
    assert dst_after - dst_before >= guaranteed_out, (
        f"{mv.id}: credited {dst_after - dst_before} raw of {dst}, below the "
        f"guaranteed minimum {guaranteed_out}"
    )


async def test_completed_cosign_swap_reads_back_in_detail_and_history(real_money, wallet):
    """The completed co-signed movement reads back consistently (no further
    signing): the detail endpoint carries the fee breakdown in the send asset with
    ``total >= platform + network``, and the first history page lists the same
    movement as ``completed`` with the fee legs omitted."""
    final = _outcome.get("movement")
    if final is None:
        pytest.skip("the co-sign swap did not complete in this session — nothing to read back")

    detail = await wallet.movement(final.id)
    assert detail.id == final.id
    assert detail.status == "completed"
    assert detail.fees is not None, "movement detail must carry the fee breakdown"
    assert detail.fees.asset == final.send.asset
    assert int(detail.fees.platform) >= 0
    assert int(detail.fees.network) >= 0
    # total = platform + network + route, and the route leg is never negative.
    assert int(detail.fees.total) >= int(detail.fees.platform) + int(detail.fees.network)

    items, _cursor = await wallet.movements(limit=50)
    row = next((m for m in items if m.id == final.id), None)
    assert row is not None, "fresh co-signed movement missing from the first history page"
    assert row.status == "completed"
    assert row.fees is None, "history rows must omit fee legs (detail endpoint only)"
    assert (row.send.asset, row.receive.asset) == (final.send.asset, final.receive.asset)
