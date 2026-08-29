import pytest
from paymos._secret import VaultSecret

def test_pack_then_parse_roundtrips_full():
    s = VaultSecret.pack("vk_live_abc", 42, '{"kp":"x"}')
    assert s.startswith("vs_live_")
    p = VaultSecret.parse(s)
    assert p.api_key == "vk_live_abc" and p.vault_id == 42 and p.share == '{"kp":"x"}'
    assert p.has_share

def test_read_secret_has_no_share():
    p = VaultSecret.parse(VaultSecret.pack("vk_live_r", 7, None))
    assert p.share is None and not p.has_share

def test_parse_rejects_garbage():
    for bad in ["", "nope", "vs_live_@@@", "vk_live_abc"]:
        with pytest.raises(ValueError):
            VaultSecret.parse(bad)

def test_repr_never_leaks_share():
    s = VaultSecret(api_key="vk_live_deadbeefcafe", vault_id=7, share='{"signing_share":"S3CR3T_SHARE_VALUE"}')
    r = repr(s)
    assert "S3CR3T_SHARE_VALUE" not in r
    assert s.share not in r
    assert "signing_share" not in r
    assert "vault_id=7" in r and "has_share=True" in r
    assert "deadbeefcafe" not in r          # the random tail of the api key is masked
    assert r.startswith("VaultSecret(api_key='vk_live_")
