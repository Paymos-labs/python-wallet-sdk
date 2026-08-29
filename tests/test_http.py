import httpx
import pytest

from paymos import errors
from paymos._http import Http


def _http(handler):
    """Build an Http whose transport is a MockTransport, per the brief."""
    h = Http("https://api.test", "vk_live_test")
    h._client = httpx.AsyncClient(
        base_url="https://api.test",
        transport=httpx.MockTransport(handler),
    )
    return h


async def test_maps_status_codes():
    def handler(req):
        assert req.headers["authorization"] == "Bearer vk_live_test"
        table = {
            "/a": (401, {"error": "invalid or missing API key"}),
            "/b": (409, {"error": "idempotency key reused with a different request"}),
            "/c": (400, {"error": "cross-asset withdraw is not allowed"}),
            "/ok": (200, {"hello": 1}),
        }
        code, body = table[req.url.path]
        hdrs = {"Retry-After": "60"} if code == 429 else {}
        return httpx.Response(code, json=body, headers=hdrs)

    h = _http(handler)
    assert (await h.get("/ok")) == {"hello": 1}
    for path, exc in [
        ("/a", errors.AuthError),
        ("/b", errors.Conflict),
        ("/c", errors.CrossAssetWithdrawNotAllowed),
    ]:
        with pytest.raises(exc):
            await h.get(path)


def _single(code, body, headers=None):
    """A handler that always answers with one canned response."""
    def handler(req):
        assert req.headers["authorization"] == "Bearer vk_live_test"
        return httpx.Response(code, json=body, headers=headers or {})
    return handler


async def test_403_is_forbidden():
    h = _http(_single(403, {"error": "this key lacks the required scope"}))
    with pytest.raises(errors.Forbidden):
        await h.get("/x")


async def test_404_is_base_paymos_error():
    # The spec has no NotFound type — a 404 must surface as the base PaymosError.
    h = _http(_single(404, {"error": "movement not found"}))
    with pytest.raises(errors.PaymosError) as exc:
        await h.get("/x")
    # Base, not any subclass.
    assert type(exc.value) is errors.PaymosError


async def test_429_carries_retry_after():
    h = _http(_single(429, {"error": "slow down"}, {"Retry-After": "60"}))
    with pytest.raises(errors.RateLimited) as exc:
        await h.get("/x")
    assert exc.value.retry_after == 60


async def test_429_without_header_has_none_retry_after():
    h = _http(_single(429, {"error": "slow down"}))
    with pytest.raises(errors.RateLimited) as exc:
        await h.get("/x")
    assert exc.value.retry_after is None


async def test_400_insufficient_funds():
    h = _http(_single(400, {"error": "insufficient unlocked balance to cover the network fee"}))
    with pytest.raises(errors.InsufficientFunds):
        await h.get("/x")


async def test_400_route_unavailable():
    h = _http(_single(400, {"error": "the route can't be quoted right now — try again in a moment."}))
    with pytest.raises(errors.RouteUnavailable):
        await h.get("/x")


async def test_400_quote_expired():
    h = _http(_single(400, {"error": "This quote expired — open it again for a fresh price."}))
    with pytest.raises(errors.QuoteExpired):
        await h.get("/x")


async def test_400_slippage_exceeded():
    h = _http(_single(400, {"error": "slippage tolerance exceeded"}))
    with pytest.raises(errors.SlippageExceeded):
        await h.get("/x")


async def test_400_generic_is_base_paymos_error():
    h = _http(_single(400, {"error": "commitments are required"}))
    with pytest.raises(errors.PaymosError) as exc:
        await h.get("/x")
    assert type(exc.value) is errors.PaymosError


async def test_500_is_base_paymos_error():
    h = _http(_single(500, {"error": "boom"}))
    with pytest.raises(errors.PaymosError) as exc:
        await h.get("/x")
    assert type(exc.value) is errors.PaymosError


async def test_sign_envelope_error_key_is_extracted():
    # Sign endpoints answer {"ok":false,"status":"failed","error":"…"} — the `error`
    # key still drives the mapping.
    h = _http(_single(400, {"ok": False, "status": "failed", "error": "cross-asset withdraw is not allowed"}))
    with pytest.raises(errors.CrossAssetWithdrawNotAllowed):
        await h.get("/x")


async def test_non_json_body_falls_back_to_text():
    def handler(req):
        return httpx.Response(500, text="upstream exploded")
    h = _http(handler)
    with pytest.raises(errors.PaymosError) as exc:
        await h.get("/x")
    assert "upstream exploded" in str(exc.value)


async def test_error_carries_status_and_message():
    h = _http(_single(401, {"error": "invalid or missing API key"}))
    with pytest.raises(errors.AuthError) as exc:
        await h.get("/x")
    assert exc.value.status == 401
    assert exc.value.message == "invalid or missing API key"


async def test_post_sends_bearer_and_returns_json():
    def handler(req):
        assert req.headers["authorization"] == "Bearer vk_live_test"
        assert req.method == "POST"
        return httpx.Response(200, json={"ok": True})
    h = _http(handler)
    assert (await h.post("/thing", {"a": 1})) == {"ok": True}


async def test_post_extra_headers_merge_with_bearer():
    def handler(req):
        assert req.headers["authorization"] == "Bearer vk_live_test"
        assert req.headers["idempotency-key"] == "abc123"
        return httpx.Response(200, json={"ok": True})
    h = _http(handler)
    assert (await h.post("/thing", {"a": 1}, headers={"Idempotency-Key": "abc123"})) == {"ok": True}


async def test_get_passes_query_params():
    def handler(req):
        assert req.url.params.get("cursor") == "xyz"
        return httpx.Response(200, json={"items": []})
    h = _http(handler)
    assert (await h.get("/list", params={"cursor": "xyz"})) == {"items": []}
