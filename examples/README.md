# Paymos SDK demo — clickable local web app (prod by default)

A small app you run **on your own machine** that points at **prod
(`https://wallet.paymos.io`)**. Paste your `vs_live_` vault secret and click to:

- **Load balances** for the vault,
- get a **quote** with the full **fee breakdown** (platform / network / route / total — the headline), and
- — gated behind a confirm box — do a **real swap**.

The backend (`app.py`) is a thin FastAPI shim over `from paymos import Wallet`. It
mirrors the first integration client's stack.

## Files

| File | What it is |
|------|------------|
| `app.py` | FastAPI backend. `GET /` serves the page; `POST /api/balances`, `POST /api/quote`, `POST /api/swap` build a `Wallet` **per request** from the posted secret and delegate to the SDK. Prod by default via `PAYMOS_BASE_URL`. |
| `index.html` | The clickable UI — vanilla JS, self-contained (inline CSS/JS), **no build step**. Masked secret input, Load-balances, a Withdraw/Swap quote form, the fee-breakdown panel, and a Swap button behind a confirm checkbox + red warning. All fetches go to same-origin `/api/*`. |
| `demo.py` | A minimal CLI (`Wallet.from_env()`): prints balances + a quote with the fee breakdown; `--swap` does a real `swap()` + `wait()`. |

## Run the web app

```powershell
# From sdk/python
cd sdk\python

# 1. Install the SDK (dev build) + the demo's web deps (FastAPI/uvicorn are NOT SDK deps)
pip install maturin fastapi uvicorn
maturin develop                      # or: pip install dist\paymos_wallet-*.whl  (once the wheel is built)

# 2. Start the local server (prod by default — no env needed)
uvicorn examples.app:app --reload    # open http://127.0.0.1:8000
```

Then in the browser:

1. In the wallet **Mini App → Settings → Vault API → Create key → copy** the `vs_live_…`.
   Use a **read** key to just click around (balances + quotes); a **full** key to swap.
2. **Paste** the secret into the page. It is sent only to your local backend.
3. Click **Load balances** and **Get quote** — these run live against prod today.
4. To swap: fill the **Swap** fields, tick the confirm box, click **Swap (real money)**.

Point at a different host by setting `PAYMOS_BASE_URL` before `uvicorn`
(e.g. `$env:PAYMOS_BASE_URL = "https://staging.example.com"`).

## Run the CLI

```powershell
cd sdk\python
$env:PAYMOS_VAULT_SECRET = "vs_live_…"     # created in the Mini App (Settings → Vault API)
# optional: $env:PAYMOS_BASE_URL = "https://wallet.paymos.io"   # default is prod

python examples\demo.py                      # balances + a dry quote with the fee breakdown
python examples\demo.py --swap               # REAL on-chain swap + wait()  (funded full key only)
```

`demo.py` prints the fee breakdown as clearly labeled raw-unit lines:

```
Fee breakdown (in USDC@base, raw units):
  platform : <raw>
  network  : <raw>
  route    : <raw>  (estimate: <bool>)      # or "route: —" when there is no route leg
  ─────────
  total    : <raw>
  ~usd     : platform <…> / network <…> / route <…> / total <…>   # if priced
```

## The fee breakdown (the headline)

`Quote.fees` is a `Fees` dataclass: `asset` (the `SYMBOL@chain` the fees are
denominated in), `platform`, `network`, `total` (raw integer **strings**), `route`
(a `RouteFee` with `.amount` + `.estimate`, or `None`), and `usd` (a nullable dict of
nullable strings). Amounts are **raw smallest-unit strings** — the web panel and the
CLI both label the unit and show the raw string as the source of truth; the web panel
additionally shows an optional human value scaled by the asset's decimals (from
`balances()`), computed with BigInt so no amount is ever coerced through a float.

## Live-against-prod reality

- **Read + quote works against prod TODAY.** A **read**-scope `vs_live_` (or a full
  one) → balances + the fee breakdown, clickable right now. This is the live proof of
  the SDK and of the headline feature.
- The **real swap** additionally needs: **(a)** the server **redeployed** with the
  `sources` field (the co-sign reads `quote.sources`), and **(b)** a **funded vault +
  a full key**. Until that redeploy, `/api/swap` (and `demo.py --swap`) will fail at
  the co-sign; the read/quote demo is fully live in the meantime.

## Acceptance gate

**GREEN only when a real swap settles to `completed`** — via the page's Swap button or
`python examples/demo.py --swap`, once the server is redeployed (the `sources` field)
and the vault is funded with a full key. Until then, the live read + quote path is the
proof and the real swap is the final manual step.

## Security — run this LOCALLY

- The secret you paste is sent to **your local backend only**. It is **never persisted,
  logged, or echoed** — `app.py` builds a fresh `Wallet` per request and closes it.
- A **full** secret carries **spend power** inside this process. **Never host this app
  publicly**, and never paste a full secret into a remote copy of it.
- A **read** key is identity-only (no share, cannot co-sign) — safe to click around with.
