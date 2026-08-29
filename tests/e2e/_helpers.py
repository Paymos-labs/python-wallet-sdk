"""Verified prod-contract constants + tiny async helpers shared by the e2e modules.

Every string here is the ACTUAL message / shape the live server returned when the
contract was characterized on 2026-07-07 (see ``docs/audits/2026-07-07-vault-api-audit.md``
and the SDK's ``docs/integration.md``). Assert against these substrings, not exact
equality, so a future rail-safe rewording of a message doesn't break the suite.
"""

from __future__ import annotations

from typing import Any

# --- error message substrings the LIVE server returns (all HTTP 400 unless noted) ----
ERR_NO_AUTH = "invalid or missing API key"            # 401
ERR_UNKNOWN_SEND_ASSET = "unknown send asset"          # swap, unknown `send`
ERR_UNKNOWN_ASSET = "unknown asset"                    # withdraw, unknown `asset`
ERR_SAME_ASSET_SWAP = "source and destination assets must differ"
ERR_CROSS_ASSET_WITHDRAW = "cross-asset withdraw"      # -> CrossAssetWithdrawNotAllowed
ERR_NEGATIVE_AMOUNT = "non-negative decimal"
ERR_TOO_MANY_DECIMALS = "fractional digits"
ERR_JUNK_AMOUNT = "must be a decimal number"
ERR_MOVEMENT_NOT_FOUND = "movement not found"          # 404
ERR_IDEMPOTENCY_CONFLICT = "idempotency key reused"    # 409 -> Conflict

# A syntactically valid EVM address (dry withdraw previews never send; this is only
# echoed into the fee math). vitalik.eth — do NOT use as a real payout target.
EVM_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

# A well-known valid Tron base58 address (dry only).
TRON_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def usd_of(balance: Any) -> float:
    """A balance's USD value as a float (0.0 when unpriced)."""
    return float(balance.usd) if balance.usd else 0.0


async def balances_by_asset(wallet: Any) -> dict[str, Any]:
    """``{ 'SYMBOL@chain': Balance }`` for the wallet's current balances."""
    return {b.asset: b for b in await wallet.balances()}


async def richest_balance(wallet: Any, *, min_raw: int = 1):
    """The Balance with the largest USD value that has at least ``min_raw`` raw units,
    or ``None`` if the vault holds nothing spendable. Used to pick a real swap source."""
    best = None
    best_usd = -1.0
    for b in await wallet.balances():
        if int(b.amount_raw) >= min_raw:
            u = usd_of(b)
            if u > best_usd:
                best, best_usd = b, u
    return best
