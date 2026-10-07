"""FROST client-role co-signing + swap / withdraw / wait orchestration.

Three layers:

1. **Native round** — drive the FULL 2-of-2 protocol through ``paymos._core``
   (``dkg_part1/2/3`` for BOTH parties → ``commit`` → ``build_signing_package``
   → ``sign`` → ``aggregate``), mirroring the crate's own vector at
   ``mpc/wallet-mpc/src/lib.rs:234-250``. This proves the Python↔native bridge
   signs — a REAL, verified ed25519 signature, not a stub. The client's real
   ``key_package`` from this DKG becomes the fixture the orchestration test signs
   with, so the client's ``frost.sign`` succeeds cryptographically end to end.

2. **Orchestration** — ``swap`` / ``withdraw`` against an ``httpx.MockTransport``
   that returns a NON-dry quote (with ``sources``), then per-source real
   ``signing_packages`` built via ``_core`` from the client's fixture material, so
   the SDK's commit→sign/begin→sign→sign/aggregate loop runs for real. Asserts the
   wire shapes: ``dry:false`` + an ``Idempotency-Key`` on the quote; exactly
   ``sources`` commitments to ``sign/begin``; matching ``shares`` to
   ``sign/aggregate``; a ``Movement`` back from ``GET /movements/{id}``.

3. **Guards** — a read-only secret rejects ``swap`` / ``withdraw`` BEFORE any
   quote; a ``min_receive`` floor above the quote's guaranteed ``receive.min``
   raises ``SlippageExceeded`` and never POSTs ``sign/begin``.
"""

import hashlib
import json

import httpx
import pytest

from paymos import _core
from paymos._frost import commit, sign
from paymos._digest import payload_digest_hex
from paymos._secret import VaultSecret
from paymos._types import Movement, Quote
from paymos._wallet import Wallet
from paymos.errors import PaymosError, SlippageExceeded


# --- native helpers ----------------------------------------------------------

def _call(req: dict) -> dict:
    """One op through the native core; asserts ``ok`` and returns the fields."""
    out = _core.mpc_call(json.dumps(req))
    assert isinstance(out, str)
    r = json.loads(out)
    assert r.get("ok") is True, f"native op failed: {r}"
    return r


def _dkg_2of2() -> dict:
    """Run a full 2-of-2 DKG through ``_core`` and return both key packages + the
    shared public key package (the ground-truth vault key material)."""
    a1 = _call({"op": "dkg_part1", "id": 1, "max": 2, "min": 2})
    b1 = _call({"op": "dkg_part1", "id": 2, "max": 2, "min": 2})
    a2 = _call({"op": "dkg_part2", "secret": a1["secret"], "round1_packages": [[2, b1["package"]]]})
    b2 = _call({"op": "dkg_part2", "secret": b1["secret"], "round1_packages": [[1, a1["package"]]]})
    r2_for_a = next(x for x in b2["packages"] if x[0] == 1)[1]
    r2_for_b = next(x for x in a2["packages"] if x[0] == 2)[1]
    a3 = _call({"op": "dkg_part3", "secret": a2["secret"],
                "round1_packages": [[2, b1["package"]]], "round2_packages": [[2, r2_for_a]]})
    b3 = _call({"op": "dkg_part3", "secret": b2["secret"],
                "round1_packages": [[1, a1["package"]]], "round2_packages": [[1, r2_for_b]]})
    assert a3["group_pubkey_hex"] == b3["group_pubkey_hex"], "DKG must agree on one group key"
    return {
        "client_kp": a3["key_package"],       # id 1 — the SDK's share
        "server_kp": b3["key_package"],       # id 2 — the co-signer
        "public_key_package": a3["public_key_package"],
        "group_pubkey_hex": a3["group_pubkey_hex"],
    }


# One DKG for the whole module (heavy-ish; reuse the client share as the fixture).
_KEYS = _dkg_2of2()
_CLIENT_KP = _KEYS["client_kp"]
_CLIENT_KP_JSON = json.dumps(_CLIENT_KP)

# The vault's group verifying key — the implicit account the SDK's blind-sign guard requires every
# disclosed transfer to be signed FROM. The mock "server" builds its disclosed messages with this as
# signer_id so an honest co-sign passes the guard.
_VK = _CLIENT_KP["verifying_key"]
_RECIPIENT = "vault.example"


def _transfer_message(amount_raw, signer_id=_VK, token="usdc-base"):
    # A minimal single "transfer" intent the SDK's disclosure guard accepts (shape + signer_id + one
    # token whose id hashes to USDC@base's catalog fingerprint). Built by hand so the exact utf-8
    # bytes are what the digest is taken over.
    return ('{"deadline":"2026-07-07T00:00:00.000Z","intents":[{"intent":"transfer",'
            '"receiver_id":"acct.example","tokens":{"' + token + '":"' + str(amount_raw) + '"}}],'
            '"signer_id":"' + signer_id + '"}')


def _nonce(i):
    return bytes((i + k) % 256 for k in range(32))


def _server_countersign(commitment: dict, message_hex: str) -> tuple[dict, dict]:
    """The SERVER side of one signing session, run in-test via ``_core`` (this is
    what the real Vault API server does). Given the client's commitment, the server
    commits, builds the signing package over ``message_hex``, and produces its own
    share. Returns ``(signing_package, server_signature_share)`` — the pieces the
    stub hands back so the client's ``sign`` verifies against a real 2-of-2 session.
    """
    sc = _call({"op": "commit", "key_package": _KEYS["server_kp"]})
    sp = _call({"op": "build_signing_package",
                "commitments": [[1, commitment], [2, sc["commitments"]]],
                "message_hex": message_hex})["signing_package"]
    server_share = _call({"op": "sign", "signing_package": sp,
                          "nonces": sc["nonces"], "key_package": _KEYS["server_kp"]})["signature_share"]
    return sp, server_share


# --- canned quote / movement wire (snake_case, WhenWritingNull, Sources set) --

def _swap_quote(movement_id="mv_1", sources=1):
    return {
        "movement_id": movement_id,
        "mode": "exact_in",
        "send": {"amount": "5000000", "asset": "USDC@base"},
        "debit": {"amount": "5012000", "asset": "USDC@base"},
        "receive": {"amount": "1600000000000000", "min": "1580000000000000", "asset": "ETH@arb"},
        "fees": {
            "asset": "USDC@base", "platform": "5000", "network": "2000",
            "route": {"amount": "3000", "estimate": True}, "total": "10000",
            "usd": {"platform": "0.005", "network": "0.002", "route": "0.003", "total": "0.01"},
        },
        "expires_at": "2026-07-06T12:05:00+00:00",
        "sources": sources,
    }


def _withdraw_quote(movement_id="mv_w", sources=1):
    return {
        "movement_id": movement_id,
        "mode": "exact_out",
        "send": {"amount": "10000000", "asset": "USDC@base"},
        "debit": {"amount": "10012000", "asset": "USDC@base"},
        "receive": {"amount": "10000000", "min": "10000000", "asset": "USDC@base"},
        "fees": {
            "asset": "USDC@base", "platform": "10000", "network": "2000",
            "route": {"amount": "0", "estimate": False}, "total": "12000",
            "usd": {"platform": "0.01", "network": "0.002", "route": "0", "total": "0.012"},
        },
        "expires_at": "2026-07-06T12:00:00+00:00",
        "sources": sources,
    }


def _withdraw_total_in_quote(movement_id="mv_ti", sources=1):
    # A REALISTIC total_in withdraw of "10" USDC: the caller types the TOTAL debit (10000000), and the
    # server returns send = total - fees (< total) and debit == the typed total. Pinning the send leg
    # (the 0.1.1 bug) would false-fail here; the fix verifies the debit leg.
    return {
        "movement_id": movement_id,
        "mode": "total_in",
        "send": {"amount": "9988000", "asset": "USDC@base"},
        "debit": {"amount": "10000000", "asset": "USDC@base"},
        "receive": {"amount": "9988000", "min": "9988000", "asset": "USDC@base"},
        "fees": {
            "asset": "USDC@base", "platform": "10000", "network": "2000",
            "route": {"amount": "0", "estimate": False}, "total": "12000",
            "usd": {"platform": "0.01", "network": "0.002", "route": "0", "total": "0.012"},
        },
        "expires_at": "2026-07-06T12:00:00+00:00",
        "sources": sources,
    }


def _movement(mid, status="completed", typ="swap"):
    return {
        "id": mid, "type": typ, "status": status,
        "send": {"amount": "5000000", "asset": "USDC@base"},
        "receive": {"amount": "1600000000000000", "min": "1580000000000000", "asset": "ETH@arb"},
        "fees": None,
        "dest_chain_tx_hash": "0xdeadbeef" if status == "completed" else None,
        "dest_chain_explorer_url": "https://explorer/tx/0xdeadbeef" if status == "completed" else None,
        "created_at": "2026-07-06T12:00:00+00:00",
        "completed_at": "2026-07-06T12:01:00+00:00" if status == "completed" else None,
    }


def _fp(token_id: str) -> str:
    """An asset fingerprint as the server publishes it: lowercase hex sha256 of the raw token id."""
    return hashlib.sha256(token_id.encode("utf-8")).hexdigest()


ASSETS = [
    {"asset": "USDC@base", "symbol": "USDC", "chain": "base", "decimals": 6,
     "min_deposit_raw": None, "min_withdraw_raw": None, "fingerprint": _fp("usdc-base")},
    {"asset": "ETH@arb", "symbol": "ETH", "chain": "arb", "decimals": 18,
     "min_deposit_raw": None, "min_withdraw_raw": None, "fingerprint": _fp("eth-arb")},
]


def _full_secret():
    """A vs_live_ secret carrying the real client key_package (a full-scope key)."""
    return VaultSecret.pack("vk_live_test", 1, _CLIENT_KP_JSON)


def _read_secret():
    return VaultSecret.pack("vk_live_test", 1, None)


class _Server:
    """A MockTransport server that drives a REAL co-sign: it captures every POST,
    countersigns each commitment via ``_core`` so the returned signing packages are
    genuine, and verifies the aggregate of the client's returned shares + its own."""

    def __init__(self, quote, movement_id, *, message_hex="deadbeef"):
        self.quote = quote
        self.movement_id = movement_id
        self.message_hex = message_hex
        self.posts = {}            # path -> parsed body
        self.headers = {}          # path -> request headers
        self.begin_calls = 0
        self.aggregate_calls = 0
        self._server_shares: list[dict] = []
        self._signing_packages: list[dict] = []
        self._client_commitments: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer vk_live_test"
        path = req.url.path
        body = json.loads(req.content.decode() or "{}") if req.method == "POST" else None
        if req.method == "POST":
            self.posts[path] = body
            self.headers[path] = dict(req.headers)

        if path == "/vault/v1/assets":
            return httpx.Response(200, json=ASSETS)
        if path in ("/vault/v1/quote/swap", "/vault/v1/quote/withdraw"):
            return httpx.Response(200, json=self.quote)
        if path.endswith("/sign/begin"):
            self.begin_calls += 1
            commitments = body["commitments"]
            self._client_commitments = commitments
            self._signing_packages = []
            self._server_shares = []
            self._disclosed = []
            n = len(commitments)
            # Split the approved debit across the sources so an honest run's amounts sum to <= debit
            # (the SDK's no-inflation guard). Tamper subclasses override _message_for / _digest_for.
            per = int(self.quote["debit"]["amount"]) // n
            for i, c in enumerate(commitments):
                msg = self._message_for(i, per)
                nonce = _nonce(i)
                digest_hex = self._digest_for(msg, nonce)   # what the FROST package actually binds to
                sp, server_share = _server_countersign(c, digest_hex)
                self._signing_packages.append(sp)
                self._server_shares.append(server_share)
                self._disclosed.append({"message": msg, "nonce": nonce.hex(), "recipient": _RECIPIENT})
            return httpx.Response(200, json={
                "token": "tkn", "signing_packages": self._signing_packages, "messages": self._disclosed})
        if path.endswith("/sign/aggregate"):
            self.aggregate_calls += 1
            assert body["token"] == "tkn"
            client_shares = body["shares"]
            assert len(client_shares) == len(self._signing_packages), "one share per package"
            # REAL cryptographic proof: aggregate each client share with the SERVER
            # share captured at begin (same session / nonces / signing package) and
            # verify a genuine 2-of-2 ed25519 signature. If the client signed with the
            # wrong nonces or key, aggregate/verify fails and this test fails.
            for i, client_share in enumerate(client_shares):
                agg = _call({
                    "op": "aggregate",
                    "signing_package": self._signing_packages[i],
                    "signature_shares": [[1, client_share], [2, self._server_shares[i]]],
                    "public_key_package": _KEYS["public_key_package"],
                })
                assert agg["verified"] is True, f"session {i} did not verify"
            return httpx.Response(200, json={"ok": True, "status": "processing", "error": None})
        if path.startswith("/vault/v1/movements/"):
            return httpx.Response(200, json=_movement(self.movement_id))
        raise AssertionError(f"unexpected path {path}")

    # Honest disclosure hooks: the disclosed message IS what the FROST package binds to, signed FROM
    # this vault, amount within the debit. Tamper subclasses override exactly one to break one guard.
    def _message_for(self, i, amount):
        return _transfer_message(amount)

    def _digest_for(self, message, nonce):
        return payload_digest_hex(message, nonce, _RECIPIENT)

    def wallet(self, secret) -> Wallet:
        w = Wallet(secret, base_url="https://api.test")
        w._http._client = httpx.AsyncClient(
            base_url="https://api.test", transport=httpx.MockTransport(self.handler)
        )
        return w


# =============================================================================
# 1. Native round — proves _core signs from Python (REAL 2-of-2 signature).
# =============================================================================

def test_native_full_2of2_roundtrip_signs_and_verifies():
    keys = _dkg_2of2()
    ca = _call({"op": "commit", "key_package": keys["client_kp"]})
    cb = _call({"op": "commit", "key_package": keys["server_kp"]})
    sp = _call({"op": "build_signing_package",
                "commitments": [[1, ca["commitments"]], [2, cb["commitments"]]],
                "message_hex": "deadbeef"})["signing_package"]
    sa = _call({"op": "sign", "signing_package": sp, "nonces": ca["nonces"],
                "key_package": keys["client_kp"]})["signature_share"]
    sb = _call({"op": "sign", "signing_package": sp, "nonces": cb["nonces"],
                "key_package": keys["server_kp"]})["signature_share"]
    agg = _call({"op": "aggregate", "signing_package": sp,
                 "signature_shares": [[1, sa], [2, sb]],
                 "public_key_package": keys["public_key_package"]})
    assert agg["verified"] is True
    assert isinstance(agg["signature_hex"], str) and len(agg["signature_hex"]) == 128


def test_frost_commit_and_sign_wrappers_roundtrip():
    """The thin ``_frost.commit`` / ``_frost.sign`` wrappers accept dict key
    material and produce a share the server aggregates into a valid signature."""
    commitments, nonces = commit(_CLIENT_KP)
    assert isinstance(commitments, dict) and isinstance(nonces, dict)
    # The server countersigns; the client signs its half through the wrapper.
    sp, server_share = _server_countersign(commitments, "deadbeef")
    client_share = sign(sp, nonces, _CLIENT_KP)
    assert isinstance(client_share, dict)
    agg = _call({"op": "aggregate", "signing_package": sp,
                 "signature_shares": [[1, client_share], [2, server_share]],
                 "public_key_package": _KEYS["public_key_package"]})
    assert agg["verified"] is True


def test_frost_wrapper_raises_paymos_error_on_bad_input():
    with pytest.raises(PaymosError):
        commit({"not": "a key package"})


# =============================================================================
# 2. Orchestration — swap / withdraw drive the co-sign correctly.
# =============================================================================

async def test_swap_drives_full_cosign_and_returns_movement():
    srv = _Server(_swap_quote(movement_id="mv_1", sources=1), movement_id="mv_1")
    w = srv.wallet(_full_secret())
    mv = await w.swap(send="USDC@base", receive="ETH@arb", amount="5")

    # The quote was the real, non-dry create, with an idempotency key.
    qbody = srv.posts["/vault/v1/quote/swap"]
    assert qbody["dry"] is False
    assert qbody["send"] == "USDC@base" and qbody["receive"] == "ETH@arb"
    assert qbody["amount"] == "5"                # human on the wire; server scales to raw
    assert qbody["slippage_bps"] == 50
    assert "idempotency-key" in {k.lower() for k in srv.headers["/vault/v1/quote/swap"]}

    # One source → one commitment to begin, one share to aggregate.
    assert srv.begin_calls == 1 and srv.aggregate_calls == 1
    begin_body = srv.posts["/vault/v1/movements/mv_1/sign/begin"]
    agg_body = srv.posts["/vault/v1/movements/mv_1/sign/aggregate"]
    assert len(begin_body["commitments"]) == 1
    assert len(agg_body["shares"]) == 1
    assert agg_body["token"] == "tkn"

    # The share never leaks onto the wire.
    for path, body in srv.posts.items():
        assert _CLIENT_KP_JSON not in json.dumps(body), f"share leaked in {path}"

    assert isinstance(mv, Movement)
    assert mv.id == "mv_1" and mv.status == "completed"


async def test_swap_multi_source_commits_and_signs_per_source():
    srv = _Server(_swap_quote(movement_id="mv_2", sources=3), movement_id="mv_2")
    w = srv.wallet(_full_secret())
    await w.swap(send="USDC@base", receive="ETH@arb", amount="5")

    begin_body = srv.posts["/vault/v1/movements/mv_2/sign/begin"]
    agg_body = srv.posts["/vault/v1/movements/mv_2/sign/aggregate"]
    assert len(begin_body["commitments"]) == 3   # one FROST commitment per source
    assert len(agg_body["shares"]) == 3          # one share per signing package


async def test_withdraw_drives_cosign_and_returns_movement():
    srv = _Server(_withdraw_quote(movement_id="mv_w", sources=1), movement_id="mv_w")
    w = srv.wallet(_full_secret())
    mv = await w.withdraw(asset="USDC@base", amount="10", to="0xabc")

    qbody = srv.posts["/vault/v1/quote/withdraw"]
    assert qbody["dry"] is False
    assert qbody["asset"] == "USDC@base" and qbody["to"] == "0xabc"
    assert qbody["amount"] == "10"               # human on the wire; server scales to raw
    assert qbody["mode"] == "exact_out"
    assert "idempotency-key" in {k.lower() for k in srv.headers["/vault/v1/quote/withdraw"]}

    assert srv.begin_calls == 1 and srv.aggregate_calls == 1
    assert isinstance(mv, Movement) and mv.id == "mv_w"


async def test_aggregate_failure_raises_paymos_error():
    class _FailAgg(_Server):
        def handler(self, req):
            if req.url.path.endswith("/sign/aggregate"):
                # capture then fail
                super().handler(req)  # records + verifies begin/shape state
                return httpx.Response(200, json={"ok": False, "status": "failed",
                                                 "error": "co-sign rejected"})
            return super().handler(req)

    srv = _FailAgg(_swap_quote(movement_id="mv_f", sources=1), movement_id="mv_f")
    w = srv.wallet(_full_secret())
    with pytest.raises(PaymosError):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5")


async def test_aggregate_http_400_raises_paymos_error():
    # Mirrors the REAL server: sign/aggregate answers HTTP 400 with the SignResultDto
    # body {ok:false, status:"failed", error}. This raises inside Http._raise_for
    # (before _cosign's own ok-check) — the point is the true 400 path is covered.
    class _FailAgg400(_Server):
        def handler(self, req):
            if req.url.path.endswith("/sign/aggregate"):
                super().handler(req)  # records + verifies begin/shape state
                return httpx.Response(400, json={"ok": False, "status": "failed",
                                                 "error": "co-sign rejected"})
            return super().handler(req)

    srv = _FailAgg400(_swap_quote(movement_id="mv_f4", sources=1), movement_id="mv_f4")
    w = srv.wallet(_full_secret())
    with pytest.raises(PaymosError):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5")

    srv_w = _FailAgg400(_withdraw_quote(movement_id="mv_wf4", sources=1), movement_id="mv_wf4")
    w2 = srv_w.wallet(_full_secret())
    with pytest.raises(PaymosError):
        await w2.withdraw(asset="USDC@base", amount="10", to="0xabc")


# =============================================================================
# 3. Guards — read-only rejection + min_receive floor.
# =============================================================================

async def test_swap_rejects_read_only_secret():
    srv = _Server(_swap_quote(), movement_id="mv_1")
    w = srv.wallet(_read_secret())
    with pytest.raises(PaymosError):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="1")
    # Guard fires BEFORE any network call — no quote POSTed.
    assert "/vault/v1/quote/swap" not in srv.posts


async def test_withdraw_rejects_read_only_secret():
    srv = _Server(_withdraw_quote(), movement_id="mv_w")
    w = srv.wallet(_read_secret())
    with pytest.raises(PaymosError):
        await w.withdraw(asset="USDC@base", amount="1", to="0xabc")
    assert "/vault/v1/quote/withdraw" not in srv.posts


async def test_swap_min_receive_floor_raises_before_signing():
    # Quote guarantees receive.min = 1580000000000000 (0.00158 ETH @ 18dp). Demand
    # 0.002 ETH → floor is below the demand → reject, and NEVER sign.
    srv = _Server(_swap_quote(movement_id="mv_1", sources=1), movement_id="mv_1")
    w = srv.wallet(_full_secret())
    with pytest.raises(SlippageExceeded):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5",
                     min_receive="0.002")
    assert srv.begin_calls == 0
    assert "/vault/v1/movements/mv_1/sign/begin" not in srv.posts


async def test_swap_min_receive_satisfied_proceeds():
    # Demand 0.001 ETH; the guaranteed min 0.00158 clears it → the co-sign runs.
    srv = _Server(_swap_quote(movement_id="mv_1", sources=1), movement_id="mv_1")
    w = srv.wallet(_full_secret())
    mv = await w.swap(send="USDC@base", receive="ETH@arb", amount="5",
                      min_receive="0.001")
    assert srv.begin_calls == 1
    assert isinstance(mv, Movement)


# =============================================================================
# 3b. Blind-sign guard (audit #5) — never sign a message we cannot verify.
# =============================================================================

async def _expect_refusal(server):
    """Drive a swap against a tampered server; assert it raises and NEVER signs (no aggregate)."""
    w = server.wallet(_full_secret())
    with pytest.raises(PaymosError):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert server.aggregate_calls == 0          # refused before round 2
    assert "/vault/v1/movements/mv_g/sign/aggregate" not in server.posts


async def test_refuses_when_server_omits_the_disclosed_messages():
    class _NoDisclosure(_Server):
        def handler(self, req):
            resp = super().handler(req)
            if req.url.path.endswith("/sign/begin"):
                body = json.loads(resp.content); body.pop("messages", None)
                return httpx.Response(200, json=body)
            return resp
    await _expect_refusal(_NoDisclosure(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g"))


async def test_refuses_when_package_does_not_bind_to_the_disclosed_message():
    # The server discloses message M but builds the FROST package over digest(M + " "): the client
    # recomputes digest(M), sees it != the package's message, and refuses.
    class _BindingMismatch(_Server):
        def _digest_for(self, message, nonce):
            return payload_digest_hex(message + " ", nonce, _RECIPIENT)
    await _expect_refusal(_BindingMismatch(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g"))


async def test_refuses_when_message_signs_from_a_different_vault():
    class _WrongSigner(_Server):
        def _message_for(self, i, amount):
            return _transfer_message(amount, signer_id="00" * 32)   # not this vault's group key
    await _expect_refusal(_WrongSigner(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g"))


async def test_refuses_when_sources_move_more_than_the_approved_debit():
    class _Inflated(_Server):
        def _message_for(self, i, amount):
            return _transfer_message(int(self.quote["debit"]["amount"]) * 1000)   # far above the debit
    await _expect_refusal(_Inflated(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g"))


# A disclosed per-source amount is a raw, positive, ASCII-digit string — nothing ``int()`` merely
# tolerates. Refused BEFORE a single share is computed: the spy counts calls into ``_frost.sign``.

@pytest.fixture
def sign_spy(monkeypatch):
    import paymos._frost as frost_mod
    calls = []
    real = frost_mod.sign

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(frost_mod, "sign", spy)
    return calls


async def _expect_amount_refusal(server, sign_spy):
    w = server.wallet(_full_secret())
    with pytest.raises(PaymosError, match="co-sign refused: source .* transfer amount"):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == []                       # no share computed for ANY source
    assert server.aggregate_calls == 0


@pytest.mark.parametrize("bad", ["-900", "+5", " 5", "5 ", "", "0", "00", "1_000", "\u0665", "5\n", "0x10"])
async def test_refuses_a_malformed_source_amount(bad, sign_spy):
    class _BadAmount(_Server):
        def _message_for(self, i, amount):
            return _transfer_message(json.dumps(bad)[1:-1])  # JSON-escaped: decodes back to `bad`
    await _expect_amount_refusal(_BadAmount(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g"), sign_spy)


async def test_refuses_a_json_number_amount(sign_spy):
    # The server sends amounts as strings; a bare JSON number is not one.
    class _NumberAmount(_Server):
        def _message_for(self, i, amount):
            return _transfer_message(amount).replace(f'"{amount}"', str(amount))
    await _expect_amount_refusal(_NumberAmount(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g"), sign_spy)


async def test_refuses_a_negative_source_that_hides_an_inflated_one(sign_spy):
    # Source A moves 2D, source B "moves" -D: the sum is D and would pass the debit check, while A
    # alone takes twice the approved debit. The negative leg must sink the whole co-sign.
    class _Offsetting(_Server):
        def _message_for(self, i, amount):
            debit = int(self.quote["debit"]["amount"])
            return _transfer_message(2 * debit if i == 0 else -debit)
    await _expect_amount_refusal(_Offsetting(_swap_quote(movement_id="mv_g", sources=2), movement_id="mv_g"), sign_spy)


async def test_refuses_when_the_key_package_has_no_verifying_key(sign_spy):
    # With nothing to hold signer_id against, the guard used to skip its "not from another vault"
    # check. Today the core refuses such a package at commit time, before the guard runs; the guard's
    # own refusal is the second line. Either way: a typed error, and nothing signed.
    kp = {k: v for k, v in json.loads(_CLIENT_KP_JSON).items() if k != "verifying_key"}
    srv = _Server(_swap_quote(movement_id="mv_vk", sources=1), movement_id="mv_vk")
    w = srv.wallet(VaultSecret.pack("vk_live_test", 1, json.dumps(kp)))
    with pytest.raises(PaymosError, match="verifying.key"):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == [] and srv.aggregate_calls == 0


# The token each source moves must be the approved SEND asset: its id hashes to that asset's catalog
# fingerprint. Otherwise a debit approved in USDC could sign away the same count of something dearer.

async def test_refuses_a_source_moving_another_catalog_asset(sign_spy):
    class _OtherToken(_Server):
        def _message_for(self, i, amount):
            return _transfer_message(amount, token="eth-arb")   # ETH@arb's fingerprint, not USDC@base's
    srv = _OtherToken(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g")
    with pytest.raises(PaymosError, match="not the approved send asset USDC@base"):
        await srv.wallet(_full_secret()).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == [] and srv.aggregate_calls == 0


async def test_refuses_one_bad_token_among_several_sources(sign_spy):
    class _SecondSourceSwapped(_Server):
        def _message_for(self, i, amount):
            return _transfer_message(amount, token="usdc-base" if i == 0 else "usdc-base-evil")
    srv = _SecondSourceSwapped(_swap_quote(movement_id="mv_g", sources=2), movement_id="mv_g")
    with pytest.raises(PaymosError, match="source 1 moves a token"):
        await srv.wallet(_full_secret()).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == [] and srv.aggregate_calls == 0


async def test_refuses_when_the_server_publishes_no_fingerprint(sign_spy):
    # An older server: reads keep working, but there is nothing to hold the token against — refuse,
    # never skip the check.
    class _NoFingerprint(_Server):
        def handler(self, req):
            if req.url.path == "/vault/v1/assets":
                return httpx.Response(200, json=[{k: v for k, v in a.items() if k != "fingerprint"} for a in ASSETS])
            return super().handler(req)
    srv = _NoFingerprint(_swap_quote(movement_id="mv_g", sources=1), movement_id="mv_g")
    w = srv.wallet(_full_secret())
    assert (await w.assets())[0].fingerprint is None
    with pytest.raises(PaymosError, match="did not publish an asset fingerprint"):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == [] and srv.aggregate_calls == 0


class _CatalogServer(_Server):
    """A _Server whose /assets answers ``catalog``; each source moves ``token``."""

    def __init__(self, *a, catalog, token="usdc-base", **k):
        super().__init__(*a, **k)
        self.catalog = catalog
        self.token = token

    def handler(self, req):
        if req.url.path == "/vault/v1/assets":
            return httpx.Response(200, json=self.catalog)
        return super().handler(req)

    def _message_for(self, i, amount):
        return _transfer_message(amount, token=self.token)


def _usdc_catalog(*fingerprints):
    """ASSETS with USDC@base listed once per fingerprint (the same label, several contracts)."""
    usdc, eth = ASSETS
    return [{**usdc, "fingerprint": fp} for fp in fingerprints] + [eth]


@pytest.mark.parametrize("token", ["usdc-base", "usdc-base-v2"])
async def test_one_label_listed_twice_accepts_either_fingerprint(token):
    # /assets may list one label twice with different fingerprints; one label is one coin, so a token
    # matching ANY of them is the approved asset. The co-sign completes (aggregate verified for real).
    srv = _CatalogServer(_swap_quote(movement_id="mv_d", sources=2), "mv_d",
                         catalog=_usdc_catalog(_fp("usdc-base"), _fp("usdc-base-v2")), token=token)
    mv = await srv.wallet(_full_secret()).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert mv.id == "mv_d" and srv.aggregate_calls == 1


async def test_one_label_listed_twice_still_refuses_a_third_token(sign_spy):
    srv = _CatalogServer(_swap_quote(movement_id="mv_d", sources=1), "mv_d",
                         catalog=_usdc_catalog(_fp("usdc-base"), _fp("usdc-base-v2")), token="usdc-other")
    with pytest.raises(PaymosError, match="not the approved send asset"):
        await srv.wallet(_full_secret()).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == [] and srv.aggregate_calls == 0


# pinned_fingerprints: a pinned label is checked ONLY against its pins, whatever /assets publishes.

def _pinned_wallet(srv, pins):
    w = Wallet(_full_secret(), base_url="https://api.test", pinned_fingerprints=pins)
    w._http._client = httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(srv.handler))
    return w


async def test_pinned_fingerprint_signs_even_when_the_server_publishes_another():
    # The server's catalog is wrong (or absent) for USDC@base; the out-of-band pin is what counts.
    srv = _CatalogServer(_swap_quote(movement_id="mv_p", sources=1), "mv_p",
                         catalog=_usdc_catalog(_fp("something-else")))
    mv = await _pinned_wallet(srv, {"USDC@base": _fp("usdc-base").upper()}).swap(
        send="USDC@base", receive="ETH@arb", amount="5")
    assert mv.id == "mv_p" and srv.aggregate_calls == 1


async def test_pinned_label_refuses_the_published_fingerprint_it_does_not_pin(sign_spy):
    # The server publishes the token's true hash, but the integrator pinned a different value: the
    # pin wins, so the server-backed match does not count.
    srv = _CatalogServer(_swap_quote(movement_id="mv_p", sources=1), "mv_p",
                         catalog=_usdc_catalog(_fp("usdc-base")))
    w = _pinned_wallet(srv, {"USDC@base": [_fp("usdc-base-pinned"), _fp("usdc-base-pinned-2")]})
    with pytest.raises(PaymosError, match="not the approved send asset"):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == [] and srv.aggregate_calls == 0


async def test_pin_on_another_label_leaves_the_send_label_on_the_catalog():
    srv = _CatalogServer(_swap_quote(movement_id="mv_p", sources=1), "mv_p", catalog=_usdc_catalog(_fp("usdc-base")))
    mv = await _pinned_wallet(srv, {"ETH@arb": _fp("not-eth")}).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert mv.id == "mv_p"


@pytest.mark.parametrize("bad", [{"USDC@base": ""}, {"USDC@base": []}, {"USDC@base": "abc"},
                                 {"USDC@base": ["a42c7e46" * 8, "not-hex"]}, {"USDC@base": None},
                                 {"USDC@base": 42}, {"USDC@base": ["a42c7e46" * 8, None]}])
def test_malformed_pins_fail_at_construction(bad):
    # The SDK's typed error, like every other configuration fault — not a bare ValueError/TypeError.
    with pytest.raises(PaymosError, match="pinned_fingerprints") as exc:
        Wallet(_full_secret(), base_url="https://api.test", pinned_fingerprints=bad)
    assert type(exc.value) is PaymosError


# A label listed twice with DIFFERENT decimals: the debit is in the first entry's units, so only
# entries with the first entry's decimals may supply a fingerprint. Pins are exempt (vouched for).

def _usdc_catalog_decimals(*entries):
    """ASSETS with USDC@base listed once per (fingerprint, decimals)."""
    usdc, eth = ASSETS
    return [{**usdc, "fingerprint": fp, "decimals": d} for fp, d in entries] + [eth]


async def test_label_listed_twice_refuses_the_entry_with_other_decimals(sign_spy):
    srv = _CatalogServer(_swap_quote(movement_id="mv_x", sources=1), "mv_x",
                         catalog=_usdc_catalog_decimals((_fp("usdc-base"), 6), (_fp("usdc-base-18"), 18)),
                         token="usdc-base-18")
    with pytest.raises(PaymosError, match="not the approved send asset"):
        await srv.wallet(_full_secret()).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert sign_spy == [] and srv.aggregate_calls == 0


async def test_label_listed_twice_still_signs_the_first_entrys_token_despite_other_decimals():
    srv = _CatalogServer(_swap_quote(movement_id="mv_x", sources=1), "mv_x",
                         catalog=_usdc_catalog_decimals((_fp("usdc-base"), 6), (_fp("usdc-base-18"), 18)))
    mv = await srv.wallet(_full_secret()).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert mv.id == "mv_x" and srv.aggregate_calls == 1


@pytest.mark.parametrize("token", ["usdc-base", "usdc-base-v2"])
async def test_label_listed_twice_with_equal_decimals_signs_either_token(token):
    srv = _CatalogServer(_swap_quote(movement_id="mv_x", sources=1), "mv_x",
                         catalog=_usdc_catalog_decimals((_fp("usdc-base"), 6), (_fp("usdc-base-v2"), 6)),
                         token=token)
    mv = await srv.wallet(_full_secret()).swap(send="USDC@base", receive="ETH@arb", amount="5")
    assert mv.id == "mv_x" and srv.aggregate_calls == 1


async def test_a_pin_is_not_filtered_by_decimals():
    # The integrator pinned the second entry's fingerprint: pins are taken as given.
    srv = _CatalogServer(_swap_quote(movement_id="mv_x", sources=1), "mv_x",
                         catalog=_usdc_catalog_decimals((_fp("usdc-base"), 6), (_fp("usdc-base-18"), 18)),
                         token="usdc-base-18")
    mv = await _pinned_wallet(srv, {"USDC@base": _fp("usdc-base-18")}).swap(
        send="USDC@base", receive="ETH@arb", amount="5")
    assert mv.id == "mv_x"


# =============================================================================
# 3c. Amount-cap guards — total_in verifies the debit leg; optional max_debit cap.
# =============================================================================

async def test_withdraw_total_in_verifies_debit_not_send():
    # 0.1.1 pinned the SEND leg for total_in and always false-failed (send = total - fees); the fix
    # verifies the DEBIT leg, so a well-formed total_in quote now signs.
    srv = _Server(_withdraw_total_in_quote(movement_id="mv_ti", sources=1), movement_id="mv_ti")
    w = srv.wallet(_full_secret())
    mv = await w.withdraw(asset="USDC@base", amount="10", to="0xabc", mode="total_in")
    assert srv.posts["/vault/v1/quote/withdraw"]["mode"] == "total_in"
    assert srv.begin_calls == 1 and isinstance(mv, Movement)


async def test_withdraw_total_in_rejects_debit_above_typed_total():
    q = _withdraw_total_in_quote(movement_id="mv_ti2", sources=1)
    q["debit"]["amount"] = "10000001"                       # 1 unit above the typed 10.000000 total
    srv = _Server(q, movement_id="mv_ti2")
    w = srv.wallet(_full_secret())
    with pytest.raises(PaymosError):
        await w.withdraw(asset="USDC@base", amount="10", to="0xabc", mode="total_in")
    assert srv.begin_calls == 0


async def test_withdraw_rejects_debit_above_max_debit():
    # exact_out debit 10012000 exceeds max_debit 10.00 (=10000000) -> refuse before signing.
    srv = _Server(_withdraw_quote(movement_id="mv_md", sources=1), movement_id="mv_md")
    w = srv.wallet(_full_secret())
    with pytest.raises(PaymosError):
        await w.withdraw(asset="USDC@base", amount="10", to="0xabc", max_debit="10.00")
    assert srv.begin_calls == 0


async def test_withdraw_within_max_debit_signs():
    srv = _Server(_withdraw_quote(movement_id="mv_md2", sources=1), movement_id="mv_md2")
    w = srv.wallet(_full_secret())
    mv = await w.withdraw(asset="USDC@base", amount="10", to="0xabc", max_debit="10.02")  # cap >= debit
    assert srv.begin_calls == 1 and isinstance(mv, Movement)


async def test_swap_rejects_debit_above_max_debit():
    srv = _Server(_swap_quote(movement_id="mv_sd", sources=1), movement_id="mv_sd")
    w = srv.wallet(_full_secret())
    with pytest.raises(PaymosError):
        await w.swap(send="USDC@base", receive="ETH@arb", amount="5", max_debit="5.00")  # debit 5012000
    assert srv.begin_calls == 0


# =============================================================================
# 4. movement / movements / wait
# =============================================================================

async def test_movement_maps_detail():
    srv = _Server(_swap_quote(), movement_id="mv_x")
    w = srv.wallet(_full_secret())
    mv = await w.movement("mv_x")
    assert isinstance(mv, Movement)
    assert mv.id == "mv_x" and mv.dest_chain_tx_hash == "0xdeadbeef"


async def test_movements_page_maps_items_and_cursor():
    def handler(req):
        assert req.url.path == "/vault/v1/movements"
        # limit / cursor go on the query string.
        return httpx.Response(200, json={
            "items": [_movement("mv_a"), _movement("mv_b", status="processing")],
            "next_cursor": "CUR2",
        })

    w = Wallet(_full_secret(), base_url="https://api.test")
    w._http._client = httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler))
    items, cursor = await w.movements(limit=2)
    assert cursor == "CUR2"
    assert [m.id for m in items] == ["mv_a", "mv_b"]
    assert all(isinstance(m, Movement) for m in items)


async def test_wait_polls_until_terminal():
    states = ["processing", "processing", "completed"]

    def handler(req):
        st = states.pop(0) if len(states) > 1 else states[0]
        return httpx.Response(200, json=_movement("mv_p", status=st))

    w = Wallet(_full_secret(), base_url="https://api.test")
    w._http._client = httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler))
    mv = await w.wait("mv_p", timeout=5, poll=0.01)
    assert mv.status == "completed"


async def test_wait_times_out():
    def handler(req):
        return httpx.Response(200, json=_movement("mv_stuck", status="processing"))

    w = Wallet(_full_secret(), base_url="https://api.test")
    w._http._client = httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler))
    with pytest.raises(PaymosError):
        await w.wait("mv_stuck", timeout=0.05, poll=0.01)


# --- Quote.sources mapping ---------------------------------------------------

def test_quote_sources_maps_when_present_and_defaults_none():
    q_dry = Quote.from_dict({k: v for k, v in _swap_quote().items() if k != "sources"})
    assert q_dry.sources is None                 # dry quote omits the key
    q_live = Quote.from_dict(_swap_quote(sources=2))
    assert q_live.sources == 2
