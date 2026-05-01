#!/usr/bin/env python3
"""
Scanner v9.5 — live scan + HTML report.
Użycie: python strategy_wyckoff_9_5/scanner.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import csv
import json
import subprocess
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import strategy as st
import config as cfg

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_THIS_DIR, "output")
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
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
            f'background:{color};color:{text_color};font-size:11px;font-weight:bold;margin:1px">{text}</span>')

def _status_badge(s):
    m = {"at_fvg_and_order_block": ("#00ff8c", "FVG+OB"),
         "at_fvg":                 ("#00e5ff", "AT FVG"),
         "at_order_block":         ("#00ffcc", "AT OB"),
         "confluence_zone":        ("#ffd700", "CONFLUENCE")}
    col, lbl = m.get(s, ("#888", s.upper().replace("_", " ")))
    return _badge(lbl, col)

def _cat_badge(cat):
    m = {"premium_setup":    ("#00ff8c", "PREMIUM",      "#000"),
         "high_quality":     ("#00ffcc", "HIGH QUALITY", "#000"),
         "secondary_quality":("#88ccff", "SECONDARY",    "#000"),
         "watchlist":        ("#4488ff", "WATCHLIST",    "#fff"),
         "rejected":         ("#ff4d4d", "REJECTED",     "#fff")}
    col, lbl, tc = m.get(cat, ("#888", cat.upper(), "#fff"))
    return _badge(lbl, col, tc)

def _macd_badge(m):
    if m == "yes":     return _badge("MACD ✓", "#00ff8c")
    if m == "against": return _badge("MACD ✗", "#ff4d4d", "#fff")
    return _badge("MACD –", "#334", "#fff")

def _verdict_badge(v):
    m = {
        "CLEAN PATH":       ("#00ff8c", "#000"),
        "TARGET POSSIBLE":  ("#00ffcc", "#000"),
        "TARGET DIFFICULT": ("#ffd700", "#000"),
        "TARGET BLOCKED":   ("#ff8844", "#000"),
        "NO FUEL":          ("#ff4d4d", "#fff"),
    }
    col, txt = m.get(v, ("#556677", "#fff"))
    return _badge(v or "N/A", col, txt)

def _base_badge(v):
    m = {
        "VERY_STRONG_BASE": ("#00ff8c", "#000"),
        "STRONG_BASE":      ("#00ffcc", "#000"),
        "NORMAL_BASE":      ("#88ccff", "#000"),
        "WEAK_BASE":        ("#ffcc00", "#000"),
        "CHOP_BASE":        ("#ff8844", "#000"),
        "UNKNOWN_BASE":     ("#556677", "#fff"),
    }
    col, txt = m.get(v, ("#556677", "#fff"))
    return _badge((v or "UNKNOWN_BASE").replace("_", " "), col, txt)

def _family_badge(f):
    m = {
        "WYCKOFF_STRICT":           ("#00ff8c", "#000"),
        "TREND_PULLBACK":           ("#00e5ff", "#000"),
        "LIQUIDITY_SWEEP_REVERSAL": ("#ffd700", "#000"),
        "POC_MEAN_REVERSION":       ("#88ccff", "#000"),
        "COUNTERTREND_ABC":         ("#ff8844", "#000"),
    }
    col, txt = m.get(f, ("#556677", "#fff"))
    return _badge((f or "UNKNOWN").replace("_", " "), col, txt)

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
# PANELE v9.5
# ============================================================

def render_target_panel(s):
    tfs = s.get("target_feasibility_score")
    verdict = s.get("verdict", "N/A")
    score_color = (
        "#00ff8c" if (tfs or 0) >= 70 else
        "#ffd700" if (tfs or 0) >= 55 else
        "#ff8844" if (tfs or 0) >= 40 else
        "#ff4d4d"
    )
    return _panel("🎯 Target Feasibility / Fuel",
        _row("TFS", f'<span style="color:{score_color};font-weight:bold">{tfs if tfs is not None else "N/A"}/100</span>') +
        _row("Verdict",          _verdict_badge(verdict)) +
        _row("Clean Path",       f'{s.get("clean_path_score","N/A")}/20') +
        _row("Liquidity Magnet", f'{s.get("liquidity_magnet_score","N/A")}/18') +
        _row("POC Alignment",    f'{s.get("poc_score","N/A")}/17') +
        _row("Momentum Fuel",    f'{s.get("momentum_score","N/A")}/18') +
        _row("ATR Capacity",     f'{s.get("atr_capacity_score","N/A")}/12') +
        _row("Nearest Obstacle", s.get("nearest_obstacle") or "–") +
        _row("Nearest Magnet",   s.get("nearest_magnet") or "–") +
        _row("TP2 ATR Ratio",    f'{s.get("tp2_atr_ratio","N/A")}')
    )

def render_wyckoff_cause_panel(s):
    wcs  = s.get("wyckoff_cause_score")
    base = s.get("base_strength_label", "UNKNOWN_BASE")
    return _panel("📦 Wyckoff Cause / Base Strength",
        _row("Cause Score",    f'{wcs if wcs is not None else "N/A"}/15') +
        _row("Base Strength",  _base_badge(base)) +
        _row("Range Duration", f'{s.get("range_duration_hours","N/A")}h') +
        _row("Range ATR Ratio",f'{s.get("range_atr_ratio","N/A")}') +
        _row("SOS/SOW Disp",   f'{s.get("sos_sow_displacement_atr","N/A")} ATR') +
        _row("Phase D Age",    f'{s.get("phase_d_age_bars","N/A")} bars') +
        _row("Pullback Depth", f'{s.get("pullback_depth_percent_of_range","N/A")}%') +
        _row("Note",           f'<span style="font-size:11px">{s.get("wyckoff_cause_note","N/A")}</span>')
    )

def render_model_a_plan(s):
    return _panel("🟢 Recommended Management — Model A",
        _row("TP1a",       "1.1R → close 35%") +
        _row("After TP1a", "SL to BE + fee buffer") +
        _row("TP1b",       "2.0R → close 35%") +
        _row("Runner",     "3.0R / HTF target → close 30% or trail") +
        _row("Why Model A", "Best v9.5 backtest profile: higher WR, PF, lower DD")
    )

def render_family_panel(s):
    family  = s.get("setup_family", "WYCKOFF_STRICT")
    variant = s.get("setup_variant", "–")
    ct      = s.get("countertrend", False)
    htf     = s.get("htf_context", "–")
    tp_mode = s.get("recommended_tp_mode", "–")
    reason  = s.get("family_reason", "–")
    risk    = s.get("family_risk", "–")
    return _panel("🏷 Setup Family Logic",
        _row("Family",       _family_badge(family)) +
        _row("Variant",      f'<span style="font-size:11px">{variant}</span>') +
        _row("Countertrend", "⚠ YES" if ct else "NO") +
        _row("Trend Conflict","⚠ YES" if s.get("trend_conflict") else "NO") +
        _row("HTF Context",  htf) +
        _row("LTF Trigger",  s.get("status", "–").replace("_", " ")) +
        _row("Target Mode",  tp_mode) +
        _row("Why applied",  f'<span style="font-size:11px;color:#aaffcc">{reason}</span>') +
        _row("Family risk",  f'<span style="font-size:11px;color:#ffcc88">{risk}</span>')
    )

# ============================================================
# KARTY SETUPÓW
# ============================================================

def render_card(s):
    sym   = s["symbol"]
    d     = s["direction"]
    cat   = s.get("category", "")
    w     = s["wyckoff"]
    fvg   = s["fvg"]
    ob    = s["ob"]
    mps   = s["mps"]
    ts    = s["ts"]
    ee    = s["entry_ext"]
    age   = s["choch_age"]
    pr    = s["price"]
    model = s.get("model", "Model B")

    ee_cls = ("ideal+" if ee <= 0.10 else
              "ideal"  if ee <= 0.25 else
              "acceptable" if ee <= 0.50 else "chased")

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
        mps_row("MACD confirmation", 20 if s["macd_5m"] == "yes" else 0, 20) +
        mps_row("Entry quality", 20 if ee <= 0.10 else (15 if ee <= 0.25 else 5), 20) +
        mps_row("Location", {"at_fvg_and_order_block": 20, "at_fvg": 16, "at_order_block": 14, "confluence_zone": 5}.get(s["status"], 0), 20) +
        mps_row("ChoCH quality", {0: 15, 1: 15, 2: 12, 3: 8, 4: 5}.get(age, 0), 15) +
        mps_row("Pattern quality", 10 if (s["touches_fvg"] or s["touches_ob"]) and "Engulfing" in s["pattern_type"] else 4, 10) +
        mps_row("R:R quality", 10 if s["rr"] >= 3.0 else 7, 10) +
        mps_row("Regime", 5 if s["aligned"] else (-5 if s["aligned"] is False else 0), 5)
    )

    if d == "LONG":
        invalid_price = _f(fvg["fvg_low"], pr) if fvg else _f(w["range_low"], pr)
        invalid_txt   = f"Close 5M below {invalid_price} (FVG filled / powrót do range)"
    else:
        invalid_price = _f(fvg["fvg_high"], pr) if fvg else _f(w["range_high"], pr)
        invalid_txt   = f"Close 5M above {invalid_price} (FVG filled / powrót do range)"

    border = "#00ff8c" if cat == "premium_setup" else "#1e2d40"
    glow   = "0 0 16px rgba(0,255,140,0.3)" if cat == "premium_setup" else "none"

    return f"""
<div style="background:#101827;border-radius:8px;padding:20px;border:1px solid {border};box-shadow:{glow};margin-bottom:16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
    <div>
      <span style="font-size:20px;font-weight:bold;margin-right:8px">{sym}</span>
      {_dir_span(d)} {_cat_badge(cat)} {_family_badge(s.get("setup_family","WYCKOFF_STRICT"))} {_status_badge(s["status"])} {_macd_badge(s["macd_5m"])}
      {_verdict_badge(s.get("verdict",""))} {_base_badge(s.get("base_strength_label","UNKNOWN_BASE"))}
    </div>
    <div style="text-align:right">
      <div style="font-size:26px;font-weight:bold;color:#00ff8c">{mps}</div>
      <div style="font-size:11px;color:#8899aa">MPS | Score: {ts} | TFS: {s.get("target_feasibility_score","N/A")}</div>
      <div style="font-size:11px;color:#8899aa">Model: {model}</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px">
    <div>
      {_panel("📐 Wyckoff",
        _row("Pattern",    w["pattern"]) +
        _row("Phase D",    "✅" if w["phase_d"] else "⏳") +
        _row("Event",      w["event"]) +
        _row("Range High", _f(w["range_high"], pr)) +
        _row("Range Low",  _f(w["range_low"],  pr)) +
        _row("Pullback",   "✅" if w["pullback"] else "❌")
      )}
      {render_wyckoff_cause_panel(s)}
    </div>
    <div>
      {_panel("🕯 ChoCH / Pattern",
        _row("ChoCH 5M",  "✅ Confirmed") +
        _row("Age",        f'{age}c {"🔥" if age<=1 else ("✅" if age<=2 else "⏱")}') +
        _row("Pattern",    s["pattern_type"]) +
        _row("Touches FVG","✅" if s["touches_fvg"] else "❌") +
        _row("Touches OB", "✅" if s["touches_ob"]  else "❌") +
        _row("Vol Bonus",  "✅" if s["vol_bonus"]   else "–")
      )}
      {_panel(f"💰 Trade Plan ({model})",
        _row("Entry",       _f(s["entry_mid"], pr)) +
        _row("Stop Loss",   _f(s["sl"], pr), "#ff4d4d") +
        _row("TP1a (1.1R)", _f(s["tp1a"], pr)) +
        _row("TP1b (2.0R)", _f(s["tp1b"], pr)) +
        _row("TP2 / Runner",_f(s["tp2"], pr), "#00ff8c") +
        _row("R:R",         f'{s["rr"]:.1f}R', "#00ff8c") +
        _row("Risk %",      _pct(s["risk"] / s["entry_mid"])) +
        _row("Entry Ext",   f'{_pct(ee)} ({ee_cls})') +
        _row("FVG",         f'{_f(fvg["fvg_low"],pr)} – {_f(fvg["fvg_high"],pr)}' if fvg else "N/A") +
        _row("OB/FVG Overlap", f'{ob_fvg_pct:.1f}%')
      )}
    </div>
    <div>
      {render_target_panel(s)}
      {render_family_panel(s)}
      {_panel("📊 Manual Pick Score", mps_html +
        f'<div style="display:flex;justify-content:space-between;margin-top:8px">'
        f'<span style="font-weight:bold">TOTAL</span>'
        f'<span style="color:#00ff8c;font-size:18px">{mps}/100</span></div>'
      )}
      {_panel("MACD / Regime",
        _row("MACD 5M",  s["macd_5m"],  "#00ff8c" if s["macd_5m"]=="yes" else ("#ff4d4d" if s["macd_5m"]=="against" else "#8899aa")) +
        _row("MACD 15M", s["macd_15m"], "#00ff8c" if s["macd_15m"]=="yes" else "#8899aa") +
        _row("Regime",   s["regime"]) +
        _row("Aligned",  "✅" if s["aligned"] else ("❌" if s["aligned"] is False else "–"))
      )}
    </div>
  </div>
  {render_model_a_plan(s) if model == "Model A" else ""}
  <div style="background:#0d1520;border-radius:6px;padding:12px;margin-top:10px">
    <div style="color:#00aaff;font-size:13px;font-weight:bold;margin-bottom:8px">⚡ Trigger Checklist v9.5</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px;color:#aabbcc">
      <span>□ Cena w FVG lub OB przy LPS/LPSY</span>
      <span>□ ChoCH na 5M potwierdzony i świeży</span>
      <span>□ Engulfing zamknięty na 5M</span>
      <span>□ MACD divergence nie jest przeciwko setupowi</span>
      <span>□ Entry extension idealnie ≤ 0.25</span>
      <span>□ FVG nie jest zapełnione</span>
      <span>□ R:R do TP2 ≥ 2.0R</span>
      <span>□ TFS ≥ 55 (TARGET POSSIBLE lub lepsza)</span>
    </div>
    <div style="margin-top:8px;color:#ffcc00;font-size:12px"><strong>Invalidation:</strong> {invalid_txt}</div>
  </div>
</div>"""

def render_top_pick(s, rank):
    sym   = s["symbol"]
    mps   = s["mps"]
    ts    = s["ts"]
    cat   = s.get("category", "")
    model = s.get("model", "Model B")
    pr    = s["price"]
    tfs   = s.get("target_feasibility_score")
    verdict = s.get("verdict", "")
    base  = s.get("base_strength_label", "UNKNOWN_BASE")

    reasons = []
    if s["macd_5m"] == "yes":
        reasons.append("MACD divergence confirmed")
    if s["status"] == "at_fvg_and_order_block":
        reasons.append("FVG + OB confluence")
    elif s["status"] == "at_fvg":
        reasons.append("Price at FVG")
    if s["choch_age"] <= 1:
        reasons.append("🔥 Fresh ChoCH (≤1c)")
    elif s["choch_age"] <= 2:
        reasons.append("Fresh ChoCH (2c)")
    if s["entry_ext"] <= 0.10:
        reasons.append("Ideal+ entry extension")
    if s["aligned"]:
        reasons.append("Regime aligned")
    if (tfs or 0) >= 70:
        reasons.append("Strong target feasibility / fuel")
    elif (tfs or 0) >= 55:
        reasons.append("Target feasible")
    if verdict in ("CLEAN PATH", "TARGET POSSIBLE"):
        reasons.append(f"Target verdict: {verdict}")
    if base in ("NORMAL_BASE", "STRONG_BASE", "VERY_STRONG_BASE"):
        reasons.append(f"Base strength: {base.replace('_',' ')}")
    if s.get("wyckoff_cause_score", 0) >= 9:
        reasons.append("Good Wyckoff cause / base strength")

    risks = []
    if s["macd_5m"] == "none":
        risks.append("No MACD confirmation")
    if s["choch_age"] >= 3:
        risks.append(f"ChoCH {s['choch_age']}c old")
    if s["entry_ext"] > 0.25:
        risks.append(f"Entry extension {_pct(s['entry_ext'])}")
    if s["status"] == "confluence_zone":
        risks.append("Confluence zone only")
    if not (s["touches_fvg"] or s["touches_ob"]):
        risks.append("Pattern doesn't touch FVG/OB")
    if (tfs or 100) < 70:
        risks.append(f"TFS only {tfs}/100")
    if verdict in ("TARGET DIFFICULT", "TARGET BLOCKED", "NO FUEL"):
        risks.append(f"Target risk: {verdict}")
    if base in ("WEAK_BASE", "CHOP_BASE"):
        risks.append(f"Base risk: {base.replace('_',' ')}")
    if s.get("nearest_obstacle"):
        risks.append(f"Nearest obstacle: {s['nearest_obstacle']}")
    if not risks:
        risks.append("Standard execution risk")

    tfs_color = ("#00ff8c" if (tfs or 0) >= 70 else
                 "#ffd700" if (tfs or 0) >= 55 else
                 "#ff8844" if (tfs or 0) >= 40 else "#ff4d4d")

    return f"""
<div style="background:linear-gradient(135deg,#001a0d,#002818);border:2px solid #00ff8c;border-radius:10px;padding:20px;margin-bottom:16px;box-shadow:0 0 24px rgba(0,255,140,0.35)">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:14px">
    <div>
      <span style="font-size:13px;color:#00ff8c;font-weight:bold">#{rank} TOP PICK</span>
      <span style="font-size:22px;font-weight:bold;margin:0 8px">{sym}</span>
      {_dir_span(s["direction"])} {_cat_badge(cat)} {_status_badge(s["status"])} {_macd_badge(s["macd_5m"])}
      {_verdict_badge(verdict)} {_base_badge(base)}
    </div>
    <div style="text-align:right">
      <div style="font-size:32px;font-weight:bold;color:#00ff8c">{mps}</div>
      <div style="font-size:11px;color:#8899aa">Manual Pick Score | Total: {ts}</div>
      <div style="font-size:13px;color:{tfs_color};font-weight:bold">TFS: {tfs if tfs is not None else "N/A"}/100</div>
      <div style="font-size:11px;color:#8899aa">Model: {model}</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px">
    {_panel(f"💰 Trade Plan ({model})",
      _row("Entry",       _f(s["entry_mid"], pr)) +
      _row("Stop Loss",   _f(s["sl"], pr), "#ff4d4d") +
      _row("TP1a (1.1R)", _f(s["tp1a"], pr)) +
      _row("TP1b (2.0R)", _f(s["tp1b"], pr)) +
      _row("TP2/Runner",  _f(s["tp2"], pr), "#00ff8c") +
      _row("R:R",         f'{s["rr"]:.1f}R', "#00ff8c") +
      _row("Risk %",      _pct(s["risk"] / s["entry_mid"]))
    )}
    {_panel("📐 Setup",
      _row("Wyckoff",    s["wyckoff"]["pattern"]) +
      _row("ChoCH Age",  f'{s["choch_age"]}c') +
      _row("Entry Ext",  _pct(s["entry_ext"])) +
      _row("Pattern",    s["pattern_type"]) +
      _row("MACD 5M",    s["macd_5m"]) +
      _row("WCS",        f'{s.get("wyckoff_cause_score","N/A")}/15') +
      _row("Model",      model)
    )}
    {_panel("🏷 Family",
      _row("Family",     _family_badge(s.get("setup_family","WYCKOFF_STRICT"))) +
      _row("Variant",    f'<span style="font-size:11px">{s.get("setup_variant","–")}</span>') +
      _row("Countertrend","⚠ YES" if s.get("countertrend") else "NO") +
      _row("HTF Context",s.get("htf_context","–")) +
      _row("Target Mode",s.get("recommended_tp_mode","–")) +
      _row("Why",        f'<span style="font-size:11px;color:#aaffcc">{s.get("family_reason","–")}</span>') +
      _row("Risk",       f'<span style="font-size:11px;color:#ffcc88">{s.get("family_risk","–")}</span>')
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
        d    = s["direction"]
        dc   = "#00ff8c" if d == "LONG" else "#ff4d4d"
        mc   = "#00ff8c" if s["macd_5m"] == "yes" else ("#ff4d4d" if s["macd_5m"] == "against" else "#888")
        pr   = s["price"]
        tfs  = s.get("target_feasibility_score")
        tfc  = ("#00ff8c" if (tfs or 0) >= 70 else
                "#ffd700" if (tfs or 0) >= 55 else
                "#ff8844" if (tfs or 0) >= 40 else "#ff4d4d")
        family = s.get("setup_family", "WYCKOFF_STRICT")
        fc     = {"WYCKOFF_STRICT":"#00ff8c","TREND_PULLBACK":"#00e5ff",
                  "LIQUIDITY_SWEEP_REVERSAL":"#ffd700","POC_MEAN_REVERSION":"#88ccff",
                  "COUNTERTREND_ABC":"#ff8844"}.get(family, "#888")
        rows += f"""<tr>
          <td>{i}</td>
          <td style="font-weight:bold">{s["symbol"]}</td>
          <td style="color:{dc}">{d}</td>
          <td style="color:{fc};font-size:11px">{family.replace("_"," ")}</td>
          <td>{s["wyckoff"]["pattern"]}</td>
          <td>{s.get("category","").replace("_"," ").title()}</td>
          <td style="color:#00ff8c;font-weight:bold">{s["mps"]}</td>
          <td>{s["ts"]}</td>
          <td style="color:{tfc};font-weight:bold">{tfs if tfs is not None else "N/A"}</td>
          <td>{s.get("verdict","N/A")}</td>
          <td>{s.get("wyckoff_cause_score","N/A")}/15</td>
          <td>{(s.get("base_strength_label","N/A") or "N/A").replace("_"," ")}</td>
          <td>{s.get("nearest_obstacle","–") or "–"}</td>
          <td>{s.get("nearest_magnet","–") or "–"}</td>
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
        <tr>
          <th style="padding:8px 6px;text-align:left">#</th>
          <th>Symbol</th><th>Dir</th><th>Family</th><th>Wyckoff</th><th>Category</th>
          <th>MPS</th><th>Score</th>
          <th>TFS</th><th>Verdict</th><th>WCS</th><th>Base</th><th>Obstacle</th><th>Magnet</th>
          <th>Status</th><th>Pattern</th><th>ChoCH</th>
          <th>EntExt</th><th>FVG</th><th>FVG+OB</th>
          <th>Entry</th><th>SL</th><th>TP1a</th><th>TP1b</th><th>TP2</th>
          <th>R:R</th><th>MACD</th><th>Model</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table></div>"""

# ============================================================
# MANAGEMENT NOTE (family-aware)
# ============================================================

def _management_note(s):
    fam     = s.get("setup_family", "WYCKOFF_STRICT")
    tfs     = s.get("target_feasibility_score", 0)
    verdict = s.get("verdict", "")
    if fam == "WYCKOFF_STRICT":
        return "Default: Model A. Runner allowed if clean target."
    if fam == "TREND_PULLBACK":
        if tfs >= 70 and verdict in ("CLEAN PATH", "TARGET POSSIBLE"):
            return "Strong trend pullback: Model B default, FIXED_2R optional manually."
        return "Trend pullback: Model B / 1.5R–2R preferred. No default 3R runner."
    return "Use defensive Model B unless manually confirmed."


def _entry_price(s):
    return s.get("entry_mid") or s.get("entry")


# ============================================================
# WATCHLIST FUEL CANDIDATES
# ============================================================

def _watchlist_fuel_rank(s):
    return (
        s.get("target_feasibility_score", 0),
        s.get("mps", 0),
        s.get("ts", 0),
        1 if s.get("verdict") in ("CLEAN PATH", "TARGET POSSIBLE", "TARGET DIFFICULT") else 0,
        1 if s.get("status") in ("at_fvg", "at_order_block", "at_fvg_and_order_block") else 0,
        -s.get("entry_ext", 1.0),
    )


def _is_fuel_candidate(s):
    if s.get("category") != "watchlist":
        return False
    if s.get("macd_5m") == "against":
        return False
    if s.get("verdict") == "NO FUEL":
        return False
    if s.get("entry_ext", 1.0) > 0.50:
        return False
    if s.get("rr", 0) < 1.2:
        return False
    if s.get("choch_age", 999) > 4:
        return False
    if (s.get("fvg") or {}).get("filled"):
        return False
    return True


def _missing_reasons(s):
    reasons = []
    if s.get("target_feasibility_score", 0) < 55:
        reasons.append(f'TFS only {s.get("target_feasibility_score","N/A")}/100')
    if s.get("verdict") in ("TARGET DIFFICULT", "TARGET BLOCKED", "NO FUEL"):
        reasons.append(f'Target verdict: {s.get("verdict")}')
    if s.get("choch_age", 999) > 2:
        reasons.append(f'ChoCH age {s.get("choch_age")}c')
    if s.get("entry_ext", 0) > 0.25:
        reasons.append(f'Entry extension {_pct(s.get("entry_ext"))}')
    if s.get("status") == "confluence_zone":
        reasons.append("Confluence only — no direct FVG/OB entry status")
    if s.get("macd_5m") == "none":
        reasons.append("MACD neutral / no divergence")
    if not s.get("touches_fvg") and not s.get("touches_ob"):
        reasons.append("Pattern does not touch FVG/OB")
    if s.get("rr", 0) < 2.0:
        reasons.append(f'R:R only {s.get("rr",0):.2f}R')
    if (s.get("fvg") or {}).get("filled"):
        reasons.append("FVG filled")
    if not reasons:
        reasons.append("Manual review required — software classified as watchlist")
    return reasons


def _manual_confirmations_needed(s):
    checks = []
    if s.get("target_feasibility_score", 0) < 55:
        checks.append("Confirm manually that target path is clear enough")
    if s.get("verdict") == "TARGET BLOCKED":
        checks.append("Check if obstacle was already broken or reduce TP target")
    if s.get("verdict") == "TARGET DIFFICULT":
        checks.append("Consider lower TP: 1.2R–1.5R or POC target")
    if s.get("choch_age", 999) > 2:
        checks.append("Confirm price has not moved too far from entry zone")
    if s.get("entry_ext", 0) > 0.25:
        checks.append("Wait for better entry closer to FVG/OB")
    if s.get("status") == "confluence_zone":
        checks.append("Confirm real entry trigger at FVG/OB manually")
    if s.get("macd_5m") == "none":
        checks.append("Check momentum manually — MACD gives no confirmation")
    if not s.get("touches_fvg") and not s.get("touches_ob"):
        checks.append("Confirm pattern candle really reacts from the zone")
    if not checks:
        checks.append("Manual chart confirmation before entry")
    return checks


def render_fuel_candidate_card(s, rank):
    pr       = s.get("price", 0)
    tfs      = s.get("target_feasibility_score")
    verdict  = s.get("verdict", "")
    missing  = _missing_reasons(s)
    checks   = _manual_confirmations_needed(s)
    d_color  = "#00ff8c" if s.get("direction") == "LONG" else "#ff4d4d"
    mgmt     = _management_note(s)

    if (tfs or 0) >= 55 and verdict in ("CLEAN PATH", "TARGET POSSIBLE"):
        mgmt_note = f"Model B default / FIXED_2R optional after manual confirmation. ({mgmt})"
    elif verdict == "TARGET BLOCKED":
        mgmt_note = "⚠ TARGET BLOCKED — use reduced TP (POC / nearest liquidity) or skip."
    else:
        mgmt_note = f"Model B / 1.5R–2R preferred. ({mgmt})"

    missing_html = "".join(f'<li style="margin-bottom:3px;color:#ffcc88">{m}</li>' for m in missing)
    checks_html  = "".join(f'<li style="margin-bottom:3px;color:#aaddff">□ {c}</li>' for c in checks)

    blocked_bar = (
        '<div style="margin-top:8px;background:#2a0a00;border-left:3px solid #ff8844;'
        'padding:8px 10px;border-radius:0 4px 4px 0;font-size:12px;color:#ff8844">'
        '⚠ TARGET BLOCKED — use reduced target or skip trade.</div>'
    ) if verdict == "TARGET BLOCKED" else ""

    return f"""
<div style="background:#101827;border:1px solid #4488ff;border-radius:8px;padding:16px;margin-bottom:14px;box-shadow:0 0 10px rgba(68,136,255,0.15)">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap">
    <div>
      <div style="font-size:11px;color:#88ccff;font-weight:bold;letter-spacing:1px;margin-bottom:3px">
        🔍 MANUAL REVIEW #{rank} — NOT ACTIVE
      </div>
      <div style="font-size:20px;font-weight:bold">
        {s.get("symbol","?")} <span style="color:{d_color}">{s.get("direction","?")}</span>
      </div>
      <div style="margin-top:5px">
        {_family_badge(s.get("setup_family","WYCKOFF_STRICT"))}
        {_cat_badge(s.get("category","watchlist"))}
        {_status_badge(s.get("status",""))}
        {_macd_badge(s.get("macd_5m","none"))}
        {_verdict_badge(verdict)}
      </div>
    </div>
    <div style="text-align:right">
      <div style="font-size:22px;color:#88ccff;font-weight:bold">{s.get("mps","N/A")}</div>
      <div style="font-size:11px;color:#8899aa">MPS | Score: {s.get("ts","N/A")}</div>
      <div style="font-size:12px;color:#ffd700;margin-top:3px">TFS: {tfs if tfs is not None else "N/A"}/100</div>
    </div>
  </div>
  <div style="margin-top:10px;padding:8px 10px;background:#08111d;border-left:3px solid #4488ff;color:#cfe8ff;font-size:12px;border-radius:0 4px 4px 0">
    <strong>⚠ NOT AN ACTIVE SIGNAL</strong> — Take only after confirming missing conditions on the chart.
  </div>
  {blocked_bar}
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:10px">
    <div style="background:#0d1520;border-radius:6px;padding:10px">
      <div style="color:#ffcc00;font-weight:bold;margin-bottom:6px;font-size:12px">⚠ Missing / Weak</div>
      <ul style="padding-left:16px;margin:0;font-size:12px">{missing_html}</ul>
    </div>
    <div style="background:#0d1520;border-radius:6px;padding:10px">
      <div style="color:#88ccff;font-weight:bold;margin-bottom:6px;font-size:12px">🔍 Manual Checks</div>
      <ul style="padding-left:0;margin:0;list-style:none;font-size:12px">{checks_html}</ul>
    </div>
    <div style="background:#0d1520;border-radius:6px;padding:10px">
      <div style="color:#00ffcc;font-weight:bold;margin-bottom:6px;font-size:12px">📌 Conditional Plan</div>
      {_row("Entry",       _f(s.get("entry_mid"), pr))}
      {_row("SL",          _f(s.get("sl"), pr), "#ff4d4d")}
      {_row("TP1a (1.1R)", _f(s.get("tp1a"), pr))}
      {_row("TP1b (2.0R)", _f(s.get("tp1b"), pr))}
      {_row("TP2",         _f(s.get("tp2"), pr), "#00ff8c")}
      {_row("R:R",         f'{s.get("rr",0):.2f}R')}
      {_row("Management",  mgmt_note)}
    </div>
    <div style="background:#0d1520;border-radius:6px;padding:10px">
      <div style="color:#00aaff;font-weight:bold;margin-bottom:6px;font-size:12px">🎯 Target / Fuel</div>
      {_row("TFS",              f'{tfs if tfs is not None else "N/A"}/100')}
      {_row("Verdict",          _verdict_badge(verdict))}
      {_row("Nearest Obstacle", s.get("nearest_obstacle") or "–")}
      {_row("Nearest Magnet",   s.get("nearest_magnet") or "–")}
      {_row("WCS",              f'{s.get("wyckoff_cause_score","N/A")}/15')}
    </div>
  </div>
</div>"""


# ============================================================
# ALERT COOLDOWN STATE
# ============================================================

_ALERT_STATE_FILE = os.path.join(_THIS_DIR, "output", "alert_state.json")


def _load_alert_state():
    if os.path.exists(_ALERT_STATE_FILE):
        try:
            with open(_ALERT_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_alert_state(state):
    try:
        with open(_ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _alert_key(s):
    return f'{s.get("symbol")}_{s.get("direction")}_{s.get("setup_family","UNKNOWN")}'


def _cooldown_status(s, state, now_dt, cooldown_hours):
    """Returns 'NEW', 'SEEN', or 'COOLDOWN'."""
    key = _alert_key(s)
    if key not in state:
        return "NEW"
    last_str = state[key].get("last_seen", "")
    try:
        last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
        hours   = (now_dt - last_dt).total_seconds() / 3600
        return "NEW" if hours >= cooldown_hours else "COOLDOWN"
    except Exception:
        return "NEW"


def _cooldown_badge(status):
    if status == "NEW":
        return _badge("🆕 NEW", "#00ff8c", "#000")
    if status == "COOLDOWN":
        return _badge("🔄 COOLDOWN", "#ffd700", "#000")
    return _badge("SEEN", "#4488ff", "#fff")


# ============================================================
# RAPORT HTML
# ============================================================

def _live_rank_key(s):
    return st.rank_key_v95(s)

def generate_html(all_setups, meta, now_dt=None, alert_state=None):
    if now_dt is None:
        now_dt = datetime.now()
    if alert_state is None:
        alert_state = {}

    premium   = [s for s in all_setups if s.get("category") == "premium_setup"]
    hq        = [s for s in all_setups if s.get("category") == "high_quality"]
    sec       = [s for s in all_setups if s.get("category") == "secondary_quality"]
    watchlist = [s for s in all_setups if s.get("category") == "watchlist"]
    rejected  = [s for s in all_setups if s.get("category") == "rejected"]

    active = sorted(premium + hq + sec, key=_live_rank_key, reverse=True)[:cfg.MAX_ACTIVE_SETUPS_HTML]

    top_picks = [
        s for s in active
        if s.get("mps", 0) >= 70
        and (s.get("target_feasibility_score") or 0) >= 55
        and s.get("verdict") not in ("TARGET BLOCKED", "NO FUEL")
    ][:cfg.MAX_TOP_PICKS]

    # Cooldown status for top picks
    for s in top_picks:
        s["_alert_status"] = _cooldown_status(s, alert_state, now_dt, cfg.ALERT_COOLDOWN_HOURS)

    # Watchlist fuel candidates
    fuel_candidates = sorted(
        [s for s in watchlist if _is_fuel_candidate(s)],
        key=_watchlist_fuel_rank, reverse=True
    )[:cfg.MAX_WATCHLIST_REVIEW]

    regime = meta.get("regime", "unclear")
    rc     = {"bullish": "#00ff8c", "bearish": "#ff4d4d", "mixed": "#ffd700", "chop": "#88ccff"}.get(regime, "#888")
    rt_col = "#fff" if regime in ("bearish", "watchlist") else "#000"

    all_active = premium + hq + sec  # for family counting (untruncated)
    wyckoff_strict_n  = sum(1 for s in all_active if s.get("setup_family") == "WYCKOFF_STRICT")
    trend_pb_n        = sum(1 for s in all_active if s.get("setup_family") == "TREND_PULLBACK")
    other_family_n    = len(all_active) - wyckoff_strict_n - trend_pb_n

    stats_data = [
        ("Top Picks",       len(top_picks),  "#00ff8c"),
        ("Premium",         len(premium),    "#00ff8c"),
        ("High Quality",    len(hq),         "#00ffcc"),
        ("Secondary",       len(sec),        "#88ccff"),
        ("Watchlist",        len(watchlist),          "#4488ff"),
        ("WL Review",        len(fuel_candidates),    "#88ccff"),
        ("Rejected",         len(rejected),           "#ff4d4d"),
        ("Wyckoff Strict",   wyckoff_strict_n,        "#00ff8c"),
        ("Trend Pullback",  trend_pb_n,      "#00e5ff"),
        ("Other Family",    other_family_n,  "#88ccff"),
        ("Fuel ON",         "YES",           "#00ff8c"),
        ("TFS ≥ 70",        sum(1 for s in active if (s.get("target_feasibility_score") or 0) >= 70), "#00ff8c"),
        ("TFS 55–69",       sum(1 for s in active if 55 <= (s.get("target_feasibility_score") or 0) < 70), "#ffd700"),
        ("Clean/Possible",  sum(1 for s in active if s.get("verdict") in ("CLEAN PATH", "TARGET POSSIBLE")), "#00ffcc"),
        ("Strong Base",     sum(1 for s in active if s.get("base_strength_label") in ("STRONG_BASE", "VERY_STRONG_BASE")), "#00ff8c"),
        ("Normal Base",     sum(1 for s in active if s.get("base_strength_label") == "NORMAL_BASE"), "#88ccff"),
        ("Chop Base",       sum(1 for s in active if s.get("base_strength_label") == "CHOP_BASE"), "#ff8844"),
        ("Model A",         sum(1 for s in active if s.get("model") == "Model A"), "#00ff8c"),
        ("Model B",         sum(1 for s in active if s.get("model") == "Model B"), "#ffd700"),
        ("MACD Yes",        sum(1 for s in active if s["macd_5m"] == "yes"), "#00ff8c"),
        ("ChoCH ≤ 2c",      sum(1 for s in active if s["choch_age"] <= 2),  "#00ffcc"),
        ("EntExt ≤ 0.25",   sum(1 for s in active if s["entry_ext"] <= 0.25), "#88ccff"),
        ("At FVG+OB",       sum(1 for s in active if s.get("status") == "at_fvg_and_order_block"), "#00ff8c"),
        ("Avg MPS",         f'{sum(s["mps"] for s in active)/len(active):.0f}' if active else "N/A", "#00ff8c"),
    ]
    stats_html = "".join(
        f'<div style="background:#101827;border:1px solid #1e2d40;border-radius:6px;padding:12px;text-align:center">'
        f'<div style="font-size:22px;font-weight:bold;color:{col}">{val}</div>'
        f'<div style="font-size:11px;color:#8899aa;margin-top:4px">{label}</div></div>'
        for label, val, col in stats_data
    )

    if top_picks:
        top1_badge = _cooldown_badge(top_picks[0].get("_alert_status","NEW"))
        picks_html = (
            f'<h3 style="color:#00ff8c;margin-bottom:12px">🏆 TOP 1 SETUP OF THE DAY {top1_badge}</h3>'
            + render_top_pick(top_picks[0], 1)
        )
        if len(top_picks) > 1:
            picks_html += '<h3 style="color:#00ff8c;margin-bottom:12px;margin-top:20px">TOP 3 MANUAL PICKS</h3>'
            for i, p in enumerate(top_picks[1:], 2):
                badge = _cooldown_badge(p.get("_alert_status","NEW"))
                picks_html += f'<div style="margin-bottom:4px">{badge}</div>' + render_top_pick(p, i)
    else:
        picks_html = '<div style="text-align:center;padding:40px;color:#556677;font-size:18px">⚠ No high-confidence manual trade today.</div>'

    cards_html = "".join(render_card(s) for s in active)

    # Fuel candidates section
    if cfg.ENABLE_WATCHLIST_REVIEW and fuel_candidates:
        fc_cards = "".join(render_fuel_candidate_card(s, i + 1) for i, s in enumerate(fuel_candidates))
        fuel_review_html = f"""
<div style="padding:0 32px 24px">
  <h2 style="color:#88ccff;border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:14px">
    🔍 Top Watchlist Fuel Candidates (Manual Review Only)
  </h2>
  <div style="background:#08111d;border-left:4px solid #4488ff;padding:10px 14px;margin-bottom:14px;color:#cfe8ff;font-size:13px;border-radius:0 6px 6px 0">
    <strong>NOT active signals.</strong> Strongest watchlist setups for manual chart verification.
    Take only after confirming missing condition — use reduced targets if target is blocked.
  </div>
  {fc_cards}
</div>"""
    else:
        fuel_review_html = ""

    # Family summary breakdown
    _family_order = [
        ("WYCKOFF_STRICT",           "#00ff8c", "#000"),
        ("TREND_PULLBACK",           "#00e5ff", "#000"),
        ("LIQUIDITY_SWEEP_REVERSAL", "#ffd700", "#000"),
        ("POC_MEAN_REVERSION",       "#88ccff", "#000"),
        ("COUNTERTREND_ABC",         "#ff8844", "#000"),
    ]
    family_rows = ""
    for fam, bg, tc in _family_order:
        fam_setups = [s for s in all_active if s.get("setup_family") == fam]
        if not fam_setups:
            continue
        avg_tfs = sum(s.get("target_feasibility_score") or 0 for s in fam_setups) / len(fam_setups)
        avg_mps = sum(s.get("mps", 0) for s in fam_setups) / len(fam_setups)
        premium_n = sum(1 for s in fam_setups if s.get("category") == "premium_setup")
        hq_n = sum(1 for s in fam_setups if s.get("category") == "high_quality")
        family_rows += (
            f'<tr>'
            f'<td><span style="background:{bg};color:{tc};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold">{fam.replace("_"," ")}</span></td>'
            f'<td style="text-align:center">{len(fam_setups)}</td>'
            f'<td style="text-align:center">{premium_n}</td>'
            f'<td style="text-align:center">{hq_n}</td>'
            f'<td style="text-align:center">{avg_tfs:.0f}</td>'
            f'<td style="text-align:center">{avg_mps:.0f}</td>'
            f'<td style="text-align:center">{", ".join(s["symbol"] for s in sorted(fam_setups, key=lambda x: x.get("mps",0), reverse=True)[:5])}</td>'
            f'</tr>'
        )
    family_summary_html = (
        f'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px">'
        f'<thead style="background:#0d1520;color:#8899aa">'
        f'<tr><th style="padding:8px 6px;text-align:left">Family</th>'
        f'<th>Active</th><th>Premium</th><th>HQ</th>'
        f'<th>Avg TFS</th><th>Avg MPS</th><th>Top Symbols</th></tr></thead>'
        f'<tbody>{family_rows}</tbody></table></div>'
        if family_rows else "<p style='color:#556677'>No active setups by family.</p>"
    )

    watchlist_rows = "".join(
        f'<tr><td style="font-weight:bold">{s["symbol"]}</td>'
        f'<td style="color:{"#00ff8c" if s["direction"]=="LONG" else "#ff4d4d"}">{s["direction"]}</td>'
        f'<td>{s["wyckoff"]["pattern"]}</td><td>{s["ts"]}</td>'
        f'<td style="color:#00ff8c">{s.get("target_feasibility_score","N/A")}</td>'
        f'<td>{s.get("verdict","N/A")}</td>'
        f'<td style="color:#8899aa;font-size:12px">'
        + (", ".join(filter(None, [
            "MACD unconfirmed"     if s["macd_5m"] != "yes" else "",
            f'ChoCH {s["choch_age"]}c' if s["choch_age"] > 2 else "",
            f'EntExt {_pct(s["entry_ext"])}' if s["entry_ext"] > 0.25 else "",
            "Confluence zone"      if s["status"] == "confluence_zone" else "",
            f'TFS {s.get("target_feasibility_score")}' if (s.get("target_feasibility_score") or 0) < 55 else "",
            s.get("verdict", "")   if s.get("verdict") in ("TARGET BLOCKED", "NO FUEL") else "",
            s.get("base_strength_label", "").replace("_", " ")
                                   if s.get("base_strength_label") in ("WEAK_BASE", "CHOP_BASE") else "",
        ])) or "General weakness") +
        f'</td></tr>'
        for s in watchlist[:20]
    )

    reject_rows = "".join(
        f'<tr><td>{s["symbol"]}</td>'
        f'<td style="color:{"#00ff8c" if s["direction"]=="LONG" else "#ff4d4d"}">{s["direction"]}</td>'
        f'<td style="color:#8899aa;font-size:12px">'
        + (", ".join(filter(None, [
            "MACD against"    if s["macd_5m"] == "against" else "",
            f'Score {s["ts"]}' if s["ts"] < 73 else "",
            "Chased entry"    if s["entry_ext"] > 0.50 else "",
            f'ChoCH {s["choch_age"]}c' if s["choch_age"] > 4 else "",
            f'R:R {s["rr"]:.2f}' if s["rr"] < 2.0 else "",
            "Pin bar"         if "Pin" in s["pattern_type"] else "",
            "NO FUEL"         if s.get("verdict") == "NO FUEL" else "",
            "TARGET BLOCKED"  if s.get("verdict") == "TARGET BLOCKED" else "",
            f'TFS {s.get("target_feasibility_score")}' if (s.get("target_feasibility_score") or 0) < 40 else "",
            "FVG filled"      if s.get("fvg", {}).get("filled") else "",
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

    _html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Crypto Wyckoff + SMC {meta.get("scanner_version","v9.5")} — {meta.get("data_time","")}</title>
  <style>{css}</style>
</head>
<body>

<div style="background:#0d1520;border-bottom:2px solid #00ff8c;padding:24px 32px">
  <h1 style="font-size:22px;color:#00ff8c;margin-bottom:8px">📊 Crypto Wyckoff + SMC Scanner {meta.get("scanner_version","v9.5")}</h1>
  <div style="color:#8899aa;font-size:12px;margin-bottom:8px">
    Report: <strong>{meta.get("report_time","")}</strong> |
    Data: <strong>{meta.get("data_time","")}</strong> |
    {"⚠ BACKTEST MODE" if meta.get("is_backtest") else "✅ LIVE DATA"} |
    Analyzed: <strong>{meta.get("total_symbols",0)}</strong> symbols |
    Active: <strong>{len(active)}</strong> |
    Premium: <strong>{len(premium)}</strong> |
    TOP picks: <strong>{len(top_picks)}</strong>
  </div>
  <div style="font-size:12px;color:#aabbcc;display:flex;gap:20px;flex-wrap:wrap;margin-bottom:8px">
    <span>Fuel Filter: <strong style="color:{'#00ff8c' if meta.get('fuel_filter') else '#ff8844'}">{'ON' if meta.get('fuel_filter') else 'OFF'}</strong></span>
    <span>Default management: <strong style="color:#00ff8c">family-aware — Model A for Wyckoff/strongest setups, Model B defensive</strong></span>
    <span>Entry mode: <strong>Manual / Immediate confirmation</strong></span>
    <span>Limit entry: <strong style="color:#ffcc00">not preferred by current backtest</strong></span>
  </div>
  <div style="margin-top:10px;font-size:11px;background:#061b12;border-left:3px solid #00ff8c;padding:8px 10px;display:inline-block;color:#aaffcc">
    <strong>{meta.get("scanner_version","v9.5")} Live Workflow:</strong>
    TOP1/TOP3 → manual confirmation → immediate entry → Model A management.
    Limit entry is not preferred by current backtest.
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

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">🏷 Setup Family Summary</h2>
  {family_summary_html}
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

{fuel_review_html}

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">👁 Watchlist</h2>
  {"<div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr><th>Symbol</th><th>Dir</th><th>Wyckoff</th><th>Score</th><th>TFS</th><th>Verdict</th><th>Missing</th></tr></thead><tbody>" + watchlist_rows + "</tbody></table></div>" if watchlist_rows else "<p style='color:#556677'>No watchlist setups.</p>"}
</div>

<div style="padding:0 32px 24px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">❌ Rejected (sample)</h2>
  {"<div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr><th>Symbol</th><th>Dir</th><th>Reason</th></tr></thead><tbody>" + reject_rows + "</tbody></table></div>" if reject_rows else "<p style='color:#556677'>No rejected setups to display.</p>"}
</div>

<div style="background:#0a0f1a;border-top:1px solid #1e2d40;padding:20px 32px;font-size:11px;color:#556677">
  <strong style="color:#8899aa">DISCLAIMER:</strong> This is not financial advice. Trading involves risk.
  strategy_wyckoff_9_5 {meta.get("scanner_version","v9.5")} | Fuel Filter {'ON' if meta.get('fuel_filter') else 'OFF'} | Generated: {meta.get("report_time","")}
</div>

</body>
</html>"""
    return _html, top_picks, fuel_candidates

# ============================================================
# MAIN
# ============================================================

def main():
    global OUTPUT_DIR

    st.USE_TARGET_FEASIBILITY_FILTER = cfg.USE_TARGET_FEASIBILITY_FILTER

    print("=" * 60)
    print(f"  Crypto Wyckoff + SMC Scanner {cfg.SCANNER_VERSION}")
    print(f"  Engine: {cfg.ENGINE} | Fuel Filter: {'ON' if cfg.USE_TARGET_FEASIBILITY_FILTER else 'OFF'} | Model: family-aware")
    print("  Families: WYCKOFF_STRICT + TREND_PULLBACK")
    print(f"  Cooldown: {cfg.ALERT_COOLDOWN_HOURS}h | WL Review: {'ON' if cfg.ENABLE_WATCHLIST_REVIEW else 'OFF'}")
    print("=" * 60)

    now_dt = datetime.now()

    choice = input("\nDane teraźniejsze czy historyczne? (t=teraz / h=historyczne): ").strip().lower()

    is_backtest = False
    data_time   = now_dt.strftime("%Y-%m-%d %H:%M")

    if choice == "h":
        is_backtest = True
        date_str = input("Podaj datę i czas (dd/mm/yyyy hh:mm): ").strip()
        try:
            dt_obj              = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
            st.BACKTEST_TIME_MS = int(dt_obj.timestamp() * 1000)
            data_time           = dt_obj.strftime("%Y-%m-%d %H:%M")
            print(f"Tryb backtest: {data_time}")
        except ValueError:
            print("Nieprawidłowy format — używam danych teraźniejszych.")
            is_backtest         = False
            st.BACKTEST_TIME_MS = None

    report_time = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Load cooldown state (skip in backtest mode — no point tracking history)
    alert_state = {} if is_backtest else _load_alert_state()

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
    btc_regime = st._trend(btc_data["timeframes"].get("1H", []) if btc_data else [])
    eth_regime = st._trend(eth_data["timeframes"].get("1H", []) if eth_data else [])
    print(f"  Regime: {regime.upper()} | BTC 1H: {btc_regime} | ETH 1H: {eth_regime}")

    print(f"[4/4] Analiza setupów ({cfg.SCANNER_VERSION} multi-family)...")
    raw_setups = []
    for sym, data in all_data.items():
        s = st.analyze_symbol_v95(sym, data, regime)
        if s:
            raw_setups.append(s)

    print(f"  Wstępnie wykryte setupy: {len(raw_setups)}")

    for s in raw_setups:
        s["ts"]       = st.total_score(s)
        s["mps"]      = st.manual_pick_score(s)
        s["category"] = st.classify_v95(s)
        s["model"]    = st.recommend_model_v95(s)

    active_count = sum(1 for s in raw_setups if s.get("category") not in ("rejected", "watchlist"))
    print(f"  Aktywne setupy (po filtracji): {active_count}")

    meta = {
        "report_time":    report_time,
        "data_time":      data_time,
        "is_backtest":    is_backtest,
        "total_symbols":  len(all_data),
        "regime":         regime,
        "btc_regime":     btc_regime,
        "eth_regime":     eth_regime,
        "scanner_version": cfg.SCANNER_VERSION,
        "fuel_filter":    st.USE_TARGET_FEASIBILITY_FILTER,
    }

    html, top_picks, fuel_candidates = generate_html(raw_setups, meta, now_dt=now_dt, alert_state=alert_state)

    ts_str = now_dt.strftime("%Y_%m_%d_%H%M")
    if is_backtest and st.BACKTEST_TIME_MS:
        bt_ts   = datetime.fromtimestamp(st.BACKTEST_TIME_MS / 1000).strftime("%Y_%m_%d_%H%M")
        outfile = os.path.join(OUTPUT_DIR, f"signals_{bt_ts}_backtest.html")
    else:
        outfile = os.path.join(OUTPUT_DIR, f"signals_{ts_str}.html")

    if cfg.WRITE_TIMESTAMPED_HTML:
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n✅ Raport: {outfile}")

    if cfg.WRITE_LATEST_HTML and not is_backtest:
        latest_path = os.path.join(OUTPUT_DIR, "latest.html")
        with open(latest_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"   → latest.html aktualizowany")

    print(f"   Aktywnych setupów: {active_count}")

    if not is_backtest:
        # ── alerts.json ────────────────────────────────────────
        if cfg.WRITE_ALERTS_JSON:
            def _alert_entry(s):
                return {
                    "symbol":        s["symbol"],
                    "direction":     s["direction"],
                    "setup_family":  s.get("setup_family", "WYCKOFF_STRICT"),
                    "setup_variant": s.get("setup_variant", ""),
                    "category":      s.get("category", ""),
                    "mps":           s.get("mps", 0),
                    "tfs":           s.get("target_feasibility_score"),
                    "verdict":       s.get("verdict", ""),
                    "model":         s.get("model", ""),
                    "entry":         _entry_price(s),
                    "sl":            s.get("sl"),
                    "tp2":           s.get("tp2"),
                    "rr":            s.get("rr"),
                    "alert_status":  s.get("_alert_status", ""),
                }
            alerts_payload = {
                "generated_at": report_time,
                "engine":       cfg.SCANNER_VERSION,
                "top_picks":    [_alert_entry(s) for s in top_picks],
                "watchlist_review": [_alert_entry(s) for s in fuel_candidates],
            }
            alerts_path = os.path.join(OUTPUT_DIR, "alerts.json")
            with open(alerts_path, "w", encoding="utf-8") as f:
                json.dump(alerts_payload, f, indent=2)
            print(f"   → alerts.json zapisany ({len(top_picks)} top picks, {len(fuel_candidates)} WL review)")

        # ── alert_state.json update ────────────────────────────
        ts_now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        for s in top_picks:
            key = _alert_key(s)
            alert_state[key] = {
                "last_seen":     ts_now,
                "last_mps":      s.get("mps"),
                "last_tfs":      s.get("target_feasibility_score"),
                "last_category": s.get("category"),
                "last_verdict":  s.get("verdict"),
            }
        _save_alert_state(alert_state)

        # ── scan_log.csv (append) ──────────────────────────────
        if cfg.WRITE_SCAN_LOG_CSV:
            _CSV_LOG_FIELDS = [
                "timestamp", "symbol", "direction", "setup_family", "setup_variant",
                "category", "mps", "ts", "tfs", "verdict", "model",
                "entry", "sl", "tp1a", "tp1b", "tp2", "rr",
                "status", "macd_5m", "choch_age", "entry_ext",
                "trend_conflict", "base_strength_label",
                "watchlist_review_candidate", "alert_status",
            ]
            log_path = os.path.join(OUTPUT_DIR, "scan_log.csv")
            write_header = not os.path.exists(log_path)
            _active_cats = {"premium_setup", "high_quality", "secondary_quality"}
            fuel_syms    = {s["symbol"] for s in fuel_candidates}
            setups_to_log = (
                [s for s in raw_setups if s.get("category") in _active_cats]
                + fuel_candidates
            )
            with open(log_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=_CSV_LOG_FIELDS, extrasaction="ignore")
                if write_header:
                    w.writeheader()
                for s in setups_to_log:
                    w.writerow({
                        "timestamp":                  report_time,
                        "symbol":                     s.get("symbol", ""),
                        "direction":                  s.get("direction", ""),
                        "setup_family":               s.get("setup_family", "WYCKOFF_STRICT"),
                        "setup_variant":              s.get("setup_variant", ""),
                        "category":                   s.get("category", ""),
                        "mps":                        s.get("mps", 0),
                        "ts":                         s.get("ts", 0),
                        "tfs":                        s.get("target_feasibility_score", ""),
                        "verdict":                    s.get("verdict", ""),
                        "model":                      s.get("model", ""),
                        "entry":                      _entry_price(s) or "",
                        "sl":                         s.get("sl", ""),
                        "tp1a":                       s.get("tp1a", ""),
                        "tp1b":                       s.get("tp1b", ""),
                        "tp2":                        s.get("tp2", ""),
                        "rr":                         s.get("rr", ""),
                        "status":                     s.get("status", ""),
                        "macd_5m":                    s.get("macd_5m", ""),
                        "choch_age":                  s.get("choch_age", ""),
                        "entry_ext":                  s.get("entry_ext", ""),
                        "trend_conflict":             s.get("trend_conflict", False),
                        "base_strength_label":        s.get("base_strength_label", ""),
                        "watchlist_review_candidate": s["symbol"] in fuel_syms,
                        "alert_status":               s.get("_alert_status", ""),
                    })
            print(f"   → scan_log.csv dopisano {len(setups_to_log)} wpisów")

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
