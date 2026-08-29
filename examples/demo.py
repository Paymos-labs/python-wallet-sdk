"""Paymos SDK demo — minimal CLI.

Reads the vault secret from the environment (``Wallet.from_env()`` →
``PAYMOS_VAULT_SECRET``, optional ``PAYMOS_BASE_URL``, default prod), prints the
vault's balances, then previews a quote and prints the full fee breakdown — the
SDK's headline. ``--swap`` additionally runs a REAL on-chain swap + ``wait()``.

    export PAYMOS_VAULT_SECRET=vs_live_…      # created in the wallet Mini App
    python examples/demo.py                    # balances + a dry quote (safe)
    python examples/demo.py --swap             # REAL money (funded full key only)

The dry read + quote path runs against live prod today. ``--swap`` moves real funds
and needs a funded FULL vault secret. Amounts you pass are HUMAN decimal strings
(``"1"``, ``"0.5"``) — never a float; the server scales them, and response amounts
come back as raw integer strings. Labels are white-label (``SYMBOL@chain`` + neutral
fee names).
"""

from __future__ import annotations

import asyncio
import sys

from paymos import Wallet
from paymos.errors import PaymosError

# The quote to preview. Same-asset withdraw to the zero address — a dry preview
# moves nothing, so the destination is only echoed in the fee math.
QUOTE_ASSET = "USDC@base"
QUOTE_AMOUNT = "1"
QUOTE_TO = "0x0000000000000000000000000000000000000000"

# The swap that ``--swap`` runs for real.
SWAP_SEND = "USDC@base"
SWAP_RECEIVE = "ETH@arb"
SWAP_AMOUNT = "1"
SWAP_SLIPPAGE_BPS = 50


def print_fees(fees) -> None:
    """Print the fee breakdown as clearly labeled raw-unit lines — the centerpiece."""
    print(f"\nFee breakdown (in {fees.asset}, raw units):")
    print(f"  platform : {fees.platform}")
    print(f"  network  : {fees.network}")
    if fees.route is not None:
        print(f"  route    : {fees.route.amount}  (estimate: {fees.route.estimate})")
    else:
        print("  route    : —")
    print("  ─────────")
    print(f"  total    : {fees.total}")
    if fees.usd is not None:
        u = fees.usd
        print(
            f"  ~usd     : platform {u.get('platform')} / network {u.get('network')} "
            f"/ route {u.get('route')} / total {u.get('total')}"
        )


async def main() -> None:
    do_swap = "--swap" in sys.argv
    try:
        # from_env raises PaymosError when PAYMOS_VAULT_SECRET is unset; a malformed
        # secret raises ValueError. Handle both as a clean one-line message below.
        wallet = Wallet.from_env()
    except (PaymosError, ValueError) as exc:
        msg = getattr(exc, "message", str(exc))
        print(f"Error: {msg}", file=sys.stderr)
        print("Set PAYMOS_VAULT_SECRET to a vs_live_ secret (Mini App → Settings → Vault API).",
              file=sys.stderr)
        sys.exit(1)
    try:
        print("Balances:")
        balances = await wallet.balances()
        if not balances:
            print("  (none)")
        for b in balances:
            usd = f"  (~${b.usd})" if b.usd is not None else ""
            print(f"  {b.symbol:<8} {b.asset:<16} {b.amount_raw} raw{usd}")

        print(f"\nQuote (dry preview): withdraw {QUOTE_AMOUNT} {QUOTE_ASSET}")
        quote = await wallet.quote_withdraw(
            asset=QUOTE_ASSET, amount=QUOTE_AMOUNT, to=QUOTE_TO
        )
        print(f"  send   : {quote.send.amount} {quote.send.asset}")
        print(f"  debit  : {quote.debit.amount} {quote.debit.asset}  (send + fees)")
        print(f"  receive: {quote.receive.amount} {quote.receive.asset} "
              f"(min {quote.receive.min})")
        print_fees(quote.fees)

        if do_swap:
            print(f"\nReal swap: {SWAP_AMOUNT} {SWAP_SEND} -> {SWAP_RECEIVE} …")
            movement = await wallet.swap(
                send=SWAP_SEND,
                receive=SWAP_RECEIVE,
                amount=SWAP_AMOUNT,
                slippage_bps=SWAP_SLIPPAGE_BPS,
            )
            final = await wallet.wait(movement.id)
            print(f"  final status: {final.status}")
            if final.dest_chain_tx_hash:
                print(f"  settlement tx: {final.dest_chain_tx_hash}")
                if final.dest_chain_explorer_url:
                    print(f"  explorer     : {final.dest_chain_explorer_url}")
    except PaymosError as exc:
        # A read-only key on --swap, an unfunded vault, an unreachable server, etc.
        # surface as a clean message rather than a traceback.
        print(f"\nError: {exc.message}", file=sys.stderr)
        sys.exit(1)
    finally:
        await wallet.aclose()


if __name__ == "__main__":
    asyncio.run(main())
