from __future__ import annotations
import re

_DECIMAL = re.compile(r"^\d+(\.\d+)?$")

def parse_units(human: str, decimals: int) -> str:
    if decimals < 0:
        raise ValueError("decimals must be >= 0")
    s = (human or "").strip()
    if not _DECIMAL.match(s):
        raise ValueError(f"not a non-negative decimal: {human!r}")
    whole, _, frac = s.partition(".")
    if len(frac) > decimals:
        raise ValueError(f"more than {decimals} fractional digits: {human!r}")
    frac = frac.ljust(decimals, "0")
    raw = int(whole + frac) if decimals else int(whole)
    return str(raw)

def format_units(raw: str, decimals: int) -> str:
    n = int(raw)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if decimals == 0:
        return f"{sign}{n}"
    s = str(n).rjust(decimals + 1, "0")
    whole, frac = s[:-decimals], s[-decimals:].rstrip("0")
    return f"{sign}{whole}.{frac}" if frac else f"{sign}{whole}"
