"""Shared fixtures for the paymos SDK **end-to-end** suite — these run against LIVE
production (``https://wallet.paymos.io`` by default).

Environment:

- ``PAYMOS_VAULT_SECRET`` — a full-scope ``vs_live_`` vault secret (carries the FROST
  share). REQUIRED; without it every e2e test is skipped, so a bare ``pytest`` in CI
  without the secret is a no-op rather than a failure.
- ``PAYMOS_BASE_URL`` — override the target (defaults to prod).
- ``PAYMOS_E2E_REAL_MONEY`` — must equal ``"1"`` to run the money-moving tests (the
  :func:`real_money` gate). A default run is READ-ONLY / dry-only and moves nothing.

Money safety: read / quote / error / idempotency-shape tests never move funds (quotes
are ``dry`` previews; the one non-dry idempotency check creates but never signs a
movement, which simply expires). Only tests that depend on the ``real_money`` fixture
co-sign a real movement, and by design that is a single tiny in-vault swap.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from paymos import Wallet
from paymos._secret import VaultSecret

BASE_URL = os.environ.get("PAYMOS_BASE_URL", "https://wallet.paymos.io")
_SECRET = os.environ.get("PAYMOS_VAULT_SECRET")


def vault_secret() -> str:
    """The configured full-scope secret, or skip the test if none is set."""
    if not _SECRET:
        pytest.skip("PAYMOS_VAULT_SECRET is not set — skipping live e2e")
    return _SECRET


@pytest_asyncio.fixture
async def wallet():
    """A fresh full-scope :class:`Wallet` bound to the live target, closed after the test."""
    w = Wallet(vault_secret(), base_url=BASE_URL)
    try:
        yield w
    finally:
        await w.aclose()


@pytest_asyncio.fixture
async def read_only_wallet():
    """A :class:`Wallet` built from a READ-ONLY view of the same secret — same api_key +
    vault, but the FROST share stripped. Used to prove the SDK's client-side read-only
    guard: ``swap`` / ``withdraw`` must raise BEFORE any network call, because a
    shareless secret can never co-sign."""
    parsed = VaultSecret.parse(vault_secret())
    read_only = VaultSecret.pack(parsed.api_key, parsed.vault_id, None)
    w = Wallet(read_only, base_url=BASE_URL)
    try:
        yield w
    finally:
        await w.aclose()


@pytest.fixture
def real_money():
    """Gate for tests that move REAL funds on prod. Skipped unless
    ``PAYMOS_E2E_REAL_MONEY=1``, so a default ``pytest`` run never signs anything."""
    if os.environ.get("PAYMOS_E2E_REAL_MONEY") != "1":
        pytest.skip("set PAYMOS_E2E_REAL_MONEY=1 to run real-money (co-sign) e2e tests")
