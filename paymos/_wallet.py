"""``Wallet`` — the read / quote / move surface of the Paymos SDK.

Construct with a ``vs_live_`` vault secret. Only the secret's ``api_key`` ever
reaches the wire; the co-signing FROST share is held for the sign flow and NEVER
goes to :class:`~paymos._http.Http` — it only feeds :mod:`paymos._frost` locally.

Read / preview: :meth:`assets`, :meth:`balances`, :meth:`quote_swap`,
:meth:`quote_withdraw`, :meth:`movement`, :meth:`movements`. Move money (full-scope
secret only): :meth:`swap`, :meth:`withdraw` create a real movement and drive the
2-of-2 co-sign; :meth:`wait` polls a movement to a terminal status.

Money safety: the public ``quote_*`` methods ALWAYS send ``dry=true`` — a pure
fee-breakdown preview that persists nothing and works for read and full keys
alike. :meth:`swap` / :meth:`withdraw` reuse the private :meth:`_quote` with
``dry=False`` + an ``Idempotency-Key`` to create the movement, then co-sign it.

Amounts are HUMAN decimal strings both in the SDK API and on the REQUEST wire — the
server owns the asset decimals and scales the amount to raw itself, so the SDK must
NOT pre-scale (doing so double-scales by 10^decimals). RESPONSE amounts (balances,
quote send/debit/receive/fees) are raw integer strings. Never a float.

Before any signature, :meth:`swap` / :meth:`withdraw` run :meth:`_verify_quote_echo`:
the server-returned quote MUST echo the exact assets + amount the caller approved, or
nothing is signed. That turns contract drift or a quote that disagrees with the request
into a clean refusal. It checks assets and amounts only: the destination is chosen by the
server and is not checked, and an ``exact_out`` debit is server-computed unless the caller
caps it with ``max_debit``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any, Iterable, Mapping
from uuid import uuid4

from . import _frost
from . import _digest
from ._amounts import parse_units
from ._http import Http
from ._secret import VaultSecret
from ._types import Asset, Balance, Movement, Quote
from .errors import PaymosError, SlippageExceeded

# A movement is settled (no more polling) once it reaches one of these.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "refunded", "expired", "cancelled"})

# A movement past "pending" is already committed to the co-sign / relay pipeline. If a
# retry (same durable Idempotency-Key) replays the SAME movement in one of these states,
# converge on it rather than trying to sign a second time.
_PROGRESSED_STATUSES = frozenset({"processing", "completed", "refunded"})

# A disclosed per-source transfer amount: raw, unsigned, ASCII digits only (re's \d would admit
# any Unicode digit, and ``$`` a trailing newline — hence the explicit class and ``fullmatch``).
_RAW_AMOUNT = re.compile(r"[0-9]+")

_DEFAULT_BASE_URL = "https://wallet.paymos.io"
_ENV_SECRET = "PAYMOS_VAULT_SECRET"
_ENV_BASE_URL = "PAYMOS_BASE_URL"

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def _normalize_pins(pins: Mapping[str, str | Iterable[str]] | None) -> dict[str, frozenset[str]]:
    """``pinned_fingerprints`` as ``{label: {lowercase hex sha256, ...}}``. A malformed pin is a
    configuration error and fails at construction with :class:`PaymosError` — never silently at
    the first payout."""
    out: dict[str, frozenset[str]] = {}
    for label, value in (pins or {}).items():
        try:
            values = [value] if isinstance(value, str) else list(value)
        except TypeError:
            values = []  # not a string, not iterable: malformed, reported below
        if not values or not all(isinstance(v, str) for v in values):
            values = []  # nothing, or a non-string among the pins: malformed, reported below
        norm = frozenset(v.strip().lower() for v in values)
        if not norm or not all(_FINGERPRINT.fullmatch(v) for v in norm):
            raise PaymosError(
                f"pinned_fingerprints[{label!r}] must be one or more 64-char hex sha256 fingerprints"
            )
        out[label] = norm
    return out


class Wallet:
    """A handle to one vault, bound to the key inside its ``vs_live_`` secret."""

    def __init__(
        self,
        secret: str,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        timeout: Any = None,
        pinned_fingerprints: Mapping[str, str | Iterable[str]] | None = None,
    ) -> None:
        """``pinned_fingerprints`` maps an asset label (``"USDC@base"``) to the fingerprint — or
        several — you obtained out of band (lowercase hex sha256 of the raw token id). A pinned
        label is checked ONLY against its pins, whatever ``/assets`` publishes; unpinned labels use
        the published catalog. It gives the co-sign's token check a trust anchor independent of
        the server that also builds the messages being signed."""
        self._secret = VaultSecret.parse(secret)
        self._http = Http(base_url, self._secret.api_key, timeout=timeout)
        self._pins = _normalize_pins(pinned_fingerprints)
        # Catalog cache: the Asset list + an {asset_id: decimals} map, loaded once.
        self._assets: list[Asset] | None = None
        self._decimals_by_asset: dict[str, int] = {}

    def __repr__(self) -> str:
        # Useful without ever surfacing the secret or the FROST share: a stray
        # print(wallet) / traceback must not leak either.
        return f"Wallet(vault_id={self._secret.vault_id}, has_share={self._secret.has_share}, base_url={self._http._base_url!r})"

    @classmethod
    def from_env(
        cls,
        base_url: str | None = None,
        *,
        timeout: Any = None,
        pinned_fingerprints: Mapping[str, str | Iterable[str]] | None = None,
    ) -> "Wallet":
        """Build a wallet from the environment.

        Reads the vault secret from ``PAYMOS_VAULT_SECRET`` (required — raises
        :class:`PaymosError` if unset). The base URL resolves as: an explicit
        ``base_url`` argument, else ``PAYMOS_BASE_URL``, else the built-in default
        (``https://wallet.paymos.io``)."""
        secret = os.environ.get(_ENV_SECRET)
        if not secret:
            raise PaymosError(f"{_ENV_SECRET} is not set")
        base_url = base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL
        return cls(secret, base_url, timeout=timeout, pinned_fingerprints=pinned_fingerprints)

    async def __aenter__(self) -> "Wallet":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # --- catalog / decimals --------------------------------------------------

    async def assets(self) -> list[Asset]:
        """The curated asset catalog (``SYMBOL@chain`` ids + decimals).

        Fetched once and cached; the decimals map is populated from the same call
        so an amount conversion never guesses or refetches."""
        if self._assets is None:
            raw = _bare_array(await self._http.get("/vault/v1/assets"), "/assets")
            assets = [Asset.from_dict(a) for a in raw]
            self._assets = assets
            # One label can appear more than once (two contracts of one coin); the server resolves a
            # label to its FIRST catalog entry, so the decimals must come from the first one too — a
            # dict comprehension would keep the last.
            decimals: dict[str, int] = {}
            for a in assets:
                decimals.setdefault(a.asset, a.decimals)
            self._decimals_by_asset = decimals
        return self._assets

    async def _decimals(self, asset_id: str) -> int:
        """Decimals for ``asset_id``, lazy-loading the catalog if needed. Raises
        :class:`PaymosError` for an unknown asset — never guesses decimals."""
        if self._assets is None:
            await self.assets()
        try:
            return self._decimals_by_asset[asset_id]
        except KeyError:
            raise PaymosError(f"unknown asset {asset_id}") from None

    async def _fingerprints(self, asset_id: str) -> frozenset[str]:
        """Every fingerprint a disclosed token of ``asset_id`` may hash to.

        A pinned label answers with its pins alone (the integrator vouches for them). Otherwise:
        the fingerprints ``/assets`` publishes under the label — the catalog can list one label
        twice (two contracts of one coin) — but only from entries whose ``decimals`` equal the
        FIRST entry's. The debit the guard bounds amounts by is in the first entry's units; a token
        with other decimals would compare raw units worth 10^Δ more against it. Empty when the
        server publishes none; the guard refuses on empty."""
        if asset_id in self._pins:
            return self._pins[asset_id]
        decimals = await self._decimals(asset_id)  # the first entry's, as the server resolves it
        return frozenset(
            a.fingerprint.lower()
            for a in await self.assets()
            if a.asset == asset_id and a.fingerprint and a.decimals == decimals
        )

    async def _validate_amount(self, amount: str, asset: str) -> int:
        """Validate a HUMAN decimal ``amount`` against ``asset``'s decimals WITHOUT
        scaling it for the wire (the server scales). Returns the asset's decimals so
        the caller can compute the expected raw for the echo check. Raises
        :class:`PaymosError` on an unknown asset or a malformed amount (a sign, junk,
        or more fractional digits than the asset supports)."""
        decimals = await self._decimals(asset)
        try:
            parse_units(amount, decimals)
        except ValueError as e:
            raise PaymosError(str(e)) from e
        return decimals

    # --- balances ------------------------------------------------------------

    async def balances(self) -> list[Balance]:
        """The caller's per-asset vault balances (raw amounts + nullable USD)."""
        raw = _bare_array(await self._http.get("/vault/v1/balances"), "/balances")
        return [Balance.from_dict(b) for b in raw]

    # --- quotes --------------------------------------------------------------

    async def _quote(
        self,
        path: str,
        body: dict[str, Any],
        *,
        dry: bool,
        idempotency_key: str | None = None,
    ) -> Quote:
        """POST a quote request and map the response to a :class:`Quote`.

        Merges ``{"dry": dry}`` into ``body``. When ``idempotency_key`` is set it's
        passed as the ``Idempotency-Key`` header so a retry after a dropped 200 replays
        the SAME movement instead of double-spending. The public ``quote_*`` methods
        call this with ``dry=True`` and no key — a money-safe preview.

        ``body["amount"]`` is the HUMAN decimal string (the server scales it); the SDK
        must not pre-scale it to raw."""
        payload = {**body, "dry": dry}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        data = await self._http.post(path, payload, headers=headers)
        return Quote.from_dict(data)

    async def quote_swap(
        self,
        send: str,
        receive: str,
        amount: str,
        slippage_bps: int = 50,
    ) -> Quote:
        """Preview a cross-asset swap (delivered back to the caller's own vault).

        ``amount`` is a HUMAN decimal in the ``send`` asset (validated against that
        asset's decimals; the server scales it to raw). Always a ``dry=true`` preview."""
        await self._validate_amount(amount, send)
        body = {
            "send": send,
            "receive": receive,
            "amount": amount,
            "mode": "exact_in",  # the only swap mode; sent explicitly, as the recorded request does
            "slippage_bps": slippage_bps,
        }
        return await self._quote("/vault/v1/quote/swap", body, dry=True)

    async def quote_withdraw(
        self,
        asset: str,
        amount: str,
        to: str,
        mode: str = "exact_out",
    ) -> Quote:
        """Preview a same-asset withdraw (payout) to an external chain address.

        ``amount`` is a HUMAN decimal in ``asset`` (validated against that asset's
        decimals; the server scales it to raw). ``receive`` is omitted → same-asset
        payout. Always a ``dry=true`` preview."""
        await self._validate_amount(amount, asset)
        body = {
            "asset": asset,
            "amount": amount,
            "to": to,
            "mode": mode,
        }
        return await self._quote("/vault/v1/quote/withdraw", body, dry=True)

    # --- move money (full-scope secret only) ---------------------------------

    def _require_share(self, action: str) -> None:
        """Guard the sign flow: a read-only secret carries no share and can never
        co-sign. Fails BEFORE any quote so a read key never creates a movement."""
        if not self._secret.has_share:
            raise PaymosError(
                f"read-only key: {action} needs a full-scope vault secret with the share"
            )

    async def swap(
        self,
        send: str,
        receive: str,
        amount: str,
        slippage_bps: int = 50,
        min_receive: str | None = None,
        *,
        max_debit: str | None = None,
        idempotency_key: str | None = None,
    ) -> Movement:
        """Swap ``amount`` of ``send`` for ``receive``, delivered back to this vault.

        Creates a real movement (non-dry quote) and drives the 2-of-2 co-sign, then
        returns the resulting :class:`Movement`. ``amount`` is a HUMAN decimal in the
        ``send`` asset. When ``min_receive`` is set (human decimal in the ``receive``
        asset) and the quote's GUARANTEED ``receive.min`` is below it, raises
        :class:`SlippageExceeded` and signs nothing.

        Pass your own ``idempotency_key`` (e.g. a durable job id) to make a retry after
        an ambiguous failure replay the SAME movement instead of paying twice; omitted,
        a fresh key is generated per call. Requires a full-scope secret."""
        self._require_share("swap")
        send_dec = await self._validate_amount(amount, send)
        recv_dec = await self._decimals(receive)  # validates the receive asset is known
        body = {
            "send": send,
            "receive": receive,
            "amount": amount,
            "mode": "exact_in",  # the only swap mode; sent explicitly, as the recorded request does
            "slippage_bps": slippage_bps,
        }
        q = await self._quote(
            "/vault/v1/quote/swap", body, dry=False, idempotency_key=idempotency_key or uuid4().hex
        )
        if min_receive is not None:
            floor = int(parse_units(min_receive, recv_dec))
            if int(q.receive.min) < floor:
                raise SlippageExceeded(
                    f"guaranteed receive {q.receive.min} is below the requested "
                    f"minimum {floor} ({min_receive} {receive})"
                )
        # Safety gate: the server's quote must echo exactly what we approved (a swap is
        # exact_in, so send.amount is the raw of `amount` in the send asset).
        self._verify_quote_echo(
            q, send_asset=send, receive_asset=receive,
            expect_send_raw=int(parse_units(amount, send_dec)),
        )
        self._enforce_max_debit(q, max_debit, send_dec)
        await self._cosign(q)
        return await self.movement(_require_movement_id(q))

    async def withdraw(
        self,
        asset: str,
        amount: str,
        to: str,
        mode: str = "exact_out",
        *,
        max_debit: str | None = None,
        idempotency_key: str | None = None,
    ) -> Movement:
        """Withdraw ``amount`` of ``asset`` to the external address ``to`` (same-asset
        payout). Creates a real movement and drives the 2-of-2 co-sign, then returns
        the resulting :class:`Movement`. ``amount`` is a HUMAN decimal in ``asset``;
        ``mode`` is ``"exact_out"`` (default — the recipient receives exactly ``amount``)
        or ``"total_in"`` (``amount`` is the total debited from the vault).

        ``max_debit`` (human decimal in ``asset``) caps the total vault debit: if the quote's debit
        exceeds it, this raises and signs nothing — strongly recommended for unattended ``exact_out``
        payouts, whose input side is server-computed. Pass your own ``idempotency_key`` (e.g. your
        payout id) to make a retry replay the SAME movement instead of paying twice. Requires a
        full-scope secret."""
        self._require_share("withdraw")
        dec = await self._validate_amount(amount, asset)
        body = {
            "asset": asset,
            "amount": amount,
            "to": to,
            "mode": mode,
        }
        q = await self._quote(
            "/vault/v1/quote/withdraw", body, dry=False, idempotency_key=idempotency_key or uuid4().hex
        )
        # Safety gate: a withdraw is same-asset. For exact_out the approved `amount` is the guaranteed
        # OUT; for total_in it is the total DEBIT — verify the debit leg (the server returns send =
        # total - fees, so pinning send would always false-fail), capped so it never exceeds the total.
        expect_raw = int(parse_units(amount, dec))
        if mode == "total_in":
            self._verify_quote_echo(q, send_asset=asset, receive_asset=asset, max_debit_raw=expect_raw)
        else:
            self._verify_quote_echo(q, send_asset=asset, receive_asset=asset, expect_receive_raw=expect_raw)
        self._enforce_max_debit(q, max_debit, dec)
        await self._cosign(q)
        return await self.movement(_require_movement_id(q))

    @staticmethod
    def _verify_quote_echo(
        quote: Quote,
        *,
        send_asset: str,
        receive_asset: str,
        expect_send_raw: int | None = None,
        expect_receive_raw: int | None = None,
        max_debit_raw: int | None = None,
    ) -> None:
        """Refuse to sign unless the server's quote echoes the assets and the amount the
        caller approved. A decimals/units bug, contract drift, or a quote whose assets or
        pinned amount differ from the request raises :class:`PaymosError` BEFORE any FROST
        commitment. What it pins: ``send.amount`` for a swap, ``receive.amount`` for an
        ``exact_out`` withdraw, a debit ceiling for ``total_in``. The ``exact_out`` debit itself
        is server-computed and only bounded by ``max_debit`` when the caller passes one; the
        destination address is not part of the quote and is not checked here."""
        if quote.send.asset != send_asset or quote.receive.asset != receive_asset:
            raise PaymosError(
                f"quote asset mismatch: approved {send_asset}->{receive_asset}, "
                f"quote {quote.send.asset}->{quote.receive.asset}; refusing to sign"
            )
        if expect_send_raw is not None and int(quote.send.amount) != expect_send_raw:
            raise PaymosError(
                f"quote amount mismatch: approved send {expect_send_raw}, "
                f"quote send {quote.send.amount}; refusing to sign"
            )
        if expect_receive_raw is not None and int(quote.receive.amount) != expect_receive_raw:
            raise PaymosError(
                f"quote amount mismatch: approved receive {expect_receive_raw}, "
                f"quote receive {quote.receive.amount}; refusing to sign"
            )
        # total_in: the vault debit must never EXCEED the caller-typed total (the server may debit
        # slightly less due to rounding, never more) — pinning the send leg would false-fail on fees.
        if max_debit_raw is not None and int(quote.debit.amount) > max_debit_raw:
            raise PaymosError(
                f"quote debit {quote.debit.amount} exceeds the approved total {max_debit_raw}; "
                f"refusing to sign"
            )

    @staticmethod
    def _enforce_max_debit(quote: Quote, max_debit: str | None, decimals: int) -> None:
        """Optional caller cap for unattended payouts: refuse to sign if the quote's total vault debit
        exceeds ``max_debit`` (human decimal in the source asset). Guards ``exact_out`` withdraws (and
        swaps), whose input side is server-computed and otherwise unbounded. No cap → no-op."""
        if max_debit is None:
            return
        cap = int(parse_units(max_debit, decimals))
        if int(quote.debit.amount) > cap:
            raise PaymosError(
                f"quote debit {quote.debit.amount} exceeds max_debit {cap} ({max_debit}); refusing to sign"
            )

    async def _cosign(self, quote: Quote) -> None:
        """Drive the 2-of-2 co-sign for a freshly-created movement.

        The SDK holds ONE key package (this vault's client share); it is reused for
        every source. For each of the quote's ``sources`` sources it commits with
        fresh nonces, ships the commitments to ``sign/begin``, signs each returned
        signing package, and ships the shares to ``sign/aggregate``. The share only
        ever feeds :mod:`paymos._frost` locally — never the wire.

        Idempotent-retry convergence: if ``sign/begin`` (round 1) is REJECTED because
        the movement already left the awaiting-signature state — a prior attempt with
        the same durable ``Idempotency-Key`` replayed the SAME movement and already
        signed/relayed it — and the movement has progressed past ``pending``, converge
        on it instead of raising (that is idempotent, not a double-sign). A round-2
        (``sign/aggregate``) failure is a genuine crypto/relay rejection and ALWAYS
        propagates — it is never swallowed."""
        mid = _require_movement_id(quote)
        n = quote.sources
        if not n or n < 1:
            # A real quote must report its source count; a missing/zero one is a
            # server/contract fault, not a signable movement.
            raise PaymosError("quote is missing its source count — cannot co-sign")

        kp = self._client_key_package()
        # Every source of a movement moves the SEND asset; the guard holds each disclosed token to it.
        send_fingerprints = await self._fingerprints(quote.send.asset)

        commitments: list[dict[str, Any]] = []
        nonces: list[dict[str, Any]] = []
        for _ in range(n):
            c, nn = _frost.commit(kp)
            commitments.append(c)
            nonces.append(nn)

        try:
            begin = await self._http.post(
                f"/vault/v1/movements/{mid}/sign/begin", {"commitments": commitments}
            )
        except PaymosError:
            mv = await self.movement(mid)
            if mv.status in _PROGRESSED_STATUSES:
                return  # already signed/relayed by a prior attempt — not a double-sign
            raise
        try:
            token = begin["token"]
            packages = list(begin["signing_packages"])
        except (KeyError, TypeError) as e:
            raise PaymosError(f"co-sign refused: malformed sign/begin response: {e!r}") from e

        # The server must return exactly one signing package per source we committed
        # to. A count mismatch means we'd silently under-/over-sign against nonces we
        # generated — fail loudly instead of proceeding on a broken protocol state.
        if len(packages) != n:
            raise PaymosError(
                f"co-sign protocol mismatch: expected {n} signing packages, got {len(packages)}"
            )

        # BLIND-SIGN GUARD — the server discloses each source's plaintext message; we recompute its
        # digest, confirm the package we are about to sign binds to EXACTLY that message, and check
        # it is one transfer FROM our own vault, of the send asset, for no MORE than the approved
        # debit. The transfer's recipient is server-chosen and not checked. Any mismatch raises and
        # signs nothing.
        self._verify_signing_disclosure(quote, packages, begin.get("messages"), kp, send_fingerprints)

        shares = [
            _frost.sign(packages[i], nonces[i], kp) for i in range(len(packages))
        ]

        res = await self._http.post(
            f"/vault/v1/movements/{mid}/sign/aggregate",
            {"token": token, "shares": shares},
        )
        if not res.get("ok"):
            raise PaymosError(res.get("error") or "sign failed")

    def _client_key_package(self) -> dict[str, Any]:
        """Parse the vault's client key package from the secret's share (a JSON
        string). Guarded by :meth:`_require_share` before any call reaches here."""
        assert self._secret.share is not None  # guaranteed by _require_share
        return json.loads(self._secret.share)

    @staticmethod
    def _verify_signing_disclosure(
        quote: Quote,
        packages: list[Any],
        messages: Any,
        kp: dict[str, Any],
        send_fingerprints: frozenset[str],
    ) -> None:
        """Refuse to blind-sign a server-chosen message (audit #5).

        The server returns, per source, the plaintext ``{message, nonce, recipient}``. For each we
        (1) recompute the digest and confirm the signing package's embedded ``message`` equals
        it — the server cannot disclose one message and sign another; (2) confirm the message is a
        single ``transfer`` intent whose ``signer_id`` is THIS vault's group key — never sign from
        another vault, moving exactly one token whose id hashes to one of ``send_fingerprints``;
        and (3) confirm the sources together move no MORE than the quote's approved debit. Any
        mismatch raises :class:`PaymosError` and the co-sign aborts before a single share is
        produced.

        Limits. (3) bounds the transfer by the APPROVED debit, and for ``exact_out`` that debit is
        itself server-computed unless the caller passed ``max_debit`` — so for unattended payouts
        pass ``max_debit``. The transfer's recipient (``receiver_id``, the route's deposit address)
        is chosen by the server and is NOT checked: a compromised server could direct the approved
        amount elsewhere.

        What (2)'s token check proves depends on where the fingerprints came from. From ``/assets``
        they come from the same server that builds the messages: the check catches a server bug or
        a compromised signing path, NOT a fully compromised server, which could publish a false
        fingerprint along with a false message. Pinned via ``pinned_fingerprints`` they are a trust
        anchor the server does not control."""
        if not isinstance(messages, list) or len(messages) != len(packages):
            raise PaymosError(
                "co-sign refused: the server did not disclose the signing messages — "
                "refusing to blind-sign (needs a current server)"
            )
        # Without the vault's own group key there is nothing to hold ``signer_id`` against, and an
        # unchecked signer is exactly "sign from another vault". Every key package the core's DKG
        # produces carries it; one that does not is refused rather than trusted.
        expected_signer = str(kp.get("verifying_key") or "").lower()
        if not expected_signer:
            raise PaymosError("co-sign refused: the vault key package has no verifying key to check the signer against")
        # Without a fingerprint for the send asset the token check below has nothing to compare
        # with. Skipping it would re-open "approve USDC, sign away something else" — refuse instead.
        if not send_fingerprints:
            raise PaymosError(
                "co-sign refused: the server did not publish an asset fingerprint — update the server"
            )
        want_fingerprints = frozenset(f.lower() for f in send_fingerprints)
        total = 0
        for i, (pkg, disc) in enumerate(zip(packages, messages)):
            try:
                message = disc["message"]
                nonce = bytes.fromhex(disc["nonce"])
                recipient = disc["recipient"]
            except (KeyError, TypeError, ValueError) as e:
                raise PaymosError(f"co-sign refused: malformed disclosure for source {i}: {e}") from e

            # (1) binding: the package we are about to sign hashes to EXACTLY the disclosed message.
            # The digest rejects a nonce that isn't 32 bytes with a ValueError; that is a malformed
            # disclosure too, and must refuse as a PaymosError like every other one.
            try:
                want = _digest.payload_digest_hex(message, nonce, recipient)
            except (TypeError, ValueError) as e:
                raise PaymosError(f"co-sign refused: malformed disclosure for source {i}: {e}") from e
            got = pkg.get("message") if isinstance(pkg, dict) else None
            if not isinstance(got, str) or got.lower() != want:
                raise PaymosError(
                    f"co-sign refused: signing package {i} does not bind to the disclosed message"
                )

            # (2) structure + own-vault: a single transfer intent signed FROM this vault.
            try:
                m = json.loads(message)
                intents = m["intents"]
                signer_id = str(m["signer_id"]).lower()
                one = intents[0]
                tokens = one["tokens"]
            except (KeyError, IndexError, TypeError, ValueError) as e:
                raise PaymosError(f"co-sign refused: unreadable message for source {i}: {e}") from e
            if len(intents) != 1 or one.get("intent") != "transfer":
                raise PaymosError(f"co-sign refused: source {i} is not a single transfer intent")
            if signer_id != expected_signer:
                raise PaymosError(f"co-sign refused: source {i} signs from a different vault")
            if not isinstance(tokens, dict) or len(tokens) != 1:
                raise PaymosError(f"co-sign refused: source {i} does not transfer exactly one token")
            token_id, amount = next(iter(tokens.items()))
            # The token moved must be the send asset: sha256(utf-8 token id) is one of its fingerprints.
            if (not isinstance(token_id, str)
                    or hashlib.sha256(token_id.encode("utf-8")).hexdigest() not in want_fingerprints):
                raise PaymosError(
                    f"co-sign refused: source {i} moves a token that is not the approved send asset "
                    f"{quote.send.asset}"
                )
            # Each source's amount must be a positive ASCII-digit string — exactly what the server
            # builds. ``int()`` alone accepts "-900", "+5", " 5", "1_000" and non-ASCII digits: a
            # negative source B = -D would let source A move 2D while the SUM still passes (3).
            if not isinstance(amount, str) or not _RAW_AMOUNT.fullmatch(amount) or int(amount) == 0:
                raise PaymosError(
                    f"co-sign refused: source {i} transfer amount {amount!r} is not a positive integer string"
                )
            total += int(amount)

        # (3) no inflation: the sources together move no more than the approved debit.
        try:
            approved = int(quote.debit.amount)
        except (ValueError, AttributeError) as e:
            raise PaymosError(f"co-sign refused: cannot read the approved debit: {e}") from e
        if total > approved:
            raise PaymosError(
                f"co-sign refused: sources move {total}, above the approved debit {approved}"
            )

    # --- movements / history -------------------------------------------------

    async def movement(self, movement_id: str) -> Movement:
        """One movement in full (with the fee breakdown): ``GET /movements/{id}``."""
        data = await self._http.get(f"/vault/v1/movements/{movement_id}")
        return Movement.from_dict(data)

    async def movements(
        self, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Movement], str | None]:
        """A page of the caller's movement history (newest first, no fee legs).

        Returns ``(items, next_cursor)``; pass ``next_cursor`` back as ``cursor`` for
        the next page (``None`` when exhausted)."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._http.get("/vault/v1/movements", params=params)
        items = [Movement.from_dict(m) for m in data["items"]]
        return items, data.get("next_cursor")

    async def wait(
        self, movement_id: str, timeout: float = 120, poll: float = 2.0
    ) -> Movement:
        """Poll a movement until it reaches a terminal status, then return it.

        Terminal = ``completed | failed | refunded | expired | cancelled``. Raises
        :class:`PaymosError` if ``timeout`` seconds elapse first."""
        deadline = time.monotonic() + timeout
        while True:
            mv = await self.movement(movement_id)
            if mv.status in _TERMINAL_STATUSES:
                return mv
            if time.monotonic() >= deadline:
                raise PaymosError(f"timed out waiting for movement {movement_id}")
            await asyncio.sleep(poll)

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._http.aclose()


def _bare_array(data: Any, endpoint: str) -> list[Any]:
    """``/assets`` and ``/balances`` answer a bare JSON array (sdk/contract/fixtures). Anything
    else is a contract fault: iterating an object would walk its KEYS and fail somewhere far
    from the cause, and seven SDKs once read ``{"assets": [...]}`` from here — refuse loudly."""
    if not isinstance(data, list):
        raise PaymosError(f"{endpoint} answered {type(data).__name__}, expected a JSON array")
    return data


def _require_movement_id(quote: Quote) -> str:
    """The movement id of a real (non-dry) quote. A missing one means the server
    returned a preview where a created movement was expected — a contract fault."""
    if not quote.movement_id:
        raise PaymosError("quote created no movement — cannot sign or fetch it")
    return quote.movement_id
