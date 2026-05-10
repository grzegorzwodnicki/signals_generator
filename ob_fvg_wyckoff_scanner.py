#!/usr/bin/env python3
"""
OB / FVG + Wyckoff Confirmation Scanner
Skanuje kryptowaluty/akcje pod kątem Order Blocków lub Fair Value Gap na wybranym TF,
z potwierdzeniem struktury Wyckoff na niższym TF jako sygnał wejścia.

Logika:
  LONG  — cena przy bullish OB lub FVG long (na OB TF)
          + Wyckoff Accumulation/Reaccumulation (na Wyckoff TF)
  SHORT — cena przy bearish OB lub FVG short (na OB TF)
          + Wyckoff Distribution/Redistribution (na Wyckoff TF)

Użycie:
  python ob_fvg_wyckoff_scanner.py
"""

import requests
import time
import os
import subprocess
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from wyckoff_core import analyze_wyckoff

# ============================================================
# CONSTANTS
# ============================================================

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

BACKTEST_TIME_MS   = None
REQUIRE_RSI_CLIMAX = False

TIMEFRAMES = ["5m", "15m", "30m", "1H", "4H", "1D", "1W"]
TF_LABEL   = {"5m": "5M", "15m": "15M", "30m": "30M", "1H": "1H",
               "4H": "4H", "1D": "1D", "1W": "1W"}
TF_BYBIT   = {"5m": "5",  "15m": "15",  "30m": "30",  "1H": "60",
               "4H": "240", "1D": "D",  "1W": "W"}
TF_BINANCE = {"5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h",
               "4H": "4h", "1D": "1d",  "1W": "1w"}

ATR_ZONE_MULT  = 0.5    # default: price within N×ATR of nearest zone boundary
ATR_PERIOD     = 14     # ATR period for zone proximity check
OB_LOOKBACK    = 150    # candles to search for OB
OB_PIVOT_LEN   = 5      # volume pivot length (bars on each side) — matches LuxAlgo default
FVG_LOOKBACK   = 120    # candles to search for FVG

_SEMAPHORE         = threading.Semaphore(20)
_POLYGON_SEMAPHORE = threading.Semaphore(5)
_BINANCE_SEMAPHORE = threading.Semaphore(8)

# ============================================================
# DATA FETCHING — BYBIT
# ============================================================

def _read_polygon_key():
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env) as f:
            for line in f:
                line = line.strip()
                if line.startswith("POLYGON_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get("POLYGON_API_KEY", "")

POLYGON_API_KEY = _read_polygon_key()


def _get_json(url, params, timeout=15):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        return r.json()
    except Exception:
        return {}


def _bybit_candles_raw(symbol, interval, limit=200, end_time=None):
    params = {
        "category": "linear", "symbol": symbol,
        "interval": TF_BYBIT.get(interval, interval), "limit": limit,
    }
    if end_time:
        params["end"] = end_time
    for attempt in range(4):
        d  = _get_json("https://api.bybit.com/v5/market/kline", params)
        rc = d.get("retCode")
        if rc == 0:
            return d["result"]["list"]
        if rc in (10002, 10006, 10018) or "rate" in d.get("retMsg", "").lower():
            time.sleep(2 ** (attempt + 1))
            continue
        return []
    return []


def _parse(raw):
    out = [{"time": int(c[0]) // 1000, "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in raw]
    out.reverse()
    return out


def get_candles(symbol, interval, end_time_ms=None):
    with _SEMAPHORE:
        b1 = _bybit_candles_raw(symbol, interval, 200, end_time_ms)
        if not b1:
            return []
        oldest_ts = min(int(c[0]) for c in b1)
        b2 = _bybit_candles_raw(symbol, interval, 200, oldest_ts - 1)
        return _parse(b1 + b2)


def get_top_symbols(n=400):
    d = _get_json("https://api.bybit.com/v5/market/tickers", {"category": "linear"})
    if d.get("retCode") != 0:
        return []
    tickers = d["result"]["list"]
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
    usdt.sort(key=lambda t: float(t.get("turnover24h", 0)), reverse=True)
    return [t["symbol"] for t in usdt[:n]]

# ============================================================
# DATA FETCHING — BINANCE
# ============================================================

def _binance_candles_raw(symbol, interval, limit=200, end_time=None):
    params = {"symbol": symbol, "interval": TF_BINANCE.get(interval, interval), "limit": limit}
    if end_time:
        params["endTime"] = end_time
    for attempt in range(4):
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                             params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 2))
                continue
            return []
        except Exception:
            if attempt < 3:
                time.sleep(2 ** attempt)
    return []


def _parse_binance(raw):
    seen, out = set(), []
    for c in raw:
        t = int(c[0])
        if t not in seen:
            seen.add(t)
            out.append({"time": t // 1000, "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])})
    out.sort(key=lambda x: x["time"])
    return out


def get_candles_binance(symbol, interval, end_time_ms=None):
    with _BINANCE_SEMAPHORE:
        b1 = _binance_candles_raw(symbol, interval, 200, end_time_ms)
        if not b1:
            return []
        time.sleep(0.08)
        oldest_ts = min(int(c[0]) for c in b1)
        b2 = _binance_candles_raw(symbol, interval, 200, oldest_ts - 1)
        return _parse_binance(b2 + b1)


def get_top_symbols_binance(n=400):
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15)
        if r.status_code != 200:
            return []
        tickers = r.json()
        if not isinstance(tickers, list):
            return []
        usdt = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
        return [t["symbol"] for t in usdt[:n]]
    except Exception:
        return []

# ============================================================
# DATA FETCHING — POLYGON (STOCKS)
# ============================================================

_POLYGON_TF = {
    "5m": (5, "minute"), "15m": (15, "minute"), "30m": (30, "minute"),
    "1H": (1, "hour"),   "4H": (4, "hour"),     "1D": (1, "day"), "1W": (1, "week"),
}
_POLYGON_LOOKBACK_DAYS = {
    "5m": 15, "15m": 30, "30m": 60, "1H": 110,
    "4H": 450, "1D": 650, "1W": 1400,
}


def get_candles_stock(symbol, interval, end_time_ms=None):
    mult, timespan = _POLYGON_TF.get(interval, (1, "day"))
    days    = _POLYGON_LOOKBACK_DAYS.get(interval, 120)
    end_ms  = end_time_ms or int(time.time() * 1000)
    from_ms = end_ms - days * 24 * 3600 * 1000
    from_date = datetime.fromtimestamp(from_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    to_date   = datetime.fromtimestamp(end_ms   / 1000, timezone.utc).strftime("%Y-%m-%d")
    url    = (f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range"
              f"/{mult}/{timespan}/{from_date}/{to_date}")
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": POLYGON_API_KEY}
    for attempt in range(4):
        try:
            with _POLYGON_SEMAPHORE:
                r = requests.get(url, params=params, timeout=20)
            data = r.json()
            if data.get("status") in ("OK", "DELAYED"):
                raw     = data.get("results") or []
                candles = [
                    {"time": c["t"] // 1000, "open": float(c["o"]), "high": float(c["h"]),
                     "low": float(c["l"]), "close": float(c["c"]), "volume": float(c.get("v", 0))}
                    for c in raw if isinstance(c, dict)
                ]
                return candles[-400:] if len(candles) > 400 else candles
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 2))
                continue
            return []
        except Exception:
            time.sleep(2 ** attempt)
    return []


def get_stock_symbols(filepath):
    symbols = []
    try:
        with open(filepath) as f:
            for line in f:
                sym = line.strip()
                if sym and not sym.startswith("#"):
                    symbols.append(sym)
    except Exception as e:
        print(f"Błąd odczytu pliku {filepath}: {e}")
    return symbols

# ============================================================
# OB / FVG DETECTION
# ============================================================

def detect_fvg(candles, direction, lookback=FVG_LOOKBACK):
    """Return the most recent unfilled FVG in the given direction, or None."""
    src  = candles[-lookback:] if len(candles) > lookback else candles
    fvgs = []
    for i in range(2, len(src)):
        if direction == "LONG":
            if src[i]["low"] > src[i-2]["high"]:
                fvgs.append({
                    "fvg_low":  src[i-2]["high"],
                    "fvg_high": src[i]["low"],
                    "time":     src[i]["time"],
                })
        else:
            if src[i]["high"] < src[i-2]["low"]:
                fvgs.append({
                    "fvg_low":  src[i]["high"],
                    "fvg_high": src[i-2]["low"],
                    "time":     src[i]["time"],
                })
    if not fvgs:
        return None
    fvg = fvgs[-1]
    fvg["mid"]  = (fvg["fvg_low"] + fvg["fvg_high"]) / 2
    fvg["size"] = fvg["fvg_high"] - fvg["fvg_low"]
    last_close  = candles[-1]["close"]
    fvg["filled"] = (last_close < fvg["fvg_low"] if direction == "LONG"
                     else last_close > fvg["fvg_high"])
    return fvg


def detect_ob(candles, direction, lookback=OB_LOOKBACK, pivot_len=OB_PIVOT_LEN):
    """
    LuxAlgo-style Order Block detection (volume pivot + market structure).

    Bullish OB (LONG):
      - Volume pivot high at candle i (higher vol than pivot_len bars each side)
      - Market moved UP since: min low of all subsequent bars > OB candle's low
        (simultaneously confirms bullish structure AND that OB is not yet mitigated)
      - Zone = candle.low → (candle.high + candle.low) / 2  [lower half]

    Bearish OB (SHORT):
      - Volume pivot high at candle i
      - Market moved DOWN since: max high of all subsequent bars < OB candle's high
      - Zone = (candle.high + candle.low) / 2 → candle.high  [upper half]

    Returns the most recent valid (unmitigated) OB, or None.
    """
    src = candles[-lookback:] if len(candles) > lookback else candles
    n   = len(src)
    if n < pivot_len * 2 + 2:
        return None

    # Scan newest-to-oldest pivot candidates; return first valid OB found
    for i in range(n - pivot_len - 1, pivot_len - 1, -1):
        vol_i = src[i]["volume"]
        if vol_i == 0:
            continue

        # Volume pivot high: strictly greater than pivot_len bars on each side
        if not all(vol_i > src[i - k]["volume"] for k in range(1, pivot_len + 1)):
            continue
        if not all(vol_i > src[i + k]["volume"] for k in range(1, pivot_len + 1)):
            continue

        ob_c  = src[i]
        hl2   = (ob_c["high"] + ob_c["low"]) / 2
        after = src[i + 1:]

        if direction == "LONG":
            # Bullish OB: price must have moved entirely above OB's low (structure + no mitigation)
            if min(c["low"] for c in after) <= ob_c["low"]:
                continue
            ob_low, ob_high = ob_c["low"], hl2

        else:  # SHORT
            # Bearish OB: price must have moved entirely below OB's high (structure + no mitigation)
            if max(c["high"] for c in after) >= ob_c["high"]:
                continue
            ob_low, ob_high = hl2, ob_c["high"]

        return {
            "ob_low":  ob_low,
            "ob_high": ob_high,
            "mid":     (ob_low + ob_high) / 2,
            "time":    ob_c["time"],
            "vol":     vol_i,
        }

    return None


def _calc_atr(candles, period=ATR_PERIOD):
    """Wilder ATR on the zone TF candles. Falls back to 1% of price if insufficient data."""
    if len(candles) < period + 1:
        return candles[-1]["close"] * 0.01
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _price_at_zone(price, low, high, atr_dist):
    """True if price is within atr_dist of the zone [low, high]."""
    return (low - atr_dist) <= price <= (high + atr_dist)

# ============================================================
# WYCKOFF HELPERS
# ============================================================

WYCKOFF_STRUCTS  = {"Accumulation", "Reaccumulation", "Distribution", "Redistribution"}
_BULLISH_STRUCTS = {"Accumulation", "Reaccumulation"}
_BEARISH_STRUCTS = {"Distribution", "Redistribution"}


def _matches_wyckoff(wy, selected_phases, direction):
    """True if Wyckoff result matches selected phases AND the required direction."""
    p      = wy.get("phase", "Unclear")
    struct = wy.get("structure", "UNCLEAR")
    evs    = " ".join(wy.get("events", []))

    if direction == "long" and struct not in _BULLISH_STRUCTS:
        return False
    if direction == "short" and struct not in _BEARISH_STRUCTS:
        return False

    for sp in selected_phases:
        if sp == "A":
            if "SC" in evs and direction == "long":
                return True
            if "BC" in evs and direction == "short":
                return True
        elif struct in WYCKOFF_STRUCTS and p == sp:
            return True
    return False

# ============================================================
# SCAN LOGIC
# ============================================================

def scan_symbol(ob_candles, wy_candles, wy_phases, dir_filter, zone_filter, atr_mult):
    """
    Check one symbol for OB/FVG + Wyckoff confirmation.
    zone_filter: "OB" | "FVG" | "BOTH"
    atr_mult:    tolerance = atr * atr_mult from nearest zone boundary
    Returns list of match dicts (one per valid direction), or [].
    """
    if len(ob_candles) < 30 or len(wy_candles) < 30:
        return []

    current_price = ob_candles[-1]["close"]
    atr           = _calc_atr(ob_candles)
    atr_dist      = atr * atr_mult
    matches       = []

    directions = []
    if dir_filter in ("LONG", "BOTH"):
        directions.append("LONG")
    if dir_filter in ("SHORT", "BOTH"):
        directions.append("SHORT")

    for direction in directions:
        wy_dir = "long" if direction == "LONG" else "short"

        fvg = detect_fvg(ob_candles, direction) if zone_filter in ("FVG", "BOTH") else None
        ob  = detect_ob(ob_candles, direction)  if zone_filter in ("OB",  "BOTH") else None

        at_fvg = (fvg is not None and not fvg.get("filled") and
                  _price_at_zone(current_price, fvg["fvg_low"], fvg["fvg_high"], atr_dist))
        at_ob  = (ob is not None and
                  _price_at_zone(current_price, ob["ob_low"], ob["ob_high"], atr_dist))

        if not at_fvg and not at_ob:
            continue

        wy = analyze_wyckoff(wy_candles, require_rsi_climax=REQUIRE_RSI_CLIMAX)
        if not _matches_wyckoff(wy, wy_phases, wy_dir):
            continue

        zone_type = ("OB+FVG" if at_ob and at_fvg else ("FVG" if at_fvg else "OB"))
        matches.append({
            "direction": direction,
            "zone_type": zone_type,
            "fvg":       fvg if at_fvg else None,
            "ob":        ob  if at_ob  else None,
            "wyckoff":   wy,
            "price":     current_price,
            "atr":       atr,
            "atr_dist":  atr_dist,
        })

    return matches

# ============================================================
# HTML HELPERS
# ============================================================

def _f(v):
    if v is None:  return "N/A"
    if v >= 1000:  return f"{v:,.4f}"
    if v >= 1:     return f"{v:.5f}"
    return f"{v:.7f}"


STRUCT_COLOR = {
    "Accumulation":    "#00ff8c",
    "Reaccumulation":  "#00ccff",
    "Distribution":    "#ff4d4d",
    "Redistribution":  "#ff8844",
    "UNCLEAR":         "#556677",
}
CONF_COLOR = {"HIGH": "#00ff8c", "MEDIUM": "#ffd700", "LOW": "#8899aa"}
CONF_RANK  = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
DIR_COLOR  = {"LONG": "#00ff8c", "SHORT": "#ff4d4d"}
ZONE_COLOR = {"OB": "#ffd700", "FVG": "#00e5ff", "OB+FVG": "#ff8844"}


def _rsi_vol_cells(w, candles):
    pts      = w.get("_points") or {}
    rsi_arr  = pts.get("rsi", [])
    vsma     = pts.get("vol_sma", [])
    last_rsi = next((r for r in reversed(rsi_arr) if r is not None), None)
    if last_rsi is not None:
        rsi_str = f"{last_rsi:.1f}"
        rsi_col = "#00ff8c" if last_rsi > 70 else ("#ff4d4d" if last_rsi < 30 else "#ffd700")
    else:
        rsi_str, rsi_col = "N/A", "#8899aa"
    last_vsma = next((v for v in reversed(vsma) if v is not None), None)
    last_vol  = candles[-1]["volume"] if candles else None
    if last_vol is not None and last_vsma and last_vsma > 0:
        vr      = last_vol / last_vsma
        vol_str = f"{vr:.2f}×"
        vol_col = "#00ff8c" if vr >= 1.5 else ("#ffd700" if vr >= 0.8 else "#ff8844")
    else:
        vol_str, vol_col = "N/A", "#8899aa"
    return rsi_str, rsi_col, vol_str, vol_col


def _range_label(structure, rh_val, rl_val):
    s = structure or ""
    if "Accumulation" in s or "Reaccumulation" in s:
        return f"SC {rl_val} / AR {rh_val}"
    if "Distribution" in s or "Redistribution" in s:
        return f"BC {rh_val} / AR {rl_val}"
    return f"{rl_val} — {rh_val}"


def _zone_panel(match, ob_tf_label):
    fvg      = match.get("fvg")
    ob       = match.get("ob")
    dir_col  = DIR_COLOR.get(match["direction"], "#8899aa")
    price    = match["price"]
    atr      = match.get("atr", 0)
    atr_dist = match.get("atr_dist", 0)
    rows     = ""

    if ob:
        ob_mid   = ob.get("mid", 0)
        diff_atr = (price - ob_mid) / atr if atr else 0
        rows += (
            f'<tr>'
            f'<td style="color:#ffd700;font-weight:bold">Order Block</td>'
            f'<td style="color:#e6edf7">{_f(ob["ob_low"])}</td>'
            f'<td style="color:#e6edf7">{_f(ob["ob_high"])}</td>'
            f'<td style="color:#e6edf7">{_f(ob_mid)}</td>'
            f'<td style="color:{dir_col}">{diff_atr:+.2f} ATR from mid</td>'
            f'</tr>'
        )
        if ob.get("time"):
            dt = datetime.fromtimestamp(ob["time"], tz=timezone.utc)
            rows += (
                f'<tr><td colspan="5" style="color:#445566;font-size:10px;padding-bottom:6px">'
                f'OB formed: {dt.strftime("%d/%m %H:%M")} UTC</td></tr>'
            )

    if fvg:
        fvg_mid  = fvg.get("mid", 0)
        diff_atr = (price - fvg_mid) / atr if atr else 0
        size_atr = fvg["size"] / atr if atr else 0
        rows += (
            f'<tr>'
            f'<td style="color:#00e5ff;font-weight:bold">Fair Value Gap</td>'
            f'<td style="color:#e6edf7">{_f(fvg["fvg_low"])}</td>'
            f'<td style="color:#e6edf7">{_f(fvg["fvg_high"])}</td>'
            f'<td style="color:#e6edf7">{_f(fvg_mid)}</td>'
            f'<td style="color:{dir_col}">{diff_atr:+.2f} ATR from mid '
            f'<span style="color:#445566">({size_atr:.2f} ATR size)</span></td>'
            f'</tr>'
        )
        if fvg.get("time"):
            dt = datetime.fromtimestamp(fvg["time"], tz=timezone.utc)
            rows += (
                f'<tr><td colspan="5" style="color:#445566;font-size:10px;padding-bottom:6px">'
                f'FVG formed: {dt.strftime("%d/%m %H:%M")} UTC</td></tr>'
            )

    if not rows:
        return ""
    atr_note = (f'ATR({ATR_PERIOD}): {_f(atr)} · Tolerancja: {_f(atr_dist)} '
                f'({match.get("atr_mult", ATR_ZONE_MULT)}× ATR)') if atr else ""
    return (
        f'<div style="margin-bottom:12px">'
        f'<div style="font-size:10px;color:#445566;margin-bottom:4px">'
        f'ZONE DETAIL — {ob_tf_label} · cena {_f(price)}'
        f'{" · " + atr_note if atr_note else ""}</div>'
        f'<table style="width:100%;border-collapse:collapse;font-size:12px">'
        f'<thead><tr style="background:#070b12">'
        f'<th style="padding:4px 8px;color:#334455;text-align:left">Type</th>'
        f'<th style="color:#334455">Low</th><th style="color:#334455">High</th>'
        f'<th style="color:#334455">Mid</th><th style="color:#334455">Dist od mid (ATR)</th>'
        f'</tr></thead>'
        f'<tbody style="color:#e6edf7">{rows}</tbody>'
        f'</table></div>'
    )


def _wyckoff_points_panel(w, candles=None):
    pts     = w.get("_points") or {}
    ctx     = pts.get("context", "")
    is_acc  = ctx == "accumulation"
    is_dist = ctx == "distribution"
    if not (is_acc or is_dist):
        return ""
    events   = pts["acc"] if is_acc else pts["dist"]
    pt_order = (["PS", "SC", "AR", "ST", "Spring", "SOS", "LPS"] if is_acc
                else ["PSY", "BC", "AR", "ST", "UTAD", "SOW", "LPSY"])
    vsma     = pts.get("vol_sma", [])
    items    = ""
    for key in pt_order:
        pt = events.get(key)
        if not pt:
            continue
        idx     = pt.get("idx", 0)
        price   = pt.get("price", 0)
        rsi_val = pt.get("rsi")
        vol     = pt.get("volume", 0)
        vsma_v  = vsma[idx] if idx < len(vsma) and vsma[idx] is not None else None
        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "—"
        rsi_col = ("#00ff8c" if (rsi_val or 50) > 70
                   else "#ff4d4d" if (rsi_val or 50) < 30 else "#ffd700")
        if vsma_v and vsma_v > 0:
            vr      = vol / vsma_v
            vol_str = f"{vr:.1f}×"
            vol_col = "#00ff8c" if vr >= 1.5 else ("#ffd700" if vr >= 0.8 else "#ff8844")
        else:
            vol_str, vol_col = "—", "#8899aa"
        pt_col     = "#00ff8c" if is_acc else "#ff4d4d"
        time_badge = ""
        if candles and idx < len(candles):
            ts = candles[idx].get("time")
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                time_badge = (
                    f'<span style="color:#667788;font-size:10px">'
                    f'{dt.strftime("%d/%m %H:%M")}</span>'
                )
        items += (
            f'<span style="background:#0d1520;border:1px solid #1e3050;border-radius:4px;'
            f'padding:3px 8px;display:inline-flex;gap:5px;align-items:center;'
            f'margin:2px;white-space:nowrap">'
            f'<span style="color:{pt_col};font-weight:bold;font-size:11px">{key}</span>'
            f'<span style="color:#e6edf7;font-size:11px">{_f(price)}</span>'
            f'{time_badge}'
            f'<span style="color:{rsi_col};font-size:10px">RSI {rsi_str}</span>'
            f'<span style="color:{vol_col};font-size:10px">Vol {vol_str}</span>'
            f'</span>'
        )
    if not items:
        return ""
    return (
        f'<div style="margin:6px 0 10px;padding:7px;background:#070d18;border-radius:4px">'
        f'<div style="font-size:10px;color:#445566;margin-bottom:5px">'
        f'WYCKOFF POINTS — RSI(14) · Vol/SMA(20)</div>'
        f'{items}</div>'
    )


def _multi_tf_table(data_by_tf, highlight_tfs=None):
    hl_set = set(highlight_tfs) if highlight_tfs else set()
    rows   = ""
    for tf in TIMEFRAMES:
        candles = data_by_tf.get(tf, [])
        if not candles:
            rows += (
                f'<tr><td style="color:#88ccff;font-weight:bold">{TF_LABEL[tf]}</td>'
                f'<td colspan="7" style="color:#334455">No data</td></tr>'
            )
            continue
        w   = analyze_wyckoff(candles, require_rsi_climax=REQUIRE_RSI_CLIMAX)
        sc  = STRUCT_COLOR.get(w["structure"], "#556677")
        cc  = CONF_COLOR.get(w["confidence"], "#8899aa")
        rh  = _f(w["range_high"])
        rl  = _f(w["range_low"])
        evs = " · ".join(w["events"][:3])
        hl  = "background:#0d1a2a;" if tf in hl_set else ""
        rsi_str, rsi_col, vol_str, vol_col = _rsi_vol_cells(w, candles)
        rows += (
            f'<tr style="{hl}">'
            f'<td style="color:#88ccff;font-weight:bold;white-space:nowrap">{TF_LABEL[tf]}</td>'
            f'<td style="color:{sc}">{w["structure"]}</td>'
            f'<td style="color:#ffd700;white-space:nowrap">Phase {w["phase"]}</td>'
            f'<td style="color:#00e5ff;white-space:nowrap;font-size:12px">'
            f'{_range_label(w["structure"], rh, rl)}</td>'
            f'<td style="font-size:11px;color:#8899aa;max-width:280px">{evs}</td>'
            f'<td style="color:{rsi_col};white-space:nowrap;font-size:12px">{rsi_str}</td>'
            f'<td style="color:{vol_col};white-space:nowrap;font-size:12px">{vol_str}</td>'
            f'<td style="color:{cc};white-space:nowrap;font-size:12px">{w["confidence"]}</td>'
            f'</tr>'
        )
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        '<thead style="background:#070b12"><tr>'
        '<th style="padding:5px 6px;color:#334455;text-align:left">TF</th>'
        '<th style="color:#334455">Structure</th>'
        '<th style="color:#334455">Phase</th>'
        '<th style="color:#334455">SC/AR · BC/AR · Range</th>'
        '<th style="color:#334455">Key Events</th>'
        '<th style="color:#334455">RSI(14)</th>'
        '<th style="color:#334455">Vol/SMA</th>'
        '<th style="color:#334455">Conf</th>'
        f'</tr></thead><tbody style="color:#e6edf7">{rows}</tbody></table>'
    )

# ============================================================
# HTML REPORT
# ============================================================

_PHASE_LABEL = {
    "A": "Stopping Action",
    "B": "Building Cause",
    "C": "Spring / UTAD Test",
    "D": "SOS / SOW Breakout",
    "E": "Markup / Markdown",
}


def generate_html(all_matches, scan_cfg, report_time, data_time, is_backtest):
    ob_tf       = scan_cfg["ob_tf"]
    wy_tf       = scan_cfg["wy_tf"]
    phases      = scan_cfg["phases"]
    total       = scan_cfg["total_scanned"]
    zone_filter = scan_cfg.get("zone_filter", "BOTH")
    atr_mult    = scan_cfg.get("atr_mult", ATR_ZONE_MULT)
    found       = len(all_matches)
    ob_lbl      = TF_LABEL.get(ob_tf, ob_tf)
    wy_lbl      = TF_LABEL.get(wy_tf, wy_tf)
    ph_lbl      = "/".join(phases)
    zone_label  = {"OB": "OB only", "FVG": "FVG only", "BOTH": "OB + FVG"}.get(zone_filter, zone_filter)

    css = (
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{background:#070b12;color:#e6edf7;"
        "font-family:'Segoe UI',Arial,sans-serif;font-size:14px;line-height:1.5}"
        "th{padding:7px 6px;text-align:left;border-bottom:1px solid #1e2d40}"
        "td{padding:6px 6px;border-bottom:1px solid #1a2535;vertical-align:top}"
        "tr:hover td{background:#0d1520}"
        ".card{background:#101827;border:1px solid #1e2d40;border-radius:8px;"
        "padding:18px;margin-bottom:14px}"
        "details>summary{cursor:pointer;list-style:none}"
        "details>summary::-webkit-details-marker{display:none}"
    )

    long_ct  = sum(1 for m in all_matches if m["direction"] == "LONG")
    short_ct = sum(1 for m in all_matches if m["direction"] == "SHORT")

    ph_badges = "".join(
        f'<span style="background:#1a2535;border-radius:3px;padding:2px 10px;'
        f'margin:2px;font-size:12px;display:inline-block;color:#ffd700">'
        f'Phase {p} <span style="color:#445566;font-size:11px">'
        f'{_PHASE_LABEL.get(p,"")}</span></span>'
        for p in phases
    )

    summary_html = (
        f'<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px">'
        # Found
        f'<div class="card" style="flex:0 0 auto;text-align:center;min-width:110px">'
        f'<div style="font-size:11px;color:#8899aa;margin-bottom:4px">FOUND</div>'
        f'<div style="font-size:38px;font-weight:bold;color:#00ff8c">{found}</div>'
        f'<div style="font-size:11px;color:#445566">of {total}</div>'
        f'</div>'
        # LONG
        f'<div class="card" style="flex:0 0 auto;text-align:center;min-width:90px">'
        f'<div style="font-size:11px;color:#8899aa;margin-bottom:4px">LONG</div>'
        f'<div style="font-size:32px;font-weight:bold;color:#00ff8c">{long_ct}</div>'
        f'</div>'
        # SHORT
        f'<div class="card" style="flex:0 0 auto;text-align:center;min-width:90px">'
        f'<div style="font-size:11px;color:#8899aa;margin-bottom:4px">SHORT</div>'
        f'<div style="font-size:32px;font-weight:bold;color:#ff4d4d">{short_ct}</div>'
        f'</div>'
        # Config
        f'<div class="card" style="flex:1;min-width:280px">'
        f'<div style="font-size:11px;color:#8899aa;margin-bottom:10px">KONFIGURACJA</div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
        f'<span style="background:#0d1a2a;border:1px solid #2a3d20;border-radius:5px;'
        f'padding:5px 12px;display:inline-flex;gap:8px;align-items:center">'
        f'<span style="color:#ffd700;font-weight:bold">Zone TF</span>'
        f'<span style="color:#00e5ff;font-size:15px;font-weight:bold">{ob_lbl}</span>'
        f'<span style="color:#445566">{zone_label}</span>'
        f'</span>'
        f'<span style="background:#0d1a2a;border:1px solid #1e3050;border-radius:5px;'
        f'padding:5px 12px;display:inline-flex;gap:8px;align-items:center">'
        f'<span style="color:#ffd700;font-weight:bold">Wyckoff TF</span>'
        f'<span style="color:#00e5ff;font-size:15px;font-weight:bold">{wy_lbl}</span>'
        f'</span>'
        f'</div>'
        f'<div style="margin-bottom:6px">{ph_badges}</div>'
        f'<div style="font-size:12px;color:#445566">'
        f'Tolerancja: {atr_mult}× ATR({ATR_PERIOD}) · '
        f'{"RSI gate ON" if REQUIRE_RSI_CLIMAX else "RSI gate OFF"}'
        f'</div>'
        f'</div>'
        f'</div>'
    )

    # Results table
    table_rows = ""
    for m in all_matches:
        wy      = m["wyckoff"]
        sc      = STRUCT_COLOR.get(wy["structure"], "#556677")
        cc      = CONF_COLOR.get(wy["confidence"], "#8899aa")
        dc      = DIR_COLOR.get(m["direction"], "#8899aa")
        zc      = ZONE_COLOR.get(m["zone_type"], "#8899aa")
        evs     = " · ".join(wy["events"][:4])
        sym     = m["symbol"]
        fvg     = m.get("fvg")
        ob      = m.get("ob")
        zone_hi = _f((fvg or ob or {}).get("fvg_high") or (ob or {}).get("ob_high"))
        zone_lo = _f((fvg or ob or {}).get("fvg_low")  or (ob or {}).get("ob_low"))
        if fvg:
            zone_hi = _f(fvg["fvg_high"])
            zone_lo = _f(fvg["fvg_low"])
        elif ob:
            zone_hi = _f(ob["ob_high"])
            zone_lo = _f(ob["ob_low"])

        table_rows += (
            f'<tr>'
            f'<td style="font-weight:bold">'
            f'<a href="#{sym}_{m["direction"]}" style="color:#e6edf7;text-decoration:none">'
            f'{sym}</a></td>'
            f'<td><span style="color:{dc};font-weight:bold">{m["direction"]}</span></td>'
            f'<td><span style="color:{zc}">{m["zone_type"]}</span></td>'
            f'<td style="font-size:12px;color:#8899aa;white-space:nowrap">'
            f'{zone_lo} – {zone_hi}</td>'
            f'<td style="color:{sc}">{wy["structure"]}</td>'
            f'<td style="color:#ffd700">Ph {wy["phase"]}</td>'
            f'<td style="font-size:11px;color:#8899aa">{evs}</td>'
            f'<td style="color:{cc}">{wy["confidence"]}</td>'
            f'</tr>'
        )

    if not table_rows:
        table_rows = (
            '<tr><td colspan="8" style="text-align:center;color:#445566;padding:28px">'
            'Brak instrumentów pasujących do kryteriów.</td></tr>'
        )

    results_table = (
        '<div style="overflow-x:auto">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead style="background:#0d1520"><tr>'
        '<th>Symbol</th><th>Dir</th><th>Zone</th><th>Zone Range</th>'
        '<th>Wyckoff</th><th>Phase</th><th>Key Events</th><th>Conf</th>'
        '</tr></thead>'
        f'<tbody style="color:#e6edf7">{table_rows}</tbody>'
        '</table></div>'
    )

    # Detail cards
    detail_cards = ""
    for m in all_matches:
        sym      = m["symbol"]
        wy       = m["wyckoff"]
        sc       = STRUCT_COLOR.get(wy["structure"], "#556677")
        cc       = CONF_COLOR.get(wy["confidence"], "#8899aa")
        dc       = DIR_COLOR.get(m["direction"], "#8899aa")
        zc       = ZONE_COLOR.get(m["zone_type"], "#8899aa")
        card_id  = f"{sym}_{m['direction']}"
        data_all = m.get("data_by_tf", {})

        ev_badges = "".join(
            f'<span style="background:#1a2535;border-radius:3px;padding:2px 8px;'
            f'margin:2px;font-size:11px;display:inline-block;color:#aabbcc">{e}</span>'
            for e in wy["events"]
        )

        wy_candles = data_all.get(wy_tf, [])
        pts_panel  = _wyckoff_points_panel(wy, wy_candles)
        zone_panel = _zone_panel(m, ob_lbl)
        multi_tf   = _multi_tf_table(data_all, highlight_tfs=[ob_tf, wy_tf])
        rsi_str, rsi_col, vol_str, vol_col = _rsi_vol_cells(wy, wy_candles)

        rsi_badge = (
            f'<span style="background:#1e2d40;color:{rsi_col};padding:3px 9px;'
            f'border-radius:4px;font-size:12px">RSI {rsi_str}</span>'
        )
        vol_badge = (
            f'<span style="background:#1e2d40;color:{vol_col};padding:3px 9px;'
            f'border-radius:4px;font-size:12px">Vol {vol_str}</span>'
        )

        detail_cards += (
            f'<div id="{card_id}" class="card">'
            f'<details open>'
            f'<summary style="display:flex;justify-content:space-between;'
            f'align-items:center;padding:2px 0 10px;flex-wrap:wrap;gap:6px">'
            f'<span style="font-size:20px;font-weight:bold;color:#e6edf7">{sym}</span>'
            f'<span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
            f'<span style="background:{dc}22;color:{dc};padding:4px 12px;'
            f'border-radius:4px;font-size:14px;font-weight:bold">{m["direction"]}</span>'
            f'<span style="background:{zc}22;color:{zc};padding:4px 12px;'
            f'border-radius:4px;font-size:13px">{m["zone_type"]} · {ob_lbl}</span>'
            f'<span style="color:{sc};font-weight:bold;font-size:14px">{wy["structure"]}</span>'
            f'<span style="background:#1e2d40;color:#ffd700;padding:3px 12px;'
            f'border-radius:4px;font-size:13px">Phase {wy["phase"]}</span>'
            f'{rsi_badge}{vol_badge}'
            f'<span style="color:{cc};font-size:12px">{wy["confidence"]}</span>'
            f'<span style="color:#334455;font-size:13px">▼</span>'
            f'</span>'
            f'</summary>'
            f'<div>'
            f'<div style="font-size:12px;color:#667788;margin-bottom:12px">'
            f'Strefa: <span style="color:{zc}">{ob_lbl}</span> &nbsp;|&nbsp; '
            f'Wyckoff: <span style="color:#00e5ff">{wy_lbl}</span> '
            f'<span style="color:{sc}">{wy["structure"]}</span> '
            f'<span style="color:#00e5ff">'
            f'{_range_label(wy["structure"], _f(wy["range_high"]), _f(wy["range_low"]))}'
            f'</span>'
            f'</div>'
            f'{zone_panel}'
            f'<div style="margin-bottom:8px">{ev_badges}</div>'
            f'{pts_panel}'
            f'<div style="font-size:10px;color:#334455;margin:10px 0 4px">'
            f'MULTI-TF WYCKOFF — podświetlone: {ob_lbl} (strefa) + {wy_lbl} (potwierdzenie)</div>'
            f'{multi_tf}'
            f'</div>'
            f'</details>'
            f'</div>'
        )

    bt_banner = ""
    if is_backtest:
        bt_banner = (
            f'<div style="background:#1a0f00;border:1px solid #ff8844;border-radius:6px;'
            f'padding:10px 20px;margin-bottom:20px;color:#ff8844;font-weight:bold">'
            f'⚠ BACKTEST MODE — dane z {data_time}</div>'
        )

    zone_short = {"OB": "OB", "FVG": "FVG", "BOTH": "OB/FVG"}.get(zone_filter, "OB/FVG")
    title    = f"{zone_short} {ob_lbl} + Wyckoff {wy_lbl} Ph{ph_lbl}"
    hl_note  = f"podświetlone wiersze = {ob_lbl} (strefa) + {wy_lbl} (Wyckoff)"

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <title>OB/FVG+Wyckoff · {title}</title>
  <style>{css}</style>
</head>
<body>

<div style="background:#0d1520;border-bottom:2px solid #ff8844;padding:20px 32px;margin-bottom:24px">
  <div style="font-size:11px;color:#8899aa;letter-spacing:1px;margin-bottom:4px">
    OB / FVG + WYCKOFF CONFIRMATION SCANNER</div>
  <h1 style="font-size:24px;font-weight:bold;color:#e6edf7;margin-bottom:6px">{title}</h1>
  <div style="font-size:12px;color:#8899aa">
    Raport: <strong>{report_time}</strong> &nbsp;|&nbsp;
    Dane: <strong>{data_time}</strong> &nbsp;|&nbsp;
    {"⚠ BACKTEST" if is_backtest else "✅ LIVE"} &nbsp;|&nbsp;
    Przeskanowano: <strong>{total}</strong> &nbsp;|&nbsp;
    Znaleziono: <strong style="color:#00ff8c">{found}</strong>
  </div>
</div>

<div style="padding:0 32px 32px">
{bt_banner}
{summary_html}

<h2 style="color:#e6edf7;border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">
  Dopasowania — {found} instrument{"y" if 2 <= found <= 4 else ("ów" if found >= 5 else "")}
</h2>
{results_table}

<h2 style="color:#e6edf7;border-bottom:1px solid #1e2d40;padding-bottom:8px;margin:28px 0 16px">
  Analiza szczegółowa
  <span style="font-size:12px;color:#445566;font-weight:normal;margin-left:8px">{hl_note}</span>
</h2>
{detail_cards or "<div style='color:#445566;padding:16px'>Brak dopasowań.</div>"}

</div>

<div style="background:#0a0f1a;border-top:1px solid #1e2d40;padding:16px 32px;
font-size:11px;color:#445566;margin-top:8px">
  OB/FVG + Wyckoff Scanner · {report_time} · {title}
</div>

</body>
</html>"""

# ============================================================
# MAIN
# ============================================================

_TF_ALIASES = {
    "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "1H": "1H", "4h": "4H", "4H": "4H",
    "1d": "1D", "1D": "1D", "1w": "1W", "1W": "1W",
}


def _ask_tf(prompt, default=None):
    hint = f" (Enter = {default})" if default else ""
    print("  Timeframes: 5m · 15m · 30m · 1H · 4H · 1D · 1W")
    raw = input(prompt + hint + ": ").strip()
    if not raw and default:
        return default
    tf = _TF_ALIASES.get(raw, raw)
    if tf not in TIMEFRAMES:
        print(f"  Nieznany TF '{raw}'.")
        return None
    return tf


def main():
    global BACKTEST_TIME_MS, REQUIRE_RSI_CLIMAX

    print("=" * 60)
    print("  OB / FVG + Wyckoff Confirmation Scanner")
    print("=" * 60)

    # ── Market selection ──────────────────────────────────────
    print("\nRynek:")
    print("  c = Krypto (Bybit Top 400)")
    print("  b = Krypto (Binance Top 400)")
    print("  f = Krypto FTMO (crypto_ftmo.txt)")
    print("  s = Stocki (lista z pliku)")
    market = input("Wybierz (c/b/f/s): ").strip().lower()

    stock_filename = None
    if market == "s":
        print("\nPlik symboli:")
        print("  1 = stock_ftmo.txt")
        print("  2 = stock_us.txt")
        file_choice    = input("Wybierz (1/2): ").strip()
        _root          = os.path.dirname(os.path.abspath(__file__))
        stock_filename = "stock_ftmo.txt" if file_choice == "1" else "stock_us.txt"
        stock_file     = os.path.join(_root, stock_filename)
        fetch_fn       = get_candles_stock
        max_workers    = 5
    elif market == "f":
        _root       = os.path.dirname(os.path.abspath(__file__))
        stock_file  = os.path.join(_root, "crypto_ftmo.txt")
        fetch_fn    = get_candles
        max_workers = 20
    elif market == "b":
        stock_file  = None
        fetch_fn    = get_candles_binance
        max_workers = 8
    else:
        market      = "c"
        stock_file  = None
        fetch_fn    = get_candles
        max_workers = 20

    # ── Zone TF ───────────────────────────────────────────────
    print("\n--- Strefa wejścia ---")
    ob_tf = _ask_tf("TF dla strefy OB / FVG", default="1H")
    if ob_tf is None:
        return

    # ── Zone type filter ──────────────────────────────────────
    print("\nTyp strefy:")
    print("  o = tylko Order Block   f = tylko FVG   Enter = oba")
    zf_raw      = input("Typ (o/f/Enter): ").strip().lower()
    zone_filter = {"o": "OB", "f": "FVG"}.get(zf_raw, "BOTH")
    print(f"  Strefa: {zone_filter}")

    # ── ATR multiplier ────────────────────────────────────────
    print(f"\nTolerancja strefy (odległość od krawędzi strefy):")
    print(f"  np. 0.5 = cena musi być w zasięgu 0.5×ATR({ATR_PERIOD}) od granicy strefy")
    atr_raw  = input(f"ATR multiplier (Enter = {ATR_ZONE_MULT}): ").strip()
    try:
        atr_mult = float(atr_raw) if atr_raw else ATR_ZONE_MULT
    except ValueError:
        atr_mult = ATR_ZONE_MULT
    print(f"  Tolerancja: {atr_mult}× ATR({ATR_PERIOD})")

    # ── Wyckoff TF ────────────────────────────────────────────
    print("\n--- Potwierdzenie Wyckoff ---")
    wy_tf = _ask_tf("TF dla Wyckoff", default="15m")
    if wy_tf is None:
        return

    # ── Wyckoff phases ────────────────────────────────────────
    print("\nFazy Wyckoff (potwierdzenie):")
    print("  A = SC/BC   B = Ranging   C = Spring/UTAD")
    print("  D = SOS/SOW Breakout      E = Markup/Markdown")
    print("  Wiele faz przez przecinek, np. C,D")
    raw_phases = input("Faza(y) (Enter = C,D): ").strip().upper() or "C,D"
    phases     = [p.strip() for p in raw_phases.split(",") if p.strip()]
    invalid    = [p for p in phases if p not in ("A", "B", "C", "D", "E")]
    if invalid:
        print(f"  Nieznane fazy: {', '.join(invalid)} — przerywam.")
        return
    phases = list(dict.fromkeys(phases))

    # ── Direction ─────────────────────────────────────────────
    print("\nKierunek skanowania:")
    print("  l = tylko LONG   s = tylko SHORT   Enter = oba")
    dir_raw    = input("Kierunek (l/s/Enter): ").strip().lower()
    dir_filter = {"l": "LONG", "s": "SHORT"}.get(dir_raw, "BOTH")
    print(f"  Kierunek: {dir_filter}")

    # ── RSI toggle ────────────────────────────────────────────
    rsi_raw            = input("\nFiltr RSI dla SC/BC? (Enter=włącz / n=wyłącz): ").strip().lower()
    REQUIRE_RSI_CLIMAX = (rsi_raw != "n")
    print("  RSI włączone." if REQUIRE_RSI_CLIMAX else "  RSI wyłączone.")

    # ── Time mode ─────────────────────────────────────────────
    choice      = input("\nDane (t=teraz / h=historyczne): ").strip().lower()
    is_backtest = False
    data_time   = datetime.now().strftime("%Y-%m-%d %H:%M")

    if choice == "h":
        is_backtest = True
        date_str    = input("Data i czas (dd/mm/yyyy hh:mm): ").strip()
        try:
            dt_obj           = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
            BACKTEST_TIME_MS = int(dt_obj.timestamp() * 1000)
            data_time        = dt_obj.strftime("%Y-%m-%d %H:%M")
            print(f"  Tryb backtest: {data_time}")
        except ValueError:
            print("  Nieprawidłowy format — używam danych na żywo.")
            BACKTEST_TIME_MS = None
            is_backtest      = False

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Fetch symbols ─────────────────────────────────────────
    if market in ("f", "s"):
        label   = "crypto_ftmo.txt" if market == "f" else stock_filename
        print(f"\nWczytuję symbole z {label}...")
        symbols = get_stock_symbols(stock_file)
        if not symbols:
            print("Brak symboli w pliku.")
            return
    elif market == "b":
        print("\nPobieram Top 400 z Binance...")
        symbols = get_top_symbols_binance(400)
        if not symbols:
            print("Nie udało się pobrać listy z Binance.")
            return
    else:
        print("\nPobieram Top 400 z Bybit...")
        symbols = get_top_symbols(400)
        if not symbols:
            print("Nie udało się pobrać listy z Bybit.")
            return
    print(f"  {len(symbols)} symboli.")

    # ── Phase 1: parallel scan ────────────────────────────────
    needed_tfs = list(dict.fromkeys([ob_tf, wy_tf]))
    ph_lbl     = "/".join(phases)
    zf_lbl = {"OB": "OB", "FVG": "FVG", "BOTH": "OB+FVG"}.get(zone_filter, zone_filter)
    print(f"\nSkanowanie [{TF_LABEL.get(ob_tf,ob_tf)} {zf_lbl} + "
          f"{TF_LABEL.get(wy_tf,wy_tf)} Wyckoff Ph{ph_lbl} · tol {atr_mult}×ATR]...")

    progress = {"n": 0}
    lock     = threading.Lock()

    def _scan(sym):
        try:
            data = {tf: fetch_fn(sym, tf, BACKTEST_TIME_MS) for tf in needed_tfs}
            with lock:
                progress["n"] += 1
                if progress["n"] % 50 == 0:
                    print(f"  {progress['n']}/{len(symbols)}...")
            hits = scan_symbol(data[ob_tf], data[wy_tf], phases, dir_filter,
                               zone_filter, atr_mult)
            if hits:
                return sym, hits, data
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        raw_results = list(ex.map(_scan, symbols))

    candidates = []
    for res in raw_results:
        if res is None:
            continue
        sym, hits, data_partial = res
        for hit in hits:
            candidates.append({
                "symbol":       sym,
                "direction":    hit["direction"],
                "zone_type":    hit["zone_type"],
                "fvg":          hit.get("fvg"),
                "ob":           hit.get("ob"),
                "wyckoff":      hit["wyckoff"],
                "price":        hit["price"],
                "atr":          hit.get("atr", 0),
                "atr_dist":     hit.get("atr_dist", 0),
                "atr_mult":     atr_mult,
                "data_partial": data_partial,
            })

    print(f"  Dopasowań: {len(candidates)}")

    # ── Phase 2: fetch remaining TFs ──────────────────────────
    remaining_tfs = [tf for tf in TIMEFRAMES if tf not in needed_tfs]
    if candidates and remaining_tfs:
        unique_syms = list(dict.fromkeys(m["symbol"] for m in candidates))
        print(f"\nPobieram pozostałe TF dla {len(unique_syms)} instrumentów...")
        sym_extra = {}

        def _fetch_rest(sym):
            extra = {tf: fetch_fn(sym, tf, BACKTEST_TIME_MS) for tf in remaining_tfs}
            print(f"  {sym} — OK")
            return sym, extra

        with ThreadPoolExecutor(max_workers=min(6, max_workers)) as ex:
            for sym, extra in ex.map(_fetch_rest, unique_syms):
                sym_extra[sym] = extra

        for m in candidates:
            data_all = dict(m["data_partial"])
            data_all.update(sym_extra.get(m["symbol"], {}))
            m["data_by_tf"] = data_all
    else:
        for m in candidates:
            m["data_by_tf"] = dict(m.get("data_partial", {}))

    # ── Sort: OB+FVG first, then LONG before SHORT, then by conf ──
    candidates.sort(key=lambda m: (
        m["zone_type"] == "OB+FVG",
        m["direction"] == "LONG",
        CONF_RANK.get(m["wyckoff"]["confidence"], 0),
    ), reverse=True)

    # ── Generate & save HTML ──────────────────────────────────
    scan_cfg = {"ob_tf": ob_tf, "wy_tf": wy_tf, "phases": phases,
                "total_scanned": len(symbols), "zone_filter": zone_filter,
                "atr_mult": atr_mult}
    html = generate_html(candidates, scan_cfg, report_time, data_time, is_backtest)

    ph_safe  = "".join(phases)
    zf_safe  = zone_filter.lower()
    atr_safe = str(atr_mult).replace(".", "p")
    if market == "s":
        mkt_lbl = "stocks_" + os.path.splitext(stock_filename)[0]
    elif market == "f":
        mkt_lbl = "crypto_ftmo"
    elif market == "b":
        mkt_lbl = "crypto_binance"
    else:
        mkt_lbl = "crypto_bybit"

    ts_str = datetime.now().strftime("%Y_%m_%d_%H%M")
    fname  = f"obfvg_{mkt_lbl}_{zf_safe}{ob_tf}_wy{wy_tf}_ph{ph_safe}_atr{atr_safe}"
    if is_backtest and BACKTEST_TIME_MS:
        bt_ts   = datetime.fromtimestamp(BACKTEST_TIME_MS / 1000).strftime("%Y_%m_%d_%H%M")
        outfile = os.path.join(OUTPUT_DIR, f"{fname}_{bt_ts}_bt.html")
    else:
        outfile = os.path.join(OUTPUT_DIR, f"{fname}_{ts_str}.html")

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)

    long_n  = sum(1 for m in candidates if m["direction"] == "LONG")
    short_n = sum(1 for m in candidates if m["direction"] == "SHORT")
    print(f"\n✅ Raport: {outfile}")
    print(f"   {len(candidates)} dopasowanie(ń) — {long_n} LONG, {short_n} SHORT")

    abs_path = os.path.abspath(outfile)
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", abs_path])
        print("   Otwieranie w Chrome...")
    except FileNotFoundError:
        try:
            subprocess.Popen(["open", abs_path])
        except Exception as e:
            print(f"   Nie udało się otworzyć przeglądarki: {e}")


if __name__ == "__main__":
    main()
