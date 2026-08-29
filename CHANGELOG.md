# Changelog

## 0.1.4 (2026-07-09)

### Docs
- README: added a **"Get access & your API key"** section — how to open the Paymos wallet
  (Telegram Mini App) via the bot and mint a `vs_live_…` key under **Settings → Vault API**.
  No code, API, or wire change from 0.1.3.

## 0.1.3 (2026-07-09)

### Changed
- Internal renaming/cleanup of the signed-message digest module and its docstrings. No public API,
  behavior, or wire change — the digest bytes are identical (the cross-language golden vector still
  passes), so existing integrations upgrade transparently.

### Docs
- Rewrote the README (the PyPI page) into a full reference: install, vault-secret setup, quickstart,
  core concepts (amounts, quotes/fees, moving money, withdraw modes, safety caps, idempotency),
  the complete `Wallet` API + data models, the typed error hierarchy, and the security model.

## 0.1.2 (2026-07-07)

Closes the blind-sign gap (audit #5). **Ship order matters: deploy the server that discloses the
signing messages BEFORE publishing 0.1.2** — this release fails closed if the server does not disclose
them (that's the point), so it will not work against a pre-#5 server.

### Fixed
- **`withdraw(mode="total_in")` was 100% broken in 0.1.1** — the pre-sign echo check pinned the *send*
  leg against the typed total, but the server returns `send = total − fees`, so every `total_in`
  withdraw raised `PaymosError` (and orphaned a movement). It now verifies the **debit** leg (the debit
  must not exceed the typed total; a rounding-unit under is fine).

### Added
- **`max_debit` cap** on `withdraw` (and `swap`) — an optional human-decimal ceiling on the total vault
  debit, enforced before co-sign. Recommended for unattended `exact_out` payouts, whose input side is
  server-computed and otherwise unbounded (a dislocated ExactOutput quote could debit far more than the
  delivered amount). No cap → unchanged behavior.
- **Blind-sign guard.** Before producing any FROST share, `swap`/`withdraw` now verify each source the
  server asks the vault to sign. The server discloses, per source, the plaintext message; the
  SDK (1) recomputes the digest and confirms the signing package binds to *exactly* that message — the
  server cannot disclose one message and sign another; (2) confirms it is a single `transfer` intent
  whose `signer_id` is *this vault's* group key — never sign from another vault; and (3) confirms the
  sources together move no more than the approved `debit` — the server cannot inflate the amount. Any
  mismatch raises `PaymosError` and no share is produced. This restores the "neither half alone / a
  compromised server cannot redirect funds" guarantee that the 0.1.1 quote echo-check only partially
  covered.
- `paymos._digest.payload_digest` — a byte-exact port of the server's signed-message digest
  (length-prefixed encoding + sha256), pinned to the server by a cross-language golden vector.

## 0.1.1 (2026-07-07, yanked)

Correctness + safety release. **0.1.0 should be yanked from PyPI** — its money methods
never worked against prod and, from a sufficiently funded vault, could have signed an
over-sized movement (see the P0 below).

### Fixed
- **P0 — money amounts were double-scaled (`swap`/`withdraw`/`quote_*` were non-functional).**
  The SDK pre-scaled `amount` to raw with `parse_units` before sending, but the server's
  contract is **human-decimal in** — it scales the amount itself. Every money call was
  `10^decimals` too large, which failed closed as "No liquidity" on prod, and — worse —
  from a vault holding ≥ `amount × 10^decimals` would have built and co-signed a real
  over-payout. The SDK now sends the **human decimal string** on the wire (it still
  validates it locally) and the response contract is unchanged (raw integer strings).
- Requests now carry human decimals; responses/dataclasses remain raw. Docs, README,
  module docstrings and `demo.py` corrected to state this asymmetric contract (they
  previously claimed "raw on the wire", the exact regression vector for the P0).
- `format_units` now handles negative raw values instead of emitting garbage.

### Added
- **Pre-sign echo check.** `swap`/`withdraw` verify the server-returned quote echoes the
  exact assets and the amount you approved *before* any FROST commitment. A units bug,
  contract drift, or a lying/compromised server raises `PaymosError` and **signs nothing**.
- **`idempotency_key` parameter** on `swap`/`withdraw`. Reuse a durable key (e.g. your
  payout id) so a retry after an ambiguous failure **replays the same movement** instead
  of paying twice; a retry that finds the movement already signed/relayed **converges** on
  it rather than raising.
- **Explicit HTTP timeout** (`Timeout(60s, connect=10s)`, overridable via
  `Wallet(..., timeout=...)`). The multi-round co-sign outlives httpx's 5s default.
- Transport errors (timeout/DNS/reset) are wrapped into typed `PaymosError` (`status=None`)
  so the money path never surfaces a raw `httpx` exception.
- `RouteUnavailable` now actually maps the server's route/liquidity rejections
  ("try again in a moment" / "liquidity").
- `Wallet` is an async context manager (`async with Wallet.from_env() as w:`);
  `aclose()` fully releases the client (a later call transparently re-opens).

### Verified
- Full 2-of-2 FROST co-sign exercised end-to-end against `https://wallet.paymos.io` with a
  real full-scope key: a tiny in-vault swap went `processing → completed` and balances
  moved exactly as quoted.

## 0.1.0

Initial release. **Do not use** — see the 0.1.1 P0.
