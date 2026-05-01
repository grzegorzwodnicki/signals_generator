#!/usr/bin/env python3
"""
Crypto Wyckoff + SMC Signal Generator v9.1
Pobiera top 400 kryptowalut z Bybit, analizuje setupy tradingowe
i generuje pełny raport HTML.
"""

import os
import requests
import time
import threading
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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

OUTPUT_DIR          = "output"
TOP_N               = 400
MAX_WORKERS         = 25
RATE_LIMIT_SEM      = threading.Semaphore(25)
TIMEFRAMES          = ["1W", "1D", "4H", "1H", "30m", "15m", "5m", "1m"]
BACKTEST_TIME_MS    = None   # ustawiane w main()

os.makedirs(OUTPUT_DIR, exist_ok=True)

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

    # Krótszy, bardziej aktualny kontekst: ostatnie 45 świec (z wyłączeniem 3 ostatnich)
    ctx  = candles_1h[-45:-3] if len(candles_1h) > 48 else candles_1h[:-3]
    tail = candles_1h[-15:]   # najnowsze 15 świec do wykrycia SOS/SOW

    if len(ctx) < 15:
        return None

    # Percentyl 80/20 zamiast absolutnego max/min — odporny na spike'i i trendy
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

    # SOS — zamknięcie powyżej rh (bullish candle)
    sos = any(c["close"] > rh and c["close"] > c["open"] for c in tail)
    # SOW — zamknięcie poniżej rl (bearish candle)
    sow = any(c["close"] < rl and c["close"] < c["open"] for c in tail)

    if sos == sow:      # oba lub żaden — niejednoznaczne
        return None

    direction = "LONG" if sos else "SHORT"
    pattern   = "Accumulation" if sos else "Distribution"
    event     = "SOS" if sos else "SOW"

    # Phase D: minimum 1 świeca akceptacji poza range
    if sos:
        phase_d  = sum(1 for c in tail if c["close"] > rh) >= 1
        pullback = last_close <= rh * 1.025   # luzniejszy pullback
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
            # Ostatnia bearish świeca przed bullish displacement
            if (c["close"] < c["open"] and
                    nx["close"] > c["open"] and           # zamknięcie powyżej open poprzedniej (luźniejsze)
                    (nx["close"] - nx["open"]) / c["open"] > 0.001):
                ob = {"ob_low": c["low"], "ob_high": c["high"]}
        else:
            # Ostatnia bullish świeca przed bearish displacement
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
    # Szukaj dalej wstecz (30 świec) i z mniejszym oknem lokalnym (5 świec)
    for i in range(len(src)-1, max(len(src)-30, 3), -1):
        c = src[i]
        win = src[max(0, i-5):i]   # okno 5 świec (było 10) — łatwiej przebić lokalne high/low
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

    # Porównaj dwie połówki z ostatnich 30 świec cenowych i histogramu
    price_slice = candles[-30:]
    hist_slice  = valid_h[-30:]
    mid = 15

    if direction == "LONG":
        p1 = min(c["low"]  for c in price_slice[:mid])
        p2 = min(c["low"]  for c in price_slice[mid:])
        h1 = min(hist_slice[:mid])
        h2 = min(hist_slice[mid:])
        if p2 < p1 and h2 > h1:
            return "yes"        # bullish divergence
        if p2 > p1 and h2 < h1:
            return "against"
    else:
        p1 = max(c["high"] for c in price_slice[:mid])
        p2 = max(c["high"] for c in price_slice[mid:])
        h1 = max(hist_slice[:mid])
        h2 = max(hist_slice[mid:])
        if p2 > p1 and h2 < h1:
            return "yes"        # bearish divergence
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

    # SMC
    fvg      = detect_fvg(candles_5m, direction)
    ob       = detect_ob(candles_5m, direction)
    choch    = detect_choch(candles_5m, direction)
    engulf   = detect_engulfing(candles_5m, direction)
    pin      = detect_pin_bar(candles_5m, direction)

    # Pattern
    if engulf:
        pattern = engulf
    elif pin:
        pattern = pin
    else:
        return None

    # ChoCH wymagany (max 6 świec — 5-6 trafi do watchlist w classify)
    if not choch or not choch.get("confirmed") or choch["candles_ago"] > 6:
        return None

    choch_age = choch["candles_ago"]

    # Liquidity (prefilter)
    turnover = data.get("turnover", 0)
    if turnover > 0 and turnover < 800_000:
        return None

    # Status i entry/SL
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
            # overlap FVG+OB?
            ov_lo = max(fvg["fvg_low"], ob["ob_low"])
            ov_hi = min(fvg["fvg_high"], ob["ob_high"])
            if ov_hi > ov_lo:
                status = "at_fvg_and_order_block"

    elif ob:
        entry_mid = ob["mid"]
        sl        = ob["ob_low"] * 0.990 if direction == "LONG" else ob["ob_high"] * 1.010
        status    = "at_order_block"

    else:
        # Confluence zone: cena w pobliżu range high/low — promień 15% range height
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

    # Entry extension
    if rng > 0:
        entry_ext = (entry_mid - rh) / rng if direction == "LONG" else (rl - entry_mid) / rng
    else:
        entry_ext = 0

    if entry_ext > 0.50:
        return None

    # Targets
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

    # Pattern touches FVG/OB?
    pc = pattern.get("candle", {})
    touches_fvg = False
    touches_ob  = False
    if fvg and pc:
        touches_fvg = pc.get("low", 0) <= fvg["fvg_high"] and pc.get("high", 0) >= fvg["fvg_low"]
    if ob and pc:
        touches_ob = pc.get("low", 0) <= ob["ob_high"] and pc.get("high", 0) >= ob["ob_low"]

    # MACD
    m5  = macd_divergence(candles_5m, direction)
    m15 = macd_divergence(candles_15m, direction) if candles_15m else "none"

    # Volume bonus
    avg_vol = sum(c["volume"] for c in candles_5m[-20:]) / 20 if len(candles_5m) >= 20 else 0
    vol_bonus = bool(pc and pc.get("volume", 0) > avg_vol * 1.2)

    # Regime alignment
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

    # 1. Wyckoff range quality (0-14)
    sc += min(3 + (4 if w["range_height"]/w["range_high"] > 0.015 else 0)
              + (4 if w["phase_d"] else 0) + (3 if w["pullback"] else 0), 14)

    # 2. Phase D (0-16)
    sc += min((6 if w["phase_d"] else 0) + (6 if w["event"] in ("SOS","SOW") else 0)
              + (4 if w["pullback"] else 0), 16)

    # 3. Pullback quality (0-12)
    sc += min((6 if w["pullback"] else 0) + (4 if ee <= 0.25 else 0) + 2, 12)

    # 4. FVG quality (0-14)
    if fvg:
        sc += min(4 + (4 if not fvg["filled"] else 0) + 3
                  + (3 if "fvg" in s["status"] else 0), 14)

    # 5. OB/FVG confluence (0-12)
    if s["status"] == "at_fvg_and_order_block":
        sc += 10
    elif s["status"] in ("at_fvg","at_order_block"):
        sc += 6
    if s["touches_fvg"] or s["touches_ob"]:
        sc += 2

    # 6. ChoCH freshness (0-12)
    age = s["choch_age"]
    sc += {0:12, 1:12, 2:10, 3:7, 4:4}.get(age, 0)

    # 7. Pattern candle (0-10)
    patt = 5 if "Engulfing" in s["pattern_type"] else 2
    if s["touches_fvg"] or s["touches_ob"]:
        patt += 3
    if s["vol_bonus"]:
        patt += 2
    sc += min(patt, 10)

    # 8. R:R & location (0-10)
    rr_pts  = 5 if s["rr"] >= 3.0 else (3 if s["rr"] >= 2.0 else 0)
    ee_pts  = 5 if ee <= 0.10 else (3 if ee <= 0.25 else (1 if ee <= 0.50 else 0))
    sc += min(rr_pts + ee_pts, 10)

    # 9. Market regime (-5 to +6)
    if s["aligned"] is True:   sc += 4
    elif s["aligned"] is False: sc -= 3

    # 10. MACD divergence
    if s["macd_5m"] == "against":
        return -1      # hard reject
    if s["macd_5m"] == "yes":
        sc += 8

    # 11. Liquidity note (+1)
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

    # MACD (0-20)
    if s["macd_5m"] == "yes":
        mps += 20 if "fvg" in st else (16 if "order_block" in st else 12)

    # Entry quality (0-20)
    mps += 20 if ee <= 0.10 else (15 if ee <= 0.25 else 5)

    # Location (0-20)
    mps += {"at_fvg_and_order_block":20,"at_fvg":16,"at_order_block":14,"confluence_zone":5}.get(st, 0)

    # ChoCH (0-15)
    mps += {0:15,1:15,2:12,3:8}.get(age, 5 if (age==4 and s["macd_5m"]=="yes") else 0)

    # Pattern (0-10)
    if "Engulfing" in s["pattern_type"]:
        mps += 10 if (s["touches_fvg"] or s["touches_ob"]) else 4

    # R:R (0-10)
    mps += 10 if s["rr"] >= 3.0 else (7 if s["rr"] >= 2.0 else 5)

    # Regime (-5 to +5)
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

    # Stare ChoCH (5-6c) → zawsze watchlist
    if age > 4:
        return "watchlist"

    if "Pin" in s["pattern_type"]:
        if ts >= 75 and age <= 2 and m == "yes" and ee <= 0.35:
            return "secondary_quality"
        return "watchlist"

    # Obniżony próg z 73 → 65
    if ts < 65:
        return "watchlist"

    # Confluence zone — złagodzone zasady
    if st == "confluence_zone":
        # Wystarczy: (MACD yes LUB entry_ext <= 0.25) AND ChoCH <= 3
        if not ((m == "yes" or ee <= 0.25) and age <= 3):
            return "watchlist"

    # Premium
    if (m == "yes" and ee <= 0.25 and
            st in ("at_fvg","at_order_block","at_fvg_and_order_block") and
            age <= 3 and "Engulfing" in s["pattern_type"] and
            (s["touches_fvg"] or s["touches_ob"])):
        return "premium_setup"

    # High quality
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

# ============================================================
# FORMATOWANIE HTML
# ============================================================

def _f(v, price=0):
    """Format price value adaptively."""
    if v is None:
        return "N/A"
    if price >= 1000 or v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"

def _pct(v):
    return f"{v*100:.2f}%" if v is not None else "N/A"

def _badge(text, color, text_color="#000"):
    return f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:{color};color:{text_color};font-size:11px;font-weight:bold;margin:1px">{text}</span>'

def _status_badge(st):
    m = {"at_fvg_and_order_block":("#00ff8c","FVG+OB"),
         "at_fvg":("#00e5ff","AT FVG"),
         "at_order_block":("#00ffcc","AT OB"),
         "confluence_zone":("#ffd700","CONFLUENCE")}
    col, lbl = m.get(st, ("#888", st.upper().replace("_"," ")))
    return _badge(lbl, col)

def _cat_badge(cat):
    m = {"premium_setup":("#00ff8c","PREMIUM"),
         "high_quality":("#00ffcc","HIGH QUALITY"),
         "secondary_quality":("#88ccff","SECONDARY"),
         "watchlist":("#4488ff","WATCHLIST","#fff"),
         "rejected":("#ff4d4d","REJECTED","#fff")}
    v = m.get(cat, ("#888", cat.upper()))
    if len(v) == 3:
        return _badge(v[1], v[0], v[2])
    return _badge(v[1], v[0])

def _macd_badge(m):
    if m == "yes":    return _badge("MACD ✓", "#00ff8c")
    if m == "against":return _badge("MACD ✗", "#ff4d4d", "#fff")
    return _badge("MACD –", "#334")

def _dir_span(d):
    color = "#00ff8c" if d == "LONG" else "#ff4d4d"
    return f'<span style="color:{color};font-weight:bold">{d}</span>'

def _row(label, val, color="#e6edf7"):
    return f'<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1a2535"><span style="color:#8899aa">{label}</span><span style="color:{color};font-weight:500">{val}</span></div>'

def _panel(title, content):
    return f'<div style="background:#0d1520;border-radius:6px;padding:12px;margin-top:10px"><div style="color:#00aaff;font-size:13px;font-weight:bold;margin-bottom:8px">{title}</div>{content}</div>'

# ---- Card aktywnego setupu ----
def render_card(s):
    sym  = s["symbol"]
    d    = s["direction"]
    cat  = s.get("category","")
    w    = s["wyckoff"]
    fvg  = s["fvg"]
    ob   = s["ob"]
    mps  = s["mps"]
    ts   = s["ts"]
    ee   = s["entry_ext"]
    age  = s["choch_age"]
    pr   = s["price"]
    model= s.get("model","Model B")

    ee_cls = "ideal+" if ee <= 0.10 else ("ideal" if ee <= 0.25 else ("acceptable" if ee <= 0.50 else "chased"))

    # OB/FVG overlap
    ob_fvg_pct = 0
    if fvg and ob:
        ov = min(fvg["fvg_high"], ob["ob_high"]) - max(fvg["fvg_low"], ob["ob_low"])
        if fvg["size"] > 0:
            ob_fvg_pct = max(ov, 0) / fvg["size"] * 100

    # MPS breakdown
    def mps_row(label, val, mx):
        col = "#00ff8c" if val > 0 else ("#ff4d4d" if val < 0 else "#556677")
        bar = f'<div style="background:#1e2d40;border-radius:3px;height:5px;margin-top:2px"><div style="background:{col};width:{max(val,0)/mx*100:.0f}%;height:5px;border-radius:3px"></div></div>'
        return f'<div style="display:flex;justify-content:space-between;padding:2px 0"><span style="color:#8899aa;font-size:12px">{label}</span><span style="color:{col};font-size:12px">{val}/{mx}</span></div>{bar}'

    mps_html = (
        mps_row("MACD confirmation", 20 if s["macd_5m"]=="yes" else 0, 20) +
        mps_row("Entry quality", 20 if ee<=0.10 else (15 if ee<=0.25 else 5), 20) +
        mps_row("Location", {"at_fvg_and_order_block":20,"at_fvg":16,"at_order_block":14,"confluence_zone":5}.get(s["status"],0), 20) +
        mps_row("ChoCH quality", {0:15,1:15,2:12,3:8,4:5}.get(age,0), 15) +
        mps_row("Pattern quality", 10 if (s["touches_fvg"] or s["touches_ob"]) and "Engulfing" in s["pattern_type"] else 4, 10) +
        mps_row("R:R quality", 10 if s["rr"]>=3.0 else 7, 10) +
        mps_row("Regime", 5 if s["aligned"] else (-5 if s["aligned"] is False else 0), 5)
    )

    # Invalidation
    if d == "LONG":
        invalid_price = _f(fvg["fvg_low"], pr) if fvg else _f(w["range_low"], pr)
        invalid_txt   = f"Close 5M below {invalid_price} (FVG zapełnione / powrót do range)"
    else:
        invalid_price = _f(fvg["fvg_high"], pr) if fvg else _f(w["range_high"], pr)
        invalid_txt   = f"Close 5M above {invalid_price} (FVG zapełnione / powrót do range)"

    border = "#00ff8c" if cat == "premium_setup" else "#1e2d40"
    glow   = "0 0 16px rgba(0,255,140,0.3)" if cat == "premium_setup" else "none"

    return f"""
<div style="background:#101827;border-radius:8px;padding:20px;border:1px solid {border};box-shadow:{glow};margin-bottom:16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
    <div>
      <span style="font-size:20px;font-weight:bold;margin-right:8px">{sym}</span>
      {_dir_span(d)} {_cat_badge(cat)} {_status_badge(s["status"])} {_macd_badge(s["macd_5m"])}
    </div>
    <div style="text-align:right">
      <div style="font-size:26px;font-weight:bold;color:#00ff8c">{mps}</div>
      <div style="font-size:11px;color:#8899aa">MPS | Score: {ts}</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px">
    <div>
      {_panel("📐 Wyckoff",
        _row("Pattern", w["pattern"]) +
        _row("Phase D", "✅" if w["phase_d"] else "⏳") +
        _row("Event", w["event"]) +
        _row("Range High", _f(w["range_high"], pr)) +
        _row("Range Low",  _f(w["range_low"],  pr)) +
        _row("Pullback",   "✅" if w["pullback"] else "❌")
      )}
      {_panel("🕯 ChoCH / Pattern",
        _row("ChoCH 5M", "✅ Confirmed") +
        _row("Age", f'{age}c {"🔥" if age<=1 else ("✅" if age<=2 else "⏱")}') +
        _row("Pattern", s["pattern_type"]) +
        _row("Touches FVG", "✅" if s["touches_fvg"] else "❌") +
        _row("Touches OB",  "✅" if s["touches_ob"]  else "❌") +
        _row("Vol Bonus",   "✅" if s["vol_bonus"]   else "–")
      )}
    </div>
    <div>
      {_panel(f"💰 Trade Plan ({model})",
        _row("Entry",      _f(s["entry_mid"], pr)) +
        _row("Stop Loss",  _f(s["sl"], pr), "#ff4d4d") +
        _row("TP1a (1.1R)",_f(s["tp1a"], pr)) +
        _row("TP1b (2.0R)",_f(s["tp1b"], pr)) +
        _row("TP2 / Runner",_f(s["tp2"], pr), "#00ff8c") +
        _row("R:R",        f'{s["rr"]:.1f}R', "#00ff8c") +
        _row("Risk %",     _pct(s["risk"]/s["entry_mid"])) +
        _row("Entry Ext",  f'{_pct(ee)} ({ee_cls})') +
        _row("FVG",        f'{_f(fvg["fvg_low"],pr)} – {_f(fvg["fvg_high"],pr)}' if fvg else "N/A") +
        _row("OB/FVG Overlap", f'{ob_fvg_pct:.1f}%')
      )}
    </div>
    <div>
      {_panel("📊 Manual Pick Score", mps_html +
        f'<div style="display:flex;justify-content:space-between;margin-top:8px"><span style="font-weight:bold">TOTAL</span><span style="color:#00ff8c;font-size:18px">{mps}/100</span></div>'
      )}
      {_panel("MACD / Regime",
        _row("MACD 5M",  s["macd_5m"],  "#00ff8c" if s["macd_5m"]=="yes" else ("#ff4d4d" if s["macd_5m"]=="against" else "#8899aa")) +
        _row("MACD 15M", s["macd_15m"], "#00ff8c" if s["macd_15m"]=="yes" else "#8899aa") +
        _row("Regime",   s["regime"]) +
        _row("Aligned",  "✅" if s["aligned"] else ("❌" if s["aligned"] is False else "–"))
      )}
    </div>
  </div>
  <div style="background:#0d1520;border-radius:6px;padding:12px;margin-top:10px">
    <div style="color:#00aaff;font-size:13px;font-weight:bold;margin-bottom:8px">⚡ Trigger Checklist v9.1</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px;color:#aabbcc">
      <span>□ Cena w FVG lub OB przy LPS/LPSY</span>
      <span>□ ChoCH na 5M potwierdzony i świeży</span>
      <span>□ Engulfing zamknięty na 5M</span>
      <span>□ MACD divergence nie jest przeciwko setupowi</span>
      <span>□ Entry extension idealnie ≤ 0.25</span>
      <span>□ FVG nie jest zapełnione</span>
      <span>□ R:R do TP2 ≥ 2.0R</span>
      <span>□ Liquidity OK</span>
    </div>
    <div style="margin-top:8px;color:#ffcc00;font-size:12px"><strong>Invalidation:</strong> {invalid_txt}</div>
  </div>
</div>"""

# ---- Top Pick card ----
def render_top_pick(s, rank):
    sym  = s["symbol"]
    mps  = s["mps"]
    ts   = s["ts"]
    cat  = s.get("category","")
    model= s.get("model","Model B")
    pr   = s["price"]

    reasons = []
    if s["macd_5m"] == "yes":     reasons.append("MACD divergence confirmed")
    if s["status"] == "at_fvg_and_order_block": reasons.append("FVG + OB confluence")
    elif s["status"] == "at_fvg": reasons.append("Price at FVG")
    if s["choch_age"] <= 1:       reasons.append("🔥 Fresh ChoCH (≤1c)")
    elif s["choch_age"] <= 2:     reasons.append("Fresh ChoCH (2c)")
    if s["entry_ext"] <= 0.10:    reasons.append("Ideal+ entry extension")
    if s["aligned"]:              reasons.append("Regime aligned")

    risks = []
    if s["macd_5m"] == "none":    risks.append("No MACD confirmation")
    if s["choch_age"] >= 3:       risks.append(f"ChoCH {s['choch_age']}c old")
    if s["entry_ext"] > 0.25:     risks.append(f"Entry extension {_pct(s['entry_ext'])}")
    if s["status"] == "confluence_zone": risks.append("Confluence zone only")
    if not (s["touches_fvg"] or s["touches_ob"]): risks.append("Pattern doesn't touch FVG/OB")
    if not risks: risks.append("Standard execution risk")

    return f"""
<div style="background:linear-gradient(135deg,#001a0d,#002818);border:2px solid #00ff8c;border-radius:10px;padding:20px;margin-bottom:16px;box-shadow:0 0 24px rgba(0,255,140,0.35)">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:14px">
    <div>
      <span style="font-size:13px;color:#00ff8c;font-weight:bold">#{rank} TOP PICK</span>
      <span style="font-size:22px;font-weight:bold;margin:0 8px">{sym}</span>
      {_dir_span(s["direction"])} {_cat_badge(cat)} {_status_badge(s["status"])} {_macd_badge(s["macd_5m"])}
    </div>
    <div style="text-align:right">
      <div style="font-size:32px;font-weight:bold;color:#00ff8c">{mps}</div>
      <div style="font-size:11px;color:#8899aa">Manual Pick Score | Total: {ts}</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px">
    {_panel(f"💰 Trade Plan ({model})",
      _row("Entry",       _f(s["entry_mid"],pr)) +
      _row("Stop Loss",   _f(s["sl"],pr), "#ff4d4d") +
      _row("TP1a (1.1R)", _f(s["tp1a"],pr)) +
      _row("TP1b (2.0R)", _f(s["tp1b"],pr)) +
      _row("TP2/Runner",  _f(s["tp2"],pr), "#00ff8c") +
      _row("R:R",         f'{s["rr"]:.1f}R', "#00ff8c") +
      _row("Risk %",      _pct(s["risk"]/s["entry_mid"]))
    )}
    {_panel("📐 Setup",
      _row("Wyckoff",     s["wyckoff"]["pattern"]) +
      _row("ChoCH Age",   f'{s["choch_age"]}c') +
      _row("Entry Ext",   _pct(s["entry_ext"])) +
      _row("Pattern",     s["pattern_type"]) +
      _row("MACD 5M",     s["macd_5m"]) +
      _row("Model",       model)
    )}
    {_panel("✅ Why TOP PICK",
      "".join(f'<div style="color:#00ff8c;padding:2px 0;font-size:13px">✓ {r}</div>' for r in reasons) +
      "<div style='margin-top:8px;font-size:12px;color:#8899aa;font-weight:bold'>Main Risk</div>" +
      "".join(f'<div style="color:#ffcc00;padding:1px 0;font-size:12px">⚠ {r}</div>' for r in risks)
    )}
  </div>
</div>"""

# ============================================================
# TABELA RANKINGOWA
# ============================================================

def render_table(setups):
    rows = ""
    for i, s in enumerate(setups, 1):
        d  = s["direction"]
        dc = "#00ff8c" if d == "LONG" else "#ff4d4d"
        mc = "#00ff8c" if s["macd_5m"]=="yes" else ("#ff4d4d" if s["macd_5m"]=="against" else "#888")
        pr = s["price"]
        rows += f"""<tr>
          <td>{i}</td>
          <td style="font-weight:bold">{s["symbol"]}</td>
          <td style="color:{dc}">{d}</td>
          <td>{s["wyckoff"]["pattern"]}</td>
          <td>{s.get("category","").replace("_"," ").title()}</td>
          <td style="color:#00ff8c;font-weight:bold">{s["mps"]}</td>
          <td>{s["ts"]}</td>
          <td>{s["status"].replace("_"," ")}</td>
          <td>{s["pattern_type"]}</td>
          <td>{s["choch_age"]}c</td>
          <td>{_pct(s["entry_ext"])}</td>
          <td>{"✅" if s["fvg"] and not s["fvg"]["filled"] else "❌"}</td>
          <td>{"✅" if s["status"]=="at_fvg_and_order_block" else "–"}</td>
          <td>{_f(s["entry_mid"],pr)}</td>
          <td>{_f(s["sl"],pr)}</td>
          <td>{_f(s["tp1a"],pr)}</td>
          <td>{_f(s["tp1b"],pr)}</td>
          <td>{_f(s["tp2"],pr)}</td>
          <td>{s["rr"]:.1f}R</td>
          <td style="color:{mc}">{s["macd_5m"]}</td>
          <td>{s.get("model","B")}</td>
        </tr>"""
    return f"""<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead style="background:#0d1520;color:#8899aa">
        <tr><th style="padding:8px 6px;text-align:left">#</th>
        <th>Symbol</th><th>Dir</th><th>Wyckoff</th><th>Category</th>
        <th>MPS</th><th>Score</th><th>Status</th><th>Pattern</th><th>ChoCH</th>
        <th>EntExt</th><th>FVG</th><th>FVG+OB</th>
        <th>Entry</th><th>SL</th><th>TP1a</th><th>TP1b</th><th>TP2</th>
        <th>R:R</th><th>MACD</th><th>Model</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table></div>"""

# ============================================================
# GENEROWANIE RAPORTU HTML
# ============================================================

def generate_html(all_setups, meta):
    premium  = [s for s in all_setups if s.get("category") == "premium_setup"]
    hq       = [s for s in all_setups if s.get("category") == "high_quality"]
    sec      = [s for s in all_setups if s.get("category") == "secondary_quality"]
    watchlist= [s for s in all_setups if s.get("category") == "watchlist"]
    rejected = [s for s in all_setups if s.get("category") == "rejected"]

    active = sorted(premium + hq + sec, key=lambda s: s["mps"], reverse=True)[:15]
    top_picks = [s for s in active if s["mps"] >= 70][:3]

    regime = meta.get("regime", "unclear")
    rc     = {"bullish":"#00ff8c","bearish":"#ff4d4d","mixed":"#ffd700","chop":"#88ccff"}.get(regime, "#888")
    rt_col = "#fff" if regime in ("bearish","watchlist") else "#000"

    stats_html = ""
    stats_data = [
        ("Top Picks",       len(top_picks), "#00ff8c"),
        ("Premium",         len(premium),   "#00ff8c"),
        ("High Quality",    len(hq),        "#00ffcc"),
        ("Secondary",       len(sec),       "#88ccff"),
        ("Watchlist",       len(watchlist), "#4488ff"),
        ("Rejected",        len(rejected),  "#ff4d4d"),
        ("MACD Yes",        sum(1 for s in active if s["macd_5m"]=="yes"), "#00ff8c"),
        ("ChoCH ≤ 2c",      sum(1 for s in active if s["choch_age"]<=2),  "#00ffcc"),
        ("EntExt ≤ 0.25",   sum(1 for s in active if s["entry_ext"]<=0.25),"#88ccff"),
        ("At FVG",          sum(1 for s in active if "fvg" in s.get("status","")), "#00e5ff"),
        ("At OB",           sum(1 for s in active if s.get("status")=="at_order_block"), "#00ffcc"),
        ("At FVG+OB",       sum(1 for s in active if s.get("status")=="at_fvg_and_order_block"), "#00ff8c"),
        ("Avg R:R",         f'{sum(s["rr"] for s in active)/len(active):.2f}R' if active else "N/A", "#e6edf7"),
        ("Avg MPS",         f'{sum(s["mps"] for s in active)/len(active):.0f}' if active else "N/A", "#00ff8c"),
        ("Accumulation",    sum(1 for s in active if "Accum" in s["wyckoff"]["pattern"]), "#00ffcc"),
        ("Distribution",    sum(1 for s in active if "Distrib" in s["wyckoff"]["pattern"]), "#ff8844"),
    ]
    for label, val, col in stats_data:
        stats_html += f'<div style="background:#101827;border:1px solid #1e2d40;border-radius:6px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:bold;color:{col}">{val}</div><div style="font-size:11px;color:#8899aa;margin-top:4px">{label}</div></div>'

    if top_picks:
        picks_html = '<h3 style="color:#00ff8c;margin-bottom:12px">🏆 TOP 1 SETUP OF THE DAY</h3>' + render_top_pick(top_picks[0], 1)
        if len(top_picks) > 1:
            picks_html += '<h3 style="color:#00ff8c;margin-bottom:12px;margin-top:20px">TOP 3 MANUAL PICKS</h3>'
            for i, p in enumerate(top_picks[1:], 2):
                picks_html += render_top_pick(p, i)
    else:
        picks_html = '<div style="text-align:center;padding:40px;color:#556677;font-size:18px">⚠ No high-confidence manual trade today.</div>'

    cards_html   = "".join(render_card(s) for s in active)
    watchlist_rows = "".join(
        f'<tr><td style="font-weight:bold">{s["symbol"]}</td><td style="color:{"#00ff8c" if s["direction"]=="LONG" else "#ff4d4d"}">{s["direction"]}</td>'
        f'<td>{s["wyckoff"]["pattern"]}</td><td>{s["ts"]}</td>'
        f'<td style="color:#8899aa;font-size:12px">'
        + (", ".join(filter(None, [
            "MACD unconfirmed" if s["macd_5m"]!="yes" else "",
            f'ChoCH {s["choch_age"]}c' if s["choch_age"]>2 else "",
            f'EntExt {_pct(s["entry_ext"])}' if s["entry_ext"]>0.25 else "",
            "Confluence zone only" if s["status"]=="confluence_zone" else "",
        ])) or "General weakness") +
        f'</td></tr>'
        for s in watchlist[:20]
    )
    reject_rows = "".join(
        f'<tr><td>{s["symbol"]}</td><td style="color:{"#00ff8c" if s["direction"]=="LONG" else "#ff4d4d"}">{s["direction"]}</td>'
        f'<td style="color:#8899aa;font-size:12px">'
        + (", ".join(filter(None, [
            "MACD against" if s["macd_5m"]=="against" else "",
            f'Score {s["ts"]}' if s["ts"] < 73 else "",
            "Chased entry" if s["entry_ext"] > 0.50 else "",
            f'ChoCH {s["choch_age"]}c' if s["choch_age"]>4 else "",
            f'R:R {s["rr"]:.2f}' if s["rr"]<2.0 else "",
            "Pin bar" if "Pin" in s["pattern_type"] else "",
        ])) or "Multiple factors") +
        f'</td></tr>'
        for s in rejected[:20]
    )

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#070b12;color:#e6edf7;font-family:'Segoe UI',Arial,sans-serif;font-size:14px}
h1,h2,h3{color:#e6edf7}
th{padding:8px 6px;text-align:left;color:#8899aa;border-bottom:1px solid #1e2d40}
td{padding:7px 6px;border-bottom:1px solid #1a2535}
tr:hover td{background:#0d1520}
"""

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Crypto Wyckoff + SMC v9.1 — {meta.get("data_time","")}</title>
  <style>{css}</style>
</head>
<body>

<div style="background:#0d1520;border-bottom:2px solid #00ff8c;padding:24px 32px">
  <h1 style="font-size:22px;color:#00ff8c;margin-bottom:8px">📊 Crypto Wyckoff + SMC Scanner v9.1 — Universal Manual Selection</h1>
  <div style="color:#8899aa;font-size:12px">
    Report: <strong>{meta.get("report_time","")}</strong> |
    Data: <strong>{meta.get("data_time","")}</strong> |
    {"⚠ BACKTEST MODE" if meta.get("is_backtest") else "✅ LIVE DATA"} |
    Analyzed: <strong>{meta.get("total_symbols",0)}</strong> symbols |
    Active: <strong>{len(active)}</strong> |
    Premium: <strong>{len(premium)}</strong> |
    TOP picks: <strong>{len(top_picks)}</strong><br>
    <span style="color:#ffd700">Volume pre-filtered upstream: quote_volume_usd &gt; 800k. No additional aggressive volume filter applied.</span>
  </div>
</div>

<div style="padding:24px 32px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">📈 Market Summary</h2>
  <div style="background:#101827;border:1px solid #1e2d40;border-radius:8px;padding:16px;max-width:480px">
    {_row("Market Bias", f'<span style="background:{rc};color:{rt_col};padding:2px 10px;border-radius:12px;font-weight:bold">{regime.upper()}</span>')}
    {_row("BTC 1H Regime", meta.get("btc_regime","N/A"))}
    {_row("ETH 1H Regime", meta.get("eth_regime","N/A"))}
    {_row("Longs / Shorts", f'{sum(1 for s in active if s["direction"]=="LONG")} / {sum(1 for s in active if s["direction"]=="SHORT")}')}
    {_row("Day Type", "Risk-On" if regime=="bullish" else ("Risk-Off" if regime=="bearish" else "Choppy/Mixed"))}
    {_row("Preferred Model", "Model A" if regime in ("bullish","bearish") else "Model B")}
  </div>
</div>

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">📊 Statistics</h2>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px">{stats_html}</div>
</div>

<div style="padding:0 32px 24px;background:linear-gradient(180deg,#050d10 0%,#070b12 100%)">
  <h2 style="border-bottom:2px solid #00ff8c;padding-bottom:8px;margin-bottom:20px;color:#00ff8c">🎯 TOP MANUAL PICKS</h2>
  {picks_html}
</div>

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">📋 Active Setups Ranking (max 15)</h2>
  {render_table(active)}
</div>

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">🔍 Active Setup Detail Cards</h2>
  {cards_html}
</div>

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">👁 Watchlist</h2>
  {"<div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr><th>Symbol</th><th>Dir</th><th>Wyckoff</th><th>Score</th><th>Missing</th></tr></thead><tbody>" + watchlist_rows + "</tbody></table></div>" if watchlist_rows else "<p style='color:#556677'>No watchlist setups.</p>"}
</div>

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">❌ Rejected (sample)</h2>
  {"<div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr><th>Symbol</th><th>Dir</th><th>Reason</th></tr></thead><tbody>" + reject_rows + "</tbody></table></div>" if reject_rows else "<p style='color:#556677'>No rejected setups to display.</p>"}
</div>

<div style="background:#0a0f1a;border-top:1px solid #1e2d40;padding:20px 32px;font-size:11px;color:#556677">
  <strong style="color:#8899aa">DISCLAIMER:</strong>
  This report is generated from fresh data fetched at runtime. No cache, no previous scan results, no external assumptions.
  Upstream liquidity pre-filter is assumed: quote_volume_usd &gt; 800k. No additional aggressive volume filter was applied.
  Trading involves risk. This is not financial advice.<br>
  Generated: {meta.get("report_time","")}
</div>

</body>
</html>"""

# ============================================================
# MAIN
# ============================================================

def main():
    global BACKTEST_TIME_MS

    print("=" * 60)
    print("  Crypto Wyckoff + SMC Signal Generator v9.1")
    print("=" * 60)

    choice = input("\nDane teraźniejsze czy historyczne? (t=teraz / h=historyczne): ").strip().lower()

    is_backtest = False
    data_time   = datetime.now().strftime("%Y-%m-%d %H:%M")

    if choice == "h":
        is_backtest = True
        date_str = input("Podaj datę i czas (dd/mm/yyyy hh:mm): ").strip()
        try:
            dt_obj          = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
            BACKTEST_TIME_MS = int(dt_obj.timestamp() * 1000)
            data_time        = dt_obj.strftime("%Y-%m-%d %H:%M")
            print(f"Tryb backtest: {data_time}")
        except ValueError:
            print("Nieprawidłowy format — używam danych teraźniejszych.")
            is_backtest      = False
            BACKTEST_TIME_MS = None

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Top 400 kryptowalut
    print(f"\n[1/4] Pobieranie top {TOP_N} kryptowalut...")
    top_crypto = get_top_crypto(TOP_N)
    if not top_crypto:
        print("Brak symboli. Koniec.")
        return

    # 2. OHLCV
    print(f"[2/4] Pobieranie danych OHLCV dla {len(top_crypto)} symboli...")
    all_data  = {}
    completed = 0
    t0        = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_symbol_data, c): c for c in top_crypto}
        for fut in as_completed(futs):
            sym, data = fut.result()
            if data:
                all_data[sym] = data
            completed += 1
            if completed % 50 == 0 or completed == len(top_crypto):
                print(f"  {completed}/{len(top_crypto)} ({completed/len(top_crypto)*100:.0f}%) — {time.time()-t0:.0f}s")

    print(f"  Pobrano {len(all_data)} symboli w {time.time()-t0:.1f}s")

    # 3. Market regime
    print("[3/4] Analiza market regime...")
    btc_data   = all_data.get("BTCUSDT")
    eth_data   = all_data.get("ETHUSDT")
    regime     = market_regime(btc_data, eth_data)
    btc_regime = _trend(btc_data["timeframes"].get("1H",[]) if btc_data else [])
    eth_regime = _trend(eth_data["timeframes"].get("1H",[]) if eth_data else [])
    print(f"  Regime: {regime.upper()} | BTC 1H: {btc_regime} | ETH 1H: {eth_regime}")

    # 4. Analiza i scoring
    print("[4/4] Analiza setupów...")
    raw_setups = []
    for sym, data in all_data.items():
        s = analyze_symbol(sym, data, regime)
        if s:
            raw_setups.append(s)

    print(f"  Wstępnie wykryte setupy: {len(raw_setups)}")

    for s in raw_setups:
        s["ts"]    = total_score(s)
        s["mps"]   = manual_pick_score(s)
        s["category"] = classify(s)
        s["model"] = recommend_model(s)

    active_count = sum(1 for s in raw_setups if s.get("category") not in ("rejected","watchlist"))
    print(f"  Aktywne setupy (po filtracji): {active_count}")

    # Generuj HTML
    meta = {
        "report_time":  report_time,
        "data_time":    data_time,
        "is_backtest":  is_backtest,
        "total_symbols": len(all_data),
        "regime":       regime,
        "btc_regime":   btc_regime,
        "eth_regime":   eth_regime,
    }

    html = generate_html(raw_setups, meta)

    # Zapis
    ts_str = datetime.now().strftime("%Y_%m_%d_%H%M")
    if is_backtest and BACKTEST_TIME_MS:
        bt_ts  = datetime.fromtimestamp(BACKTEST_TIME_MS/1000).strftime("%Y_%m_%d_%H%M")
        outfile = os.path.join(OUTPUT_DIR, f"signals_{bt_ts}_backtest.html")
    else:
        outfile = os.path.join(OUTPUT_DIR, f"signals_{ts_str}.html")

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Raport: {outfile}")
    print(f"   Aktywnych setupów: {active_count}")

    abs_path = os.path.abspath(outfile)
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", abs_path])
        print("   Otwieranie w Google Chrome...")
    except FileNotFoundError:
        try:
            subprocess.Popen(["open", abs_path])
            print("   Chrome nie znaleziony — otwieranie domyślną przeglądarką...")
        except Exception as e:
            print(f"   Nie udało się otworzyć przeglądarki: {e}")

if __name__ == "__main__":
    main()
    