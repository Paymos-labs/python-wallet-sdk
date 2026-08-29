"""E2E: movement HISTORY reads against the LIVE Vault API (wallet.paymos.io).

Read-only module — every call here is a GET; nothing quotes, signs, or moves funds.

What each test proves about live prod + SDK 0.1.1:

- ``test_movements_page_shape`` — ``Wallet.movements(limit=3)`` returns the documented
  ``(list[Movement], next_cursor)`` tuple; the page honors the limit (<= 3 items);
  every LIST row omits ``fees`` (``fees is None``); ``status`` is one of the lowercase
  white-label statuses (pending / processing / completed / refunded / expired /
  cancelled / failed — never an internal enum); send/receive amounts are RAW
  non-negative integer strings (no decimal point, no float); ``created_at`` is a
  non-empty ISO-8601 string; ids are unique within the page.
- ``test_movement_detail_round_trips`` — ``movement(id)`` for a real id taken from the
  list returns the SAME movement (id / type / created_at / assets round-trip exactly)
  and, unlike the list, the detail carries ``fees``: a full :class:`Fees` breakdown
  with raw-integer legs, or ``None`` only for a movement that never went through a
  quote (e.g. a deposit). Skips if the vault has no history.
- ``test_movements_pagination_no_overlap`` — a non-null ``next_cursor`` passed back as
  ``cursor`` yields a strictly DIFFERENT page: no movement id from page 1 reappears on
  page 2, and page 2 rows honor the same list-row shape. Skips if the whole history
  fits in one page of 3.
- ``test_movements_limit_clamped`` — the server clamps ``limit`` into 1..100 instead
  of rejecting it: ``movements(limit=1000)`` succeeds and returns at most 100 items.
- ``test_movement_not_found_is_paymos_error`` — an unknown movement id is a 404
  ``{"error": "movement not found"}`` that the SDK surfaces as the BASE
  :class:`PaymosError` (the taxonomy deliberately has no ``NotFound`` subclass) with
  ``status == 404`` and the server's message preserved.
"""

from __future__ import annotations

import re

import pytest

from paymos import PaymosError
from paymos._types import Fees, Movement

from _helpers import ERR_MOVEMENT_NOT_FOUND

# The white-label movement lifecycle exactly as prod returns it — always lowercase.
WHITE_LABEL_STATUSES = frozenset(
    {"pending", "processing", "completed", "refunded", "expired", "cancelled", "failed"}
)

# ISO-8601 date-time prefix. The suffix (fractional seconds / timezone) is deliberately
# not pinned — asserting the prefix proves "ISO string" without parser-version games.
_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _assert_raw_amount(value: str, what: str) -> None:
    """A RESPONSE amount must be a RAW integer string: parseable as a non-negative
    int, with no decimal point (human decimals exist on the REQUEST side only)."""
    assert isinstance(value, str), f"{what}: expected a raw string, got {type(value).__name__}"
    assert "." not in value, f"{what}: raw amounts never carry decimals: {value!r}"
    assert int(value) >= 0, f"{what}: raw amount must be a non-negative integer: {value!r}"


def _assert_list_row(m: Movement) -> None:
    """The shape every LIST row must have (summary — the list omits fee legs)."""
    assert isinstance(m, Movement)
    assert isinstance(m.id, str) and m.id
    assert isinstance(m.type, str) and m.type
    assert m.fees is None, f"list rows must omit fees; movement {m.id} carried {m.fees!r}"
    assert m.status in WHITE_LABEL_STATUSES, f"{m.id}: non-white-label status {m.status!r}"
    assert m.status == m.status.lower(), f"{m.id}: status must be lowercase: {m.status!r}"
    assert isinstance(m.send.asset, str) and m.send.asset
    assert isinstance(m.receive.asset, str) and m.receive.asset
    _assert_raw_amount(m.send.amount, f"{m.id} send.amount")
    _assert_raw_amount(m.receive.amount, f"{m.id} receive.amount")
    _assert_raw_amount(m.receive.min, f"{m.id} receive.min")
    assert isinstance(m.created_at, str) and m.created_at, f"{m.id}: empty created_at"
    assert _ISO_PREFIX.match(m.created_at), f"{m.id}: created_at not ISO-8601: {m.created_at!r}"
    if m.completed_at is not None:
        assert _ISO_PREFIX.match(m.completed_at), (
            f"{m.id}: completed_at not ISO-8601: {m.completed_at!r}"
        )


async def test_movements_page_shape(wallet):
    page = await wallet.movements(limit=3)
    # The documented return shape: a 2-tuple of (items, next_cursor).
    assert isinstance(page, tuple) and len(page) == 2
    items, next_cursor = page
    assert isinstance(items, list)
    assert len(items) <= 3, f"limit=3 page returned {len(items)} items"
    assert next_cursor is None or (isinstance(next_cursor, str) and next_cursor)
    for m in items:
        _assert_list_row(m)
    # No duplicate ids inside a single page. (Passes vacuously on an empty history —
    # the tuple / limit / cursor shape above is still fully exercised.)
    assert len({m.id for m in items}) == len(items)


async def test_movement_detail_round_trips(wallet):
    items, _ = await wallet.movements(limit=3)
    if not items:
        pytest.skip("vault has no movement history — nothing to fetch in detail")
    row = items[0]

    detail = await wallet.movement(row.id)
    assert isinstance(detail, Movement)
    # Immutable identity fields round-trip exactly. Status is deliberately NOT
    # compared — it may legally advance (e.g. processing -> completed) between reads.
    assert detail.id == row.id
    assert detail.type == row.type
    assert detail.created_at == row.created_at
    assert detail.send.asset == row.send.asset
    assert detail.receive.asset == row.receive.asset
    assert detail.status in WHITE_LABEL_STATUSES

    # THE list/detail difference: the detail carries the fee breakdown. None is legal
    # only for a movement that never went through a quote (e.g. a deposit).
    assert detail.fees is None or isinstance(detail.fees, Fees)
    if detail.fees is not None:
        f = detail.fees
        assert isinstance(f.asset, str) and f.asset
        _assert_raw_amount(f.platform, f"{detail.id} fees.platform")
        _assert_raw_amount(f.network, f"{detail.id} fees.network")
        _assert_raw_amount(f.total, f"{detail.id} fees.total")
        if f.route is not None:
            _assert_raw_amount(f.route.amount, f"{detail.id} fees.route.amount")
            assert isinstance(f.route.estimate, bool)


async def test_movements_pagination_no_overlap(wallet):
    page1, cursor = await wallet.movements(limit=3)
    if cursor is None:
        pytest.skip("history fits in a single page of 3 — no next_cursor to follow")

    page2, cursor2 = await wallet.movements(limit=3, cursor=cursor)
    assert isinstance(page2, list) and len(page2) <= 3
    assert cursor2 is None or (isinstance(cursor2, str) and cursor2)
    for m in page2:
        _assert_list_row(m)

    ids1 = {m.id for m in page1}
    ids2 = {m.id for m in page2}
    assert ids1.isdisjoint(ids2), f"cursor page replayed ids from page 1: {ids1 & ids2}"


async def test_movements_limit_clamped(wallet):
    # An out-of-range limit is clamped (1..100), not rejected with a 400.
    items, _ = await wallet.movements(limit=1000)
    assert isinstance(items, list)
    assert len(items) <= 100, f"limit must clamp to 100; server returned {len(items)}"
    assert all(isinstance(m, Movement) for m in items)
    assert len({m.id for m in items}) == len(items)


async def test_movement_not_found_is_paymos_error(wallet):
    with pytest.raises(PaymosError) as exc:
        await wallet.movement("mov_deadbeefdeadbeef")
    assert ERR_MOVEMENT_NOT_FOUND in str(exc.value)
    assert exc.value.status == 404
    # 404 maps to the BASE error on purpose — the taxonomy has no NotFound subclass.
    assert type(exc.value) is PaymosError
