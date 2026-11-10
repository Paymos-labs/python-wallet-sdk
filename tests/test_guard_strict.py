"""The blind-sign guard reads a disclosed message the way the on-chain verifier reads it.

``json.loads`` keeps the LAST of a repeated object key; the verifier re-parses the exact signed bytes
with a reader that rejects a repeated field. A message naming one key twice is therefore refused, at
every depth, instead of being read one way. Runs without the native core: it drives the guard
(a static method) directly over hand-built disclosures.
"""

import hashlib
import json
from pathlib import Path

import pytest

from paymos._digest import payload_digest_hex
from paymos._types import Quote
from paymos._wallet import Wallet, _parse_strict
from paymos.errors import PaymosError

_SIGNER = "ab12cd34"
_KP = {"verifying_key": "AB12CD34"}
_TOKEN = "usdc-base"
_FINGERPRINTS = frozenset({hashlib.sha256(_TOKEN.encode()).hexdigest()})
_RECIPIENT = "recipient.example"
_NONCE = bytes(range(32))

# What the verifier would execute (5000 USDC, against a debit of 100) and what a last-key-wins
# reader is shown instead (10).
_REAL = '{"intent":"transfer","receiver_id":"attacker.example","tokens":{"usdc-base":"5000000000"}}'
_DECOY = '{"intent":"transfer","receiver_id":"receiver.example","tokens":{"usdc-base":"10"}}'


def _quote(debit="100"):
    return Quote.from_dict({
        "movement_id": "mv_1", "mode": "exact_in",
        "send": {"amount": debit, "asset": "USDC@base"},
        "debit": {"amount": debit, "asset": "USDC@base"},
        "receive": {"amount": "1", "min": "1", "asset": "USDT@base"},
        "fees": {"asset": "USDC@base", "platform": "0", "network": "0", "route": None, "total": "0"},
        "expires_at": "2026-01-01T00:00:00+00:00", "sources": 1,
    })


def _source(message: str):
    package = {"message": payload_digest_hex(message, _NONCE, _RECIPIENT)}
    disclosure = {"message": message, "nonce": _NONCE.hex(), "recipient": _RECIPIENT}
    return package, disclosure


def _guard(message: str):
    package, disclosure = _source(message)
    Wallet._verify_signing_disclosure(_quote(), [package], [disclosure], _KP, _FINGERPRINTS)


def test_the_servers_own_message_shape_still_signs():
    # the server's Nep413Signer.BuildTransferIntentMessage: deadline, one transfer, signer_id last.
    _guard('{"deadline":"2026-01-01T00:00:00.000Z","intents":[{"intent":"transfer",'
           '"receiver_id":"deposit\\"route.example","tokens":{"usdc-base":"100"}}],"signer_id":"ab12cd34"}')


@pytest.mark.parametrize("message", [
    '{"signer_id":"ab12cd34","intents":[' + _REAL + '],"intents":[' + _DECOY + ']}',
    # The same key spelled with an escape is the same key.
    '{"signer_id":"ab12cd34","intents":[' + _REAL + '],"\\u0069ntents":[' + _DECOY + ']}',
    '{"signer_id":"ffff","intents":[' + _DECOY + '],"signer_id":"ab12cd34"}',
    '{"signer_id":"ab12cd34","intents":[{"intent":"ft_withdraw","intent":"transfer","tokens":{"usdc-base":"10"}}]}',
    '{"signer_id":"ab12cd34","intents":[{"intent":"transfer","tokens":{"usdc-base":"5000000000"},"tokens":{"usdc-base":"10"}}]}',
    '{"signer_id":"ab12cd34","intents":[{"intent":"transfer","tokens":{"usdc-base":"5000000000","usdc-base":"10"}}]}',
    '{"signer_id":"ab12cd34","intents":[{"intent":"transfer","tokens":{"usdc-base":"10"},"memo":{"a":1,"a":2}}]}',
])
def test_a_repeated_key_is_refused_not_read_last(message):
    # json.loads alone would have read the last value — that is the reading this refuses.
    assert json.loads(message)
    with pytest.raises(PaymosError, match="co-sign refused: unreadable message"):
        _guard(message)


def test_a_second_spelling_is_ignored_like_the_verifier_ignores_it():
    # Case-sensitive on both sides: "Intents" is another, unread key — the guard checks "intents",
    # which here moves more than the debit, exactly what the verifier would execute.
    message = '{"signer_id":"ab12cd34","intents":[' + _REAL + '],"Intents":[' + _DECOY + ']}'
    with pytest.raises(PaymosError, match="above the approved debit"):
        _guard(message)


def test_a_message_nested_past_the_recursion_limit_is_the_guards_refusal():
    # The json scanner recurses per bracket and raises RecursionError, not ValueError, past the
    # interpreter's limit; that is still a refusal, typed, never a raw error out of the money path.
    deep = '{"signer_id":"ab12cd34","intents":[' + _DECOY + '],"memo":' + "[" * 100_000 + "]" * 100_000 + "}"
    with pytest.raises(PaymosError, match="co-sign refused: unreadable message"):
        _guard(deep)
    _guard('{"signer_id":"ab12cd34","intents":[' + _DECOY + '],"memo":' + "[" * 8 + "]" * 8 + "}")


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a":1,"a":1}'])
def test_parse_strict_refuses_what_json_loads_lets_through(text):
    with pytest.raises(ValueError):
        _parse_strict(text)


def test_every_contract_fixture_parses_strictly():
    here = Path(__file__).resolve()
    fixtures = next(
        (p for base in here.parents for p in (base / "contract" / "fixtures", base / "sdk" / "contract" / "fixtures")
         if (p / "assets.json").is_file()),
        None,
    )
    assert fixtures is not None, "contract fixtures not found"
    for path in sorted(fixtures.glob("*.json")):
        _parse_strict(path.read_text(encoding="utf-8"))
    begin = json.loads((fixtures / "sign_begin.json").read_text(encoding="utf-8"))["response"]["body"]
    for disclosure in begin["messages"]:
        _parse_strict(disclosure["message"])
