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

    return {
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

    return min(max(mps, 0), 100)

def classify(s):
    ts  = s["ts"]
    mps = s["mps"]
    if ts < 0 or mps < 0 or s["macd_5m"] == "against" or s["entry_ext"] > 0.50:
        return "rejected"
    if s["choch_age"] > 6 or s["rr"] < 2.0:
        return "rejected"

    st  = s["status"]
    ee  = s["entry_ext"]
    age = s["choch_age"]
    m   = s["macd_5m"]

    if age > 4:
        return "watchlist"

    if "Pin" in s["pattern_type"]:
        if ts >= 75 and age <= 2 and m == "yes" and ee <= 0.35:
            return "secondary_quality"
        return "watchlist"

    if ts < 65:
        return "watchlist"

    if st == "confluence_zone":
        if not ((m == "yes" or ee <= 0.25) and age <= 3):
            return "watchlist"

    if (m == "yes" and ee <= 0.25 and
            st in ("at_fvg","at_order_block","at_fvg_and_order_block") and
            age <= 3 and "Engulfing" in s["pattern_type"] and
            (s["touches_fvg"] or s["touches_ob"])):
        return "premium_setup"

    if (ee <= 0.35 and "Engulfing" in s["pattern_type"] and m in ("yes","none") and
            st in ("at_fvg","at_order_block","at_fvg_and_order_block") and age <= 4):
        return "high_quality"

    return "secondary_quality"

def recommend_model(s):
    cat = s.get("category", "")
    if cat == "premium_setup":
        return "Model A"
    if s["mps"] >= 75 and s["macd_5m"] == "yes" and s["entry_ext"] <= 0.25:
        return "Model A"
    if s["status"] in ("at_fvg","at_order_block","at_fvg_and_order_block") and s["macd_5m"] == "yes":
        return "Model A"
    return "Model B"
