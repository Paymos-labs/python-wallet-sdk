"""An idempotency key that is empty is refused, before anything reaches the server.

``idempotency_key or uuid4().hex`` used to turn ``""`` (a job id that was never set, say) into a fresh
random key per call: a retry then carried a different key and paid again, while the caller believed
the call was deduplicated. Omitting the key is still fine, and still gets a fresh one.
"""

import httpx
import pytest

from paymos._secret import VaultSecret
from paymos._wallet import Wallet, _idempotency_key
from paymos.errors import PaymosError

_FULL = VaultSecret.pack("vk_live_test", 1, '{"verifying_key":"ab"}')


def _wallet(seen):
    def handler(req):
        seen.append(req.url.path)
        raise AssertionError(f"nothing may reach the server: {req.url.path}")

    w = Wallet(_FULL, base_url="https://api.test")
    w._http._client = httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return w


@pytest.mark.parametrize("key", ["", " ", "\t\n"])
async def test_an_empty_key_is_refused_before_any_request(key):
    seen = []
    w = _wallet(seen)
    with pytest.raises(PaymosError, match="idempotency_key is empty"):
        await w.withdraw("USDC@base", "10", "0xabc", idempotency_key=key)
    with pytest.raises(PaymosError, match="idempotency_key is empty"):
        await w.swap("USDC@base", "USDT@base", "5", idempotency_key=key)
    assert seen == []


def test_an_omitted_key_is_a_fresh_one_and_a_given_key_is_sent_as_given():
    a, b = _idempotency_key(None), _idempotency_key(None)
    assert a != b and len(a) == 32
    assert _idempotency_key(" payout-42 ") == " payout-42 "
