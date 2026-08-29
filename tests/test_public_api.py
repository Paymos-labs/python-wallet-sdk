"""The public package surface: what ``import paymos`` exposes, plus ``from_env``.

Task 9 wires the top-level exports (``Wallet`` + every error type + every
dataclass) and extends ``Wallet.from_env`` to honor ``PAYMOS_BASE_URL``. These
tests pin the surface (the names importable from ``paymos``) and the env
resolution: ``PAYMOS_VAULT_SECRET`` is required (missing → ``PaymosError``), an
explicit ``base_url`` wins, ``PAYMOS_BASE_URL`` is the fallback, and the built-in
default is used last.

No network: ``from_env`` only parses the secret and constructs the ``Http``
client (whose ``base_url`` we read back off ``wallet._http._base_url``); nothing
is dialed.
"""

import pytest

import paymos
from paymos import Wallet
from paymos._secret import VaultSecret
from paymos.errors import PaymosError

# A packed READ secret (no share): enough to construct a Wallet, never to sign.
READ_SECRET = VaultSecret.pack("vk_live_test", 1, None)
_DEFAULT_BASE_URL = "https://wallet.paymos.io"


def test_public_surface_imports():
    """The headline names are importable straight off ``paymos`` and listed in
    ``__all__`` — Wallet, a representative error, and a representative dataclass."""
    from paymos import Wallet, AuthError, Quote  # noqa: F401

    # Not a literal. The invariant is that what the package reports and what the manifest declares
    # are the same string; a literal here is a fourth place to forget, and it was already wrong.
    import pathlib, tomllib

    manifest = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(manifest.read_text())["project"]["version"]
    assert paymos.__version__ == declared, f"package reports {paymos.__version__}, manifest says {declared}"
    for name in ("Wallet", "AuthError", "Quote"):
        assert name in paymos.__all__, f"{name} missing from __all__"


def test_all_exports_are_resolvable():
    """Every name in ``__all__`` is a real attribute on the package (no dangling
    export that would break ``from paymos import *``)."""
    assert paymos.__all__, "__all__ must not be empty"
    for name in paymos.__all__:
        assert hasattr(paymos, name), f"{name} in __all__ but not on paymos"


def test_error_and_dataclass_names_exported():
    """The full error hierarchy and dataclass set are on the public surface."""
    from paymos import (  # noqa: F401
        PaymosError,
        AuthError,
        Forbidden,
        InsufficientFunds,
        QuoteExpired,
        SlippageExceeded,
        CrossAssetWithdrawNotAllowed,
        RouteUnavailable,
        RateLimited,
        Conflict,
        Asset,
        Balance,
        Quote,
        Fees,
        RouteFee,
        Amount,
        Receive,
        Movement,
    )


def test_from_env_constructs_wallet(monkeypatch):
    """``PAYMOS_VAULT_SECRET`` set → ``from_env`` builds a ``Wallet`` on the
    default base URL (no ``PAYMOS_BASE_URL``, no explicit arg)."""
    monkeypatch.setenv("PAYMOS_VAULT_SECRET", READ_SECRET)
    monkeypatch.delenv("PAYMOS_BASE_URL", raising=False)

    w = Wallet.from_env()

    assert isinstance(w, Wallet)
    assert w._http._base_url == _DEFAULT_BASE_URL


def test_from_env_missing_secret_raises(monkeypatch):
    """No ``PAYMOS_VAULT_SECRET`` → ``PaymosError`` (not a bare KeyError)."""
    monkeypatch.delenv("PAYMOS_VAULT_SECRET", raising=False)

    with pytest.raises(PaymosError):
        Wallet.from_env()


def test_from_env_honors_base_url_env(monkeypatch):
    """``PAYMOS_BASE_URL`` overrides the built-in default."""
    monkeypatch.setenv("PAYMOS_VAULT_SECRET", READ_SECRET)
    monkeypatch.setenv("PAYMOS_BASE_URL", "https://staging.example.com")

    w = Wallet.from_env()

    assert w._http._base_url == "https://staging.example.com"


def test_from_env_explicit_base_url_wins_over_env(monkeypatch):
    """An explicit ``base_url=`` argument beats ``PAYMOS_BASE_URL``."""
    monkeypatch.setenv("PAYMOS_VAULT_SECRET", READ_SECRET)
    monkeypatch.setenv("PAYMOS_BASE_URL", "https://staging.example.com")

    w = Wallet.from_env(base_url="https://explicit.example.com")

    assert w._http._base_url == "https://explicit.example.com"


def test_constructor_still_works(monkeypatch):
    """The direct ``Wallet(secret, base_url=...)`` constructor is unchanged."""
    w = Wallet(READ_SECRET, base_url="https://direct.example.com")

    assert isinstance(w, Wallet)
    assert w._http._base_url == "https://direct.example.com"
