#!/usr/bin/env python3
"""
Strategy v9.1 — shared Wyckoff + SMC logic.
Importowany przez scanner.py i backtest.py. Nie uruchamiaj bezpośrednio.
"""

import os
import requests
import time
import threading

def _load_env():
    """Wczytuje zmienne z .env w katalogu głównym projektu."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

# ============================================================
# KONFIGURACJA
# ============================================================
API_KEY    = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")

TOP_N            = 400
MAX_WORKERS      = 25
RATE_LIMIT_SEM   = threading.Semaphore(25)
TIMEFRAMES       = ["1W", "1D", "4H", "1H", "30m", "15m", "5m", "1m"]
BACKTEST_TIME_MS = None   # ustawiane przez scanner.py / backtest.py

# Target Feasibility Filter — set False to disable TFS gates in classify()
USE_TARGET_FEASIBILITY_FILTER = True

# ============================================================
# POBIERANIE DANYCH
# ============================================================

def _tf_map(tf):
    return {"1W":"W","1D":"D","4H":"240","1H":"60","30m":"30","15m":"15","5m":"5","1m":"1"}.get(tf, tf)

def _get_json(url, params, timeout=15):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        return r.json()
    except Exception:
        return {}

def get_all_bybit_tickers():
    d = _get_json("https://api.bybit.com/v5/market/tickers", {"category": "linear"})
    if d.get("retCode") != 0:
        return []
    return d["result"]["list"]

def get_top_crypto(n=400):
    tickers = get_all_bybit_tickers()
    lst = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        lst.append({
            "symbol":   sym,
            "price":    float(t.get("lastPrice", 0)),
            "volume":   float(t.get("volume24h", 0)),
            "turnover": float(t.get("turnover24h", 0)),
        })
    lst.sort(key=lambda x: x["turnover"], reverse=True)
    top = lst[:n]
    print(f"Wybrano {len(top)} par USDT Futures.")
    return top

def _bybit_candles_raw(symbol, interval, limit=200, end_time=None):
    params = {"category":"linear","symbol":symbol,"interval":_tf_map(interval),"limit":limit}
    if end_time:
        params["end"] = end_time
    for attempt in range(5):
        try:
            with RATE_LIMIT_SEM:
                d = _get_json("https://api.bybit.com/v5/market/kline", params)
            rc = d.get("retCode")
            if rc == 0:
                return d["result"]["list"]
            if rc in (10002, 10006, 10018) or "rate" in d.get("retMsg","").lower():
                time.sleep(2 ** (attempt + 2))
                continue
            return []
        except Exception:
            time.sleep(2 ** attempt)
    return []

def _parse_candles(raw):
    out = [{"time":int(c[0])//1000,"open":float(c[1]),"high":float(c[2]),
             "low":float(c[3]),"close":float(c[4]),"volume":float(c[5])} for c in raw]
    out.reverse()
    return out

def get_400_candles(symbol, interval, end_time_ms=None):
    b1 = _bybit_candles_raw(symbol, interval, 200, end_time_ms)
    if not b1:
        return []
    last_ts = int(b1[-1][0]) - 1
    b2 = _bybit_candles_raw(symbol, interval, 200, last_ts)
    return _parse_candles(b1 + b2)

def fetch_symbol_data(info):
    sym = info["symbol"]
    try:
        tfs = {}
        for tf in TIMEFRAMES:
            tfs[tf] = get_400_candles(sym, tf, BACKTEST_TIME_MS)
        price = info["price"]
        if BACKTEST_TIME_MS and tfs.get("1m"):
            price = tfs["1m"][-1]["close"]
        return sym, {"price": price, "turnover": info["turnover"], "timeframes": tfs}
    except Exception:
        return sym, None

# ============================================================
# TECHNICZNE — POMOCNIKI
# ============================================================

def _ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out = [None] * (period - 1)
    out.append(sum(values[:period]) / period)
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def calc_macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return None, None, None
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    macd = [None if (f is None or s is None) else f - s for f, s in zip(ef, es)]
    valid = [v for v in macd if v is not None]
    if len(valid) < sig:
        return macd, None, None
    sig_line_raw = _ema(valid, sig)
    pad = len([v for v in macd if v is None])
    sig_line = [None] * pad + sig_line_raw
    hist = [None if (m is None or s is None) else m - s for m, s in zip(macd, sig_line)]
    return macd, sig_line, hist

def swing_points(candles, lb=4):
    highs, lows = [], []
    for i in range(lb, len(candles) - lb):
        h = candles[i]["high"]
        l = candles[i]["low"]
        if all(h >= candles[i-j]["high"] and h >= candles[i+j]["high"] for j in range(1, lb+1)):
            highs.append({"idx": i, "price": h})
        if all(l <= candles[i-j]["low"]  and l <= candles[i+j]["low"]  for j in range(1, lb+1)):
            lows.append({"idx": i, "price": l})
    return highs, lows

# ============================================================
# WYCKOFF
# ============================================================

def detect_wyckoff(candles_1h):
    if not candles_1h or len(candles_1h) < 35:
        return None

    ctx  = candles_1h[-45:-3] if len(candles_1h) > 48 else candles_1h[:-3]
    tail = candles_1h[-15:]

    if len(ctx) < 15:
        return None

    highs_sorted = sorted(c["high"] for c in ctx)
    lows_sorted  = sorted(c["low"]  for c in ctx)
    n   = len(highs_sorted)
    rh  = highs_sorted[int(n * 0.80)]
    rl  = lows_sorted[int(n * 0.20)]

    rng = rh - rl
    mid = (rh + rl) / 2
    if rng == 0 or mid == 0:
        return None
    rng_ratio = rng / mid
    if rng_ratio > 0.30 or rng_ratio < 0.002:
        return None

    last_close = candles_1h[-1]["close"]

    sos = any(c["close"] > rh and c["close"] > c["open"] for c in tail)
    sow = any(c["close"] < rl and c["close"] < c["open"] for c in tail)

    if sos == sow:
        return None

    direction = "LONG" if sos else "SHORT"
    pattern   = "Accumulation" if sos else "Distribution"
    event     = "SOS" if sos else "SOW"

    if sos:
        phase_d  = sum(1 for c in tail if c["close"] > rh) >= 1
        pullback = last_close <= rh * 1.025
    else:
        phase_d  = sum(1 for c in tail if c["close"] < rl) >= 1
        pullback = last_close >= rl * 0.975

    return {
        "pattern":      pattern,
        "direction":    direction,
        "event":        event,
        "range_high":   rh,
        "range_low":    rl,
        "range_height": rng,
        "phase_d":      phase_d,
        "pullback":     pullback,
    }

# ============================================================
# FVG
# ============================================================

def detect_fvg(candles, direction, lookback=120):
    src = candles[-lookback:] if len(candles) > lookback else candles
    fvgs = []
    for i in range(2, len(src)):
        if direction == "LONG":
            if src[i]["low"] > src[i-2]["high"]:
                fvgs.append({"fvg_low": src[i-2]["high"], "fvg_high": src[i]["low"]})
        else:
            if src[i]["high"] < src[i-2]["low"]:
                fvgs.append({"fvg_low": src[i]["high"], "fvg_high": src[i-2]["low"]})
    if not fvgs:
        return None
    fvg = fvgs[-1]
    fvg["mid"]  = (fvg["fvg_low"] + fvg["fvg_high"]) / 2
    fvg["size"] = fvg["fvg_high"] - fvg["fvg_low"]
    last_close  = candles[-1]["close"]
    fvg["filled"] = last_close < fvg["fvg_low"] if direction == "LONG" else last_close > fvg["fvg_high"]
    return fvg

# ============================================================
# ORDER BLOCK
# ============================================================

def detect_ob(candles, direction, lookback=150):
    src = candles[-lookback:] if len(candles) > lookback else candles
    ob  = None
    for i in range(1, len(src) - 2):
        c, nx = src[i], src[i+1]
        if c["open"] == 0:
            continue
        if direction == "LONG":
            if (c["close"] < c["open"] and
                    nx["close"] > c["open"] and
                    (nx["close"] - nx["open"]) / c["open"] > 0.001):
                ob = {"ob_low": c["low"], "ob_high": c["high"]}
        else:
            if (c["close"] > c["open"] and
                    nx["close"] < c["open"] and
                    (c["open"] - nx["close"]) / c["open"] > 0.001):
                ob = {"ob_low": c["low"], "ob_high": c["high"]}
    if ob:
        ob["mid"] = (ob["ob_low"] + ob["ob_high"]) / 2
    return ob

# ============================================================
# CHOCH
# ============================================================

def detect_choch(candles_5m, direction):
    src = candles_5m[-100:] if len(candles_5m) > 100 else candles_5m
    if len(src) < 10:
        return None
    for i in range(len(src)-1, max(len(src)-30, 3), -1):
        c = src[i]
        win = src[max(0, i-5):i]
        if not win:
            continue
        if direction == "LONG":
            local_high = max(x["high"] for x in win)
            if c["close"] > local_high:
                return {"confirmed": True, "candles_ago": len(src) - 1 - i, "price": c["close"]}
        else:
            local_low = min(x["low"] for x in win)
            if c["close"] < local_low:
                return {"confirmed": True, "candles_ago": len(src) - 1 - i, "price": c["close"]}
    return {"confirmed": False, "candles_ago": 999}

# ============================================================
# ENGULFING / PIN BAR
# ============================================================

def detect_engulfing(candles_5m, direction, lookback=20):
    src = candles_5m[-lookback:] if len(candles_5m) > lookback else candles_5m
    for i in range(len(src)-1, 0, -1):
        c, p = src[i], src[i-1]
        total  = c["high"] - c["low"]
        body   = abs(c["close"] - c["open"])
        if total == 0:
            continue
        if direction == "LONG":
            if (c["close"] > c["open"] and p["close"] < p["open"] and
                    c["close"] > p["open"] and c["open"] < p["close"]):
                return {"type": "Bullish Engulfing", "candles_ago": len(src)-1-i,
                        "candle": c, "body_ratio": body/total}
        else:
            if (c["close"] < c["open"] and p["close"] > p["open"] and
                    c["close"] < p["open"] and c["open"] > p["close"]):
                return {"type": "Bearish Engulfing", "candles_ago": len(src)-1-i,
                        "candle": c, "body_ratio": body/total}
    return None

def detect_pin_bar(candles_5m, direction, lookback=10):
    src = candles_5m[-lookback:] if len(candles_5m) > lookback else candles_5m
    for i in range(len(src)-1, -1, -1):
        c = src[i]
        total = c["high"] - c["low"]
        if total == 0:
            continue
        body  = abs(c["close"] - c["open"])
        if direction == "LONG":
            low_wick = min(c["open"], c["close"]) - c["low"]
            if low_wick > total * 0.60 and body < total * 0.30:
                return {"type": "Pin Bar Bullish", "candles_ago": len(src)-1-i, "candle": c}
        else:
            hi_wick = c["high"] - max(c["open"], c["close"])
            if hi_wick > total * 0.60 and body < total * 0.30:
                return {"type": "Pin Bar Bearish", "candles_ago": len(src)-1-i, "candle": c}
    return None

# ============================================================
# MACD DIVERGENCE
# ============================================================

def macd_divergence(candles, direction):
    if len(candles) < 60:
        return "none"
    closes = [c["close"] for c in candles]
    _, _, hist = calc_macd(closes)
    if hist is None:
        return "none"
    valid_h = [v for v in hist if v is not None]
    if len(valid_h) < 20:
        return "none"

    price_slice = candles[-30:]
    hist_slice  = valid_h[-30:]
    mid = 15

    if direction == "LONG":
        p1 = min(c["low"]  for c in price_slice[:mid])
        p2 = min(c["low"]  for c in price_slice[mid:])
        h1 = min(hist_slice[:mid])
        h2 = min(hist_slice[mid:])
        if p2 < p1 and h2 > h1:
            return "yes"
        if p2 > p1 and h2 < h1:
            return "against"
    else:
        p1 = max(c["high"] for c in price_slice[:mid])
        p2 = max(c["high"] for c in price_slice[mid:])
        h1 = max(hist_slice[:mid])
        h2 = max(hist_slice[mid:])
        if p2 > p1 and h2 < h1:
            return "yes"
        if p2 < p1 and h2 > h1:
            return "against"
    return "none"

# ============================================================
# MARKET REGIME
# ============================================================

def _trend(candles, lb=20):
    if not candles or len(candles) < lb:
        return "neutral"
    c = [x["close"] for x in candles[-lb:]]
    mid = lb // 2
    f, s = sum(c[:mid])/mid, sum(c[mid:])/mid
    if s > f * 1.01:  return "bullish"
    if s < f * 0.99:  return "bearish"
    return "neutral"

def market_regime(btc_data, eth_data):
    if not btc_data or not eth_data:
        return "unclear"
    trends = [
        _trend(btc_data["timeframes"].get("1H",  [])),
        _trend(btc_data["timeframes"].get("15m", [])),
        _trend(eth_data["timeframes"].get("1H",  [])),
    ]
    b = trends.count("bullish")
    e = trends.count("bearish")
    if b >= 2: return "bullish"
    if e >= 2: return "bearish"
    if b == 1 and e == 1: return "mixed"
    return "chop"

# ============================================================
# TARGET FEASIBILITY HELPERS
# ============================================================

def _compute_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _poc_simple(candles, n_buckets=30):
    """OHLCV-based point of control estimate."""
    if not candles or len(candles) < 5:
        return None
    src  = candles[-100:] if len(candles) > 100 else candles
    pmin = min(c["low"]  for c in src)
    pmax = max(c["high"] for c in src)
    if pmax == pmin:
        return None
    bsize   = (pmax - pmin) / n_buckets
    profile = [0.0] * n_buckets
    for c in src:
        lo_b = max(0, min(n_buckets - 1, int((c["low"]  - pmin) / bsize)))
        hi_b = max(0, min(n_buckets - 1, int((c["high"] - pmin) / bsize)))
        span = hi_b - lo_b + 1
        vpp  = c["volume"] / span
        for b in range(lo_b, hi_b + 1):
            profile[b] += vpp
    poc_b = profile.index(max(profile))
    return pmin + (poc_b + 0.5) * bsize


def compute_target_feasibility(setup, candles_5m):
    """
    Scores how feasible it is that price will reach TP2 (3R).
    Returns dict with target_feasibility_score 0-100 and sub-scores.
    """
    direction = setup["direction"]
    entry     = setup["entry_mid"]
    sl        = setup["sl"]
    risk      = abs(entry - sl)
    if risk == 0:
        return {"target_feasibility_score": 0, "verdict": "NO FUEL",
                "clean_path_score": 0, "liquidity_magnet_score": 0,
                "poc_score": 0, "momentum_score": 0, "atr_capacity_score": 0,
                "nearest_obstacle": None, "nearest_obstacle_r": None,
                "nearest_magnet": None, "poc": None, "poc_position": "unknown",
                "tp2_atr_ratio": None}

    sign = 1 if direction == "LONG" else -1
    tp1  = setup.get("tp1b") or (entry + sign * 2.0 * risk)
    tp2  = setup.get("tp2")  or (entry + sign * 3.0 * risk)

    # ── A) Clean path to target (0–25) ────────────────────────
    src = candles_5m[-100:] if len(candles_5m) > 100 else candles_5m
    sw_h, sw_l = swing_points(src, lb=4)

    obstacles = []
    if direction == "LONG":
        for sh in sw_h:
            p = sh["price"]
            if entry < p < tp2:
                obstacles.append(("swing high", p, (p - entry) / risk))
        rh = setup["wyckoff"].get("range_high")
        if rh and entry < rh < tp2:
            obstacles.append(("range high", rh, (rh - entry) / risk))
    else:
        for sl_p in sw_l:
            p = sl_p["price"]
            if tp2 < p < entry:
                obstacles.append(("swing low", p, (entry - p) / risk))
        rl = setup["wyckoff"].get("range_low")
        if rl and tp2 < rl < entry:
            obstacles.append(("range low", rl, (entry - rl) / risk))

    obstacles.sort(key=lambda x: x[2])
    nearest_obstacle   = obstacles[0][0] if obstacles else None
    nearest_obstacle_r = obstacles[0][2] if obstacles else None

    if not obstacles:
        clean_path_score = 25
    elif obstacles[0][2] < 1.2:
        clean_path_score = 0
    elif obstacles[0][2] < 2.0:
        clean_path_score = 10
    elif obstacles[0][2] < 3.0:
        clean_path_score = 18
    else:
        clean_path_score = 25

    # ── B) Liquidity magnet quality (0–20) ────────────────────
    nearest_magnet       = None
    liquidity_magnet_score = 5  # default: mathematical target only
    tol = 0.015

    if direction == "LONG":
        rh = setup["wyckoff"].get("range_high")
        candidates = [(sh["price"], "swing high") for sh in sw_h if sh["price"] > entry]
        if rh:
            candidates.append((rh, "range high"))
        for p, ltype in candidates:
            dist = abs(p - tp2) / max(tp2, 0.0001)
            if dist <= tol:
                liquidity_magnet_score = 20
                nearest_magnet = f"{ltype} @ {p:.4f}"
                break
            elif p > tp1 and liquidity_magnet_score < 15:
                liquidity_magnet_score = 10
                nearest_magnet = nearest_magnet or f"{ltype} @ {p:.4f}"
    else:
        rl = setup["wyckoff"].get("range_low")
        candidates = [(sl_p["price"], "swing low") for sl_p in sw_l if sl_p["price"] < entry]
        if rl:
            candidates.append((rl, "range low"))
        for p, ltype in sorted(candidates, reverse=True):
            dist = abs(p - tp2) / max(tp2, 0.0001)
            if dist <= tol:
                liquidity_magnet_score = 20
                nearest_magnet = f"{ltype} @ {p:.4f}"
                break
            elif p < tp1 and liquidity_magnet_score < 15:
                liquidity_magnet_score = 10
                nearest_magnet = nearest_magnet or f"{ltype} @ {p:.4f}"

    # ── C) POC alignment (0–20) ───────────────────────────────
    poc         = _poc_simple(candles_5m)
    poc_score   = 12
    poc_position = "unknown"

    if poc:
        if direction == "LONG":
            if poc < sl:
                poc_score, poc_position = 8,  "below SL"
            elif sl <= poc <= entry:
                poc_score, poc_position = 20, "support (SL–entry)"
            elif entry < poc <= tp1:
                poc_score, poc_position = 0,  "obstacle (entry–TP1)"
            elif tp1 < poc <= tp2:
                poc_score, poc_position = 5,  "mild obstacle (TP1–TP2)"
            else:
                poc_score, poc_position = 20, "magnet above TP2"
        else:
            if poc > sl:
                poc_score, poc_position = 8,  "above SL"
            elif entry <= poc <= sl:
                poc_score, poc_position = 20, "resistance (entry–SL)"
            elif tp1 <= poc < entry:
                poc_score, poc_position = 0,  "obstacle (entry–TP1)"
            elif tp2 <= poc < tp1:
                poc_score, poc_position = 5,  "mild obstacle (TP1–TP2)"
            else:
                poc_score, poc_position = 20, "magnet below TP2"

    # ── D) Momentum / displacement fuel (0–20) ────────────────
    pc         = setup.get("pattern", {}).get("candle", {})
    vol_bonus  = setup.get("vol_bonus", False)
    avg_vol    = setup.get("avg_vol_5m", 0)
    momentum_score = 8

    if pc:
        h, l, o, c, v = (pc.get("high",0), pc.get("low",0),
                          pc.get("open",0), pc.get("close",0), pc.get("volume",0))
        candle_range = h - l
        body_size    = abs(c - o)
        body_ratio   = body_size / candle_range if candle_range > 0 else 0

        if direction == "LONG":
            close_pos = (c - l) / candle_range if candle_range > 0 else 0
        else:
            close_pos = (h - c) / candle_range if candle_range > 0 else 0
        strong_close = close_pos >= 0.65

        recent   = candles_5m[-20:] if len(candles_5m) >= 20 else candles_5m
        avg_body = (sum(abs(x["close"] - x["open"]) for x in recent) / len(recent)
                    if recent else 0)
        strong_body   = body_size > avg_body
        strong_volume = vol_bonus or (avg_vol > 0 and v >= avg_vol * 1.2)

        if strong_close and strong_body and strong_volume:
            momentum_score = 20
        elif strong_close and strong_body:
            momentum_score = 15
        elif strong_close or strong_body:
            momentum_score = 12
        # else stays 8

    # ── E) ATR / volatility capacity (0–15) ───────────────────
    atr_5m         = _compute_atr(candles_5m, 14)
    atr_capacity_score = 10
    tp2_atr_ratio  = None

    if atr_5m and atr_5m > 0:
        dist_to_tp2   = abs(tp2 - entry)
        tp2_atr_ratio = dist_to_tp2 / atr_5m
        if tp2_atr_ratio <= 4:
            atr_capacity_score = 15
        elif tp2_atr_ratio <= 7:
            atr_capacity_score = 10
        elif tp2_atr_ratio <= 10:
            atr_capacity_score = 5
        else:
            atr_capacity_score = 0

    # ── Total ─────────────────────────────────────────────────
    tfs = max(0, min(100,
        clean_path_score + liquidity_magnet_score + poc_score +
        momentum_score   + atr_capacity_score
    ))

    if tfs >= 85:   verdict = "CLEAN PATH"
    elif tfs >= 70: verdict = "TARGET POSSIBLE"
    elif tfs >= 55: verdict = "TARGET DIFFICULT"
    elif tfs >= 40: verdict = "TARGET BLOCKED"
    else:           verdict = "NO FUEL"

    return {
        "target_feasibility_score": tfs,
        "clean_path_score":         clean_path_score,
        "liquidity_magnet_score":   liquidity_magnet_score,
        "poc_score":                poc_score,
        "momentum_score":           momentum_score,
        "atr_capacity_score":       atr_capacity_score,
        "nearest_obstacle":         nearest_obstacle,
        "nearest_obstacle_r":       round(nearest_obstacle_r, 2) if nearest_obstacle_r else None,
        "nearest_magnet":           nearest_magnet,
        "poc":                      round(poc, 6) if poc else None,
        "poc_position":             poc_position,
        "tp2_atr_ratio":            round(tp2_atr_ratio, 1) if tp2_atr_ratio else None,
        "verdict":                  verdict,
    }


# ============================================================
# ANALIZA SYMBOLU
# ============================================================

def analyze_symbol(symbol, data, regime):
    candles_1h  = data["timeframes"].get("1H",  [])
    candles_15m = data["timeframes"].get("15m", [])
    candles_5m  = data["timeframes"].get("5m",  [])
    price       = data.get("price", 0)

    if len(candles_5m) < 30:
        return None

    wyckoff = detect_wyckoff(candles_1h)
    if not wyckoff:
        return None
    direction = wyckoff["direction"]

    fvg      = detect_fvg(candles_5m, direction)
    ob       = detect_ob(candles_5m, direction)
    choch    = detect_choch(candles_5m, direction)
    engulf   = detect_engulfing(candles_5m, direction)
    pin      = detect_pin_bar(candles_5m, direction)

    if engulf:
        pattern = engulf
    elif pin:
        pattern = pin
    else:
        return None

    if not choch or not choch.get("confirmed") or choch["candles_ago"] > 6:
        return None

    choch_age = choch["candles_ago"]

    turnover = data.get("turnover", 0)
    if turnover > 0 and turnover < 800_000:
        return None

    rh  = wyckoff["range_high"]
    rl  = wyckoff["range_low"]
    rng = wyckoff["range_height"]

    status    = "no_entry"
    entry_mid = price
    sl        = None

    if fvg and not fvg["filled"]:
        entry_mid = fvg["mid"]
        sl        = fvg["fvg_low"] * 0.990 if direction == "LONG" else fvg["fvg_high"] * 1.010
        status    = "at_fvg"

        if ob:
            ov_lo = max(fvg["fvg_low"], ob["ob_low"])
            ov_hi = min(fvg["fvg_high"], ob["ob_high"])
            if ov_hi > ov_lo:
                status = "at_fvg_and_order_block"

    elif ob:
        entry_mid = ob["mid"]
        sl        = ob["ob_low"] * 0.990 if direction == "LONG" else ob["ob_high"] * 1.010
        status    = "at_order_block"

    else:
        near = rng * 0.15
        if direction == "LONG" and abs(price - rh) <= near:
            status    = "confluence_zone"
            entry_mid = price
            sl        = rl * 0.990
        elif direction == "SHORT" and abs(price - rl) <= near:
            status    = "confluence_zone"
            entry_mid = price
            sl        = rh * 1.010
        else:
            return None

    if sl is None or sl <= 0:
        return None

    if rng > 0:
        entry_ext = (entry_mid - rh) / rng if direction == "LONG" else (rl - entry_mid) / rng
    else:
        entry_ext = 0

    if entry_ext > 0.50:
        return None

    risk = abs(entry_mid - sl)
    if risk <= 0:
        return None

    sign = 1 if direction == "LONG" else -1
    tp1a = entry_mid + sign * 1.1 * risk
    tp1b = entry_mid + sign * 2.0 * risk
    tp2  = entry_mid + sign * 3.0 * risk
    tp3  = entry_mid + sign * 4.0 * risk
    rr   = 3.0

    if rr < 2.0:
        return None

    pc = pattern.get("candle", {})
    touches_fvg = False
    touches_ob  = False
    if fvg and pc:
        touches_fvg = pc.get("low", 0) <= fvg["fvg_high"] and pc.get("high", 0) >= fvg["fvg_low"]
    if ob and pc:
        touches_ob = pc.get("low", 0) <= ob["ob_high"] and pc.get("high", 0) >= ob["ob_low"]

    m5  = macd_divergence(candles_5m, direction)
    m15 = macd_divergence(candles_15m, direction) if candles_15m else "none"

    avg_vol = sum(c["volume"] for c in candles_5m[-20:]) / 20 if len(candles_5m) >= 20 else 0
    vol_bonus = bool(pc and pc.get("volume", 0) > avg_vol * 1.2)

    if direction == "LONG":
        aligned = True if regime == "bullish" else (False if regime == "bearish" else None)
    else:
        aligned = True if regime == "bearish" else (False if regime == "bullish" else None)

    setup = {
        "symbol":        symbol,
        "direction":     direction,
        "wyckoff":       wyckoff,
        "fvg":           fvg,
        "ob":            ob,
        "choch":         choch,
        "choch_age":     choch_age,
        "pattern_type":  pattern["type"],
        "pattern":       pattern,
        "touches_fvg":   touches_fvg,
        "touches_ob":    touches_ob,
        "macd_5m":       m5,
        "macd_15m":      m15,
        "status":        status,
        "entry_mid":     entry_mid,
        "sl":            sl,
        "tp1a":          tp1a,
        "tp1b":          tp1b,
        "tp2":           tp2,
        "tp3":           tp3,
        "rr":            rr,
        "risk":          risk,
        "entry_ext":     entry_ext,
        "regime":        regime,
        "aligned":       aligned,
        "vol_bonus":     vol_bonus,
        "avg_vol_5m":    avg_vol,
        "price":         price,
        "turnover":      turnover,
    }
    setup.update(compute_target_feasibility(setup, candles_5m))
    return setup

# ============================================================
# SCORING
# ============================================================

def total_score(s):
    sc = 0
    w  = s["wyckoff"]
    fvg = s["fvg"]
    ee  = s["entry_ext"]

    sc += min(3 + (4 if w["range_height"]/w["range_high"] > 0.015 else 0)
              + (4 if w["phase_d"] else 0) + (3 if w["pullback"] else 0), 14)

    sc += min((6 if w["phase_d"] else 0) + (6 if w["event"] in ("SOS","SOW") else 0)
              + (4 if w["pullback"] else 0), 16)

    sc += min((6 if w["pullback"] else 0) + (4 if ee <= 0.25 else 0) + 2, 12)

    if fvg:
        sc += min(4 + (4 if not fvg["filled"] else 0) + 3
                  + (3 if "fvg" in s["status"] else 0), 14)

    if s["status"] == "at_fvg_and_order_block":
        sc += 10
    elif s["status"] in ("at_fvg","at_order_block"):
        sc += 6
    if s["touches_fvg"] or s["touches_ob"]:
        sc += 2

    age = s["choch_age"]
    sc += {0:12, 1:12, 2:10, 3:7, 4:4}.get(age, 0)

    patt = 5 if "Engulfing" in s["pattern_type"] else 2
    if s["touches_fvg"] or s["touches_ob"]:
        patt += 3
    if s["vol_bonus"]:
        patt += 2
    sc += min(patt, 10)

    rr_pts  = 5 if s["rr"] >= 3.0 else (3 if s["rr"] >= 2.0 else 0)
    ee_pts  = 5 if ee <= 0.10 else (3 if ee <= 0.25 else (1 if ee <= 0.50 else 0))
    sc += min(rr_pts + ee_pts, 10)

    if s["aligned"] is True:   sc += 4
    elif s["aligned"] is False: sc -= 3

    if s["macd_5m"] == "against":
        return -1
    if s["macd_5m"] == "yes":
        sc += 8

    if s["turnover"] >= 800_000:
        sc += 1

    return min(max(sc, 0), 100)

def manual_pick_score(s):
    if s["macd_5m"] == "against" or s["entry_ext"] > 0.50 or s["choch_age"] > 4:
        return -1
    mps = 0
    ee  = s["entry_ext"]
    st  = s["status"]
    age = s["choch_age"]
    tfs = s.get("target_feasibility_score", 50)

    if s["macd_5m"] == "yes":
        mps += 20 if "fvg" in st else (16 if "order_block" in st else 12)

    mps += 20 if ee <= 0.10 else (15 if ee <= 0.25 else 5)

    mps += {"at_fvg_and_order_block":20,"at_fvg":16,"at_order_block":14,"confluence_zone":5}.get(st, 0)

    mps += {0:15,1:15,2:12,3:8}.get(age, 5 if (age==4 and s["macd_5m"]=="yes") else 0)

    if "Engulfing" in s["pattern_type"]:
        mps += 10 if (s["touches_fvg"] or s["touches_ob"]) else 4

    mps += 10 if s["rr"] >= 3.0 else (7 if s["rr"] >= 2.0 else 5)

    if s["aligned"] is True:   mps += 5
    elif s["aligned"] is False: mps -= 5

    # Target feasibility component (0–20) — only when filter is active
    if USE_TARGET_FEASIBILITY_FILTER:
        if tfs >= 70:   mps += 20
        elif tfs >= 55: mps += 12
        elif tfs >= 40: mps += 5
        # tfs < 55 → cap at 65
        if tfs < 55:
            mps = min(mps, 65)

    return min(max(mps, 0), 100)

def classify(s):
    ts        = s["ts"]
    mps       = s["mps"]
    pattern   = s["pattern_type"]
    macd      = s["macd_5m"]
    choch_age = s["choch_age"]
    entry_ext = s["entry_ext"]
    status    = s["status"]
    fvg       = s.get("fvg")
    rr        = s["rr"]

    is_engulfing = "Engulfing" in pattern
    is_pin_bar   = "Pin Bar"   in pattern

    valid_entry_status = status in {
        "at_fvg", "at_order_block", "at_fvg_and_order_block",
    }
    fvg_filled = bool(fvg and fvg.get("filled", False))

    tfs = s.get("target_feasibility_score", 50)

    # Hard rejects
    if macd == "against":   return "rejected"
    if entry_ext > 0.50:    return "rejected"
    if rr < 2.0:            return "rejected"
    if fvg_filled:          return "rejected"
    if choch_age > 4:       return "rejected"
    if USE_TARGET_FEASIBILITY_FILTER and tfs < 40:
        return "rejected"

    # Below active score threshold
    if USE_TARGET_FEASIBILITY_FILTER:
        if ts < 73 or tfs < 55:
            return "watchlist"
    else:
        if ts < 73:
            return "watchlist"

    # Pin bar: only exceptional cases can be ACTIVE
    if is_pin_bar:
        exceptional = (
            ts >= 85
            and choch_age <= 1
            and macd == "yes"
            and entry_ext <= 0.25
            and status == "at_fvg_and_order_block"
            and rr >= 2.0
        )
        if not exceptional:
            return "watchlist"

    # Engulfing required for all non-pin-bar active setups
    if not is_engulfing and not is_pin_bar:
        return "watchlist"

    # Status rule — confluence_zone only allowed in exceptional case
    if not valid_entry_status:
        exceptional_confluence = (
            status == "confluence_zone"
            and macd == "yes"
            and choch_age <= 2
            and entry_ext <= 0.25
            and rr >= 2.0
        )
        if not exceptional_confluence:
            return "watchlist"

    # Entry extension 0.25–0.50 needs ≥ 3 premium confirmations
    if entry_ext > 0.25:
        pc = 0
        if macd == "yes":                                        pc += 1
        if status in {"at_fvg","at_order_block",
                      "at_fvg_and_order_block"}:                pc += 1
        if choch_age <= 2:                                       pc += 1
        if ts >= 85:                                             pc += 1
        if status == "at_fvg_and_order_block":                  pc += 1
        if rr >= 3.0:                                            pc += 1
        if pc < 3:
            return "watchlist"

    # Premium
    if (
        macd == "yes"
        and entry_ext <= 0.25
        and valid_entry_status
        and choch_age <= 3
        and is_engulfing
        and rr >= 2.0
        and mps >= 75
    ):
        return "premium_setup"

    # High quality
    if (
        entry_ext <= 0.25
        and is_engulfing
        and macd in {"yes", "none"}
        and valid_entry_status
        and choch_age <= 3
        and rr >= 2.0
    ):
        return "high_quality"

    return "secondary_quality"


def recommend_model(s):
    category  = s.get("category", "")
    mps       = s.get("mps", 0)
    macd      = s.get("macd_5m", "none")
    entry_ext = s.get("entry_ext", 1.0)
    choch_age = s.get("choch_age", 999)
    status    = s.get("status", "")
    regime    = s.get("regime", "unclear")

    strong_status     = status in {"at_fvg", "at_order_block", "at_fvg_and_order_block"}
    strong_confluence = status == "at_fvg_and_order_block"

    # Defensive conditions → Model B has priority
    defensive = (
        regime == "chop"
        or choch_age >= 4
        or entry_ext > 0.25
        or category == "secondary_quality"
        or status == "confluence_zone"
        or mps < 75
        or (macd != "yes" and not strong_confluence)
    )

    if defensive:
        return "Model B"

    if category == "premium_setup":
        return "Model A"

    if category == "high_quality" and mps >= 75:
        return "Model A"

    if macd == "yes" and entry_ext <= 0.25 and strong_status:
        return "Model A"

    return "Model B"
