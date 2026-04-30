#!/usr/bin/env python3
"""
Scanner v9.1 — live scan + HTML report.
Użycie: python strategy_9_1/scanner.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subprocess
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import strategy as st

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# FORMATOWANIE HTML
# ============================================================

def _f(v, price=0):
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

def _status_badge(s):
    m = {"at_fvg_and_order_block":("#00ff8c","FVG+OB"),
         "at_fvg":("#00e5ff","AT FVG"),
         "at_order_block":("#00ffcc","AT OB"),
         "confluence_zone":("#ffd700","CONFLUENCE")}
    col, lbl = m.get(s, ("#888", s.upper().replace("_"," ")))
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
    return (f'<div style="display:flex;justify-content:space-between;padding:3px 0;'
            f'border-bottom:1px solid #1a2535">'
            f'<span style="color:#8899aa">{label}</span>'
            f'<span style="color:{color};font-weight:500">{val}</span></div>')

def _panel(title, content):
    return (f'<div style="background:#0d1520;border-radius:6px;padding:12px;margin-top:10px">'
            f'<div style="color:#00aaff;font-size:13px;font-weight:bold;margin-bottom:8px">{title}</div>'
            f'{content}</div>')

# ============================================================
# KARTY SETUPÓW
# ============================================================

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

    ob_fvg_pct = 0
    if fvg and ob:
        ov = min(fvg["fvg_high"], ob["ob_high"]) - max(fvg["fvg_low"], ob["ob_low"])
        if fvg["size"] > 0:
            ob_fvg_pct = max(ov, 0) / fvg["size"] * 100

    def mps_row(label, val, mx):
        col = "#00ff8c" if val > 0 else ("#ff4d4d" if val < 0 else "#556677")
        bar = (f'<div style="background:#1e2d40;border-radius:3px;height:5px;margin-top:2px">'
               f'<div style="background:{col};width:{max(val,0)/mx*100:.0f}%;height:5px;border-radius:3px"></div></div>')
        return (f'<div style="display:flex;justify-content:space-between;padding:2px 0">'
                f'<span style="color:#8899aa;font-size:12px">{label}</span>'
                f'<span style="color:{col};font-size:12px">{val}/{mx}</span></div>{bar}')

    mps_html = (
        mps_row("MACD confirmation", 20 if s["macd_5m"]=="yes" else 0, 20) +
        mps_row("Entry quality", 20 if ee<=0.10 else (15 if ee<=0.25 else 5), 20) +
        mps_row("Location", {"at_fvg_and_order_block":20,"at_fvg":16,"at_order_block":14,"confluence_zone":5}.get(s["status"],0), 20) +
        mps_row("ChoCH quality", {0:15,1:15,2:12,3:8,4:5}.get(age,0), 15) +
        mps_row("Pattern quality", 10 if (s["touches_fvg"] or s["touches_ob"]) and "Engulfing" in s["pattern_type"] else 4, 10) +
        mps_row("R:R quality", 10 if s["rr"]>=3.0 else 7, 10) +
        mps_row("Regime", 5 if s["aligned"] else (-5 if s["aligned"] is False else 0), 5)
    )

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
# RAPORT HTML
# ============================================================

def generate_html(all_setups, meta):
    premium  = [s for s in all_setups if s.get("category") == "premium_setup"]
    hq       = [s for s in all_setups if s.get("category") == "high_quality"]
    sec      = [s for s in all_setups if s.get("category") == "secondary_quality"]
    watchlist= [s for s in all_setups if s.get("category") == "watchlist"]
    rejected = [s for s in all_setups if s.get("category") == "rejected"]

    active    = sorted(premium + hq + sec, key=lambda s: s["mps"], reverse=True)[:15]
    top_picks = [s for s in active if s["mps"] >= 70][:3]

    regime = meta.get("regime", "unclear")
    rc     = {"bullish":"#00ff8c","bearish":"#ff4d4d","mixed":"#ffd700","chop":"#88ccff"}.get(regime, "#888")
    rt_col = "#fff" if regime in ("bearish","watchlist") else "#000"

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
    stats_html = "".join(
        f'<div style="background:#101827;border:1px solid #1e2d40;border-radius:6px;padding:12px;text-align:center">'
        f'<div style="font-size:22px;font-weight:bold;color:{col}">{val}</div>'
        f'<div style="font-size:11px;color:#8899aa;margin-top:4px">{label}</div></div>'
        for label, val, col in stats_data
    )

    if top_picks:
        picks_html = '<h3 style="color:#00ff8c;margin-bottom:12px">🏆 TOP 1 SETUP OF THE DAY</h3>' + render_top_pick(top_picks[0], 1)
        if len(top_picks) > 1:
            picks_html += '<h3 style="color:#00ff8c;margin-bottom:12px;margin-top:20px">TOP 3 MANUAL PICKS</h3>'
            for i, p in enumerate(top_picks[1:], 2):
                picks_html += render_top_pick(p, i)
    else:
        picks_html = '<div style="text-align:center;padding:40px;color:#556677;font-size:18px">⚠ No high-confidence manual trade today.</div>'

    cards_html = "".join(render_card(s) for s in active)
    watchlist_rows = "".join(
        f'<tr><td style="font-weight:bold">{s["symbol"]}</td>'
        f'<td style="color:{"#00ff8c" if s["direction"]=="LONG" else "#ff4d4d"}">{s["direction"]}</td>'
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
        f'<tr><td>{s["symbol"]}</td>'
        f'<td style="color:{"#00ff8c" if s["direction"]=="LONG" else "#ff4d4d"}">{s["direction"]}</td>'
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

    css = ("*{box-sizing:border-box;margin:0;padding:0}"
           "body{background:#070b12;color:#e6edf7;font-family:'Segoe UI',Arial,sans-serif;font-size:14px}"
           "h1,h2,h3{color:#e6edf7}"
           "th{padding:8px 6px;text-align:left;color:#8899aa;border-bottom:1px solid #1e2d40}"
           "td{padding:7px 6px;border-bottom:1px solid #1a2535}"
           "tr:hover td{background:#0d1520}")

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
  <h1 style="font-size:22px;color:#00ff8c;margin-bottom:8px">📊 Crypto Wyckoff + SMC Scanner v9.1</h1>
  <div style="color:#8899aa;font-size:12px">
    Report: <strong>{meta.get("report_time","")}</strong> |
    Data: <strong>{meta.get("data_time","")}</strong> |
    {"⚠ BACKTEST MODE" if meta.get("is_backtest") else "✅ LIVE DATA"} |
    Analyzed: <strong>{meta.get("total_symbols",0)}</strong> symbols |
    Active: <strong>{len(active)}</strong> |
    Premium: <strong>{len(premium)}</strong> |
    TOP picks: <strong>{len(top_picks)}</strong>
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
  <strong style="color:#8899aa">DISCLAIMER:</strong> This is not financial advice. Trading involves risk.
  Generated: {meta.get("report_time","")}
</div>

</body>
</html>"""

# ============================================================
# MAIN
# ============================================================

def main():
    global OUTPUT_DIR
    print("=" * 60)
    print("  Crypto Wyckoff + SMC Scanner v9.1")
    print("=" * 60)

    choice = input("\nDane teraźniejsze czy historyczne? (t=teraz / h=historyczne): ").strip().lower()

    is_backtest = False
    data_time   = datetime.now().strftime("%Y-%m-%d %H:%M")

    if choice == "h":
        is_backtest = True
        date_str = input("Podaj datę i czas (dd/mm/yyyy hh:mm): ").strip()
        try:
            dt_obj            = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
            st.BACKTEST_TIME_MS = int(dt_obj.timestamp() * 1000)
            data_time          = dt_obj.strftime("%Y-%m-%d %H:%M")
            print(f"Tryb backtest: {data_time}")
        except ValueError:
            print("Nieprawidłowy format — używam danych teraźniejszych.")
            is_backtest         = False
            st.BACKTEST_TIME_MS = None

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n[1/4] Pobieranie top {st.TOP_N} kryptowalut...")
    top_crypto = st.get_top_crypto(st.TOP_N)
    if not top_crypto:
        print("Brak symboli. Koniec.")
        return

    print(f"[2/4] Pobieranie danych OHLCV dla {len(top_crypto)} symboli...")
    all_data  = {}
    completed = 0
    t0        = time.time()

    with ThreadPoolExecutor(max_workers=st.MAX_WORKERS) as ex:
        futs = {ex.submit(st.fetch_symbol_data, c): c for c in top_crypto}
        for fut in as_completed(futs):
            sym, data = fut.result()
            if data:
                all_data[sym] = data
            completed += 1
            if completed % 50 == 0 or completed == len(top_crypto):
                print(f"  {completed}/{len(top_crypto)} ({completed/len(top_crypto)*100:.0f}%) — {time.time()-t0:.0f}s")

    print(f"  Pobrano {len(all_data)} symboli w {time.time()-t0:.1f}s")

    print("[3/4] Analiza market regime...")
    btc_data   = all_data.get("BTCUSDT")
    eth_data   = all_data.get("ETHUSDT")
    regime     = st.market_regime(btc_data, eth_data)
    btc_regime = st._trend(btc_data["timeframes"].get("1H",[]) if btc_data else [])
    eth_regime = st._trend(eth_data["timeframes"].get("1H",[]) if eth_data else [])
    print(f"  Regime: {regime.upper()} | BTC 1H: {btc_regime} | ETH 1H: {eth_regime}")

    print("[4/4] Analiza setupów...")
    raw_setups = []
    for sym, data in all_data.items():
        s = st.analyze_symbol(sym, data, regime)
        if s:
            raw_setups.append(s)

    print(f"  Wstępnie wykryte setupy: {len(raw_setups)}")

    for s in raw_setups:
        s["ts"]       = st.total_score(s)
        s["mps"]      = st.manual_pick_score(s)
        s["category"] = st.classify(s)
        s["model"]    = st.recommend_model(s)

    active_count = sum(1 for s in raw_setups if s.get("category") not in ("rejected","watchlist"))
    print(f"  Aktywne setupy (po filtracji): {active_count}")

    meta = {
        "report_time":   report_time,
        "data_time":     data_time,
        "is_backtest":   is_backtest,
        "total_symbols": len(all_data),
        "regime":        regime,
        "btc_regime":    btc_regime,
        "eth_regime":    eth_regime,
    }

    html = generate_html(raw_setups, meta)

    ts_str = datetime.now().strftime("%Y_%m_%d_%H%M")
    if is_backtest and st.BACKTEST_TIME_MS:
        bt_ts   = datetime.fromtimestamp(st.BACKTEST_TIME_MS/1000).strftime("%Y_%m_%d_%H%M")
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
        except Exception as e:
            print(f"   Nie udało się otworzyć przeglądarki: {e}")

if __name__ == "__main__":
    main()
