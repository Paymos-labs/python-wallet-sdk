"""The SDK against the server's recorded wire contract (``sdk/contract/fixtures``).

Every other test in this suite mocks the server from its author's memory; this one mocks it
from what the real request pipeline answered. The fixtures are loaded from disk — walking up
from this file for ``contract/fixtures`` (the public mirror's layout), then
``sdk/contract/fixtures`` (the monorepo's) — and served through an ``httpx.MockTransport``.
No network. A missing fixtures folder FAILS every test here, never skips: a suite that quietly
stops reading the contract is exactly how seven SDKs shipped reading ``{"assets": [...]}``.

For each quote the request the SDK SENDS is compared with the fixture's request too, so the
contract binds both directions. ``sign_begin`` / ``sign_aggregate`` carry placeholder crypto
values (see the contract README): here they prove parsing, the count checks, the blind-sign
guard and the ``ok:false`` path — not signature validity, which ``test_frost.py`` covers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from paymos import _frost, errors
from paymos._http import Http
from paymos._secret import VaultSecret
from paymos._types import Amount, Asset, Balance, Fees, Movement, Quote, Receive, RouteFee
from paymos._wallet import _PROGRESSED_STATUSES, _TERMINAL_STATUSES, Wallet

BASE = "https://api.test"
API_KEY = "vk_live_test"
TO = "0xabc0000000000000000000000000000000000000"


# --- fixtures on disk ------------------------------------------------------------

def _fixtures_dir() -> Path:
    here = Path(__file__).resolve()
    for rel in ("contract/fixtures", "sdk/contract/fixtures"):
        for parent in here.parents:
            candidate = parent / rel
            if candidate.is_dir():
                return candidate
    pytest.fail(
        "wire-contract fixtures not found (looked for contract/fixtures, then sdk/contract/fixtures, "
        f"walking up from {here}) — the contract tests must never skip"
    )


def _load(name: str) -> dict[str, Any]:
    return json.loads((_fixtures_dir() / f"{name}.json").read_text(encoding="utf-8"))


def _target(req: httpx.Request) -> str:
    """Path plus query, as a fixture's ``request.path`` records it."""
    q = req.url.query.decode() if isinstance(req.url.query, bytes) else req.url.query
    return req.url.path + (f"?{q}" if q else "")


class _Server:
    """Answers each (method, path) with the fixture recorded for it, and keeps every request."""

    def __init__(self, *names: str) -> None:
        self.routes: dict[tuple[str, str], dict[str, Any]] = {}
        for n in names:
            fx = _load(n)
            key = (fx["request"]["method"], fx["request"]["path"])
            assert key not in self.routes, f"two fixtures answer {key}"
            self.routes[key] = fx["response"]
        self.requests: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == f"Bearer {API_KEY}"
        self.requests.append(req)
        key = (req.method, _target(req))
        if key not in self.routes:
            raise AssertionError(f"unexpected request {key}; served {sorted(self.routes)}")
        r = self.routes[key]
        return httpx.Response(r["status"], json=r["body"])

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and _target(r) == path]

    def wallet(self, share: str | None = None) -> Wallet:
        w = Wallet(VaultSecret.pack(API_KEY, 1, share), base_url=BASE)
        w._http._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(self.handler))
        return w


def _body(req: httpx.Request) -> Any:
    return json.loads(req.content.decode())


# Fields the SDK may send that the recorded request did not, and why each is harmless.
# Python sends ``dry`` on every quote (false on a real one); the server's default is false.
_EXTRA_OK = {"dry": False}


def _assert_request_matches(sent: httpx.Request, fixture: str) -> None:
    """The SDK's request equals the fixture's: method, path, and every body field."""
    want = _load(fixture)["request"]
    assert sent.method == want["method"]
    assert _target(sent) == want["path"]
    got = _body(sent)
    for k, v in want["body"].items():
        assert k in got, f"{fixture}: SDK did not send {k!r}"
        assert got[k] == v and type(got[k]) is type(v), f"{fixture}: {k!r} sent {got[k]!r}, recorded {v!r}"
    for k, v in got.items():
        if k not in want["body"]:
            assert k in _EXTRA_OK and _EXTRA_OK[k] == v, f"{fixture}: SDK sent unrecorded field {k}={v!r}"


def _assert_quote_equals_fixture(q: Quote, fixture: str) -> None:
    """Every field of the recorded quote, parsed — nothing silently dropped or defaulted."""
    b = _load(fixture)["response"]["body"]
    assert isinstance(q, Quote)
    assert q.movement_id == b.get("movement_id")
    assert q.mode == b["mode"]
    assert q.send == Amount(b["send"]["amount"], b["send"]["asset"])
    assert q.debit == Amount(b["debit"]["amount"], b["debit"]["asset"])
    assert q.receive == Receive(b["receive"]["amount"], b["receive"]["min"], b["receive"]["asset"])
    _assert_fees(q.fees, b["fees"])
    assert q.expires_at == b["expires_at"]
    assert q.sources == b.get("sources")


def _assert_fees(f: Fees | None, b: dict[str, Any]) -> None:
    assert isinstance(f, Fees)
    assert (f.asset, f.platform, f.network, f.total) == (b["asset"], b["platform"], b["network"], b["total"])
    assert f.route == RouteFee(b["route"]["amount"], b["route"]["estimate"])
    assert isinstance(f.route.estimate, bool)
    assert f.usd == b["usd"]


def _assert_movement_equals(m: Movement, b: dict[str, Any]) -> None:
    assert isinstance(m, Movement)
    assert (m.id, m.type, m.status) == (b["id"], b["type"], b["status"])
    assert m.send == Amount(b["send"]["amount"], b["send"]["asset"])
    assert m.receive == Receive(b["receive"]["amount"], b["receive"]["min"], b["receive"]["asset"])
    if "fees" in b:
        _assert_fees(m.fees, b["fees"])
    else:
        assert m.fees is None  # omitted on the wire, not null
    assert m.dest_chain_tx_hash == b.get("dest_chain_tx_hash")
    assert m.dest_chain_explorer_url == b.get("dest_chain_explorer_url")
    assert m.created_at == b["created_at"]
    assert m.completed_at == b.get("completed_at")


# A full-scope share for the money paths. The co-sign crypto is stubbed below: the fixtures'
# signing values are placeholders, and the wire (counts, shapes, guards) is what's under test.
_SHARE = json.dumps({"verifying_key": "vault.example"})


@pytest.fixture
def stub_frost(monkeypatch):
    """Replace the native commit/sign with the fixtures' placeholder shapes; count the calls."""
    calls = {"commit": 0, "sign": 0}

    def commit(kp):
        calls["commit"] += 1
        return {"identifier": "01", "hiding": "aa", "binding": "bb"}, {"n": calls["commit"]}

    def sign(pkg, nonces, kp):
        calls["sign"] += 1
        return {"identifier": "01", "share": "cc"}

    monkeypatch.setattr(_frost, "commit", commit)
    monkeypatch.setattr(_frost, "sign", sign)
    return calls


def test_fixtures_are_found():
    d = _fixtures_dir()
    assert (d / "assets.json").is_file()


# --- reads -----------------------------------------------------------------------

async def test_assets_bare_array():
    srv = _Server("assets")
    got = await srv.wallet().assets()
    want = _load("assets")["response"]["body"]
    assert isinstance(want, list)  # the contract: a bare array, never {"assets": [...]}
    assert got == [Asset(a["asset"], a["symbol"], a["chain"], a["decimals"], a["fingerprint"]) for a in want]
    assert len(got) == 2
    # The fingerprint is sha256 of the raw token id — the value the co-sign guard recomputes.
    assert got[0].fingerprint == hashlib.sha256(b"usdc-base").hexdigest()
    assert got[1].fingerprint == hashlib.sha256(b"usdt-base").hexdigest()


async def test_assets_without_fingerprint_still_parse():
    # An older server omits it: reads must keep working (only the co-sign refuses).
    body = [{k: v for k, v in a.items() if k != "fingerprint"} for a in _load("assets")["response"]["body"]]

    def handler(req):
        return httpx.Response(200, json=body)

    w = Wallet(VaultSecret.pack(API_KEY, 1, None), base_url=BASE)
    w._http._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    got = await w.assets()
    assert [a.fingerprint for a in got] == [None, None] and got[0].decimals == 6


async def test_balances_bare_array():
    srv = _Server("balances")
    got = await srv.wallet().balances()
    want = _load("balances")["response"]["body"]
    assert isinstance(want, list)
    assert got == [
        Balance(b["asset"], b["symbol"], b["chain"], b["decimals"], b["amount_raw"], b.get("usd"))
        for b in want
    ]
    assert got[0].amount_raw == "1000000000" and got[0].usd == "1000"


async def test_object_wrapped_catalog_is_refused_not_misread():
    # The shape seven SDKs imagined. Iterating it would walk the dict's keys; it must be a typed error.
    def handler(req):
        return httpx.Response(200, json={"assets": _load("assets")["response"]["body"]})

    w = Wallet(VaultSecret.pack(API_KEY, 1, None), base_url=BASE)
    w._http._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    with pytest.raises(errors.PaymosError, match="expected a JSON array"):
        await w.assets()
    with pytest.raises(errors.PaymosError, match="expected a JSON array"):
        await w.balances()


async def test_movements_page_with_next_cursor():
    srv = _Server("movements_page")
    items, cursor = await srv.wallet().movements(limit=2)
    body = _load("movements_page")["response"]["body"]
    assert srv.sent("GET", "/vault/v1/movements?limit=2")
    assert len(items) == len(body["items"]) == 2
    for m, b in zip(items, body["items"]):
        _assert_movement_equals(m, b)
        assert m.fees is None  # list items carry no fee legs
    assert cursor == body["next_cursor"] == "cursor_1"


async def test_movements_passes_the_cursor_back():
    seen = []

    def handler(req):
        seen.append(req.url.params.get("cursor"))
        return httpx.Response(200, json={"items": []})  # last page: next_cursor omitted

    w = Wallet(VaultSecret.pack(API_KEY, 1, None), base_url=BASE)
    w._http._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    items, cursor = await w.movements(limit=2, cursor="cursor_1")
    assert seen == ["cursor_1"] and items == [] and cursor is None


async def test_movement_with_fees():
    srv = _Server("movement")
    m = await srv.wallet().movement("mv_3")
    _assert_movement_equals(m, _load("movement")["response"]["body"])
    assert m.fees is not None and m.fees.route.estimate is True
    # Omitted on the wire → None, not a KeyError.
    assert m.dest_chain_tx_hash is None and m.completed_at is None


# --- dry quotes (the public preview surface) ----------------------------------------

async def test_quote_withdraw_dry():
    srv = _Server("assets", "quote_withdraw_dry")
    q = await srv.wallet().quote_withdraw("USDC@base", "10", TO)
    (sent,) = srv.sent("POST", "/vault/v1/quote/withdraw")
    _assert_request_matches(sent, "quote_withdraw_dry")
    assert "idempotency-key" not in sent.headers  # a preview persists nothing
    _assert_quote_equals_fixture(q, "quote_withdraw_dry")
    assert q.movement_id is None and q.sources is None  # omitted on a dry preview


async def test_quote_swap_dry():
    srv = _Server("assets", "quote_swap_dry")
    q = await srv.wallet().quote_swap("USDC@base", "USDT@base", "5", slippage_bps=50)
    (sent,) = srv.sent("POST", "/vault/v1/quote/swap")
    _assert_request_matches(sent, "quote_swap_dry")
    assert "idempotency-key" not in sent.headers
    _assert_quote_equals_fixture(q, "quote_swap_dry")
    assert q.movement_id is None and q.sources is None


# --- real quotes: the request, the parse, and the co-sign they lead into ------------

async def test_quote_withdraw_real_then_signing_package_count_is_checked(stub_frost):
    # sources=2 → two commitments; the recorded sign/begin returns ONE package → refuse.
    srv = _Server("assets", "quote_withdraw", "sign_begin")
    srv.routes[("POST", "/vault/v1/movements/mv_1/sign/begin")] = srv.routes.pop(
        ("POST", "/vault/v1/movements/mv_3/sign/begin"))
    w = srv.wallet(_SHARE)
    with pytest.raises(errors.PaymosError, match="expected 2 signing packages, got 1"):
        await w.withdraw("USDC@base", "10", TO)

    (sent,) = srv.sent("POST", "/vault/v1/quote/withdraw")
    _assert_request_matches(sent, "quote_withdraw")
    assert _body(sent)["dry"] is False
    assert sent.headers.get("idempotency-key")
    _assert_quote_equals_fixture(Quote.from_dict(_load("quote_withdraw")["response"]["body"]), "quote_withdraw")

    (begin,) = srv.sent("POST", "/vault/v1/movements/mv_1/sign/begin")
    assert len(_body(begin)["commitments"]) == 2 == stub_frost["commit"]
    assert stub_frost["sign"] == 0  # nothing signed


async def test_a_label_listed_twice_takes_the_first_entrys_decimals(stub_frost):
    # The server resolves a label to its FIRST catalog entry; so must amount validation and the echo
    # check. Listed second with 18 decimals, USDC@base must still convert with the first entry's 6.
    srv = _Server("assets", "quote_withdraw", "sign_begin")
    catalog = srv.routes[("GET", "/vault/v1/assets")]["body"]
    catalog.append({**catalog[0], "decimals": 18, "fingerprint": hashlib.sha256(b"usdc-base-2").hexdigest()})
    srv.routes[("POST", "/vault/v1/movements/mv_1/sign/begin")] = srv.routes.pop(
        ("POST", "/vault/v1/movements/mv_3/sign/begin"))
    w = srv.wallet(_SHARE)

    # Validation: 7 fractional digits exceed the first entry's 6 (the last entry's 18 would allow them).
    with pytest.raises(errors.PaymosError, match="more than 6 fractional digits"):
        await w.quote_withdraw("USDC@base", "10.0000001", TO)
    # Echo check: "10" is 10_000_000 raw at 6 decimals — the recorded quote's receive.amount — so the
    # echo passes and the withdraw reaches sign/begin (then stops on the fixture's package count).
    with pytest.raises(errors.PaymosError, match="expected 2 signing packages"):
        await w.withdraw("USDC@base", "10", TO)
    assert srv.sent("POST", "/vault/v1/movements/mv_1/sign/begin")


async def test_quote_withdraw_total_in_passes_the_echo_guard(stub_frost):
    # total_in: amount "10" is the total DEBIT (10000000 raw) — the echo guard must accept the
    # recorded quote (debit == total, send == total - fees) and reach sign/begin.
    srv = _Server("assets", "quote_withdraw_total_in", "sign_begin")
    srv.routes[("POST", "/vault/v1/movements/mv_2/sign/begin")] = srv.routes.pop(
        ("POST", "/vault/v1/movements/mv_3/sign/begin"))
    w = srv.wallet(_SHARE)
    with pytest.raises(errors.PaymosError, match="expected 2 signing packages"):
        await w.withdraw("USDC@base", "10", TO, mode="total_in")
    (sent,) = srv.sent("POST", "/vault/v1/quote/withdraw")
    _assert_request_matches(sent, "quote_withdraw_total_in")
    assert srv.sent("POST", "/vault/v1/movements/mv_2/sign/begin")  # the echo guard let it through

    q = Quote.from_dict(_load("quote_withdraw_total_in")["response"]["body"])
    _assert_quote_equals_fixture(q, "quote_withdraw_total_in")


@pytest.mark.parametrize("nonce, refusal", [
    # As recorded: the placeholder nonce is 2 bytes, not 32 — a malformed disclosure. This once
    # escaped as a raw ValueError from the digest; it must refuse as a typed error.
    (None, "malformed disclosure for source 0"),
    # A well-formed nonce: now the placeholder package fails to hash to the disclosed message.
    ("00" * 32, "does not bind to the disclosed message"),
])
async def test_quote_swap_real_then_blind_sign_guard_refuses_placeholder_package(stub_frost, nonce, refusal):
    # sources=1, one package — the guard runs, and nothing the fixture discloses is signable.
    srv = _Server("assets", "quote_swap", "sign_begin")
    if nonce is not None:
        begin_body = srv.routes[("POST", "/vault/v1/movements/mv_3/sign/begin")]["body"]
        begin_body["messages"][0]["nonce"] = nonce
    w = srv.wallet(_SHARE)
    with pytest.raises(errors.PaymosError, match=refusal) as exc:
        await w.swap("USDC@base", "USDT@base", "5", slippage_bps=50)
    assert type(exc.value) is errors.PaymosError

    (sent,) = srv.sent("POST", "/vault/v1/quote/swap")
    _assert_request_matches(sent, "quote_swap")
    assert sent.headers.get("idempotency-key")
    _assert_quote_equals_fixture(Quote.from_dict(_load("quote_swap")["response"]["body"]), "quote_swap")

    (begin,) = srv.sent("POST", "/vault/v1/movements/mv_3/sign/begin")
    assert _body(begin) == _load("sign_begin")["request"]["body"]
    assert stub_frost["sign"] == 0
    assert not srv.sent("POST", "/vault/v1/movements/mv_3/sign/aggregate")


async def test_sign_begin_and_aggregate_parse(stub_frost, monkeypatch):
    # Past the guard (its own tests use real digests), the recorded begin → aggregate(ok:true) →
    # GET movement path completes and returns the parsed movement.
    guarded = []
    monkeypatch.setattr(Wallet, "_verify_signing_disclosure", staticmethod(lambda *a: guarded.append(a)))
    srv = _Server("assets", "quote_swap", "sign_begin", "sign_aggregate", "movement")
    m = await srv.wallet(_SHARE).swap("USDC@base", "USDT@base", "5")

    # The guard ran once, against the SEND asset's fingerprint as /assets published it.
    ((_q, _pkgs, messages, _kp, send_fingerprints),) = guarded
    assert send_fingerprints == {_load("assets")["response"]["body"][0]["fingerprint"]}
    assert messages == _load("sign_begin")["response"]["body"]["messages"]

    (agg,) = srv.sent("POST", "/vault/v1/movements/mv_3/sign/aggregate")
    assert _body(agg) == _load("sign_aggregate")["request"]["body"]  # token echoed, one share
    _assert_movement_equals(m, _load("movement")["response"]["body"])


async def test_sign_aggregate_ok_false_is_a_typed_error(stub_frost, monkeypatch):
    monkeypatch.setattr(Wallet, "_verify_signing_disclosure", staticmethod(lambda *a: None))
    srv = _Server("assets", "quote_swap", "sign_begin", "sign_aggregate_error")
    with pytest.raises(errors.PaymosError) as exc:
        await srv.wallet(_SHARE).swap("USDC@base", "USDT@base", "5")
    body = _load("sign_aggregate_error")["response"]["body"]
    assert body["ok"] is False
    assert exc.value.message == body["error"]
    assert exc.value.status == 400


async def test_sign_aggregate_ok_false_on_200_is_a_typed_error(stub_frost, monkeypatch):
    # Same envelope on a 2xx must still refuse, not report success.
    monkeypatch.setattr(Wallet, "_verify_signing_disclosure", staticmethod(lambda *a: None))
    srv = _Server("assets", "quote_swap", "sign_begin", "sign_aggregate_error")
    srv.routes[("POST", "/vault/v1/movements/mv_3/sign/aggregate")] = {
        "status": 200, "body": _load("sign_aggregate_error")["response"]["body"]}
    with pytest.raises(errors.PaymosError, match="token and shares are required"):
        await srv.wallet(_SHARE).swap("USDC@base", "USDT@base", "5")


async def test_sign_begin_error_on_a_pending_movement_propagates(stub_frost):
    # A rejected round 1 converges only if the movement already progressed; mv_3 is pending.
    srv = _Server("assets", "quote_swap", "sign_begin_error", "movement")
    with pytest.raises(errors.PaymosError) as exc:
        await srv.wallet(_SHARE).swap("USDC@base", "USDT@base", "5")
    assert exc.value.message == _load("sign_begin_error")["response"]["body"]["error"]
    assert srv.sent("GET", "/vault/v1/movements/mv_3")  # it looked before re-raising


# --- errors ----------------------------------------------------------------------

_ERROR_TYPES = {
    "error_400": errors.PaymosError,
    "error_400_body": errors.PaymosError,
    "error_401": errors.AuthError,
    "error_403": errors.Forbidden,
    "error_404_movement": errors.PaymosError,
    "error_404_path": errors.PaymosError,
    "error_409_idempotency": errors.Conflict,
}


def test_every_error_fixture_is_covered():
    on_disk = {p.stem for p in _fixtures_dir().glob("error_*.json")}
    assert on_disk == set(_ERROR_TYPES)


@pytest.mark.parametrize("name", sorted(_ERROR_TYPES))
async def test_error_fixture_maps_to_typed_error(name):
    fx = _load(name)
    resp = fx["response"]
    h = Http(BASE, API_KEY)
    h._client = httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(lambda req: httpx.Response(resp["status"], json=resp["body"])),
    )
    with pytest.raises(errors.PaymosError) as exc:
        if fx["request"]["method"] == "GET":
            await h.get(fx["request"]["path"])
        else:
            await h.post(fx["request"]["path"], fx["request"]["body"])
    assert type(exc.value) is _ERROR_TYPES[name]
    assert exc.value.message == resp["body"]["error"]
    assert exc.value.status == resp["status"]


async def test_error_fixtures_through_the_wallet():
    # The same mapping reached through the public methods a caller actually uses.
    srv = _Server("error_401")
    with pytest.raises(errors.AuthError):
        await srv.wallet().balances()
    srv = _Server("error_404_movement")
    with pytest.raises(errors.PaymosError, match="movement not found"):
        await srv.wallet().movement("does-not-exist")
    srv = _Server("assets", "error_400")
    with pytest.raises(errors.PaymosError, match="invalid mode"):
        await srv.wallet().quote_withdraw("USDC@base", "10", TO, mode="exact-out")


_HTML_404 = "<!doctype html><html><head><title>Not found</title></head><body>" + "<p>lorem</p>" * 500 + "</body></html>"


@pytest.mark.parametrize("status", [404, 200, 502])
async def test_non_json_html_body_is_a_typed_error(status):
    # A proxy's HTML page in front of /vault/ — on an error status AND on a 200 (SPA fallback).
    def handler(req):
        return httpx.Response(status, text=_HTML_404, headers={"content-type": "text/html; charset=utf-8"})

    w = Wallet(VaultSecret.pack(API_KEY, 1, None), base_url=BASE)
    w._http._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    with pytest.raises(errors.PaymosError) as exc:
        await w.assets()
    assert type(exc.value) is errors.PaymosError
    assert exc.value.status == status
    msg = exc.value.message
    assert "non-JSON" in msg and f"HTTP {status}" in msg and "text/html" in msg and "base_url" in msg
    assert len(msg) < 500  # names the page, does not become the page


async def test_empty_2xx_body_is_a_typed_error():
    h = Http(BASE, API_KEY)
    h._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(errors.PaymosError, match="non-JSON"):
        await h.get("/vault/v1/balances")


# --- status vocabulary -----------------------------------------------------------

def test_status_sets_use_only_server_words():
    vocab = _load("vocabulary")
    words = set(vocab["movement_statuses"])
    # Every status the SDK branches on is one the server can send — no internal words.
    assert _TERMINAL_STATUSES <= words
    assert _PROGRESSED_STATUSES <= words
    assert set(vocab["sign_statuses"]) <= words


@pytest.mark.parametrize("status", _load("vocabulary")["movement_statuses"])
def test_every_movement_status_is_classified(status):
    terminal = {"completed", "failed", "refunded", "expired", "cancelled"}
    progressed = {"processing", "completed", "refunded"}
    assert (status in _TERMINAL_STATUSES) == (status in terminal)
    assert (status in _PROGRESSED_STATUSES) == (status in progressed)


async def test_wait_stops_on_every_terminal_status_and_only_those():
    vocab = _load("vocabulary")["movement_statuses"]
    body = _load("movement")["response"]["body"]
    for status in vocab:
        def handler(req, status=status):
            return httpx.Response(200, json={**body, "status": status})

        w = Wallet(VaultSecret.pack(API_KEY, 1, None), base_url=BASE)
        w._http._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
        if status in _TERMINAL_STATUSES:
            assert (await w.wait("mv_3", timeout=0, poll=0)).status == status
        else:
            with pytest.raises(errors.PaymosError, match="timed out"):
                await w.wait("mv_3", timeout=0, poll=0)


async def test_sign_begin_2xx_without_token_is_a_typed_error(stub_frost):
    srv = _Server("assets", "quote_swap", "sign_begin")
    begin = srv.routes[("POST", "/vault/v1/movements/mv_3/sign/begin")]["body"]
    del begin["token"]
    with pytest.raises(errors.PaymosError, match="malformed sign/begin response"):
        await srv.wallet(_SHARE).swap("USDC@base", "USDT@base", "5")
    assert stub_frost["sign"] == 0
