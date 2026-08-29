# Paymos Python SDK — Integration Guide

A zero-to-production onboarding guide for backend developers integrating the
`paymos` Python SDK. Every example is async and imports from the public surface:

```python
from paymos import Wallet
```

---

## 1. What it is

`paymos` gives your backend **headless, non-custodial** control of a wallet vault.

- **Non-custodial, co-signed 2-of-2.** Every money-moving action (swap, withdraw)
  is signed by two parties: **your process** holds one half of the signing key, our
  server holds the other. Neither half alone can move funds.
- **The share never leaves your process.** For a full-scope credential the SDK holds
  your co-signing share in memory and uses it to sign locally; it is **never sent over
  the wire**. Only the API key reaches our server.
- **White-label and headless.** You talk to it in plain asset terms
  (`USDC@base`, `ETH@arb`). There is no UI, no redirect, no wallet popup — just async
  method calls returning typed dataclasses.
- **Money-safe by construction.** Every amount is a **human decimal string** in and a
  **raw integer string** out — never a float. Previews (`quote_*`) move nothing.

The native signing core ships compiled **inside the wheel**, so your machine needs no
Rust toolchain and no extra native dependency.

---

## 2. Prerequisites

| Requirement | Detail |
| --- | --- |
| Python | **≥ 3.11** (a single `cp311-abi3` wheel serves 3.11 / 3.12 / 3.13+) |
| Platform | Windows (`win_amd64`) or Linux (`manylinux2014_x86_64`) prebuilt wheels |
| Dependency | Only `httpx` (installed automatically) |
| Credential | One `vs_live_…` **vault secret**, minted in the wallet Mini App (see §3) |
| Runtime | An async context — `asyncio.run(...)`, or a running loop (e.g. FastAPI) |

---

## 3. Get a credential

The SDK is driven by one `vs_live_…` string called a **vault secret**. You mint it in
the **wallet Mini App → Settings → Vault API**. There is **no CLI provisioner** — the
secret is created in-app (the co-signing share is decrypted on your device at mint time
and packed into the string).

Two scopes:

| Scope | Can do | Carries the share? | If leaked |
| --- | --- | --- | --- |
| **read** | balances, quotes (previews) | No | **View-only.** Cannot sign, cannot spend. |
| **full** | everything a read key does, **plus** `swap` and `withdraw` | Yes | **Spend power.** Treat like a private key. |

- The secret is a **single opaque string** (`vs_live_…`). It bundles the API key and,
  for a full key, your co-signing share.
- A **read** secret is identity-only — a leaked read key can view balances and price
  quotes but can never move funds.
- A **full** secret carries your share. Combined with the server co-sign it can move
  real money, so guard it like a seed phrase.
- Secrets are **revocable** in the same Settings screen. Revoking the API key stops the
  server from ever co-signing for that key again.

> Prefer a **read** key everywhere you only need balances and quotes. Reach for a
> **full** key only in the code path that actually calls `swap` / `withdraw`.

---

## 4. Install

```bash
pip install paymos-wallet
```

The wheel is self-contained: the native signing core is bundled as a compiled
extension, so there is nothing else to build or install. Prebuilt wheels exist for
Windows and Linux; the only runtime dependency (`httpx`) is pulled automatically.

Installing a specific wheel directly:

```powershell
# Windows
pip install paymos_wallet-1.0.0-cp311-abi3-win_amd64.whl
```

```bash
# Linux (x86-64)
pip install paymos-0.1.2-cp311-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
```

---

## 5. Configure

### From the environment

```powershell
$env:PAYMOS_VAULT_SECRET = "vs_live_…"
# optional — defaults to https://wallet.paymos.io
# $env:PAYMOS_BASE_URL = "https://wallet.paymos.io"
```

```python
from paymos import Wallet

w = Wallet.from_env()   # reads PAYMOS_VAULT_SECRET (required) + PAYMOS_BASE_URL (optional)
```

`from_env()` raises `PaymosError` if `PAYMOS_VAULT_SECRET` is unset. The base URL
resolves as: explicit `base_url` argument → `PAYMOS_BASE_URL` → built-in default
(`https://wallet.paymos.io`).

### Explicitly

```python
w = Wallet(secret="vs_live_…", base_url="https://wallet.paymos.io")
```

A malformed `vs_live_…` string makes the constructor raise **`ValueError`** (not a
`PaymosError`) — catch it if you accept the secret from user input.

### Lifecycle

`Wallet` owns an async HTTP client. Close it when done:

```python
w = Wallet.from_env()
try:
    ...
finally:
    await w.aclose()
```

`repr(w)` is safe to log — it renders `Wallet(vault_id=…, has_share=…, base_url=…)`
and **never** surfaces the secret or the share.

---

## 6. Concepts

**Assets** are `SYMBOL@chain` strings — e.g. `USDC@base`, `ETH@arb`, `USDC@arb`. Call
`assets()` for the live catalog (each entry has `asset`, `symbol`, `chain`, `decimals`).

**Amounts are human decimal strings — never floats.** You pass `"100"`, `"0.5"`,
`"25"` on every request; the server owns each asset's on-chain `decimals` and scales
the amount to raw itself (the SDK validates the amount but does **not** pre-scale it —
pre-scaling would double-scale). In every returned dataclass and response field,
amounts are **raw integer strings** (the wire is intentionally asymmetric:
human-decimal in, raw-integer out). A malformed amount (empty, non-numeric, negative,
or more fractional digits than the asset allows) raises `PaymosError`.

**Quotes are dry previews.** `quote_swap` and `quote_withdraw` always send `dry=true`:
they price the operation, persist nothing, sign nothing, and work with **read or full**
keys. The headline of a quote is the **fee breakdown**.

**The fee breakdown** (`Quote.fees`, a `Fees`) is the number your users care about:

| Field | Type | Meaning |
| --- | --- | --- |
| `asset` | `str` | The `SYMBOL@chain` the fees are denominated in (the send asset) |
| `platform` | `str` | Platform fee, raw integer string |
| `network` | `str` | Network fee, raw integer string |
| `route` | `RouteFee \| None` | Route (spread) leg — `None` when there's no route leg |
| `total` | `str` | Total fee, raw integer string |
| `usd` | `dict[str, str \| None] \| None` | Nullable USD estimates, keyed `platform` / `network` / `route` / `total` |

`RouteFee` has `.amount` (raw string) and `.estimate` (`bool`: `False` for an exact
same-asset spread, `True` for a rate-derived cross-asset one).

**Statuses.** A movement is one of:

- Non-terminal (keep polling): `pending`, `processing`
- **Terminal** (settled): `completed`, `failed`, `refunded`, `expired`, `cancelled`

`wait()` polls until a movement reaches a terminal status.

---

## 7. Full method reference

`Wallet` splits into **read / preview** methods (any key) and **money-moving** methods
(full key only). All are `async`.

### Read / preview

#### `assets() -> list[Asset]`

The curated asset catalog. Fetched once and cached.

```python
for a in await w.assets():
    print(a.asset, a.symbol, a.chain, a.decimals)
```

`Asset` fields: `asset`, `symbol`, `chain`, `decimals`.

#### `balances() -> list[Balance]`

The caller's per-asset vault balances.

```python
for b in await w.balances():
    print(b.symbol, b.asset, b.amount_raw, b.usd)   # amount_raw is a raw string; usd may be None
```

`Balance` fields: `asset`, `symbol`, `chain`, `decimals`, `amount_raw` (raw integer
string), `usd` (nullable string).

#### `quote_swap(send, receive, amount, slippage_bps=50) -> Quote`

Dry preview of a cross-asset swap delivered back to your own vault.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `send` | `str` | — | Source asset, `SYMBOL@chain` |
| `receive` | `str` | — | Destination asset, `SYMBOL@chain` |
| `amount` | `str` | — | Human decimal in the **send** asset |
| `slippage_bps` | `int` | `50` | Accepted slippage, basis points |

```python
q = await w.quote_swap(send="USDC@base", receive="ETH@arb", amount="100")
print(q.fees.total, q.receive.amount, q.receive.min)
```

#### `quote_withdraw(asset, amount, to, mode="exact_out") -> Quote`

Dry preview of a same-asset payout to an external chain address.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `asset` | `str` | — | Asset to withdraw, `SYMBOL@chain` |
| `amount` | `str` | — | Human decimal in `asset` |
| `to` | `str` | — | External destination address |
| `mode` | `str` | `"exact_out"` | `"exact_out"` (recipient gets exactly `amount`) or `"total_in"` (debit totals `amount`) |

```python
q = await w.quote_withdraw(asset="USDC@base", amount="25", to="0xRecipient…")
print(q.fees.platform, q.fees.network, q.fees.route, q.fees.total)
```

**`Quote` fields** (returned by every `quote_*`, `swap`, `withdraw`):

| Field | Type | Meaning |
| --- | --- | --- |
| `movement_id` | `str \| None` | `None` for a dry preview; set on a created movement |
| `mode` | `str` | The pricing mode |
| `send` | `Amount` | Amount fed to the route (`.amount`, `.asset`) |
| `debit` | `Amount` | Full vault debit = send + fees |
| `receive` | `Receive` | `.amount` (expected), `.min` (guaranteed), `.asset` |
| `fees` | `Fees` | The fee breakdown (see §6) |
| `expires_at` | `str` | Raw ISO-8601 string (the SDK does not parse it) |
| `sources` | `int \| None` | Co-sign source count; `None` on a dry preview, `>= 1` on a real one |

#### `movement(movement_id) -> Movement`

One movement in full, including its fee breakdown.

```python
mv = await w.movement("mv_123")
print(mv.status, mv.type, mv.dest_chain_tx_hash)
```

#### `movements(limit=50, cursor=None) -> tuple[list[Movement], str | None]`

A page of movement history (newest first; summary rows carry no `fees`). Returns
`(items, next_cursor)`; pass `next_cursor` back as `cursor` for the next page (`None`
when exhausted).

```python
items, cursor = await w.movements(limit=20)
while cursor:
    page, cursor = await w.movements(limit=20, cursor=cursor)
    items += page
```

**`Movement` fields:** `id`, `type`, `status`, `send` (`Amount`), `receive`
(`Receive`), `fees` (`Fees | None` — `None` on summary rows), `dest_chain_tx_hash`
(`str | None`), `dest_chain_explorer_url` (`str | None`), `created_at` (ISO string),
`completed_at` (`str | None`).

#### `wait(movement_id, timeout=120, poll=2.0) -> Movement`

Poll a movement until it reaches a terminal status, then return it. Raises
`PaymosError` if `timeout` seconds elapse first.

```python
final = await w.wait(mv.id)
print(final.status)   # completed | failed | refunded | expired | cancelled
```

### Money-moving (full key only)

Both `swap` and `withdraw`:

1. Fail immediately with `PaymosError` if the secret is **read-only** (no share) —
   before any movement is created.
2. Create a **real** movement under an idempotency key — auto-generated, or your own if
   you pass `idempotency_key` (see §10).
3. Drive the 2-of-2 co-sign locally (your share never leaves the process).
4. Return the resulting `Movement`.

Call `wait()` afterward to poll to a terminal status.

#### `swap(send, receive, amount, slippage_bps=50, min_receive=None, *, max_debit=None, idempotency_key=None) -> Movement`

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `send` | `str` | — | Source asset |
| `receive` | `str` | — | Destination asset (delivered back to this vault) |
| `amount` | `str` | — | Human decimal in the **send** asset |
| `slippage_bps` | `int` | `50` | Accepted slippage, basis points |
| `min_receive` | `str \| None` | `None` | Human decimal in the **receive** asset. If the quote's **guaranteed** `receive.min` is below it, raises `SlippageExceeded` and **signs nothing**. |
| `max_debit` | `str \| None` | `None` | *Keyword-only.* Human-decimal ceiling on the total vault debit (send + fees). If the quote's debit exceeds it, raises `PaymosError` and **signs nothing**. |
| `idempotency_key` | `str \| None` | `None` | *Keyword-only.* Your own durable key (e.g. a job id). A retry under the same key replays the **same** movement instead of creating a second one. Omit → a fresh key is generated per call. |

```python
mv = await w.swap(send="USDC@base", receive="ETH@arb", amount="100", min_receive="0.03")
final = await w.wait(mv.id)
```

#### `withdraw(asset, amount, to, mode="exact_out", *, max_debit=None, idempotency_key=None) -> Movement`

Same-asset payout to external address `to`. Takes the same `asset` / `amount` / `to` /
`mode` as `quote_withdraw`, **plus** two keyword-only guards:

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_debit` | `str \| None` | `None` | Human-decimal ceiling on the total vault debit. If the quote's debit exceeds it, raises `PaymosError` and **signs nothing** — strongly recommended for unattended `exact_out` payouts, whose input side is server-computed and otherwise unbounded. |
| `idempotency_key` | `str \| None` | `None` | Your own durable key (e.g. your payout id). A retry under the same key replays the **same** movement instead of paying twice. Omit → a fresh key per call. |

```python
mv = await w.withdraw(asset="USDC@base", amount="25", to="0xRecipient…")
final = await w.wait(mv.id)
```

### The high-level co-sign flow

You don't drive the signing protocol yourself — `swap` / `withdraw` do it for you:

```
swap()/withdraw()
  └─ require full-scope share (else PaymosError)
  └─ create real movement  (non-dry quote, auto idempotency key)
  └─ [swap only] enforce min_receive → SlippageExceeded if guaranteed min too low
  └─ enforce max_debit (if set)       → PaymosError if the quote's debit exceeds the cap
  └─ co-sign locally with your share  (share never sent over the wire)
  └─ return Movement
wait(movement.id)  → poll to terminal status
```

---

## 8. Error handling

Every failure is a typed exception in `paymos.errors`, all subclasses of
`PaymosError`. The base carries `.message` (the server's already-sanitized, client-safe
reason) and `.status` (the HTTP status, or `None`). Branch on the **type**, not the
message text.

```python
from paymos.errors import (
    PaymosError, AuthError, Forbidden, InsufficientFunds, QuoteExpired,
    SlippageExceeded, CrossAssetWithdrawNotAllowed, RouteUnavailable,
    RateLimited, Conflict,
)
```

| Exception | HTTP | When it fires | How to handle |
| --- | --- | --- | --- |
| `AuthError` | 401 | API key missing, malformed, or unrecognized | Fix / rotate the secret; don't retry blindly |
| `Forbidden` | 403 | Key is valid but lacks scope for this call | Use a higher-scope (full) key |
| `InsufficientFunds` | 400 | Balance can't cover amount + fees | Surface to the user; reduce amount |
| `QuoteExpired` | 400 | Referenced quote has expired | **Re-quote** and retry |
| `SlippageExceeded` | 400 | Delivered amount fell outside the slippage bound (or `min_receive` not met) | Re-quote, widen `slippage_bps`, or adjust `min_receive` |
| `CrossAssetWithdrawNotAllowed` | 400 | A withdraw changed the asset (that's a swap) | Use `swap`, not `withdraw` |
| `RouteUnavailable` | 400 | No route could be quoted for this pair right now | Retry shortly |
| `RateLimited` | 429 | Too many requests | **Back off** using `.retry_after` (see below) |
| `Conflict` | 409 | Idempotency-key conflict (reused with a different request, or a quote for the key is still being created) | Wait and retry the same request unchanged |
| `PaymosError` | any | Base / catch-all. Also the error a **read-only key raises on `swap`/`withdraw`** (no share to co-sign), and any 404 or 5xx | Inspect `.message` / `.status` |

**`RateLimited` is the only exception with `retry_after`** — an `int` (seconds from the
server's `Retry-After` header) when the server sent one, else `None`.

> **Read-key attempting a money move** raises the **base `PaymosError`** — not
> `Forbidden`. The check is local (no share present), so it fires before any request.

### Try / except example

```python
import asyncio
from paymos import Wallet
from paymos.errors import (
    PaymosError, RateLimited, QuoteExpired, SlippageExceeded, InsufficientFunds,
)

async def do_swap(w: Wallet):
    try:
        mv = await w.swap(send="USDC@base", receive="ETH@arb",
                          amount="100", min_receive="0.03")
        return await w.wait(mv.id)
    except RateLimited as e:
        await asyncio.sleep(e.retry_after or 5)   # back off, then retry
        return await do_swap(w)
    except (QuoteExpired, SlippageExceeded):
        # price moved — re-quote / retry with a fresh preview
        raise
    except InsufficientFunds as e:
        # user-facing: not enough balance for amount + fees
        raise
    except PaymosError as e:
        # includes a read-only key with no share, plus any 4xx/5xx
        print("paymos error:", e.status, e.message)
        raise
```

---

## 9. Realistic FastAPI integration

This mirrors the shipped example (`examples/app.py`): build a **fresh `Wallet` per
request** from that user's stored secret, return a quote with its fee breakdown, and
gate money moves. The secret is loaded from encrypted storage, used for one request,
and **never logged or echoed back**.

```python
from typing import Any
import dataclasses

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from paymos import Wallet
from paymos.errors import PaymosError, RateLimited

app = FastAPI()


def _to_json(obj: Any) -> Any:
    """SDK dataclasses (and lists of them) → JSON-able structures.
    dataclasses.asdict recurses through the nested Amount / Receive / Fees / RouteFee,
    so a whole Quote — fee breakdown included — round-trips with no field wiring."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_json(x) for x in obj]
    return obj


async def wallet_for(user_id: str) -> Wallet:
    """Load THIS user's vault secret from your encrypted store and build a Wallet.
    The secret lives only for the request — never persisted here, never logged."""
    secret = await load_encrypted_secret(user_id)   # your KMS / vault-backed lookup
    return Wallet(secret=secret)                     # base_url defaults to prod


@app.exception_handler(PaymosError)
async def _paymos_error(request: Request, exc: PaymosError) -> JSONResponse:
    """Any PaymosError → a clean 4xx, never a 500. Business errors are the client's
    to display. RateLimited passes retry_after through so the caller can back off."""
    status = exc.status or (429 if isinstance(exc, RateLimited) else 400)
    body: dict[str, Any] = {"error": exc.message, "type": type(exc).__name__}
    if isinstance(exc, RateLimited) and exc.retry_after is not None:
        body["retry_after"] = exc.retry_after
    return JSONResponse(body, status_code=status)


@app.post("/wallets/{user_id}/balances")
async def balances(user_id: str) -> JSONResponse:
    w = await wallet_for(user_id)
    try:
        return JSONResponse(_to_json(await w.balances()))
    finally:
        await w.aclose()


@app.post("/wallets/{user_id}/quote")
async def quote(user_id: str, request: Request) -> JSONResponse:
    """Preview a swap and return the fee breakdown. Read or full key — moves nothing."""
    body = await request.json()
    w = await wallet_for(user_id)
    try:
        q = await w.quote_swap(
            send=str(body["send"]),
            receive=str(body["receive"]),
            amount=str(body["amount"]),                 # human decimal string
            slippage_bps=int(body.get("slippage_bps", 50)),
        )
        return JSONResponse(_to_json(q))
    finally:
        await w.aclose()


@app.post("/wallets/{user_id}/swap")
async def swap(user_id: str, request: Request) -> JSONResponse:
    """Move REAL funds when the stored secret is a funded FULL key. Gated behind an
    explicit confirm flag. A read-only secret raises PaymosError → clean 4xx."""
    body = await request.json()
    if body.get("confirm") is not True:
        return JSONResponse({"error": "confirm must be true"}, status_code=400)
    w = await wallet_for(user_id)
    try:
        mv = await w.swap(
            send=str(body["send"]),
            receive=str(body["receive"]),
            amount=str(body["amount"]),
            slippage_bps=int(body.get("slippage_bps", 50)),
        )
        mv = await w.wait(mv.id)                          # poll to terminal status
        return JSONResponse(_to_json(mv))
    finally:
        await w.aclose()
```

Notes carried over from the shipped example:

- **One `Wallet` per request**, always closed in a `finally`.
- **Malformed amounts / non-int slippage** raise `ValueError` from the SDK — catch and
  map to a 400 if the values come from client input.
- **Never** put the secret in a response, a log line, or an error body.

---

## 10. Idempotency & retries

- **Money moves are idempotent.** `swap` and `withdraw` create their movement under an
  idempotency key. Omit it and the SDK generates a fresh one per call; **pass your own
  `idempotency_key`** — a durable value like a job or payout id — and a retry after an
  ambiguous failure replays the **same** movement instead of creating a second one:

  ```python
  # Safe to retry: same key → same movement, never a double-spend.
  mv = await w.withdraw(asset="USDC@base", amount="25", to=addr,
                        idempotency_key=f"payout:{payout_id}")
  ```
- **A `Conflict` (409)** means an idempotency key was reused with a *different* request,
  or a quote for that key is still being created. Retry the **same** request unchanged;
  don't mutate the parameters.
- **`RateLimited` (429):** back off for `retry_after` seconds (fall back to your own
  delay when it's `None`), then retry.
- **`QuoteExpired` / `SlippageExceeded`:** these are price-freshness signals — re-quote
  (get a fresh preview) and retry rather than replaying the stale one.
- **`wait()` is a poller, not a mutator** — calling it again is always safe. It re-reads
  the movement until terminal; a timeout raises `PaymosError` and leaves the movement
  untouched.

---

## 11. Security checklist

- [ ] **A full secret is a seed.** It carries your co-signing share; combined with the
      server co-sign it can move real money. Treat it exactly like a private key.
- [ ] **Encrypt secrets at rest** (KMS / secrets manager). Never store them in plaintext
      config, source, or a database column without encryption.
- [ ] **Never log or commit a secret.** Keep it out of logs, tracebacks, error bodies,
      and version control. (`repr(Wallet)` and `repr(VaultSecret)` are already
      share-safe and key-masked — rely on those, don't format the raw string.)
- [ ] **Prefer read keys.** Use a **read** secret for every balance/quote path; reach
      for a **full** secret only where you actually call `swap` / `withdraw`.
- [ ] **Cap unattended payouts with `max_debit`.** For automated `exact_out` withdrawals
      (or any hands-off money move), pass `max_debit` — a human-decimal ceiling on the
      total vault debit — so a dislocated quote can never debit more than you intended.
- [ ] **A leaked read key is view-only** (no share, no signature). A leaked **full** key
      has spend power — rotate immediately.
- [ ] **Revoke in-app.** If a secret is exposed, revoke it in the wallet Mini App →
      Settings → Vault API; the server will no longer co-sign for that key.
- [ ] **Don't host the demo publicly.** The `examples/` web app builds a `Wallet` from a
      pasted secret — run it locally only; never paste a full secret into a remote copy.

---

## 12. Support & next steps

- **README:** `sdk/python/README.md` — the quickstart and the fee-breakdown overview.
- **Examples:** `sdk/python/examples/`
  - `app.py` — the FastAPI pattern this guide mirrors (balances, quote with fees, a
    confirm-gated swap; prod-pointed).
  - `demo.py` — a minimal CLI that reads `PAYMOS_VAULT_SECRET`, prints balances, and
    previews a quote with the full fee breakdown (`--swap` runs a real swap).
- **First integration:** point the SDK at prod (`https://wallet.paymos.io`, the
  default), start with a **read** key against `balances()` + `quote_*`, then graduate to
  a **full** key for `swap` / `withdraw` once the read path is wired.
