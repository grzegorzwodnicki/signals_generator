"""
data.py — OHLCV fetching from Bybit + local CSV cache.

No strategy logic — pure data plumbing.
Returns DataFrames with columns: timestamp(int ms), open, high, low, close, volume.
strategy.py's normalize_ohlcv_dataframe() handles the final conversion.
"""

from __future__ import annotations

import os
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import requests

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_THIS_DIR, "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

BYBIT_BASE = "https://api.bybit.com"
RATE_LIMIT_SEM = threading.Semaphore(20)

_TF_MAP: Dict[str, str] = {
    "1W": "W", "1D": "D", "4H": "240", "1H": "60",
    "30m": "30", "15m": "15", "5m": "5", "1m": "1",
}


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_env() -> None:
    root = os.path.dirname(_THIS_DIR)
    env_path = os.path.join(root, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


_load_env()


# ---------------------------------------------------------------------------
# Low-level Bybit helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict, timeout: int = 15) -> dict:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        return r.json()
    except Exception:
        return {}


def _bybit_candles_raw(
    symbol: str,
    interval: str,
    limit: int = 200,
    end_time_ms: Optional[int] = None,
) -> list:
    params: dict = {
        "category": "linear",
        "symbol": symbol,
        "interval": _TF_MAP.get(interval, interval),
        "limit": limit,
    }
    if end_time_ms:
        params["end"] = end_time_ms

    for attempt in range(5):
        try:
            with RATE_LIMIT_SEM:
                d = _get_json(f"{BYBIT_BASE}/v5/market/kline", params)
            rc = d.get("retCode")
            if rc == 0:
                return d["result"]["list"]
            if rc in (10002, 10006, 10018) or "rate" in d.get("retMsg", "").lower():
                time.sleep(2 ** (attempt + 2))
                continue
            return []
        except Exception:
            time.sleep(2 ** attempt)
    return []


def _parse_raw(raw: list) -> list:
    """Convert Bybit raw list → list of dicts, sorted ascending by timestamp."""
    out = [
        {
            "timestamp": int(c[0]),  # ms
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        }
        for c in raw
    ]
    out.sort(key=lambda x: x["timestamp"])
    return out


# ---------------------------------------------------------------------------
# Symbol list
# ---------------------------------------------------------------------------

def get_top_symbols(
    n: int = 100,
    min_turnover_usd: float = 5_000_000,
) -> List[Dict]:
    """Return top-N USDT perpetual symbols by 24h turnover."""
    d = _get_json(f"{BYBIT_BASE}/v5/market/tickers", {"category": "linear"})
    if d.get("retCode") != 0:
        return []

    items = []
    for t in d["result"]["list"]:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        turnover = float(t.get("turnover24h", 0))
        if turnover < min_turnover_usd:
            continue
        items.append({
            "symbol":   sym,
            "price":    float(t.get("lastPrice", 0)),
            "volume":   float(t.get("volume24h", 0)),
            "turnover": turnover,
        })

    items.sort(key=lambda x: x["turnover"], reverse=True)
    return items[:n]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> str:
    start_s = datetime.utcfromtimestamp(start_ms / 1000).strftime("%Y%m%d")
    end_s   = datetime.utcfromtimestamp(end_ms   / 1000).strftime("%Y%m%d")
    return os.path.join(CACHE_DIR, f"{symbol}_{interval}_{start_s}_{end_s}.csv")


def _load_cache(path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        df["timestamp"] = df["timestamp"].astype("int64")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return None


def _save_cache(df: pd.DataFrame, path: str) -> None:
    try:
        df.to_csv(path, index=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main fetch function — 400 candles (2 batches of 200)
# ---------------------------------------------------------------------------

def fetch_candles(
    symbol: str,
    interval: str,
    end_time_ms: Optional[int] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch ~400 OHLCV candles from Bybit or local cache.

    Returns DataFrame with columns:
      timestamp (int, ms), open, high, low, close, volume

    Pass to strategy.analyze_strategy() — normalize_ohlcv_dataframe()
    will convert the ms timestamps to pd.Timestamp.
    """
    b1 = _bybit_candles_raw(symbol, interval, 200, end_time_ms)
    if not b1:
        return pd.DataFrame()

    oldest_ts = min(int(c[0]) for c in b1)
    b2 = _bybit_candles_raw(symbol, interval, 200, oldest_ts - 1)

    rows = _parse_raw(b1 + b2)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    if use_cache and len(df) > 0:
        start_ms   = int(df["timestamp"].min())
        end_ms_val = int(df["timestamp"].max())
        path = _cache_path(symbol, interval, start_ms, end_ms_val)
        _save_cache(df, path)

    return df


# ---------------------------------------------------------------------------
# Long-range fetch for backtest (multiple API batches)
# ---------------------------------------------------------------------------

def fetch_candles_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch all candles between start_ms and end_ms (inclusive).
    Makes multiple API calls if necessary.
    Uses cache if an exact match exists.
    """
    cache_path = _cache_path(symbol, interval, start_ms, end_ms)
    if use_cache:
        cached = _load_cache(cache_path)
        if cached is not None and len(cached) > 0:
            # Verify coverage
            if (int(cached["timestamp"].min()) <= start_ms + 60_000 and
                    int(cached["timestamp"].max()) >= end_ms - 60_000):
                return cached

    all_rows: list = []
    current_end = end_ms

    while True:
        raw = _bybit_candles_raw(symbol, interval, 200, current_end)
        if not raw:
            break

        rows = _parse_raw(raw)
        if not rows:
            break

        # Filter to desired range
        rows = [r for r in rows if start_ms <= r["timestamp"] <= end_ms]
        if rows:
            all_rows.extend(rows)

        oldest_in_batch = min(int(c[0]) for c in raw)
        if oldest_in_batch <= start_ms:
            break  # We've fetched enough

        current_end = oldest_in_batch - 1

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

    if use_cache and len(df) > 0:
        _save_cache(df, cache_path)

    return df
