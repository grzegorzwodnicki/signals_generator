#!/usr/bin/env python3
"""
Instrument Market Structure Analyzer
Pobiera dane z Bybit dla jednego symbolu i generuje raport HTML
z analizą: Wyckoff, Liquidity, Sweeps, SMC Structure, Volume/POC, Elliott (light).
Brak sugestii transakcyjnych — wyłącznie opis stanu rynku.
"""

import requests
import time
import os
import subprocess
from datetime import datetime, timezone
from wyckoff_core import analyze_wyckoff as _analyze_wyckoff_core

def analyze_wyckoff(candles):
    return _analyze_wyckoff_core(candles, verbose=True)

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

BACKTEST_TIME_MS = None

TIMEFRAMES   = ["5m", "15m", "30m", "1H", "4H", "1D", "1W"]
TF_LABEL     = {"5m": "5M", "15m": "15M", "30m": "30M", "1H": "1H", "4H": "4H", "1D": "1D", "1W": "1W"}
TF_BYBIT     = {"5m": "5",  "15m": "15",  "30m": "30",  "1H": "60", "4H": "240", "1D": "D", "1W": "W"}

# ============================================================
# DATA FETCHING
# ============================================================

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
        d = _get_json("https://api.bybit.com/v5/market/kline", params)
        rc = d.get("retCode")
        if rc == 0:
            return d["result"]["list"]
        if rc in (10002, 10006, 10018) or "rate" in d.get("retMsg", "").lower():
            time.sleep(2 ** (attempt + 1))
            continue
        return []
    return []

def _parse(raw):
    out = [{"time": int(c[0])//1000, "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in raw]
    out.reverse()
    return out

def get_candles(symbol, interval, end_time_ms=None):
    b1 = _bybit_candles_raw(symbol, interval, 200, end_time_ms)
    if not b1:
        return []
    oldest_ts = min(int(c[0]) for c in b1)
    b2 = _bybit_candles_raw(symbol, interval, 200, oldest_ts - 1)
    return _parse(b1 + b2)

# ============================================================
# POLYGON (STOCKS)
# ============================================================

def _read_polygon_key():
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env) as f:
            for line in f:
                if line.strip().startswith("POLYGON_API_KEY="):
                    return line.strip().split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get("POLYGON_API_KEY", "")

POLYGON_API_KEY = _read_polygon_key()

_POLYGON_TF = {
    "5m":  (5,  "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1H":  (1,  "hour"),
    "4H":  (4,  "hour"),
    "1D":  (1,  "day"),
    "1W":  (1,  "week"),
}

_POLYGON_LOOKBACK_DAYS = {
    "5m":  15,
    "15m": 30,
    "30m": 60,
    "1H":  110,
    "4H":  450,
    "1D":  650,
    "1W":  1400,
}

def get_candles_stock(symbol, interval, end_time_ms=None):
    mult, timespan = _POLYGON_TF.get(interval, (1, "day"))
    days    = _POLYGON_LOOKBACK_DAYS.get(interval, 120)
    end_ms  = end_time_ms or int(time.time() * 1000)
    from_ms = end_ms - days * 24 * 3600 * 1000
    from_date = datetime.fromtimestamp(from_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    to_date   = datetime.fromtimestamp(end_ms   / 1000, timezone.utc).strftime("%Y-%m-%d")
    url = (f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range"
           f"/{mult}/{timespan}/{from_date}/{to_date}")
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": POLYGON_API_KEY}
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=20)
            data = r.json()
            if data.get("status") in ("OK", "DELAYED"):
                raw = data.get("results") or []
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

# ============================================================
# HELPERS
# ============================================================

def swing_points(candles, lb=3):
    highs, lows = [], []
    for i in range(lb, len(candles) - lb):
        h, l = candles[i]["high"], candles[i]["low"]
        if all(h >= candles[i-j]["high"] and h >= candles[i+j]["high"] for j in range(1, lb+1)):
            highs.append({"idx": i, "price": h})
        if all(l <= candles[i-j]["low"]  and l <= candles[i+j]["low"]  for j in range(1, lb+1)):
            lows.append({"idx": i, "price": l})
    return highs, lows

def _alternating_swings(candles, lb=5, n=10):
    """Return last n alternating H/L swing points (no consecutive same-type)."""
    src = candles[-200:] if len(candles) > 200 else candles
    sh, sl = swing_points(src, lb=lb)
    all_sw = (
        [{"idx": p["idx"], "price": p["price"], "type": "H"} for p in sh] +
        [{"idx": p["idx"], "price": p["price"], "type": "L"} for p in sl]
    )
    all_sw.sort(key=lambda x: x["idx"])
    cleaned = []
    for s in all_sw:
        if cleaned and cleaned[-1]["type"] == s["type"]:
            if (s["type"] == "H" and s["price"] >= cleaned[-1]["price"]) or \
               (s["type"] == "L" and s["price"] <= cleaned[-1]["price"]):
                cleaned[-1] = s
        else:
            cleaned.append(s)
    return cleaned[-n:] if len(cleaned) > n else cleaned

def trend(candles, lookback=30):
    if len(candles) < lookback:
        return "unclear"
    c = [x["close"] for x in candles[-lookback:]]
    mid = lookback // 2
    f, s = sum(c[:mid])/mid, sum(c[mid:])/mid
    if s > f * 1.015:  return "bullish"
    if s < f * 0.985:  return "bearish"
    return "neutral"


# ============================================================
# LIQUIDITY
# ============================================================

_LB_RANK = {"major": 3, "intermediate": 2, "minor": 1}

def analyze_liquidity(candles, tol=0.003):
    """
    Wykrywa poziomy liquidity w trzech oknach swingów:
      lb=3 → minor, lb=5 → intermediate, lb=8 → major
    Zwraca wszystkie znaczące poziomy (nie tylko equal) z etykietą siły.
    """
    if not candles or len(candles) < 10:
        return {}

    src  = candles[-150:] if len(candles) > 150 else candles
    last = candles[-1]["close"]

    # Zbierz swingi z trzech okien
    raw_h, raw_l = [], []
    for lb, label in [(3, "minor"), (5, "intermediate"), (8, "major")]:
        sh, sl = swing_points(src, lb=lb)
        for p in sh:
            raw_h.append({"price": p["price"], "lb_type": label})
        for p in sl:
            raw_l.append({"price": p["price"], "lb_type": label})

    def cluster(raw):
        levels = []
        for p in sorted(raw, key=lambda x: x["price"]):
            price, lb_type = p["price"], p["lb_type"]
            merged = False
            for lv in levels:
                if abs(price - lv["level"]) / lv["level"] <= tol:
                    n = lv["count"]
                    lv["level"] = (lv["level"] * n + price) / (n + 1)
                    lv["count"] += 1
                    if _LB_RANK.get(lb_type, 0) > _LB_RANK.get(lv["best_type"], 0):
                        lv["best_type"] = lb_type
                    merged = True
                    break
            if not merged:
                levels.append({"level": price, "count": 1, "best_type": lb_type})
        return levels

    def label_level(lv, side):
        if lv.get("force_type"):
            return lv["force_type"]
        equal = lv["count"] >= 2
        bt    = lv.get("best_type", "minor")
        if equal:
            return "EQH" if side == "high" else "EQL"
        if bt == "major":
            return "Major SH" if side == "high" else "Major SL"
        if bt == "intermediate":
            return "Swing H" if side == "high" else "Swing L"
        return "Minor SH" if side == "high" else "Minor SL"

    def score(lv):
        return lv["count"] * _LB_RANK.get(lv.get("best_type", "minor"), 1)

    cl_h = cluster(raw_h)
    cl_l = cluster(raw_l)

    rh = max(c["high"] for c in src)
    rl = min(c["low"]  for c in src)

    above, below = [], []

    for lv in cl_h:
        if lv["level"] > last:
            above.append({
                "level":  lv["level"],
                "count":  lv["count"],
                "type":   label_level(lv, "high"),
                "score":  score(lv),
            })

    for lv in cl_l:
        if lv["level"] < last:
            below.append({
                "level":  lv["level"],
                "count":  lv["count"],
                "type":   label_level(lv, "low"),
                "score":  score(lv),
            })

    # Dodaj ekstrema okresu (jeśli nie zduplikowane)
    if not any(abs(x["level"] - rh) / rh <= tol for x in above):
        above.append({"level": rh, "count": 1, "type": "Period High", "score": 6})
    if not any(abs(x["level"] - rl) / rl <= tol for x in below):
        below.append({"level": rl, "count": 1, "type": "Period Low", "score": 6})

    above.sort(key=lambda x: x["level"])               # ascending: closest above price first
    below.sort(key=lambda x: x["level"], reverse=True) # descending: closest below price first

    eq_h = [x for x in cl_h if x["count"] >= 2]
    eq_l = [x for x in cl_l if x["count"] >= 2]

    return {
        "equal_highs": eq_h, "equal_lows": eq_l,
        "range_high":  rh,   "range_low":  rl,
        "above":       above,
        "below":       below,
        "last":        last,
    }

# ============================================================
# SWEEPS
# ============================================================

def analyze_sweeps(candles):
    if not candles or len(candles) < 15:
        return {"classification": "No sweep", "sweeps": []}

    src    = candles[-50:] if len(candles) > 50 else candles
    sw_h, sw_l = swing_points(src[:-5], lb=3)
    recent = candles[-12:]

    sweeps = []
    for c in recent:
        for sl in sw_l[-5:]:
            if c["low"] < sl["price"] and c["close"] > sl["price"]:
                sweeps.append({"type": "Bullish sweep of lows", "level": sl["price"], "direction": "bullish"})
                break
        for sh in sw_h[-5:]:
            if c["high"] > sh["price"] and c["close"] < sh["price"]:
                sweeps.append({"type": "Bearish sweep of highs", "level": sh["price"], "direction": "bearish"})
                break

    cls = ["No sweep", "Minor sweep", "Major sweep", "Multiple sweeps"][min(len(sweeps), 3)]
    return {"classification": cls, "sweeps": sweeps}

# ============================================================
# SMC STRUCTURE
# ============================================================

def analyze_structure(candles):
    if not candles or len(candles) < 20:
        return {}

    src   = candles[-60:] if len(candles) > 60 else candles
    sw_h, sw_l = swing_points(src, lb=3)

    short_t = trend(candles, min(15, len(candles)))
    mid_t   = trend(candles, min(40, len(candles)))

    bos, choch = [], []

    if sw_h and sw_l:
        last_sh = sw_h[-1]["price"]
        last_sl = sw_l[-1]["price"]

        for c in candles[-8:]:
            if mid_t == "bullish"  and c["close"] > last_sh:
                bos.append({"type": "BOS ↑", "level": last_sh, "direction": "bullish"})
            elif mid_t == "bearish" and c["close"] < last_sl:
                bos.append({"type": "BOS ↓", "level": last_sl, "direction": "bearish"})
            elif mid_t == "bearish" and c["close"] > last_sh:
                choch.append({"type": "ChoCH ↑ (bullish reversal signal)", "level": last_sh})
            elif mid_t == "bullish" and c["close"] < last_sl:
                choch.append({"type": "ChoCH ↓ (bearish reversal signal)", "level": last_sl})

    bodies = [abs(c["close"]-c["open"])/(c["high"]-c["low"])
              if (c["high"]-c["low"]) > 0 else 0 for c in candles[-20:]]
    clean  = (sum(bodies)/len(bodies)) > 0.45 if bodies else False

    return {
        "short_trend": short_t, "mid_trend": mid_t,
        "bos": bos[:3], "choch": choch[:3],
        "clean": clean,
        "last_sh": sw_h[-1]["price"] if sw_h else None,
        "last_sl": sw_l[-1]["price"] if sw_l else None,
    }

# ============================================================
# VOLUME PROFILE / POC
# ============================================================

def estimate_poc(candles, n_buckets=60):
    if not candles or len(candles) < 5:
        return None

    src       = candles[-100:] if len(candles) > 100 else candles
    price_min = min(c["low"]  for c in src)
    price_max = max(c["high"] for c in src)
    if price_max == price_min:
        return None

    bsize   = (price_max - price_min) / n_buckets
    profile = [0.0] * n_buckets

    for c in src:
        lo_b = max(0, min(n_buckets-1, int((c["low"]  - price_min) / bsize)))
        hi_b = max(0, min(n_buckets-1, int((c["high"] - price_min) / bsize)))
        span = hi_b - lo_b + 1
        vpp  = c["volume"] / span
        for b in range(lo_b, hi_b+1):
            profile[b] += vpp

    poc_b   = profile.index(max(profile))
    poc_p   = price_min + (poc_b + 0.5) * bsize
    max_v   = max(profile)

    def cluster_prices(prices, tol=0.005):
        if not prices: return []
        groups, grp = [], [prices[0]]
        for p in prices[1:]:
            if abs(p - grp[-1]) / grp[-1] < tol: grp.append(p)
            else:
                groups.append(sum(grp)/len(grp))
                grp = [p]
        groups.append(sum(grp)/len(grp))
        return groups

    hvn = cluster_prices(sorted(price_min+(i+0.5)*bsize for i,v in enumerate(profile) if v > max_v*0.70))
    lvn = cluster_prices(sorted(price_min+(i+0.5)*bsize for i,v in enumerate(profile) if 0 < v < max_v*0.25))

    last = candles[-1]["close"]
    return {
        "poc": poc_p,
        "poc_above": poc_p > last,
        "poc_below": poc_p < last,
        "hvn": hvn, "lvn": lvn,
        "price_min": price_min, "price_max": price_max, "last": last,
    }

# ============================================================
# MACD helpers
# ============================================================

def _ema_list(values, period):
    """EMA; returns same-length list, None for first (period-1) elements."""
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out = [None] * (period - 1)
    out.append(sum(values[:period]) / period)
    for v in values[period:]:
        out.append(out[-1] * (1 - k) + v * k)
    return out

def _macd_hist(candles, fast=12, slow=26, sig_p=9):
    """MACD histogram list aligned with candles. None where warmup incomplete."""
    closes = [c["close"] for c in candles]
    ef = _ema_list(closes, fast)
    es = _ema_list(closes, slow)
    macd = [f - s if f is not None and s is not None else None
            for f, s in zip(ef, es)]
    valid = [v for v in macd if v is not None]
    if len(valid) < sig_p:
        return [None] * len(candles)
    sig_raw = _ema_list(valid, sig_p)
    vi, sig = 0, []
    for v in macd:
        if v is not None:
            sig.append(sig_raw[vi]); vi += 1
        else:
            sig.append(None)
    return [m - s if m is not None and s is not None else None
            for m, s in zip(macd, sig)]

def _h_at(hist, idx):
    """Histogram value nearest to idx (±5 bar fallback → 0.0 if none found)."""
    for d in range(6):
        for ii in (idx - d, idx + d):
            if 0 <= ii < len(hist) and hist[ii] is not None:
                return hist[ii]
    return 0.0

# ============================================================
# ELLIOTT
# ============================================================

def _find_impulse(swings, hist, bull):
    """
    Try to fit a 5-wave impulse to alternating swings.
    bull=True → bullish (L-H-L-H-L-H); False → bearish (H-L-H-L-H-L).
    Returns best-scoring result dict (with internal _score/_n keys) or None.
    """
    start_type = "L" if bull else "H"
    best = None
    best_score = -999

    for si in range(len(swings) - 3):
        w0 = swings[si]
        if w0["type"] != start_type:
            continue

        pts = swings[si : si + 6]   # W0 … up to W5
        n   = len(pts)
        if n < 4:
            continue

        # ── Elliott rule validation ──────────────────────────
        if bull:
            if n >= 3 and pts[2]["price"] <= pts[0]["price"]:   # W2 can't go below W0
                continue
            if n >= 5 and pts[4]["price"] <= pts[1]["price"]:   # W4 can't overlap W1
                continue
        else:
            if n >= 3 and pts[2]["price"] >= pts[0]["price"]:   # W2 can't go above W0
                continue
            if n >= 5 and pts[4]["price"] >= pts[1]["price"]:   # W4 can't overlap W1
                continue

        # W3 must not be the shortest wave (enforceable only when W5 exists)
        if n == 6:
            w1_sz = abs(pts[1]["price"] - pts[0]["price"])
            w3_sz = abs(pts[3]["price"] - pts[2]["price"])
            w5_sz = abs(pts[5]["price"] - pts[4]["price"])
            if w3_sz < w1_sz and w3_sz < w5_sz:
                continue

        # ── MACD: W3 should show most extreme histogram in W0→W3 window ──
        w0_ci = pts[0]["idx"]
        w3_ci = pts[3]["idx"]
        window_vals = [_h_at(hist, ci) for ci in range(w0_ci, w3_ci + 1)]
        w3_hist = _h_at(hist, w3_ci)

        if bull:
            peak   = max(window_vals) if window_vals else 0
            macd_q = max(0.0, min(1.0, w3_hist / peak)) if peak > 0 else 0.5
        else:
            trough = min(window_vals) if window_vals else 0
            macd_q = max(0.0, min(1.0, w3_hist / trough)) if trough < 0 else 0.5

        # ── Divergence at W5 ──────────────────────────────────
        div_note = ""
        if n == 6:
            w5_hist = _h_at(hist, pts[5]["idx"])
            if bull and pts[5]["price"] > pts[3]["price"] and w5_hist < w3_hist:
                div_note = "⚠ MACD bearish divergence — W5 top"
            elif not bull and pts[5]["price"] < pts[3]["price"] and w5_hist > w3_hist:
                div_note = "⚠ MACD bullish divergence — W5 bottom"

        # ── Score: more points + MACD quality + recency ──────
        score = n * 15 + int(macd_q * 10) + pts[-1]["idx"] // 5
        if score <= best_score:
            continue
        best_score = score

        # ── Build result ──────────────────────────────────────
        wave_label = str(n - 1)  # W0→1 W0W1→1 … W0…W5→5
        if div_note:
            wave_label = "5 div"

        w_prev = pts[-2]["price"] if n >= 2 else pts[0]["price"]
        phase  = f"Wave {wave_label}  {_f(w_prev)} → {_f(pts[-1]['price'])}"
        if div_note:
            phase += f"  {div_note}"

        levels = [{"label": f"W{k}", "price": pts[k]["price"], "type": pts[k]["type"]}
                  for k in range(n)]

        w1_sz = abs(pts[1]["price"] - pts[0]["price"]) if n >= 2 else 0
        fib_targets = []
        if n >= 3 and w1_sz > 0:
            base = pts[2]["price"]
            sign = 1 if bull else -1
            fib_targets = [
                {"label": "W3 ext 1.618×W1", "price": base + sign * w1_sz * 1.618},
                {"label": "W3 ext 2.618×W1", "price": base + sign * w1_sz * 2.618},
            ]
        if n >= 5 and w1_sz > 0:
            base5 = pts[4]["price"]
            sign  = 1 if bull else -1
            fib_targets += [
                {"label": "W5 = 1.0×W1",   "price": base5 + sign * w1_sz},
                {"label": "W5 = 1.618×W1", "price": base5 + sign * w1_sz * 1.618},
            ]

        best = {
            "wave":            wave_label,
            "phase":           phase,
            "direction":       "bullish" if bull else "bearish",
            "levels":          levels,
            "fib_targets":     fib_targets,
            "confidence":      ("HIGH"   if n >= 5 and macd_q > 0.7 else
                                "MEDIUM" if n >= 4 and macd_q > 0.4 else "LOW"),
            "macd_divergence": bool(div_note),
            "divergence_note": div_note,
            "macd_w3_quality": round(macd_q, 2),
            "_score":          score,
            "_n":              n,
        }

    return best


def _find_abc(swings, hist):
    """Try to identify an ABC correction in recent swings. Returns result dict or None."""
    best = None
    best_score = -1

    for i in range(max(0, len(swings) - 5), len(swings) - 2):
        a, b, c = swings[i], swings[i + 1], swings[i + 2]
        a_h = _h_at(hist, a["idx"])
        c_h = _h_at(hist, c["idx"])

        if a["type"] == "H" and b["type"] == "L" and c["type"] == "H":
            div  = c["price"] >= a["price"] * 0.99 and c_h < a_h
            ab   = a["price"] - b["price"]
            phase = f"ABC ↓  A:{_f(a['price'])}  B:{_f(b['price'])}  C:{_f(c['price'])}"
            if div:
                phase += "  ⚠ MACD bearish div at C"
            score = 30 + (25 if div else 0) + (15 if i >= len(swings) - 3 else 0)
            if score > best_score:
                best_score = score
                best = {
                    "wave": "C↓",
                    "phase": phase,
                    "direction": "bearish",
                    "levels": [
                        {"label": "A", "price": a["price"], "type": "H"},
                        {"label": "B", "price": b["price"], "type": "L"},
                        {"label": "C", "price": c["price"], "type": "H"},
                    ],
                    "fib_targets": [
                        {"label": "C = 1.0×AB",   "price": a["price"] - ab},
                        {"label": "C = 1.618×AB", "price": a["price"] - ab * 1.618},
                    ],
                    "confidence":      "MEDIUM" if div else "LOW",
                    "macd_divergence": div,
                    "divergence_note": "MACD bearish divergence at C" if div else "",
                }

        elif a["type"] == "L" and b["type"] == "H" and c["type"] == "L":
            div  = c["price"] <= a["price"] * 1.01 and c_h > a_h
            ab   = b["price"] - a["price"]
            phase = f"ABC ↑  A:{_f(a['price'])}  B:{_f(b['price'])}  C:{_f(c['price'])}"
            if div:
                phase += "  ⚠ MACD bullish div at C"
            score = 30 + (25 if div else 0) + (15 if i >= len(swings) - 3 else 0)
            if score > best_score:
                best_score = score
                best = {
                    "wave": "C↑",
                    "phase": phase,
                    "direction": "bullish",
                    "levels": [
                        {"label": "A", "price": a["price"], "type": "L"},
                        {"label": "B", "price": b["price"], "type": "H"},
                        {"label": "C", "price": c["price"], "type": "L"},
                    ],
                    "fib_targets": [
                        {"label": "C = 1.0×AB",   "price": a["price"] + ab},
                        {"label": "C = 1.618×AB", "price": a["price"] + ab * 1.618},
                    ],
                    "confidence":      "MEDIUM" if div else "LOW",
                    "macd_divergence": div,
                    "divergence_note": "MACD bullish divergence at C" if div else "",
                }

    return best


def analyze_elliott(candles):
    """
    Elliott Wave analysis using MACD histogram to anchor W3 (most extreme MACD = end of W3)
    and detect divergence at W5 or ABC wave C.
    """
    if not candles or len(candles) < 40:
        return {"wave": "?", "phase": "Insufficient data", "direction": "unclear",
                "levels": [], "fib_targets": [], "confidence": "LOW",
                "macd_divergence": False, "divergence_note": ""}

    # Both MACD and swings operate on the same 200-candle slice so indices align
    src    = candles[-200:] if len(candles) > 200 else candles
    hist   = _macd_hist(src)
    swings = _alternating_swings(candles, lb=5, n=20)   # internally uses candles[-200:]

    if len(swings) < 3:
        return {"wave": "?", "phase": "Not enough swing points", "direction": "unclear",
                "levels": [], "fib_targets": [], "confidence": "LOW",
                "macd_divergence": False, "divergence_note": ""}

    bull_res = _find_impulse(swings, hist, bull=True)
    bear_res = _find_impulse(swings, hist, bull=False)

    impulse = None
    if bull_res and bear_res:
        impulse = bull_res if bull_res["_score"] >= bear_res["_score"] else bear_res
    else:
        impulse = bull_res or bear_res

    abc = _find_abc(swings, hist)

    if impulse:
        n = impulse.pop("_n")
        impulse.pop("_score")
        # Prefer impulse with 4+ points; if only 3 (W0-W2) let ABC win
        if n >= 4 or abc is None:
            return impulse
        if abc:
            return abc
        return impulse

    if abc:
        return abc

    overall_dir = "bullish" if swings[-1]["price"] > swings[0]["price"] else "bearish"
    return {
        "wave": "?",
        "phase": f"No clear structure ({len(swings)} swing points)",
        "direction": overall_dir,
        "levels": [{"label": f"S{i}", "price": s["price"], "type": s["type"]}
                   for i, s in enumerate(swings[-5:])],
        "fib_targets": [],
        "confidence": "LOW",
        "macd_divergence": False,
        "divergence_note": "",
    }


# ============================================================
# HARMONIC PATTERNS
# ============================================================

_HARMONIC_DEFS = {
    "Gartley": {
        "ab_xa": (0.618, 0.618), "bc_ab": (0.382, 0.886),
        "cd_bc": (1.272, 1.618), "ad_xa": (0.786, 0.786),
    },
    "Bat": {
        "ab_xa": (0.382, 0.500), "bc_ab": (0.382, 0.886),
        "cd_bc": (1.618, 2.618), "ad_xa": (0.886, 0.886),
    },
    "Butterfly": {
        "ab_xa": (0.786, 0.786), "bc_ab": (0.382, 0.886),
        "cd_bc": (1.618, 2.618), "ad_xa": (1.272, 1.618),
    },
    "Crab": {
        "ab_xa": (0.382, 0.618), "bc_ab": (0.382, 0.886),
        "cd_bc": (2.618, 3.618), "ad_xa": (1.618, 1.618),
    },
    "Deep Crab": {
        "ab_xa": (0.886, 0.886), "bc_ab": (0.382, 0.886),
        "cd_bc": (2.618, 3.618), "ad_xa": (1.618, 1.618),
    },
    "Shark": {
        "ab_xa": (0.446, 0.618), "bc_ab": (1.130, 1.618),
        "cd_bc": (1.618, 2.240), "ad_xa": (0.886, 1.130),
    },
}

def _ratio_in(val, lo, hi, tol=0.07):
    center  = (lo + hi) / 2
    allowed = (hi - lo) / 2 + tol * center
    return abs(val - center) <= allowed

def find_harmonic_patterns(candles, tol=0.07):
    """Detect XABCD harmonic patterns from alternating swing points."""
    if not candles or len(candles) < 40:
        return []

    swings = _alternating_swings(candles, lb=5, n=14)
    if len(swings) < 5:
        return []

    patterns = []
    last_idx  = len(swings) - 1

    for i in range(len(swings) - 4):
        X, A, B, C, D = swings[i], swings[i+1], swings[i+2], swings[i+3], swings[i+4]

        xa = abs(A["price"] - X["price"])
        ab = abs(B["price"] - A["price"])
        bc = abs(C["price"] - B["price"])
        cd = abs(D["price"] - C["price"])
        ad = abs(D["price"] - A["price"])   # AD leg (A→D), not XD

        if xa < 1e-9 or ab < 1e-9 or bc < 1e-9 or cd < 1e-9:
            continue

        ab_xa = ab / xa
        bc_ab = bc / ab
        cd_bc = cd / bc
        ad_xa = ad / xa

        direction = "bullish" if D["type"] == "L" else "bearish"

        for name, defn in _HARMONIC_DEFS.items():
            if (
                _ratio_in(ab_xa, *defn["ab_xa"], tol) and
                _ratio_in(bc_ab, *defn["bc_ab"], tol) and
                _ratio_in(cd_bc, *defn["cd_bc"], tol) and
                _ratio_in(ad_xa, *defn["ad_xa"], tol)
            ):
                patterns.append({
                    "name":      name,
                    "direction": direction,
                    "X": X["price"], "A": A["price"], "B": B["price"],
                    "C": C["price"], "D": D["price"],
                    "ab_xa": round(ab_xa, 3),
                    "bc_ab": round(bc_ab, 3),
                    "cd_bc": round(cd_bc, 3),
                    "ad_xa": round(ad_xa, 3),
                    "active": (i + 4) >= last_idx - 1,
                })

    return patterns

# ============================================================
# DATA FRESHNESS
# ============================================================

def check_data_freshness(candles, tf_seconds):
    if not candles:
        return "NO_DATA"
    last_ts = candles[-1]["time"]
    now_ts  = int(time.time())
    diff    = now_ts - last_ts
    if diff > tf_seconds * 2:
        return "STALE"
    if diff > tf_seconds:
        return "WARNING"
    return "FRESH"

# ============================================================
# v9.1 READINESS
# ============================================================

def analyze_v91_readiness(data_by_tf):
    htf = data_by_tf.get("1H", [])
    mtf = data_by_tf.get("30m", [])
    ltf = data_by_tf.get("5m", [])

    if not htf or not mtf or not ltf:
        return "NO_STRUCTURE"

    wy = analyze_wyckoff(htf)
    st = analyze_structure(ltf)

    if wy.get("phase") == "D" and st.get("choch"):
        return "READY_FOR_V9_1_SCAN"
    if wy.get("phase") in ("C", "D"):
        return "WATCH_ONLY"
    if wy.get("ranging"):
        return "CHOP"
    return "NO_STRUCTURE"

# ============================================================
# HTML HELPERS
# ============================================================

def _f(v):
    if v is None: return "N/A"
    if v >= 1000: return f"{v:,.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def _row(label, val, col="#e6edf7"):
    return (f'<div style="display:flex;justify-content:space-between;padding:4px 0;'
            f'border-bottom:1px solid #1a2535">'
            f'<span style="color:#8899aa">{label}</span>'
            f'<span style="color:{col};font-weight:500">{val}</span></div>')

def _section(title, content):
    return (f'<div style="padding:20px 32px 0">'
            f'<h2 style="color:#e6edf7;border-bottom:1px solid #1e2d40;'
            f'padding-bottom:8px;margin-bottom:16px">{title}</h2>'
            f'{content}</div>')

def _card(content, border="#1e2d40", bg="#101827"):
    return (f'<div style="background:{bg};border:1px solid {border};'
            f'border-radius:8px;padding:16px;margin-bottom:12px">{content}</div>')

# ============================================================
# HTML REPORT
# ============================================================

def generate_report(symbol, data_by_tf, report_time, data_time, is_backtest):
    freshness_5m = check_data_freshness(data_by_tf.get("5m", []), 300)
    freshness_label = {
        "FRESH":   "🟢 DATA FRESH",
        "WARNING": "🟡 DATA SLIGHTLY DELAYED",
        "STALE":   "🔴 DATA TOO OLD",
        "NO_DATA": "❌ NO DATA",
    }[freshness_5m]

    v91_status = analyze_v91_readiness(data_by_tf)
    v91_color  = {
        "READY_FOR_V9_1_SCAN": "#00ff8c",
        "WATCH_ONLY":           "#ffd700",
        "CHOP":                 "#ff8844",
        "NO_STRUCTURE":         "#8899aa",
    }.get(v91_status, "#8899aa")

    # Run all analyses
    A = {}
    for tf in TIMEFRAMES:
        c = data_by_tf.get(tf, [])
        if not c:
            A[tf] = None
            continue
        A[tf] = {
            "candles":   c,
            "wyckoff":   analyze_wyckoff(c),
            "liquidity": analyze_liquidity(c),
            "sweeps":    analyze_sweeps(c),
            "structure": analyze_structure(c),
            "poc":       estimate_poc(c),
            "elliott":   analyze_elliott(c),
            "harmonics": find_harmonic_patterns(c),
        }

    # Market state (cross-TF)
    trends = [A[tf]["wyckoff"]["full_trend"] for tf in TIMEFRAMES if A.get(tf)]
    b = trends.count("bullish"); e = trends.count("bearish")
    n = len(trends)
    if   b >= n * 0.60: market_state = "BULLISH"
    elif e >= n * 0.60: market_state = "BEARISH"
    elif b > 0 and e > 0: market_state = "TRANSITION"
    elif trends.count("neutral") >= n * 0.60: market_state = "CHOP"
    else: market_state = "NEUTRAL"

    state_col = {"BULLISH":"#00ff8c","BEARISH":"#ff4d4d","CHOP":"#ff8844",
                 "NEUTRAL":"#ffd700","TRANSITION":"#88ccff"}.get(market_state,"#8899aa")

    # ── Wyckoff table ──
    wy_rows = ""
    for tf in TIMEFRAMES:
        a = A.get(tf)
        if not a:
            wy_rows += f'<tr><td style="font-weight:bold">{TF_LABEL[tf]}</td><td colspan="4" style="color:#556677">No data</td></tr>'
            continue
        w = a["wyckoff"]
        cc = {"HIGH":"#00ff8c","MEDIUM":"#ffd700","LOW":"#888"}.get(w["confidence"],"#888")
        sc = ("#00ff8c" if any(k in w["structure"] for k in ("Accum","Bullish"))
              else "#ff4d4d" if any(k in w["structure"] for k in ("Distrib","Bearish"))
              else "#ffd700")
        rh_str    = _f(w["range_high"]) if w["range_high"] else "N/A"
        rl_str    = _f(w["range_low"])  if w["range_low"]  else "N/A"
        range_str = f'{rl_str} — {rh_str}'
        wy_rows += (f'<tr><td style="font-weight:bold">{TF_LABEL[tf]}</td>'
                    f'<td style="color:{sc}">{w["structure"]}</td>'
                    f'<td>Phase {w["phase"]}</td>'
                    f'<td style="color:#00e5ff;font-size:12px;white-space:nowrap">{range_str}</td>'
                    f'<td style="font-size:12px;color:#aabbcc">{" | ".join(w["events"][:2])}</td>'
                    f'<td style="color:{cc}">{w["confidence"]}</td></tr>')

    wyckoff_html = (
        '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead style="background:#0d1520"><tr>'
        '<th style="padding:8px;color:#8899aa;text-align:left">TF</th>'
        '<th>Structure</th><th>Phase</th><th style="white-space:nowrap">Range (Low — High)</th><th>Key Events</th><th>Confidence</th>'
        f'</tr></thead><tbody style="color:#e6edf7">{wy_rows}</tbody></table></div>'
    )

    # ── Liquidity map — skonsolidowana multi-TF ──
    _TYPE_COLOR = {
        "EQH": "#00ff8c", "EQL": "#ff4d4d",
        "Period High": "#00ffcc", "Period Low": "#ff8866",
        "Major SH": "#00e5ff", "Major SL": "#ff6688",
        "Swing H": "#88ddff", "Swing L": "#ff99aa",
        "Minor SH": "#aabbcc", "Minor SL": "#aabbcc",
    }

    # Zbierz poziomy ze wszystkich TF i scal po tolerancji 0.4%
    merged_above, merged_below = [], []
    tol_merge = 0.004
    cur_price  = None

    for tf in TIMEFRAMES:
        a = A.get(tf)
        if not a or not a.get("liquidity"):
            continue
        liq = a["liquidity"]
        if cur_price is None:
            cur_price = liq["last"]

        def _merge_into(target, levels, tf_label):
            for lv in levels:
                existing = next(
                    (x for x in target
                     if abs(x["level"] - lv["level"]) / max(x["level"], 0.0001) <= tol_merge),
                    None
                )
                if existing:
                    n = existing["count"]
                    existing["level"] = (existing["level"] * n + lv["level"]) / (n + 1)
                    existing["count"] += 1
                    existing["score"] = existing.get("score", 1) + lv.get("score", 1)
                    if tf_label not in existing["tfs"]:
                        existing["tfs"].append(tf_label)
                    # Upgrade type if stronger
                    rank = {"EQH":7,"EQL":7,"Period High":6,"Period Low":6,
                            "Major SH":5,"Major SL":5,"Swing H":3,"Swing L":3,
                            "Minor SH":1,"Minor SL":1}
                    if rank.get(lv.get("type",""),0) > rank.get(existing.get("type",""),0):
                        existing["type"] = lv["type"]
                else:
                    entry = dict(lv)
                    entry["tfs"] = [tf_label]
                    entry.setdefault("score", 1)
                    target.append(entry)

        _merge_into(merged_above, liq["above"], TF_LABEL[tf])
        _merge_into(merged_below, liq["below"], TF_LABEL[tf])

    # Filtruj poziomy oddalone o więcej niż 15% od ceny, sortuj closest-first
    if cur_price:
        merged_above = [x for x in merged_above if (x["level"] - cur_price) / cur_price <= 0.15]
        merged_below = [x for x in merged_below if (cur_price - x["level"]) / cur_price <= 0.15]

    merged_above.sort(key=lambda x: x["level"])          # rosnąco — najbliższy poziom u góry jako pierwszy
    merged_below.sort(key=lambda x: x["level"], reverse=True)  # malejąco — najbliższy poziom dołu jako pierwszy

    def _liq_row(x, arrow, bg):
        lv_type  = x.get("type", "Level")
        col      = _TYPE_COLOR.get(lv_type, "#aabbcc")
        count    = x.get("count", 1)
        tfs_str  = " · ".join(x.get("tfs", []))
        touches  = f" ×{count}" if count > 1 else ""
        strength = "●" * min(x.get("score", 1), 5)
        dist_pct = abs(x["level"] - cur_price) / cur_price * 100 if cur_price else 0
        dist_str = f"+{dist_pct:.1f}%" if x["level"] > cur_price else f"-{dist_pct:.1f}%"
        return (
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:3px 6px;background:{bg};border-radius:3px;margin:2px 0">'
            f'<span style="color:{col};font-weight:bold">{arrow} {_f(x["level"])}</span>'
            f'<span style="display:flex;align-items:center;gap:8px">'
            f'<span style="color:#667788;font-size:11px">{dist_str}</span>'
            f'<span style="color:{col};font-size:11px">{lv_type}{touches}</span>'
            f'<span style="color:#445566;font-size:10px">{tfs_str}</span>'
            f'<span style="color:{col};font-size:10px;letter-spacing:-1px">{strength}</span>'
            f'</span></div>'
        )

    above_rows = "".join(_liq_row(x, "▲", "#0a1a0a") for x in merged_above[:10]) \
                 or "<div style='color:#556677;font-size:12px'>None detected</div>"
    below_rows = "".join(_liq_row(x, "▼", "#1a0a0a") for x in merged_below[:10]) \
                 or "<div style='color:#556677;font-size:12px'>None detected</div>"

    price_label = _f(cur_price) if cur_price else "N/A"
    liq_html = (
        f'<div style="font-size:11px;color:#8899aa;margin-bottom:8px">'
        f'Multi-TF consolidated · Price: <strong style="color:#e6edf7">{price_label}</strong> · '
        f'● strength score</div>'
        f'<div style="font-size:12px;color:#00ff8c;margin-bottom:4px">▲ Above price</div>'
        f'{above_rows}'
        f'<div style="text-align:center;padding:5px;background:#0d1520;margin:6px 0;'
        f'font-size:12px;color:#ffd700;border-radius:4px">── {price_label} ──</div>'
        f'<div style="font-size:12px;color:#ff4d4d;margin-bottom:4px">▼ Below price</div>'
        f'{below_rows}'
        f'<div style="margin-top:8px;font-size:10px;color:#445566">'
        f'EQH/EQL = equal highs/lows · Major/Swing/Minor SH/SL = single significant swing · '
        f'Period = absolute extreme</div>'
    )

    # ── Sweeps ──
    sw_html = ""
    for tf in TIMEFRAMES:
        a = A.get(tf)
        if not a: continue
        sw  = a["sweeps"]
        col = {"No sweep":"#8899aa","Minor sweep":"#ffd700",
               "Major sweep":"#ff8844","Multiple sweeps":"#ff4d4d"}.get(sw["classification"],"#8899aa")
        sw_html += (f'<div style="display:flex;justify-content:space-between;padding:4px 0;'
                    f'border-bottom:1px solid #1a2535">'
                    f'<span style="color:#8899aa">{TF_LABEL[tf]}</span>'
                    f'<span style="color:{col}">{sw["classification"]}</span></div>')
        for s in sw.get("sweeps",[])[:2]:
            c2 = "#00ff8c" if s["direction"]=="bullish" else "#ff4d4d"
            sw_html += (f'<div style="padding:2px 0 2px 12px;font-size:12px;color:{c2}">'
                        f'→ {s["type"]} @ {_f(s["level"])}</div>')

    # ── Structure ──
    st_cards = ""
    for tf in TIMEFRAMES:
        a = A.get(tf)
        if not a or not a.get("structure"): continue
        st  = a["structure"]
        mc  = "#00ff8c" if st.get("mid_trend")=="bullish" else ("#ff4d4d" if st.get("mid_trend")=="bearish" else "#ffd700")
        sc2 = "#00ff8c" if st.get("short_trend")=="bullish" else ("#ff4d4d" if st.get("short_trend")=="bearish" else "#ffd700")
        bos_h   = "".join(f'<div style="color:#00e5ff;font-size:12px;padding:1px 0">🔵 {x["type"]} @ {_f(x.get("level"))}</div>' for x in st.get("bos",[])[:2])
        choch_h = "".join(f'<div style="color:#ffd700;font-size:12px;padding:1px 0">⚡ {x["type"]} @ {_f(x.get("level"))}</div>' for x in st.get("choch",[])[:2])
        st_cards += (
            f'<div style="background:#0d1520;border-radius:6px;padding:12px">'
            f'<div style="font-weight:bold;color:#88ccff;margin-bottom:8px">{TF_LABEL[tf]}</div>'
            f'{_row("Short-term", st.get("short_trend","N/A").upper(), sc2)}'
            f'{_row("Mid-term",   st.get("mid_trend","N/A").upper(),   mc)}'
            f'{_row("Structure",  "Clean" if st.get("clean") else "Choppy", "#00ff8c" if st.get("clean") else "#ff8844")}'
            f'{_row("Last swing H", _f(st.get("last_sh")))}'
            f'{_row("Last swing L", _f(st.get("last_sl")))}'
            f'{bos_h}{choch_h}</div>'
        )

    # ── POC / Volume ──
    poc_cards = ""
    for tf in TIMEFRAMES:
        a = A.get(tf)
        if not a or not a.get("poc"): continue
        p    = a["poc"]
        col  = "#00ff8c" if p["poc_below"] else ("#ff4d4d" if p["poc_above"] else "#ffd700")
        role = "Support magnet (price above POC)" if p["poc_below"] else ("Resistance magnet (price below POC)" if p["poc_above"] else "At price")
        hvn_s = " | ".join(_f(x) for x in p.get("hvn",[])[:4]) or "None"
        lvn_s = " | ".join(_f(x) for x in p.get("lvn",[])[:4]) or "None"
        poc_loc    = "above price" if p["poc_above"] else ("below price" if p["poc_below"] else "at price")
        poc_label  = f"{_f(p['poc'])} ({poc_loc})"
        price_rng  = f"{_f(p['price_min'])} — {_f(p['price_max'])}"
        poc_cards += (
            f'<div style="background:#0d1520;border-radius:6px;padding:12px;border-left:3px solid {col}">'
            f'<div style="font-weight:bold;color:#88ccff;margin-bottom:8px">{TF_LABEL[tf]}</div>'
            f'{_row("POC",   poc_label, col)}'
            f'{_row("Role",  role, col)}'
            f'{_row("HVN (high-volume nodes)", hvn_s, "#00e5ff")}'
            f'{_row("LVN (thin zones)",         lvn_s, "#ff8844")}'
            f'{_row("Price range", price_rng, "#8899aa")}'
            f'</div>'
        )

    # ── Elliott Waves (detailed) ──
    ell_cards = ""
    for tf in TIMEFRAMES:
        a = A.get(tf)
        if not a:
            continue
        ew   = a["elliott"]
        ddir = ew.get("direction", "unclear")
        wave = ew.get("wave", "?")
        conf = ew.get("confidence", "LOW")
        dir_col  = "#00ff8c" if ddir == "bullish" else ("#ff4d4d" if ddir == "bearish" else "#ffd700")
        conf_col = {"HIGH": "#00ff8c", "MEDIUM": "#ffd700", "LOW": "#8899aa"}.get(conf, "#8899aa")
        _w = str(wave)
        if _w == "?":
            wave_col = "#445566"
        elif ew.get("macd_divergence") or "div" in _w:
            wave_col = "#ff8844"
        elif "↓" in _w:
            wave_col = "#ff4d4d"
        elif "↑" in _w:
            wave_col = "#00ff8c"
        else:
            wave_col = "#00e5ff"
        wave_display = _w if (_w.startswith("C") or _w == "?") else f"W{_w}"

        levels_html = ""
        for lv in ew.get("levels", []):
            arrow = "▲" if lv["type"] == "H" else "▼"
            a_col = "#00ff8c" if lv["type"] == "H" else "#ff4d4d"
            levels_html += (
                f'<div style="display:flex;justify-content:space-between;padding:2px 0;'
                f'border-bottom:1px solid #111e2e;font-size:12px">'
                f'<span style="color:#8899aa">{lv["label"]}</span>'
                f'<span style="color:{a_col}">{arrow} {_f(lv["price"])}</span></div>'
            )

        fib_html = ""
        for ft in ew.get("fib_targets", []):
            fib_html += (
                f'<div style="display:flex;justify-content:space-between;padding:2px 0;font-size:11px">'
                f'<span style="color:#556677">{ft["label"]}</span>'
                f'<span style="color:#ffd700">{_f(ft["price"])}</span></div>'
            )

        div_banner = ""
        if ew.get("macd_divergence") and ew.get("divergence_note"):
            div_banner = (
                f'<div style="background:#ff884422;border:1px solid #ff8844;border-radius:4px;'
                f'padding:4px 8px;font-size:11px;color:#ff8844;margin-top:6px">'
                f'{ew["divergence_note"]}</div>'
            )

        macd_q = ew.get("macd_w3_quality")
        macd_q_html = ""
        if macd_q is not None:
            q_col = "#00ff8c" if macd_q >= 0.75 else ("#ffd700" if macd_q >= 0.45 else "#8899aa")
            macd_q_html = (
                f'<div style="font-size:10px;color:{q_col};margin-top:2px">'
                f'W3 MACD anchor {int(macd_q*100)}%</div>'
            )

        ell_cards += (
            f'<div style="background:#0d1520;border:1px solid #1e2d40;border-radius:8px;padding:14px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">'
            f'<span style="font-weight:bold;color:#88ccff;font-size:14px">{TF_LABEL[tf]}</span>'
            f'<span style="display:flex;gap:6px">'
            f'<span style="background:{dir_col}22;color:{dir_col};padding:2px 7px;border-radius:4px;font-size:11px">'
            f'{ddir.upper()}</span>'
            f'<span style="background:#1e2d40;color:{conf_col};padding:2px 7px;border-radius:4px;font-size:11px">'
            f'{conf}</span>'
            f'</span></div>'
            f'<div style="font-size:24px;font-weight:bold;color:{wave_col};margin-bottom:2px">{wave_display}</div>'
            f'{macd_q_html}'
            f'<div style="font-size:12px;color:#aabbcc;margin:6px 0 10px">{ew.get("phase","—")}</div>'
            f'{levels_html}'
            f'{"<div style=margin-top:8px;font-size:11px;color:#667788>Fib targets:</div>" + fib_html if fib_html else ""}'
            f'{div_banner}'
            f'</div>'
        )

    # ── Harmonic Patterns ──
    harm_rows = ""
    any_harmonic = False
    for tf in TIMEFRAMES:
        a = A.get(tf)
        if not a:
            continue
        patterns = a.get("harmonics", [])
        if not patterns:
            continue
        any_harmonic = True
        for p in patterns:
            direction = p["direction"]
            dir_col   = "#00ff8c" if direction == "bullish" else "#ff4d4d"
            dir_arrow = "↑" if direction == "bullish" else "↓"
            status    = "ACTIVE" if p.get("active") else "historical"
            st_col    = "#ffd700" if p.get("active") else "#445566"
            harm_rows += (
                f'<tr>'
                f'<td style="color:#88ccff;font-weight:bold">{TF_LABEL[tf]}</td>'
                f'<td style="color:#e6edf7;font-weight:bold">{p["name"]}</td>'
                f'<td style="color:{dir_col};font-weight:bold">{dir_arrow} {direction.upper()}</td>'
                f'<td style="color:{st_col};font-size:11px">{status}</td>'
                f'<td style="white-space:nowrap">'
                f'<span style="color:#8899aa">X</span> {_f(p["X"])} &nbsp;'
                f'<span style="color:#8899aa">A</span> {_f(p["A"])} &nbsp;'
                f'<span style="color:#8899aa">B</span> {_f(p["B"])} &nbsp;'
                f'<span style="color:#8899aa">C</span> {_f(p["C"])} &nbsp;'
                f'<span style="color:{dir_col};font-weight:bold">D(PRZ)</span> '
                f'<span style="color:{dir_col};font-weight:bold">{_f(p["D"])}</span>'
                f'</td>'
                f'<td style="font-size:11px;color:#667788;white-space:nowrap">'
                f'AB/XA={p["ab_xa"]} &nbsp; BC/AB={p["bc_ab"]} &nbsp; '
                f'CD/BC={p["cd_bc"]} &nbsp; AD/XA={p["ad_xa"]}'
                f'</td>'
                f'</tr>'
            )
    if not harm_rows:
        harm_rows = '<tr><td colspan="6" style="color:#556677;text-align:center;padding:12px">No harmonic patterns detected</td></tr>'

    # ── Final interpretation ──
    interp = []
    for tf in ["1H", "4H"]:
        a = A.get(tf)
        if not a: continue
        w  = a["wyckoff"]
        st = a.get("structure", {})
        interp.append(f'{TF_LABEL[tf]}: <strong>{w["structure"]}</strong> Phase {w["phase"]} ({w["confidence"]} confidence).')
        if st.get("choch"):
            interp.append(f'{TF_LABEL[tf]}: ChoCH detected — potential character change.')
        if st.get("bos"):
            interp.append(f'{TF_LABEL[tf]}: BOS confirmed — structure continuation in mid-term direction.')

    if market_state in ("BULLISH","BEARISH"):
        interp.append(f'Cross-timeframe alignment is <strong style="color:{state_col}">{market_state}</strong>.')
    elif market_state == "TRANSITION":
        interp.append("Timeframes are conflicting — market may be transitioning or reversing.")
    else:
        interp.append(f'No clear directional consensus across timeframes ({market_state}).')

    # ── Assemble HTML ──
    css = ("*{box-sizing:border-box;margin:0;padding:0}"
           "body{background:#070b12;color:#e6edf7;font-family:'Segoe UI',Arial,sans-serif;font-size:14px;line-height:1.5}"
           "th{padding:8px 6px;text-align:left;border-bottom:1px solid #1e2d40}"
           "td{padding:7px 6px;border-bottom:1px solid #1a2535;vertical-align:top}"
           "tr:hover td{background:#0d1520}")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{symbol} — Market Structure</title>
  <style>{css}</style>
</head>
<body>

<div style="background:#0d1520;border-bottom:2px solid #00aaff;padding:20px 32px">
  <div style="font-size:11px;color:#8899aa;margin-bottom:4px;letter-spacing:1px">MARKET STRUCTURE ANALYSIS — OBJECTIVE DESCRIPTION ONLY</div>
  <h1 style="font-size:28px;color:#e6edf7;margin-bottom:6px">{symbol}</h1>
  <div style="font-size:12px;color:#8899aa">
    Report: <strong>{report_time}</strong> |
    Data: <strong>{data_time}</strong> |
    {"⚠ BACKTEST MODE" if is_backtest else "✅ LIVE DATA"} |
    {freshness_label} |
    Timeframes: {" · ".join(TF_LABEL[tf] for tf in TIMEFRAMES if A.get(tf))}
  </div>
</div>

{_section("🎯 Market State",
    f'<div style="display:inline-block;background:{state_col};color:#000;font-size:30px;'
    f'font-weight:bold;padding:12px 36px;border-radius:8px;margin-bottom:8px">{market_state}</div>'
    f'<div style="color:#8899aa;font-size:12px;margin-top:6px">Cross-timeframe Wyckoff + trend consensus</div>'
)}

{_section("📊 Wyckoff Analysis", wyckoff_html)}

<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:20px 32px 0">
  <div>
    <h2 style="color:#e6edf7;border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">💧 Liquidity Map</h2>
    {_card(liq_html or "<div style='color:#556677'>No data</div>")}
  </div>
  <div>
    <h2 style="color:#e6edf7;border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">🌊 Sweep Analysis</h2>
    {_card(sw_html or "<div style='color:#556677'>No sweeps detected</div>")}
  </div>
</div>

{_section("🏗 Structure (SMC)",
    f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">{st_cards or "<div style=\"color:#556677\">No data</div>"}</div>'
)}

{_section("📦 Volume Profile / POC",
    f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px">{poc_cards or "<div style=\"color:#ff4d4d\">POC: UNKNOWN</div>"}</div>'
    f'<div style="font-size:10px;color:#445566;margin-top:6px">POC is estimated from OHLCV candle ranges (not tick-level volume profile).</div>'
)}

{_section("〰️ Elliott Waves",
    f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px">'
    f'{ell_cards or "<div style=\'color:#556677\'>No data</div>"}'
    f'</div>'
    f'<div style="margin-top:8px;font-size:10px;color:#445566">'
    f'W0–W5 = impulse waves · C↑/C↓ = ABC correction · Fib targets shown when in W4. '
    f'Confidence MEDIUM requires ≥4 confirmed swing points.</div>'
)}

{_section("🔷 Harmonic Patterns",
    f'<div style="overflow-x:auto">'
    f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
    f'<thead style="background:#0d1520"><tr>'
    f'<th style="padding:8px;color:#8899aa;text-align:left">TF</th>'
    f'<th>Pattern</th><th>Direction</th><th>Status</th>'
    f'<th>X · A · B · C · D (PRZ)</th><th>Ratios</th>'
    f'</tr></thead>'
    f'<tbody style="color:#e6edf7">{harm_rows}</tbody>'
    f'</table></div>'
    f'<div style="margin-top:8px;font-size:10px;color:#445566">'
    f'PRZ = Potential Reversal Zone (D point) · ACTIVE = D is among last 2 swings · '
    f'Tolerance ±7% on each Fibonacci ratio. Patterns: Gartley / Bat / Butterfly / Crab / Deep Crab / Shark.</div>'
)}

{_section("🧠 v9.1 Readiness",
    f'<div style="font-size:20px;color:{v91_color};font-weight:bold">{v91_status}</div>'
    f'<div style="margin-top:8px;font-size:12px;color:#8899aa">'
    f'Assessment if instrument is ready for v9.1 scanner logic: Wyckoff + SMC alignment.</div>'
)}

{_section("🔍 Final Market Interpretation",
    _card(
        " ".join(interp) +
        "<div style='margin-top:12px;font-size:11px;color:#556677;font-style:italic'>"
        "No trade suggestions. No entry/SL/TP. Objective market description only.</div>",
        border="#00aaff"
    )
)}

<div style="background:#0a0f1a;border-top:1px solid #1e2d40;padding:16px 32px;font-size:11px;color:#556677;margin-top:20px">
  Generated: {report_time} | Data: {data_time} | {symbol} | No trade suggestions — objective description only.
</div>

</body>
</html>"""

# ============================================================
# MAIN
# ============================================================

def main():
    global BACKTEST_TIME_MS

    print("=" * 55)
    print("  Instrument Market Structure Analyzer")
    print("=" * 55)

    # ── Market selection ──
    print("\nRynek:")
    print("  c = Krypto (Bybit)")
    print("  s = Stock  (Polygon)")
    market = input("Wybierz (c/s): ").strip().lower()
    is_stock = (market == "s")

    if is_stock:
        symbol = input("\nPodaj ticker (np. AAPL): ").strip().upper()
        if not symbol:
            print("Brak symbolu. Koniec.")
            return
        fetch_fn = get_candles_stock
    else:
        symbol = input("\nPodaj symbol (np. BTC lub BTCUSDT): ").strip().upper()
        if not symbol:
            print("Brak symbolu. Koniec.")
            return
        if not symbol.endswith("USDT"):
            symbol += "USDT"
            print(f"  → {symbol}")
        fetch_fn = get_candles

    choice = input("Dane teraźniejsze czy historyczne? (t=teraz / h=historyczne): ").strip().lower()
    is_backtest = False
    data_time   = datetime.now().strftime("%Y-%m-%d %H:%M")

    if choice == "h":
        is_backtest = True
        date_str = input("Podaj datę i czas (dd/mm/yyyy hh:mm): ").strip()
        try:
            dt_obj           = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
            BACKTEST_TIME_MS = int(dt_obj.timestamp() * 1000)
            data_time        = dt_obj.strftime("%Y-%m-%d %H:%M")
            print(f"  Tryb backtest: {data_time}")
        except ValueError:
            print("  Nieprawidłowy format — używam teraźniejszych.")
            BACKTEST_TIME_MS = None
            is_backtest      = False

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\nPobieram dane dla {symbol}...")
    data_by_tf = {}
    for tf in TIMEFRAMES:
        print(f"  {TF_LABEL[tf]}...", end=" ", flush=True)
        candles = fetch_fn(symbol, tf, BACKTEST_TIME_MS)
        data_by_tf[tf] = candles
        print(f"{len(candles)} świec")

    if not any(data_by_tf.values()):
        src = "Polygon" if is_stock else "Bybit Linear"
        print(f"\nBrak danych dla {symbol}. Sprawdź czy ticker istnieje w {src}.")
        return

    print("\nGeneruję raport...")
    html = generate_report(symbol, data_by_tf, report_time, data_time, is_backtest)

    ts_str  = datetime.now().strftime("%Y_%m_%d_%H%M")
    if is_backtest and BACKTEST_TIME_MS:
        bt_ts   = datetime.fromtimestamp(BACKTEST_TIME_MS/1000).strftime("%Y_%m_%d_%H%M")
        outfile = os.path.join(OUTPUT_DIR, f"analysis_{symbol}_{bt_ts}_backtest.html")
    else:
        outfile = os.path.join(OUTPUT_DIR, f"analysis_{symbol}_{ts_str}.html")

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Raport: {outfile}")

    abs_path = os.path.abspath(outfile)
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", abs_path])
        print("   Otwieranie w Google Chrome...")
    except FileNotFoundError:
        try:
            subprocess.Popen(["open", abs_path])
        except Exception as e:
            print(f"   Nie udało się otworzyć przeglądarki: {e}")

if __name__ == "__main__":
    main()
