"""``vs_live_`` vault-secret handling — pure UNIT tests (no network, moves nothing).

Part of the e2e suite because the secret format IS the prod credential contract: the
string minted by the wallet's "Vault API" screen must parse on any integrator's box.

What each test proves:

- ``test_env_secret_parses_to_full_scope_live_identity`` — the REAL prod-issued
  ``PAYMOS_VAULT_SECRET`` parses to an inner ``vk_live_`` API key, an integer vault id
  and a present FROST share (``has_share is True``), and its fields survive a
  ``pack() -> parse()`` round trip (exactly what conftest's ``read_only_wallet`` does).
- ``test_pack_parse_round_trip_preserves_all_fields`` — ``pack()`` emits a ``vs_live_``
  string and ``parse()`` recovers api_key / vault_id / share verbatim.
- ``test_read_only_pack_has_share_false`` — packing with ``share=None`` yields a
  read-only secret (``has_share is False``) — the flag the SDK's client-side co-sign
  guard keys off before any network call.
- ``test_parse_rejects_non_vs_live_strings`` / ``..._non_string_input`` /
  ``..._malformed_payloads`` — anything that is not a well-formed ``vs_live_`` string
  fails fast with ``ValueError``, locally.
- ``test_parse_rejects_inner_api_key_that_is_not_vk_live`` — a bundle whose inner key
  is not ``vk_live_`` is rejected (a mis-pasted credential can't silently half-work).
- ``test_repr_masks_api_key_and_never_renders_share`` /
  ``test_env_secret_repr_leaks_no_live_material`` — ``repr()``/``str()`` show only
  ``vk_live_…`` and NEVER the FROST share, so a stray log line or traceback cannot
  carry a usable credential.
"""

from __future__ import annotations

import base64
import os

import pytest

from paymos._secret import VaultSecret

API_KEY_PREFIX = "vk_live_"
SECRET_PREFIX = "vs_live_"


def _env_secret() -> str:
    secret = os.environ.get("PAYMOS_VAULT_SECRET")
    if not secret:
        pytest.skip("PAYMOS_VAULT_SECRET is not set — skipping live-secret checks")
    return secret


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# --------------------------------------------------------------------------- parsing


def test_env_secret_parses_to_full_scope_live_identity():
    raw = _env_secret()
    assert raw.startswith(SECRET_PREFIX)

    s = VaultSecret.parse(raw)
    assert s.api_key.startswith(API_KEY_PREFIX)
    assert isinstance(s.vault_id, int)
    assert s.has_share is True  # full-scope secret carries the FROST share

    # The real fields survive re-pack -> parse — the exact derivation conftest's
    # read_only_wallet performs on this secret.
    assert VaultSecret.parse(VaultSecret.pack(s.api_key, s.vault_id, s.share)) == s


def test_pack_parse_round_trip_preserves_all_fields():
    api_key = "vk_live_round_trip_0123456789abcdef"
    share = "ZnJvc3Qtc2hhcmU_round-trip-share-material"

    packed = VaultSecret.pack(api_key, 42, share)
    assert packed.startswith(SECRET_PREFIX)

    s = VaultSecret.parse(packed)
    assert s.api_key == api_key
    assert s.vault_id == 42
    assert s.share == share
    assert s.has_share is True


def test_read_only_pack_has_share_false():
    s = VaultSecret.parse(VaultSecret.pack("vk_live_read_only_key", 7, None))
    assert s.share is None
    assert s.has_share is False


# --------------------------------------------------------------------------- rejects


@pytest.mark.parametrize(
    "not_a_secret",
    [
        "",
        "vk_live_abc123",  # an API key pasted where the vault SECRET belongs
        "vs_test_eyJrIjoidmtfbGl2ZV9hIn0",  # wrong-environment prefix
        "eyJrIjoidmtfbGl2ZV9hIiwidiI6MX0",  # bare payload, prefix stripped
        "VS_LIVE_ABC",  # prefix is case-sensitive
    ],
    ids=["empty", "api-key-not-secret", "vs_test-prefix", "no-prefix", "uppercased"],
)
def test_parse_rejects_non_vs_live_strings(not_a_secret):
    with pytest.raises(ValueError) as exc:
        VaultSecret.parse(not_a_secret)
    assert "vs_live_" in str(exc.value)


@pytest.mark.parametrize(
    "junk",
    [None, 12345, b"vs_live_abc", ["vs_live_abc"]],
    ids=["none", "int", "bytes", "list"],
)
def test_parse_rejects_non_string_input(junk):
    with pytest.raises(ValueError):
        VaultSecret.parse(junk)


@pytest.mark.parametrize(
    "payload",
    [
        "",  # nothing after the prefix
        "AAAA",  # decodes, but not JSON
        _b64url(b'["k","v","s"]'),  # JSON, but not an object
        _b64url(b'{"v":1,"s":null}'),  # object missing the api key
    ],
    ids=["empty-payload", "not-json", "json-list", "missing-key"],
)
def test_parse_rejects_malformed_payloads(payload):
    with pytest.raises(ValueError) as exc:
        VaultSecret.parse(SECRET_PREFIX + payload)
    assert "malformed" in str(exc.value)


@pytest.mark.parametrize(
    "bad_key",
    ["sk_prod_not_ours", "vk_test_dev_key", "", None],
    ids=["foreign-prefix", "vk_test-prefix", "empty", "null"],
)
def test_parse_rejects_inner_api_key_that_is_not_vk_live(bad_key):
    packed = VaultSecret.pack(bad_key, 1, "irrelevant-share")
    with pytest.raises(ValueError) as exc:
        VaultSecret.parse(packed)
    assert "no valid api key" in str(exc.value)


# --------------------------------------------------------------------- repr hygiene


def test_repr_masks_api_key_and_never_renders_share():
    tail = "0deadbeefcafef00d_TAIL_MUST_NOT_LEAK"
    share = "RlJPU1Qtc2hhcmU_SHARE_MUST_NOT_LEAK"
    s = VaultSecret(api_key=API_KEY_PREFIX + tail, vault_id=9, share=share)

    for rendered in (repr(s), str(s), f"{s}"):  # every stringification path
        assert "vk_live_…" in rendered  # masked prefix survives for debuggability
        assert tail not in rendered  # ...but the secret tail never does
        assert share not in rendered  # the FROST share is never rendered
    assert "has_share=True" in repr(s)  # share presence surfaces as a bool only


def test_env_secret_repr_leaks_no_live_material():
    s = VaultSecret.parse(_env_secret())
    rendered = repr(s)

    assert "vk_live_…" in rendered
    assert s.api_key not in rendered
    tail = s.api_key[len(API_KEY_PREFIX):]
    if len(tail) >= 8:  # real keys are long; guard against degenerate substrings
        assert tail not in rendered

    assert s.has_share, "prod full-scope secret must carry the FROST share"
    assert s.share not in rendered
