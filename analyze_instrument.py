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
from datetime import datetime

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

BACKTEST_TIME_MS = None

TIMEFRAMES   = ["5m", "15m", "1H", "4H", "1D"]
TF_LABEL     = {"5m": "5M", "15m": "15M", "1H": "1H", "4H": "4H", "1D": "1D"}
TF_BYBIT     = {"5m": "5", "15m": "15", "1H": "60", "4H": "240", "1D": "D"}

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
    last_ts = int(b1[-1][0]) - 1
    b2 = _bybit_candles_raw(symbol, interval, 200, last_ts)
    return _parse(b1 + b2)

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

def trend(candles, lookback=30):
    if len(candles) < lookback:
        return "unclear"
    c = [x["close"] for x in candles[-lookback:]]
    mid = lookback // 2
    f, s = sum(c[:mid])/mid, sum(c[mid:])/mid
    if s > f * 1.015:  return "bullish"
    if s < f * 0.985:  return "bearish"
    return "neutral"

def is_ranging(candles, lookback=30, thr=0.06):
    if len(candles) < lookback:
        return False
    src = candles[-lookback:]
    rng = max(c["high"] for c in src) - min(c["low"] for c in src)
    avg = sum(c["close"] for c in src) / lookback
    return avg > 0 and rng / avg < thr

# ============================================================
# WYCKOFF ANALYSIS
# ============================================================

def analyze_wyckoff(candles):
    if not candles or len(candles) < 20:
        return {"structure":"UNCLEAR","phase":"Unclear","events":["Insufficient data"],"confidence":"LOW",
                "range_high":None,"range_low":None,"full_trend":"unclear","recent_trend":"unclear","ranging":False}

    src = candles[-80:] if len(candles) > 80 else candles
    n   = len(src)

    # Percentyl 80/20 jako range high/low
    all_h = sorted(c["high"] for c in src)
    all_l = sorted(c["low"]  for c in src)
    rh = all_h[int(n * 0.82)]
    rl = all_l[int(n * 0.18)]

    last        = candles[-1]["close"]
    full_trend  = trend(candles, min(60, len(candles)))
    recent_tnd  = trend(candles, min(15, len(candles)))
    ranging     = is_ranging(candles[-30:] if len(candles) > 30 else candles, min(30, len(candles)))

    tail  = candles[-15:]
    prev  = candles[-50:-10] if len(candles) > 60 else candles[:-10]

    sos    = any(c["close"] > rh and c["close"] > c["open"] for c in tail)
    sow    = any(c["close"] < rl and c["close"] < c["open"] for c in tail)
    spring = any(c["low"] < rl and c["close"] > rl for c in tail)
    utad   = any(c["high"] > rh and c["close"] < rh for c in tail)

    # Volume climax
    if len(candles) >= 10:
        win = candles[-50:] if len(candles) > 50 else candles
        mvc = max(win, key=lambda c: c["volume"])
        sc  = mvc["close"] < mvc["open"] and mvc["low"]  <= rl * 1.005
        bc  = mvc["close"] > mvc["open"] and mvc["high"] >= rh * 0.995
    else:
        sc = bc = False

    lps  = sos and last <= rh * 1.015 and last >= rl
    lpsy = sow and last >= rl * 0.985 and last <= rh

    events = []
    if sc:     events.append("Possible SC (Selling Climax)")
    if bc:     events.append("Possible BC (Buying Climax)")
    if spring: events.append("Spring (test below support)")
    if utad:   events.append("UTAD (test above resistance)")
    if sos:    events.append("SOS — break above range high")
    if sow:    events.append("SOW — break below range low")
    if lps:    events.append("LPS — pullback after SOS")
    if lpsy:   events.append("LPSY — pullback after SOW")
    if not events:
        events.append("No clear Wyckoff events")

    # Struktura i faza
    if sos and not sow:
        structure  = "Accumulation" if full_trend in ("bearish","neutral") else "Reaccumulation"
        phase      = "D" if (lps or sos) else ("C" if spring else ("B" if ranging else "Unclear"))
        confidence = "HIGH" if (spring and lps) else ("MEDIUM" if (spring or lps) else "LOW")
    elif sow and not sos:
        structure  = "Distribution" if full_trend in ("bullish","neutral") else "Redistribution"
        phase      = "D" if (lpsy or sow) else ("C" if utad else ("B" if ranging else "Unclear"))
        confidence = "HIGH" if (utad and lpsy) else ("MEDIUM" if (utad or lpsy) else "LOW")
    elif ranging:
        structure  = "Accumulation" if full_trend == "bearish" else ("Distribution" if full_trend == "bullish" else "UNCLEAR")
        phase      = "B"
        confidence = "LOW"
    else:
        structure  = "Trend (Bullish)" if full_trend == "bullish" else ("Trend (Bearish)" if full_trend == "bearish" else "UNCLEAR")
        phase      = "E" if full_trend != "unclear" else "Unclear"
        confidence = "MEDIUM" if full_trend != "unclear" else "LOW"

    return {
        "structure":    structure,
        "phase":        phase,
        "events":       events,
        "confidence":   confidence,
        "range_high":   rh,
        "range_low":    rl,
        "full_trend":   full_trend,
        "recent_trend": recent_tnd,
        "ranging":      ranging,
    }

# ============================================================
# LIQUIDITY
# ============================================================

def analyze_liquidity(candles, tol=0.003):
    if not candles or len(candles) < 10:
        return {}

    src  = candles[-60:] if len(candles) > 60 else candles
    last = candles[-1]["close"]
    sw_h, sw_l = swing_points(src, lb=3)

    def cluster(points):
        seen = []
        for p in points:
            price = p["price"]
            merged = False
            for s in seen:
                if abs(price - s["level"]) / s["level"] <= tol:
                    s["level"] = (s["level"] * s["count"] + price) / (s["count"] + 1)
                    s["count"] += 1
                    merged = True
                    break
            if not merged:
                seen.append({"level": price, "count": 1})
        return seen

    eq_h = [x for x in cluster(sw_h) if x["count"] >= 2]
    eq_l = [x for x in cluster(sw_l) if x["count"] >= 2]

    rh = max(c["high"] for c in src)
    rl = min(c["low"]  for c in src)

    above = sorted([x for x in eq_h if x["level"] > last], key=lambda x: x["level"])
    below = sorted([x for x in eq_l if x["level"] < last], key=lambda x: x["level"], reverse=True)
    above.append({"level": rh, "count": 1, "type": "Range High"})
    below.append({"level": rl, "count": 1, "type": "Range Low"})

    return {
        "equal_highs": eq_h, "equal_lows": eq_l,
        "range_high": rh, "range_low": rl,
        "above": sorted(above, key=lambda x: x["level"]),
        "below": sorted(below, key=lambda x: x["level"], reverse=True),
        "last":  last,
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
# ELLIOTT (light)
# ============================================================

def analyze_elliott(candles):
    if not candles or len(candles) < 30:
        return "Unclear"

    full_t   = trend(candles, min(40, len(candles)))
    recent_t = trend(candles, min(10, len(candles)))
    sw_h, sw_l = swing_points(candles[-50:] if len(candles) > 50 else candles, lb=3)

    if full_t == "bullish":
        if recent_t == "bearish":
            return "Possible correction (mid-trend pullback)"
        if len(sw_h) >= 3:
            rh = [h["price"] for h in sw_h[-3:]]
            if rh[-1] < rh[-2] * 1.001:
                return "Possible exhaustion / wave 5 top"
        return "Mid trend (bullish impulse)"

    if full_t == "bearish":
        if recent_t == "bullish":
            return "Possible correction (bearish mid-trend bounce)"
        if len(sw_l) >= 3:
            rl = [l["price"] for l in sw_l[-3:]]
            if rl[-1] > rl[-2] * 0.999:
                return "Possible exhaustion / bottom forming"
        return "Mid trend (bearish impulse)"

    return "Unclear"

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
        wy_rows += (f'<tr><td style="font-weight:bold">{TF_LABEL[tf]}</td>'
                    f'<td style="color:{sc}">{w["structure"]}</td>'
                    f'<td>Phase {w["phase"]}</td>'
                    f'<td style="font-size:12px;color:#aabbcc">{" | ".join(w["events"][:2])}</td>'
                    f'<td style="color:{cc}">{w["confidence"]}</td></tr>')

    wyckoff_html = (
        '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead style="background:#0d1520"><tr>'
        '<th style="padding:8px;color:#8899aa;text-align:left">TF</th>'
        '<th>Structure</th><th>Phase</th><th>Key Events</th><th>Confidence</th>'
        f'</tr></thead><tbody style="color:#e6edf7">{wy_rows}</tbody></table></div>'
    )

    # ── Liquidity map (use 1H primary, fallback 4H) ──
    liq_html = ""
    for tf_try in ["1H", "4H", "15m"]:
        a = A.get(tf_try)
        if not a or not a.get("liquidity"): continue
        liq  = a["liquidity"]
        last = liq["last"]
        above = liq["above"][:5]
        below = liq["below"][:5]

        a_html = "".join(
            f'<div style="display:flex;justify-content:space-between;padding:3px 6px;'
            f'background:#0a1a0a;border-radius:3px;margin:2px 0">'
            f'<span style="color:#00ff8c">▲ {_f(x["level"])}</span>'
            f'<span style="color:#8899aa;font-size:11px">'
            f'{"Equal highs ×"+str(x["count"]) if x.get("count",1)>1 else x.get("type","Swing high")}</span></div>'
            for x in above
        ) or "<div style='color:#556677;font-size:12px'>None</div>"

        b_html = "".join(
            f'<div style="display:flex;justify-content:space-between;padding:3px 6px;'
            f'background:#1a0a0a;border-radius:3px;margin:2px 0">'
            f'<span style="color:#ff4d4d">▼ {_f(x["level"])}</span>'
            f'<span style="color:#8899aa;font-size:11px">'
            f'{"Equal lows ×"+str(x["count"]) if x.get("count",1)>1 else x.get("type","Swing low")}</span></div>'
            for x in below
        ) or "<div style='color:#556677;font-size:12px'>None</div>"

        liq_html = (
            f'<div style="font-size:12px;color:#8899aa;margin-bottom:8px">'
            f'{TF_LABEL[tf_try]} — Price: <strong style="color:#e6edf7">{_f(last)}</strong></div>'
            f'<div style="font-size:12px;color:#00ff8c;margin-bottom:4px">▲ Above price liquidity</div>'
            f'{a_html}'
            f'<div style="text-align:center;padding:5px;background:#0d1520;margin:6px 0;'
            f'font-size:12px;color:#ffd700;border-radius:4px">── Current Price: {_f(last)} ──</div>'
            f'<div style="font-size:12px;color:#ff4d4d;margin-bottom:4px">▼ Below price liquidity</div>'
            f'{b_html}'
        )
        break

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

    # ── Elliott ──
    ell_rows = ""
    for tf in ["1H", "4H", "1D"]:
        a = A.get(tf)
        if not a: continue
        ell = a["elliott"]
        col = "#8899aa" if ell == "Unclear" else "#88ccff"
        ell_rows += f'<div style="padding:4px 0">{TF_LABEL[tf]}: <span style="color:{col}">{ell}</span></div>'

    # ── Final interpretation ──
    interp = []
    for tf in ["1H", "4H"]:
        a = A.get(tf)
        if not a: continue
        w  = a["wyckoff"]
        st = a.get("structure", {})
        interp.append(f'{TF_LABEL[tf]}: <strong>{w["structure"]}</strong> Phase {w["phase"]} ({w["confidence"]} confidence).')
        if st.get("choch"):
            interp.append("ChoCH detected — potential character change.")
        if st.get("bos"):
            interp.append("BOS confirmed — structure continuation in mid-term direction.")
        break

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
)}

{_section("〰️ Elliott Wave (Light)",
    _card(
        (ell_rows or "<div style='color:#556677'>No data</div>") +
        "<div style='margin-top:8px;font-size:11px;color:#556677'>Optional light read — no full wave count. Only shown when obvious.</div>"
    )
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

    symbol = input("\nPodaj symbol (np. BTC lub BTCUSDT): ").strip().upper()
    if not symbol:
        print("Brak symbolu. Koniec.")
        return
    if not symbol.endswith("USDT"):
        symbol += "USDT"
        print(f"  → {symbol}")

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
        candles = get_candles(symbol, tf, BACKTEST_TIME_MS)
        data_by_tf[tf] = candles
        print(f"{len(candles)} świec")

    if not any(data_by_tf.values()):
        print(f"\nBrak danych dla {symbol}. Sprawdź czy symbol istnieje na Bybit Linear.")
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
