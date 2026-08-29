"""paymos — Python SDK for non-custodial vault control.

The public surface is :class:`Wallet` (read / quote / move one vault), the typed
:mod:`~paymos.errors` hierarchy, and the read-model dataclasses. Import them
straight off the package::

    from paymos import Wallet, Quote, InsufficientFunds

Keep this free of native imports at package top-level so importing ``paymos``
stays light and never hard-requires the compiled extension for read-only use; the
native bridge lives at ``paymos._core`` and is imported lazily inside
:mod:`paymos._frost` only when a co-sign actually needs it (import it explicitly
where needed: ``from paymos import _core``).
"""

from __future__ import annotations

from paymos._types import (
    Amount,
    Asset,
    Balance,
    Fees,
    Movement,
    Quote,
    Receive,
    RouteFee,
)
from paymos._wallet import Wallet
from paymos.errors import (
    AuthError,
    Conflict,
    CrossAssetWithdrawNotAllowed,
    Forbidden,
    InsufficientFunds,
    PaymosError,
    QuoteExpired,
    RateLimited,
    RouteUnavailable,
    SlippageExceeded,
)

# Read from the installed distribution rather than typed here. This number lived in three places —
# pyproject.toml, this line, and an assertion in the test suite — and all three had drifted apart
# before anyone noticed: the package said 0.1.4 while its own test demanded 0.1.2. A version is a
# packaging fact, and pyproject.toml is where packaging facts live.
try:
    from importlib.metadata import PackageNotFoundError, version as _installed_version

    __version__ = _installed_version("paymos-wallet")
except PackageNotFoundError:  # running straight from a source tree, never installed
    __version__ = "0.0.0+local"

__all__ = [
    # entry point
    "Wallet",
    # errors
    "PaymosError",
    "AuthError",
    "Forbidden",
    "InsufficientFunds",
    "QuoteExpired",
    "SlippageExceeded",
    "CrossAssetWithdrawNotAllowed",
    "RouteUnavailable",
    "RateLimited",
    "Conflict",
    # read-model dataclasses
    "Asset",
    "Balance",
    "Quote",
    "Fees",
    "RouteFee",
    "Amount",
    "Receive",
    "Movement",
]
