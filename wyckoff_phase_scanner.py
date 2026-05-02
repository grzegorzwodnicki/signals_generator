#!/usr/bin/env python3
"""
Wyckoff Phase Scanner
Skanuje Top 400 kryptowalut pod kątem wybranej fazy Wyckoff na wybranym timeframe.
Raport HTML z analizą multi-TF dla każdego dopasowania.

Użycie:
  python wyckoff_phase_scanner.py
"""

import requests
import time
import os
import subprocess
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

BACKTEST_TIME_MS = None

TIMEFRAMES = ["5m", "15m", "30m", "1H", "4H", "1D"]
TF_LABEL   = {"5m": "5M", "15m": "15M", "30m": "30M", "1H": "1H", "4H": "4H", "1D": "1D"}
TF_BYBIT   = {"5m": "5",  "15m": "15",  "30m": "30",  "1H": "60", "4H": "240", "1D": "D"}

_SEMAPHORE = threading.Semaphore(20)

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
# WYCKOFF ANALYSIS
# ============================================================

def _swing_points(candles, lb=3):
    highs, lows = [], []
    for i in range(lb, len(candles) - lb):
        h, l = candles[i]["high"], candles[i]["low"]
        if all(h >= candles[i-j]["high"] and h >= candles[i+j]["high"] for j in range(1, lb+1)):
            highs.append({"idx": i, "price": h})
        if all(l <= candles[i-j]["low"]  and l <= candles[i+j]["low"]  for j in range(1, lb+1)):
            lows.append({"idx": i, "price": l})
    return highs, lows

def _trend(candles, lookback=30):
    if len(candles) < lookback:
        return "unclear"
    c   = [x["close"] for x in candles[-lookback:]]
    mid = lookback // 2
    f, s = sum(c[:mid]) / mid, sum(c[mid:]) / mid
    if s > f * 1.015: return "bullish"
    if s < f * 0.985: return "bearish"
    return "neutral"

def _ranging(candles, lookback=30, thr=0.06):
    if len(candles) < lookback:
        return False
    src = candles[-lookback:]
    rng = max(c["high"] for c in src) - min(c["low"] for c in src)
    avg = sum(c["close"] for c in src) / lookback
    return avg > 0 and rng / avg < thr

def analyze_wyckoff(candles):
    if not candles or len(candles) < 20:
        return {
            "structure": "UNCLEAR", "phase": "Unclear",
            "events": ["Insufficient data"], "confidence": "LOW",
            "range_high": None, "range_low": None,
            "full_trend": "unclear", "recent_trend": "unclear", "ranging": False,
        }

    src = candles[-80:] if len(candles) > 80 else candles
    n   = len(src)

    all_h = sorted(c["high"] for c in src)
    all_l = sorted(c["low"]  for c in src)
    rh = all_h[int(n * 0.82)]
    rl = all_l[int(n * 0.18)]

    last       = candles[-1]["close"]
    full_trend = _trend(candles, min(60, len(candles)))
    recent_tnd = _trend(candles, min(15, len(candles)))
    ranging    = _ranging(candles[-30:] if len(candles) > 30 else candles, min(30, len(candles)))

    tail = candles[-15:]

    sos    = any(c["close"] > rh and c["close"] > c["open"] for c in tail)
    sow    = any(c["close"] < rl and c["close"] < c["open"] for c in tail)
    spring = any(c["low"] < rl and c["close"] > rl for c in tail)
    utad   = any(c["high"] > rh and c["close"] < rh for c in tail)

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
    if sc:     events.append("SC (Selling Climax)")
    if bc:     events.append("BC (Buying Climax)")
    if spring: events.append("Spring")
    if utad:   events.append("UTAD")
    if sos:    events.append("SOS")
    if sow:    events.append("SOW")
    if lps:    events.append("LPS")
    if lpsy:   events.append("LPSY")
    if not events:
        events.append("No clear events")

    if sos and not sow:
        structure  = "Accumulation" if full_trend in ("bearish", "neutral") else "Reaccumulation"
        phase      = "D" if (lps or sos) else ("C" if spring else ("B" if ranging else "Unclear"))
        confidence = "HIGH" if (spring and lps) else ("MEDIUM" if (spring or lps) else "LOW")
    elif sow and not sos:
        structure  = "Distribution" if full_trend in ("bullish", "neutral") else "Redistribution"
        phase      = "D" if (lpsy or sow) else ("C" if utad else ("B" if ranging else "Unclear"))
        confidence = "HIGH" if (utad and lpsy) else ("MEDIUM" if (utad or lpsy) else "LOW")
    elif ranging:
        structure  = "Accumulation" if full_trend == "bearish" else (
                     "Distribution" if full_trend == "bullish" else "UNCLEAR")
        phase      = "B"
        confidence = "LOW"
    else:
        structure  = ("Trend (Bullish)" if full_trend == "bullish" else
                      "Trend (Bearish)" if full_trend == "bearish" else "UNCLEAR")
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
# PHASE MATCHING
# ============================================================

WYCKOFF_STRUCTS  = {"Accumulation", "Reaccumulation", "Distribution", "Redistribution"}
_BULLISH_STRUCTS = {"Accumulation", "Reaccumulation"}
_BEARISH_STRUCTS = {"Distribution", "Redistribution"}

def _matches(wy, selected_phases):
    """Return True if Wyckoff result matches any of the selected phases (OR logic)."""
    p      = wy.get("phase", "Unclear")
    struct = wy.get("structure", "UNCLEAR")
    evs    = " ".join(wy.get("events", []))

    for selected_phase in selected_phases:
        if selected_phase == "A":
            if "SC" in evs or "BC" in evs:
                return True
        elif struct in WYCKOFF_STRUCTS and p == selected_phase:
            return True
    return False


def _check_conditions(data_by_tf, conditions):
    """
    Check all conditions against fetched candles.
    Returns (matched, direction, wyckoff_by_tf) or (False, None, {}).
    Direction must be consistent across all conditions.
    """
    directions    = set()
    wyckoff_by_tf = {}

    for cond in conditions:
        tf      = cond["tf"]
        phases  = cond["phases"]
        candles = data_by_tf.get(tf, [])
        if len(candles) < 20:
            return False, None, {}
        wy = analyze_wyckoff(candles)
        if not _matches(wy, phases):
            return False, None, {}
        wyckoff_by_tf[tf] = wy

        struct = wy.get("structure", "UNCLEAR")
        evs    = " ".join(wy.get("events", []))
        if struct in _BULLISH_STRUCTS:
            directions.add("bullish")
        elif struct in _BEARISH_STRUCTS:
            directions.add("bearish")
        elif "A" in phases:
            if "SC" in evs:
                directions.add("bullish")
            elif "BC" in evs:
                directions.add("bearish")

    if len(directions) > 1:
        return False, None, {}

    direction = directions.pop() if directions else "unclear"
    return True, direction, wyckoff_by_tf

# ============================================================
# HTML HELPERS
# ============================================================

def _f(v):
    if v is None: return "N/A"
    if v >= 1000: return f"{v:,.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

STRUCT_COLOR = {
    "Accumulation":    "#00ff8c",
    "Reaccumulation":  "#00ccff",
    "Distribution":    "#ff4d4d",
    "Redistribution":  "#ff8844",
    "Trend (Bullish)": "#88ccff",
    "Trend (Bearish)": "#cc88ff",
    "UNCLEAR":         "#556677",
}
CONF_COLOR = {"HIGH": "#00ff8c", "MEDIUM": "#ffd700", "LOW": "#8899aa"}
CONF_RANK  = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

def _multi_tf_table(data_by_tf, highlight_tfs=None):
    hl_set = set(highlight_tfs) if highlight_tfs else set()
    rows = ""
    for tf in TIMEFRAMES:
        candles = data_by_tf.get(tf, [])
        if not candles:
            rows += (
                f'<tr><td style="color:#88ccff;font-weight:bold">{TF_LABEL[tf]}</td>'
                f'<td colspan="5" style="color:#334455">No data</td></tr>'
            )
            continue
        w   = analyze_wyckoff(candles)
        sc  = STRUCT_COLOR.get(w["structure"], "#556677")
        cc  = CONF_COLOR.get(w["confidence"], "#8899aa")
        rh  = _f(w["range_high"])
        rl  = _f(w["range_low"])
        evs = " · ".join(w["events"][:3])
        hl  = "background:#0d1a2a;" if tf in hl_set else ""
        rows += (
            f'<tr style="{hl}">'
            f'<td style="color:#88ccff;font-weight:bold;white-space:nowrap">{TF_LABEL[tf]}</td>'
            f'<td style="color:{sc}">{w["structure"]}</td>'
            f'<td style="color:#ffd700;white-space:nowrap">Phase {w["phase"]}</td>'
            f'<td style="color:#00e5ff;white-space:nowrap;font-size:12px">{rl} — {rh}</td>'
            f'<td style="font-size:11px;color:#8899aa;max-width:300px">{evs}</td>'
            f'<td style="color:{cc};white-space:nowrap;font-size:12px">{w["confidence"]}</td>'
            f'</tr>'
        )
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        '<thead style="background:#070b12"><tr>'
        '<th style="padding:5px 6px;color:#334455;text-align:left">TF</th>'
        '<th style="color:#334455">Structure</th>'
        '<th style="color:#334455">Phase</th>'
        '<th style="color:#334455">Range (Low — High)</th>'
        '<th style="color:#334455">Key Events</th>'
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

def generate_html(matches, scan_config, report_time, data_time, is_backtest):
    conditions    = scan_config["conditions"]
    total_scanned = scan_config["total_scanned"]
    found         = len(matches)
    condition_tfs = [c["tf"] for c in conditions]
    primary_tf    = conditions[0]["tf"]
    multi_mode    = len(conditions) > 1

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

    # ── Conditions badges ──
    cond_badges = "".join(
        f'<span style="background:#0d1a2a;border:1px solid #1e3050;border-radius:5px;'
        f'padding:5px 12px;display:inline-flex;gap:8px;align-items:center">'
        f'<span style="color:#00e5ff;font-weight:bold">{TF_LABEL.get(c["tf"], c["tf"])}</span>'
        f'<span style="color:#334455">Phase</span>'
        f'<span style="color:#ffd700;font-weight:bold">{"/".join(c["phases"])}</span>'
        f'<span style="color:#445566;font-size:11px">{" / ".join(_PHASE_LABEL.get(p, "") for p in c["phases"])}</span>'
        f'</span>'
        for c in conditions
    )

    # ── Summary row ──
    summary_html = (
        f'<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px">'
        f'<div class="card" style="flex:0 0 auto;text-align:center;min-width:140px">'
        f'<div style="font-size:11px;color:#8899aa;margin-bottom:4px">FOUND</div>'
        f'<div style="font-size:38px;font-weight:bold;color:#00ff8c">{found}</div>'
        f'<div style="font-size:11px;color:#445566">of {total_scanned} scanned</div>'
        f'</div>'
        f'<div class="card" style="flex:1;min-width:300px">'
        f'<div style="font-size:11px;color:#8899aa;margin-bottom:10px">'
        f'{"CONDITIONS (" + str(len(conditions)) + ")" if multi_mode else "CONDITION"}</div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap">{cond_badges}</div>'
        f'{"<div style=margin-top:10px;font-size:12px;color:#445566>All conditions must match · Same Wyckoff direction required</div>" if multi_mode else ""}'
        f'</div>'
        f'</div>'
    )

    # ── Results table ──
    table_rows = ""
    for m in matches:
        w   = m["wyckoff_primary"]
        sc  = STRUCT_COLOR.get(w["structure"], "#556677")
        cc  = CONF_COLOR.get(w["confidence"], "#8899aa")
        evs = " · ".join(w["events"][:4])
        table_rows += (
            f'<tr>'
            f'<td style="font-weight:bold">'
            f'<a href="#{m["symbol"]}" style="color:#e6edf7;text-decoration:none">'
            f'{m["symbol"]}</a></td>'
            f'<td style="color:{sc}">{w["structure"]}</td>'
            f'<td style="color:#ffd700">Phase {w["phase"]}</td>'
            f'<td style="color:#00e5ff;white-space:nowrap;font-size:12px">'
            f'{_f(w["range_low"])} — {_f(w["range_high"])}</td>'
            f'<td style="font-size:12px;color:#8899aa">{evs}</td>'
            f'<td style="color:{cc}">{w["confidence"]}</td>'
            f'</tr>'
        )

    if not table_rows:
        table_rows = (
            '<tr><td colspan="6" style="text-align:center;color:#445566;padding:28px">'
            'No instruments matched the selected criteria.</td></tr>'
        )

    results_table = (
        '<div style="overflow-x:auto">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead style="background:#0d1520"><tr>'
        '<th>Symbol</th><th>Structure</th><th>Phase</th>'
        '<th>Range on selected TF</th><th>Key Events</th><th>Conf</th>'
        '</tr></thead>'
        f'<tbody style="color:#e6edf7">{table_rows}</tbody>'
        '</table></div>'
    )

    # ── Detail cards ──
    detail_cards = ""
    for m in matches:
        sym  = m["symbol"]
        w    = m["wyckoff_primary"]
        sc   = STRUCT_COLOR.get(w["structure"], "#556677")
        cc   = CONF_COLOR.get(w["confidence"], "#8899aa")
        ev_badges = "".join(
            f'<span style="background:#1a2535;border-radius:3px;padding:2px 8px;'
            f'margin:2px;font-size:11px;display:inline-block;color:#aabbcc">{e}</span>'
            for e in w["events"]
        )
        direction   = m.get("direction", "")
        dir_col     = "#00ff8c" if direction == "bullish" else ("#ff4d4d" if direction == "bearish" else "#8899aa")
        dir_label   = direction.upper() if direction else ""
        multi_tf    = _multi_tf_table(m["data_by_tf"], highlight_tfs=condition_tfs)

        # Per-condition range summary (multi-mode)
        cond_ranges = ""
        if multi_mode:
            for cond in conditions:
                ctf  = cond["tf"]
                cwy  = analyze_wyckoff(m["data_by_tf"].get(ctf, []))
                csc  = STRUCT_COLOR.get(cwy["structure"], "#556677")
                cond_ranges += (
                    f'<span style="margin-right:16px;font-size:12px">'
                    f'<span style="color:#667788">{TF_LABEL.get(ctf, ctf)} Ph{"/".join(cond["phases"])}: </span>'
                    f'<span style="color:{csc}">{cwy["structure"]}</span>'
                    f' <span style="color:#00e5ff">{_f(cwy["range_low"])} — {_f(cwy["range_high"])}</span>'
                    f'</span>'
                )

        detail_cards += (
            f'<div id="{sym}" class="card">'
            f'<details open>'
            f'<summary style="display:flex;justify-content:space-between;'
            f'align-items:center;padding:2px 0 10px">'
            f'<span style="font-size:20px;font-weight:bold;color:#e6edf7">{sym}</span>'
            f'<span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
            f'<span style="color:{sc};font-weight:bold;font-size:14px">{w["structure"]}</span>'
            f'<span style="background:#1e2d40;color:#ffd700;padding:3px 12px;'
            f'border-radius:4px;font-size:13px">Phase {w["phase"]}</span>'
            f'{"<span style=background:" + dir_col + "22;color:" + dir_col + ";padding:3px 10px;border-radius:4px;font-size:12px>" + dir_label + "</span>" if dir_label else ""}'
            f'<span style="color:{cc};font-size:12px">{w["confidence"]}</span>'
            f'<span style="color:#334455;font-size:13px">▼</span>'
            f'</span>'
            f'</summary>'
            f'<div>'
            f'<div style="margin-bottom:10px;flex-wrap:wrap">'
            f'{cond_ranges if multi_mode else ""}'
            f'{"<span style=font-size:12px;color:#667788>Range (" + TF_LABEL.get(primary_tf, primary_tf) + "): </span><span style=color:#00e5ff;font-size:13px>" + _f(w["range_low"]) + " — " + _f(w["range_high"]) + "</span>" if not multi_mode else ""}'
            f'</div>'
            f'<div style="margin-bottom:14px">{ev_badges}</div>'
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

    cond_title = " + ".join(
        f'{TF_LABEL.get(c["tf"], c["tf"])} Ph{"/".join(c["phases"])}' for c in conditions
    )
    hl_note = "highlighted rows = condition TFs" if multi_mode else "highlighted row = condition TF"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Wyckoff Scanner · {cond_title}</title>
  <style>{css}</style>
</head>
<body>

<div style="background:#0d1520;border-bottom:2px solid #00aaff;padding:20px 32px;margin-bottom:24px">
  <div style="font-size:11px;color:#8899aa;letter-spacing:1px;margin-bottom:4px">WYCKOFF PHASE SCANNER</div>
  <h1 style="font-size:24px;font-weight:bold;color:#e6edf7;margin-bottom:6px">{cond_title}</h1>
  <div style="font-size:12px;color:#8899aa">
    Report: <strong>{report_time}</strong> &nbsp;|&nbsp;
    Data: <strong>{data_time}</strong> &nbsp;|&nbsp;
    {"⚠ BACKTEST" if is_backtest else "✅ LIVE"} &nbsp;|&nbsp;
    Scanned: <strong>{total_scanned}</strong> &nbsp;|&nbsp;
    Found: <strong style="color:#00ff8c">{found}</strong>
  </div>
</div>

<div style="padding:0 32px 32px">

{bt_banner}
{summary_html}

<h2 style="color:#e6edf7;border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">
  📋 Matches — {found} instrument{"s" if found != 1 else ""}
</h2>
{results_table}

<h2 style="color:#e6edf7;border-bottom:1px solid #1e2d40;padding-bottom:8px;margin:28px 0 16px">
  🔍 Multi-TF Analysis
  <span style="font-size:12px;color:#445566;font-weight:normal;margin-left:8px">{hl_note}</span>
</h2>
{detail_cards or "<div style='color:#445566;padding:16px'>No matches.</div>"}

</div>

<div style="background:#0a0f1a;border-top:1px solid #1e2d40;padding:16px 32px;
font-size:11px;color:#445566;margin-top:8px">
  Wyckoff Phase Scanner · {report_time} · {cond_title}
</div>

</body>
</html>"""

# ============================================================
# MAIN
# ============================================================

_TF_ALIASES = {
    "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "1H": "1H", "4h": "4H", "4H": "4H",
    "1d": "1D", "1D": "1D",
}

def _ask_condition(label=""):
    """Prompt for one TF + Phase(s) condition. Returns dict or None on invalid input."""
    print(f"\n{label}Timeframes: 5m · 15m · 30m · 1H · 4H · 1D")
    tf_raw = input(f"{label}TF: ").strip()
    tf     = _TF_ALIASES.get(tf_raw, tf_raw)
    if tf not in TIMEFRAMES:
        print(f"  Nieznany TF '{tf_raw}'.")
        return None
    print(f"{label}Fazy: A (SC/BC)  B (ranging)  C (Spring/UTAD)  D (SOS/SOW)  E (trend)")
    print(f"{label}Można podać kilka faz przez przecinek, np. B,C = B lub C")
    raw_phases = input(f"{label}Faza(y) (np. C lub B,C): ").strip().upper()
    phases = [p.strip() for p in raw_phases.split(",") if p.strip()]
    invalid = [p for p in phases if p not in ("A", "B", "C", "D", "E")]
    if not phases or invalid:
        print(f"  Nieznane fazy: {', '.join(invalid) if invalid else '(brak)'}.")
        return None
    phases = list(dict.fromkeys(phases))  # deduplicate, preserve order
    return {"tf": tf, "phases": phases}


def main():
    global BACKTEST_TIME_MS

    print("=" * 55)
    print("  Wyckoff Phase Scanner")
    print("=" * 55)

    # ── Single or multi condition ──
    n_raw = input("\nIle warunków? (Enter = 1): ").strip()
    try:
        n_conditions = max(1, min(int(n_raw), 6)) if n_raw else 1
    except ValueError:
        n_conditions = 1

    conditions = []
    for i in range(n_conditions):
        label = f"[Warunek {i+1}/{n_conditions}] " if n_conditions > 1 else ""
        cond  = _ask_condition(label)
        if cond is None:
            print("Przerwano — nieprawidłowy warunek.")
            return
        conditions.append(cond)

    if n_conditions > 1:
        print(f"\nWarunki: " + "  +  ".join(
            f'{TF_LABEL.get(c["tf"], c["tf"])} Ph{"/".join(c["phases"])}' for c in conditions
        ))
        print("Skaner znajdzie instrumenty pasujące do WSZYSTKICH warunków w tym samym kierunku.")

    # ── Time mode ──
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
            print("  Nieprawidłowy format — używam teraźniejszych.")
            BACKTEST_TIME_MS = None
            is_backtest      = False

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Fetch symbols ──
    print("\nPobieram Top 400 symboli...")
    symbols = get_top_symbols(400)
    if not symbols:
        print("Nie udało się pobrać listy symboli. Sprawdź połączenie.")
        return
    print(f"  {len(symbols)} symboli.")

    # ── Unique TFs needed for conditions ──
    condition_tfs = list(dict.fromkeys(c["tf"] for c in conditions))

    cond_label = " + ".join(f'{TF_LABEL.get(c["tf"],c["tf"])} Ph{"/".join(c["phases"])}' for c in conditions)
    print(f"\nSkanowanie {len(symbols)} symboli [{cond_label}]...")

    progress = {"n": 0}
    lock     = threading.Lock()

    def _scan(sym):
        try:
            # Fetch all condition TFs
            data = {tf: get_candles(sym, tf, BACKTEST_TIME_MS) for tf in condition_tfs}
            with lock:
                progress["n"] += 1
                if progress["n"] % 50 == 0:
                    print(f"  {progress['n']}/{len(symbols)}...")
            matched, direction, wy_by_tf = _check_conditions(data, conditions)
            if matched:
                primary_wy = wy_by_tf.get(conditions[0]["tf"])
                return {
                    "symbol":          sym,
                    "wyckoff_primary": primary_wy,
                    "direction":       direction,
                    "data_cond_tfs":   data,
                }
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(_scan, symbols))

    candidates = [r for r in results if r is not None]
    print(f"  Dopasowań: {len(candidates)}")

    if not candidates:
        print("\nBrak dopasowań — generuję pusty raport.")

    # ── Phase 2: fetch remaining TFs for detail report ──
    remaining_tfs = [tf for tf in TIMEFRAMES if tf not in condition_tfs]

    if candidates and remaining_tfs:
        print(f"\nPobieram pozostałe TF dla {len(candidates)} dopasowań...")

        def _fetch_rest(m):
            data = dict(m["data_cond_tfs"])
            for tf in remaining_tfs:
                data[tf] = get_candles(m["symbol"], tf, BACKTEST_TIME_MS)
            m["data_by_tf"] = data
            print(f"  {m['symbol']} — OK")
            return m

        with ThreadPoolExecutor(max_workers=6) as ex:
            candidates = list(ex.map(_fetch_rest, candidates))
    else:
        for m in candidates:
            m["data_by_tf"] = dict(m.get("data_cond_tfs", {}))

    # Sort: HIGH conf first, then bullish before bearish
    candidates.sort(
        key=lambda m: (
            CONF_RANK.get(m["wyckoff_primary"]["confidence"], 0),
            m.get("direction", "") == "bullish",
        ),
        reverse=True,
    )

    # ── Generate & save HTML ──
    scan_config = {"conditions": conditions, "total_scanned": len(symbols)}
    html = generate_html(candidates, scan_config, report_time, data_time, is_backtest)

    cond_safe = "_".join(f'{c["tf"]}ph{"".join(c["phases"])}' for c in conditions)
    ts_str    = datetime.now().strftime("%Y_%m_%d_%H%M")
    if is_backtest and BACKTEST_TIME_MS:
        bt_ts   = datetime.fromtimestamp(BACKTEST_TIME_MS / 1000).strftime("%Y_%m_%d_%H%M")
        outfile = os.path.join(OUTPUT_DIR, f"wyckoff_{cond_safe}_{bt_ts}_bt.html")
    else:
        outfile = os.path.join(OUTPUT_DIR, f"wyckoff_{cond_safe}_{ts_str}.html")

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Raport: {outfile}")
    print(f"   Znaleziono {len(candidates)} instrument(ów) [{cond_label}]")

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
