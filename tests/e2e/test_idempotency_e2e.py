"""Idempotency-journal e2e — LIVE prod, money-safe (creates UNSIGNED pending movements only).

Proves, against the live Vault API, that the ``Idempotency-Key`` journal on
``POST /vault/v1/quote/swap`` (``dry=false``) behaves as characterized on 2026-07-07:

- ``test_journal_fresh_replay_then_conflict``:
  (1) FRESH   — the first use of a key journals a new movement: ``movement_id`` is a
      non-empty ``mov_…`` string and the response echoes the approved pair with the
      request's HUMAN amount scaled to raw source-asset units by the server;
  (2) REPLAY  — the SAME key + byte-identical body returns the SAME ``movement_id``
      (a journal hit — a client retry after a dropped 200 can never double-create);
  (3) CONFLICT — the SAME key with a DIFFERENT body (amount "0.2" vs "0.1") is refused
      with 409 → :class:`paymos.Conflict` ("idempotency key reused…"), and the original
      journal entry survives the failed attempt: a subsequent replay still returns it,
      and the movement row itself is fetchable with a lowercase status that is never
      ``completed`` (nothing was signed, so it can never complete).
- ``test_distinct_keys_journal_distinct_movements``: the journal is indexed by the KEY,
  not by request-body equality — the same body under two fresh keys creates two
  distinct movements (so the replay above was a journal hit, not body deduplication).

Money safety: nothing in this module ever signs. We deliberately drive the SDK
transport's private ``wallet._quote(..., dry=False, idempotency_key=…)`` (the journal
write) instead of the public ``swap()`` / ``withdraw()`` (which would FROST co-sign).
Every movement these tests create is an unsigned ``pending`` row that cannot execute
and simply expires on its own.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from paymos import Conflict, InsufficientFunds, RouteUnavailable

from _helpers import ERR_IDEMPOTENCY_CONFLICT, richest_balance, usd_of

SWAP_PATH = "/vault/v1/quote/swap"

# Journal amounts (HUMAN decimals — the server scales them). Kept tiny: each fresh key
# journals a pending movement whose debit may be reserved until it expires.
AMOUNT_FRESH = "0.2"
AMOUNT_OTHER = "0.1"

# Don't journal against a source that couldn't cover amount + fees.
MIN_SOURCE_HUMAN = Decimal("0.5")


def _human(balance: Any) -> Decimal:
    """A balance's raw integer amount as a human decimal."""
    return Decimal(balance.amount_raw) / (Decimal(10) ** balance.decimals)


def _raw(amount: str, decimals: int) -> int:
    """Independently scale a human decimal to raw units (do NOT reuse the SDK's
    scaler here — the point is to check the server's scaling against our own)."""
    return int(Decimal(amount).scaleb(decimals))


async def _pick_pair(wallet: Any):
    """A funded (source Balance, destination asset) cross-asset pair, or skip.

    Prefers another FUNDED balance as the destination (the test vault's stablecoin
    pairs are known-routable); falls back to any other stable, then any other asset,
    from the catalog."""
    src = await richest_balance(wallet)
    if src is None:
        pytest.skip("test vault holds no spendable balance")
    if _human(src) < MIN_SOURCE_HUMAN:
        pytest.skip(
            f"richest balance {src.asset} is below {MIN_SOURCE_HUMAN} — "
            "not enough headroom to journal quotes safely"
        )
    funded_others = sorted(
        (b for b in await wallet.balances() if b.asset != src.asset and int(b.amount_raw) > 0),
        key=usd_of,
        reverse=True,
    )
    if funded_others:
        return src, funded_others[0].asset
    catalog = await wallet.assets()
    for a in catalog:
        if a.asset != src.asset and a.symbol.upper().startswith("USD"):
            return src, a.asset
    for a in catalog:
        if a.asset != src.asset:
            return src, a.asset
    pytest.skip("no distinct destination asset in the catalog")


async def _journal_quote(wallet: Any, body: dict, key: str):
    """A non-dry quote — a JOURNAL WRITE that creates an unsigned pending movement.

    Only used for journal-creating calls (fresh keys). Skips on live-environment
    conditions that are not idempotency regressions: a momentarily unroutable pair,
    or balance tied up by still-pending reservations from earlier runs."""
    try:
        return await wallet._quote(SWAP_PATH, body, dry=False, idempotency_key=key)
    except RouteUnavailable as e:
        pytest.skip(f"live route unavailable for {body['send']}->{body['receive']}: {e}")
    except InsufficientFunds as e:
        pytest.skip(f"vault balance tied up (pending reservations?): {e}")


async def test_journal_fresh_replay_then_conflict(wallet):
    src, dest = await _pick_pair(wallet)
    key = uuid4().hex
    body = {"send": src.asset, "receive": dest, "amount": AMOUNT_FRESH, "slippage_bps": 50}

    # (1) FRESH — first use of the key journals a new pending movement.
    q1 = await _journal_quote(wallet, body, key)
    assert q1.movement_id, "non-dry quote must journal a movement_id"
    assert q1.movement_id.startswith("mov_")
    assert q1.send.asset == src.asset
    assert q1.receive.asset == dest
    # Request carried a HUMAN decimal; the journaled quote echoes it in RAW units.
    assert int(q1.send.amount) == _raw(AMOUNT_FRESH, src.decimals)

    # (2) REPLAY — same key, same body: the SAME movement, not a second one.
    q2 = await wallet._quote(SWAP_PATH, body, dry=False, idempotency_key=key)
    assert q2.movement_id == q1.movement_id
    assert q2.send.asset == q1.send.asset
    assert q2.send.amount == q1.send.amount

    # (3) CONFLICT — same key, different body: refused, never silently re-journaled.
    with pytest.raises(Conflict) as exc:
        await wallet._quote(
            SWAP_PATH,
            {**body, "amount": AMOUNT_OTHER},
            dry=False,
            idempotency_key=key,
        )
    assert ERR_IDEMPOTENCY_CONFLICT in str(exc.value)
    assert exc.value.status == 409

    # The failed conflict attempt did not corrupt the journal: replay still works…
    q3 = await wallet._quote(SWAP_PATH, body, dry=False, idempotency_key=key)
    assert q3.movement_id == q1.movement_id

    # …and the journaled movement is a real, UNSIGNED row: fetchable, lowercase
    # white-label status, and — since this suite never co-signs — never "completed".
    mv = await wallet.movement(q1.movement_id)
    assert mv.id == q1.movement_id
    assert mv.status == mv.status.lower()
    assert mv.status != "completed"


async def test_distinct_keys_journal_distinct_movements(wallet):
    src, dest = await _pick_pair(wallet)
    body = {"send": src.asset, "receive": dest, "amount": AMOUNT_OTHER, "slippage_bps": 50}

    q1 = await _journal_quote(wallet, body, uuid4().hex)
    q2 = await _journal_quote(wallet, body, uuid4().hex)

    assert q1.movement_id and q2.movement_id
    # Same body, different keys → different movements: the journal is keyed by the
    # Idempotency-Key alone, so the replay behavior above is a journal hit, not
    # request-body deduplication.
    assert q1.movement_id != q2.movement_id
