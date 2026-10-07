# paymos — Python SDK

Send, swap and bridge **USDT, USDC, BTC, ETH** and the rest of the wallet's catalogue from
Python — no browser, no wallet extension, and no seed phrase anywhere in your environment. It is
built for the services that pay people out: marketplaces, exchanges, payroll and treasury jobs.

Your process holds **one half** of the signing key; the Paymos server holds the other.
**Neither side alone can move funds** — every swap and withdrawal is co-signed, and your half
never reaches the server.

- **Non-custodial** — a leaked API key can't sign; your co-signing share stays in your process.
- **Fee-transparent** — every quote is a dry preview with a full `platform / network / route`
  fee breakdown you see *before* signing anything.
- **Human amounts, no floats** — you pass decimal strings like `"100"` / `"0.5"`; responses come
  back as raw integer strings.
- **No Rust toolchain** — the native signing core ships compiled inside the wheel.
- **Async-first** — built on `httpx.AsyncClient`, fully typed (`py.typed`).

The SDK talks in plain asset terms (`USDC@base`, `ETH@arb`) and hides the routing and the
2-of-2 signing behind a small, safe surface.

## Install

```bash
pip install paymos-wallet
```

Requires **Python ≥ 3.11**. The only runtime dependency is `httpx` (installed automatically).

The published sdist carries the Rust it needs and builds the signing core at install time, so
`pip install` wants a **Rust toolchain** on any platform without a matching wheel. Wheels are not
yet published for the full matrix — until they are, `rustup` is the prerequisite worth naming here
rather than discovering during a deploy.

## Get access & your API key

The SDK controls a **Paymos vault**, which you own from the **Paymos wallet**. The wallet is a
**Telegram-only Mini App** — there is no website login; you use it entirely inside Telegram. So
first get into the wallet, then mint a key.

**1. Open the wallet.** In Telegram, open
**[@Ox0000000000000000000Bot](https://t.me/Ox0000000000000000000Bot)** and tap **Open Wallet**.
Access is invite-based — if you don't have access yet, you'll need an invite. Create your vault if
you don't have one.

**2. Mint an API key.** In the Mini App go to **Settings → Vault API** and create one of:

| Key | Can do | Carries |
|---|---|---|
| **read** | `assets`, `balances`, `quote_swap`, `quote_withdraw`, `movement`, `movements` | API key only |
| **full** | everything a read key does, **plus** `swap` and `withdraw` | API key **+** your co-signing share |

The key is a `vs_live_…` string. A full key's co-signing share is decrypted on your device and
**never leaves your process** — there is no CLI provisioner. Treat a full secret like a private key.

**3. Point the SDK at it** with an environment variable:

```bash
export PAYMOS_VAULT_SECRET="vs_live_…"
# optional — defaults to https://wallet.paymos.io
# export PAYMOS_BASE_URL="https://wallet.paymos.io"
```

## Quickstart

```python
import asyncio
from paymos import Wallet

async def main():
    async with Wallet.from_env() as w:              # reads PAYMOS_VAULT_SECRET
        # Balances: per-asset, raw amount strings + a nullable USD value.
        for b in await w.balances():
            print(b.asset, b.amount_raw, b.usd)

        # Preview a withdraw — a dry, money-safe quote. Headline is the fee breakdown.
        q = await w.quote_withdraw(asset="USDC@base", amount="25", to="0xRecipient…")
        print(q.fees.platform, q.fees.network, q.fees.total)

        # Move money (full key only). Amounts are human decimal strings.
        mv = await w.swap(send="USDC@base", receive="ETH@arb", amount="100")
        settled = await w.wait(mv.id)               # poll to a terminal status
        print(settled.status)

asyncio.run(main())
```

`Wallet` is an async context manager (`aclose()` releases the HTTP client). You can also build it
directly — `Wallet("vs_live_…")` — or from the environment with `Wallet.from_env()`.

## Core concepts

### Amounts: human in, raw out

Amounts you **pass** are human decimal strings (`"100"`, `"0.5"`) — the server owns each asset's
decimals and scales them to raw. Amounts in **responses** (balances, quote legs, fees) come back
as raw integer strings. **Never a float**, in either direction.

### Assets

Assets use white-label `SYMBOL@chain` ids (`USDC@base`, `ETH@arb`). `assets()` returns the catalog
(fetched once and cached per `Wallet`); the SDK uses each asset's `decimals` to validate the
amounts you pass — an unknown asset or an over-precise amount raises `PaymosError` locally, before
anything hits the wire.

### Quotes and fees

`quote_swap` / `quote_withdraw` always return a **dry** `Quote` — a pure preview that persists
nothing and works for read and full keys alike. The headline is `quote.fees`:

```python
q = await w.quote_swap(send="USDC@base", receive="ETH@arb", amount="100")
print(q.fees.platform)   # raw string
print(q.fees.network)    # raw string
print(q.fees.route)      # RouteFee(amount, estimate) — or None when there's no route leg
print(q.fees.total)      # raw string
print(q.receive.amount)  # expected out
print(q.receive.min)     # guaranteed out (your slippage floor)
```

### Moving money

`swap` and `withdraw` (full key only) create a **real** movement and drive the 2-of-2 co-sign,
then return a `Movement`. Before any signature the SDK re-checks that the server's quote echoes
**exactly** the assets and amount you approved — a decimals bug, contract drift, or a quote that
disagrees with your request raises and **signs nothing**. The destination is not part of that
check (see [Security](#security)).

```python
# Swap, delivered back to your own vault.
mv = await w.swap(send="USDC@base", receive="ETH@arb", amount="100",
                  min_receive="0.03")               # optional slippage floor

# Withdraw to an external chain address.
mv = await w.withdraw(asset="USDC@base", amount="25", to="0xRecipient…")
```

### Withdraw modes

- `mode="exact_out"` *(default)* — the recipient receives exactly `amount`; the vault debit
  (`amount` + fees) is server-computed.
- `mode="total_in"` — `amount` is the **total** debited from the vault; the recipient gets that
  minus fees.

### Safety caps

- **`slippage_bps`** (swap / quote_swap, default `50` = **0.5%**) — the slippage tolerance sent with
  the quote in basis points; the quote's guaranteed `receive.min` already reflects it.
- **`min_receive`** (swap) — a human decimal in the receive asset. If the quote's *guaranteed*
  receive is below it, raises `SlippageExceeded` and signs nothing.
- **`max_debit`** (swap / withdraw) — a human decimal ceiling on the total vault debit. Strongly
  recommended for unattended `exact_out` payouts, whose input side is server-computed. Exceeded →
  raises, signs nothing.

### Idempotency

Pass your own `idempotency_key` (e.g. a durable payout id) so a retry after an ambiguous failure
replays the **same** movement instead of paying twice; a retry that finds it already
signed/relayed **converges** on it rather than double-signing. Omit it and a fresh key is generated
per call.

```python
mv = await w.withdraw(asset="USDC@base", amount="25", to="0xRecipient…",
                      idempotency_key="payout-8f21", max_debit="26")
```

### Waiting and statuses

`wait(id)` polls a movement until it reaches a terminal status —
`completed | failed | refunded | expired | cancelled` — and raises `PaymosError` on timeout.

```python
mv = await w.wait(mv.id, timeout=120, poll=2.0)
```

## API reference

All `Wallet` methods are `async`.

| Method | Returns | Notes |
|---|---|---|
| `Wallet(secret, base_url=…, *, timeout=None, pinned_fingerprints=None)` | `Wallet` | `secret` is a `vs_live_…` string; `pinned_fingerprints` maps a label to its fingerprint(s) |
| `Wallet.from_env(base_url=None, *, timeout=None, pinned_fingerprints=None)` | `Wallet` | reads `PAYMOS_VAULT_SECRET` / `PAYMOS_BASE_URL` |
| `assets()` | `list[Asset]` | curated catalog, cached |
| `balances()` | `list[Balance]` | per-asset vault balances |
| `quote_swap(send, receive, amount, slippage_bps=50)` | `Quote` | dry preview |
| `quote_withdraw(asset, amount, to, mode="exact_out")` | `Quote` | dry preview |
| `swap(send, receive, amount, slippage_bps=50, min_receive=None, *, max_debit=None, idempotency_key=None)` | `Movement` | full key |
| `withdraw(asset, amount, to, mode="exact_out", *, max_debit=None, idempotency_key=None)` | `Movement` | full key |
| `movement(id)` | `Movement` | one movement, with fee breakdown |
| `movements(limit=50, cursor=None)` | `tuple[list[Movement], str \| None]` | page + next cursor (newest first) |
| `wait(id, timeout=120, poll=2.0)` | `Movement` | poll to a terminal status |
| `aclose()` | — | release the underlying HTTP client |

The default HTTP timeout is `httpx.Timeout(60s, connect=10s)` (the co-sign is multi-round);
override it with `Wallet(..., timeout=…)`.

## Data models

Every amount field is a **raw integer string**. All models are frozen dataclasses.

- **`Asset`** — `asset`, `symbol`, `chain`, `decimals: int`, `fingerprint: str | None` (sha256 of the
  asset's token id; the co-sign checks every transfer against it unless the label is pinned, and
  refuses if neither a pin nor a published fingerprint exists)
- **`Balance`** — `asset`, `symbol`, `chain`, `decimals: int`, `amount_raw`, `usd: str | None`
- **`Amount`** — `amount`, `asset`
- **`Receive`** — `amount`, `min` *(guaranteed)*, `asset`
- **`RouteFee`** — `amount`, `estimate: bool` *(`True` = rate-derived cross-asset, `False` = exact same-asset spread)*
- **`Fees`** — `asset`, `platform`, `network`, `route: RouteFee | None`, `total`, `usd: dict | None`
- **`Quote`** — `movement_id: str | None` *(None on a dry preview)*, `mode`, `send: Amount`,
  `debit: Amount` *(full vault debit = send + fees)*, `receive: Receive`, `fees: Fees`,
  `expires_at`, `sources: int | None`
- **`Movement`** — `id`, `type`, `status`, `send: Amount`, `receive: Receive`, `fees: Fees | None`,
  `dest_chain_tx_hash: str | None`, `dest_chain_explorer_url: str | None`, `created_at`,
  `completed_at: str | None`

Timestamps stay raw ISO-8601 strings — the SDK does not parse them to `datetime`.

## Errors

Every call raises a typed subclass of `PaymosError`, which carries `message` and `status` (the
HTTP status, or `None` for a transport-level failure). Branch on the type instead of
string-matching a message.

| Exception | When |
|---|---|
| `AuthError` | `401` — API key missing, malformed, or unrecognized |
| `Forbidden` | `403` — the key is valid but lacks the scope this call needs |
| `InsufficientFunds` | `400` — the vault balance can't cover the amount (plus fees) |
| `QuoteExpired` | `400` — the referenced quote expired; fetch a fresh one |
| `SlippageExceeded` | `400` or local — delivered / guaranteed amount fell below your floor |
| `CrossAssetWithdrawNotAllowed` | `400` — a withdraw changed the asset (that's a swap) |
| `RouteUnavailable` | `400` — no route could be quoted right now; retry shortly |
| `RateLimited` | `429` — carries `.retry_after` (seconds, or `None`) |
| `Conflict` | `409` — idempotency key reused with a different request |
| `PaymosError` | base — `404`, any other 4xx/5xx, and transport errors (`status=None`) |

```python
import asyncio
from paymos import Wallet, InsufficientFunds, SlippageExceeded, RateLimited, PaymosError

try:
    mv = await w.swap(send="USDC@base", receive="ETH@arb", amount="100", min_receive="0.03")
except SlippageExceeded:
    ...                                   # guaranteed receive below your floor — nothing signed
except InsufficientFunds:
    ...                                   # not enough balance for amount + fees
except RateLimited as e:
    await asyncio.sleep(e.retry_after or 1)
except PaymosError as e:
    print(e.status, e.message)
```

Calling `swap` / `withdraw` with a **read-only** key raises `PaymosError` *before* any movement is
created — a read key can never sign.

## Security

- A leaked **API key alone cannot move funds** — no share, no signature (and the key can be revoked).
- A leaked **share alone cannot move funds** — no server co-sign.
- Both are required, on purpose. The SDK additionally checks every message before it co-signs:
  that it moves from your vault, moves the send asset (by the fingerprint the server publishes, or
  the one you pinned), and moves no more than the approved debit.
- What that does **not** cover: the transfer's recipient — the route's deposit address — is chosen
  by the server and is not checked. And for `exact_out` the approved debit is itself computed by the
  server; only `max_debit` puts your own ceiling on it. Pass `max_debit` on unattended payouts.

Treat a full `vs_live_…` secret like a private key, and back up the share — losing it means losing
your ability to co-sign.

## Examples

The source repository's `examples/` folder has a clickable local web demo (FastAPI, prod-pointed)
that loads balances, shows the fee breakdown, and — behind a confirm box — runs a real swap, plus a
minimal `demo.py` CLI.

## License

**Proprietary.** All rights reserved. Use of this SDK is subject to your agreement with Paymos.

---

## FAQ

### How do I send USDT (TRC20) from code?

Install the SDK, point it at your vault, and ask for a withdrawal with the asset written the way the
wallet writes it — `USDT@tron`. The same call sends USDT on BNB Chain (`USDT@bsc`), Ethereum
(`USDT@eth`), Polygon, Solana, Arbitrum, Avalanche or TON: the asset string is the only difference.

```
pip install paymos-wallet
```

### Is this custodial? Who can move the money?

Nobody alone. The vault is **2-of-2**: your process holds one signing share, the server holds the
other, and a transfer needs both. A stolen API key cannot sign, and the server cannot sign without
you. Your share never leaves your process.

### What is the difference between a quote and a transfer?

A quote is a dry run. It prices the route, separates platform, network and route fees, and reserves
nothing — ask for as many as you like. A transfer is the same numbers, signed. Nothing moves until
your half of the signature exists.

### Does it blind-sign whatever the server asks for?

No, and this is the part worth reading twice. The server discloses the exact message it wants
signed; the SDK recomputes the digest itself and refuses unless that message belongs to your vault,
moves the asset and no more than the debit of the quote you approved. A server asking for more gets
a refusal, not a signature. Two things it does not check: where the transfer goes (the route's
deposit address is the server's choice), and — for `exact_out` without `max_debit` — how large the
approved debit is, since the server computes it.

The asset check works by fingerprint: the id of the token each transfer moves must hash to the
fingerprint of the asset you are sending. By default that fingerprint comes from `GET /assets` — the same server that builds the
message — so the check catches a server bug or a tampered signing path, not a server that lies about
both. For a trust anchor the server does not control, pin the fingerprints you verified out of band:
`Wallet(secret, pinned_fingerprints={"USDC@base": "a42c7e…"})`. A pinned label is checked only
against its pins (a string or a list), whatever `/assets` says; unpinned labels use the catalog.

### Which networks and assets are supported?

USDT moves on nine chains — Tron, Ethereum, BNB Chain, Polygon, Solana, Arbitrum, Optimism,
Avalanche and TON — and USDC on eight: that same list without Tron and TON, with Base instead. Add
the native coin of each of those chains, and BTC, LTC, DOGE, XRP, BCH, DASH, ZEC and ADA, and the
catalogue is 21 assets across 18 chains. It is also the authority: an asset it does not list cannot
be quoted or sent, in any SDK.

### How are fees reported?

Every quote breaks the cost into platform, network and route, and amounts come back as raw integer
strings rather than floats: money that has been through a float is money with a rounding error in
it. You pass human amounts in (`"100"`, `"0.5"`) and read exact integers back.

### Can I use it for automated payouts?

That is what it is for: headless, no browser, no wallet extension, no seed phrase in the environment.
A worker holding one share can pay out continuously, and losing that machine costs the ability to
sign — not the funds.

### What happens if a transfer is interrupted?

Every money-moving call takes an idempotency key. Repeating a call with the same key returns the
original operation instead of starting a second one, so a retry after a timeout cannot pay twice.

## The same wallet, in eight languages

Every SDK here speaks to the same vault API and enforces the same rules; pick the one your service is
written in. The signing core is shared, so a fix there reaches all of them.

| Language | Package | Repository |
|---|---|---|
| Python *(this one)* | `pip install paymos-wallet` | [python-wallet-sdk](https://github.com/paymos-labs/python-wallet-sdk) |
| TypeScript / Node.js | `npm install @paymos/wallet` | [typescript-wallet-sdk](https://github.com/paymos-labs/typescript-wallet-sdk) |
| Go | `go get github.com/paymos-labs/go-wallet-sdk` | [go-wallet-sdk](https://github.com/paymos-labs/go-wallet-sdk) |
| Rust | `cargo add paymos-wallet` | [rust-wallet-sdk](https://github.com/paymos-labs/rust-wallet-sdk) |
| C# / .NET | `dotnet add package Paymos.Wallet` | [csharp-wallet-sdk](https://github.com/paymos-labs/csharp-wallet-sdk) |
| Java | `io.paymos:wallet` | [java-wallet-sdk](https://github.com/paymos-labs/java-wallet-sdk) |
| Ruby | `gem install paymos-wallet` | [ruby-wallet-sdk](https://github.com/paymos-labs/ruby-wallet-sdk) |
| PHP | `composer require paymos/wallet` | [php-wallet-sdk](https://github.com/paymos-labs/php-wallet-sdk) |
| Signing core | `paymos-wallet-core` | [paymos-wallet-core](https://github.com/paymos-labs/paymos-wallet-core) |

## Documentation

- [wallet.paymos.io](https://wallet.paymos.io) — the product, the supported networks, and guides for
  the routes people ask about most: USDT BEP20 to TRC20, USDC between chains, and the rest.
- [Networks and assets](https://wallet.paymos.io/networks) — every chain and token the wallet
  carries, with the standard each one is named by.
- [USDT BEP20 → TRC20](https://wallet.paymos.io/guides/usdt-bep20-to-trc20) — a worked route, priced
  live; the other pairs are linked from it.
