"""
Shared utilities for scanner.py and backtest.py.
No I/O, no strategy logic.
"""
from __future__ import annotations


def normalize_timeframe(s: str) -> str:
    """
    Normalise a user-supplied timeframe string to the canonical form
    used by Bybit and the strategy config.

    Examples:
        "1h"  → "1H"
        "4h"  → "4H"
        "15"  → "15m"
        "30"  → "30m"
        "60"  → "1H"
        "240" → "4H"
        "15m" → "15m"   (unchanged)
        "1H"  → "1H"    (unchanged)
        "1D"  → "1D"    (unchanged)
    """
    s = s.strip()

    # Pure-numeric aliases
    _NUMERIC: dict[str, str] = {
        "1":    "1m",
        "5":    "5m",
        "15":   "15m",
        "30":   "30m",
        "60":   "1H",
        "240":  "4H",
        "1440": "1D",
        "10080": "1W",
    }
    if s in _NUMERIC:
        return _NUMERIC[s]

    # Suffix normalisation: m stays lowercase, H/D/W uppercase
    if len(s) > 1 and s[-1].isalpha() and s[:-1].isdigit():
        num = s[:-1]
        unit = s[-1].lower()
        _UNIT_CASE = {"m": "m", "h": "H", "d": "D", "w": "W"}
        return num + _UNIT_CASE.get(unit, unit.upper())

    return s
