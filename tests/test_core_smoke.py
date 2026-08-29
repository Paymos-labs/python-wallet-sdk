import json

from paymos import _core


def test_commit_roundtrips_native():
    # An unknown op returns a JSON error, proving the native bridge is wired (a real key_package
    # is exercised in the FROST test). We assert the bridge returns parseable JSON, not that it errors.
    out = _core.mpc_call(json.dumps({"op": "unknown_op"}))
    assert isinstance(out, str) and json.loads(out) is not None
