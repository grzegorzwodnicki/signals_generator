#!/usr/bin/env python3
"""
Wave 3 Scanner — fetch OHLCV → analyze_strategy → HTML report.

Usage:
    python strategy_find_3rd_wave/scanner.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import data as dt
from strategy import StrategyConfig, StrategyResult, analyze_strategy
from utils import normalize_timeframe

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_THIS_DIR, "results", "scanner")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DEBUG = os.environ.get("DEBUG_WAVE3", "0") == "1"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_TIMEFRAMES = ["15m", "1H", "4H"]
DEFAULT_TOP_N      = 50
MIN_TURNOVER_USD   = 5_000_000

# Candle duration in ms — used to compute start_ms for historical range fetch
_TF_MS: dict = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H":  3_600_000,
    "4H":  14_400_000,
    "1D":  86_400_000,
    "1W":  604_800_000,
}

SCANNER_CONFIG = StrategyConfig(
    swing_left=3,
    swing_right=3,
    min_wave1_atr=2.0,
    poc_distance_atr=1.0,
    require_bos=True,
    max_bars_after_bos=3,
    max_wave1_bars=100,
    max_correction_bars=150,
    max_bars_after_d=50,
)


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _f(v, ref=1.0):
    if v is None:
        return "N/A"
    if ref >= 1000 or abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _badge(text, bg, fg="#fff"):
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'background:{bg};color:{fg};font-size:11px;font-weight:bold">{text}</span>'
    )


def _signal_badge(signal):
    m = {
        "LONG_SETUP":      ("#00ff8c", "#000", "LONG SETUP"),
        "SHORT_SETUP":     ("#ff4d4d", "#fff", "SHORT SETUP"),
        "WAITING_FOR_BOS": ("#ffd700", "#000", "WAITING BOS"),
        "NO_SIGNAL":       ("#334455", "#fff", "NO SIGNAL"),
        "INVALID":         ("#556677", "#fff", "INVALID"),
    }
    bg, fg, label = m.get(signal, ("#888", "#fff", signal))
    return _badge(label, bg, fg)


def _dir_span(d):
    c = "#00ff8c" if d == "LONG" else "#ff4d4d"
    return f'<span style="color:{c};font-weight:bold">{d or "–"}</span>'


def _row(label, val, color="#e6edf7"):
    return (
        f'<div style="display:flex;justify-content:space-between;padding:3px 0;'
        f'border-bottom:1px solid #1a2535">'
        f'<span style="color:#8899aa">{label}</span>'
        f'<span style="color:{color};font-weight:500">{val}</span></div>'
    )


def _panel(title, content):
    return (
        f'<div style="background:#0d1520;border-radius:6px;padding:12px;margin-top:10px">'
        f'<div style="color:#00aaff;font-size:13px;font-weight:bold;margin-bottom:8px">{title}</div>'
        f'{content}</div>'
    )


# ---------------------------------------------------------------------------
# Setup card
# ---------------------------------------------------------------------------

def render_setup_card(info: dict) -> str:
    sym      = info["symbol"]
    tf       = info["timeframe"]
    r: StrategyResult = info["result"]
    w        = r.wave
    signal   = r.signal
    pr       = info.get("price", 0)
    n_candles = info.get("n_candles", 0)

    border = (
        "#00ff8c" if signal == "LONG_SETUP" else
        "#ff4d4d" if signal == "SHORT_SETUP" else
        "#ffd700" if signal == "WAITING_FOR_BOS" else "#1e2d40"
    )
    glow = (
        "0 0 12px rgba(0,255,140,0.25)"  if signal == "LONG_SETUP" else
        "0 0 12px rgba(255,77,77,0.25)"  if signal == "SHORT_SETUP" else
        "none"
    )

    wave_rows = ""
    tp_rows   = ""
    if w:
        wave_rows = (
            _row("Direction",   _dir_span(w.direction)) +
            _row("Retracement", f"{(w.retracement or 0)*100:.1f}%",
                 "#00ff8c" if w.preferred_retracement else "#ffd700") +
            _row("Wave 1 size", _f(w.wave1_size, pr)) +
            _row("POC",         _f(w.poc_price, pr)) +
            _row("Dist to POC", f"{(w.distance_to_poc_atr or 0):.2f} ATR",
                 "#00ff8c" if w.poc_valid else "#ff4d4d") +
            _row("Harmonic",    "✅ AB=CD" if w.harmonic_valid else "–",
                 "#00ff8c" if w.harmonic_valid else "#888") +
            _row("BOS",         "✅" if w.bos_valid else "Pending")
        )

    if r.entry_price:
        rr1 = f"{r.risk_reward_to_t1:.2f}R" if r.risk_reward_to_t1 else "–"
        rr2 = f"{r.risk_reward_to_t2:.2f}R" if r.risk_reward_to_t2 else "–"
        rr3 = f"{r.risk_reward_to_t3:.2f}R" if r.risk_reward_to_t3 else "–"
        tp_rows = (
            _row("Entry",    _f(r.entry_price, pr)) +
            _row("Stop",     _f(r.stop_loss, pr), "#ff4d4d") +
            _row("T1 (1×)",  f"{_f(r.target_1, pr)}  ({rr1})", "#ffd700") +
            _row("T2 (1.27×)", f"{_f(r.target_2, pr)}  ({rr2})", "#00ff8c") +
            _row("T3 (1.62×)", f"{_f(r.target_3, pr)}  ({rr3})", "#00ffcc")
        )

    # Freshness display
    last_bar = n_candles - 1 if n_candles > 0 else 0
    freshness_rows = ""
    if r.bos_index is not None:
        bars_since_bos = last_bar - r.bos_index
        if bars_since_bos == 0:
            freshness_color, freshness_label = "#00ff8c", "🟢 Fresh (0 bars)"
        elif bars_since_bos <= 2:
            freshness_color, freshness_label = "#ffd700", f"🟡 Still valid ({bars_since_bos} bars)"
        else:
            freshness_color, freshness_label = "#ff4d4d", f"🔴 Late/Stale ({bars_since_bos} bars)"
        freshness_rows += _row("Bars since BOS", freshness_label, freshness_color)
    if r.entry_price and pr:
        dist_pct = abs(pr - r.entry_price) / r.entry_price * 100
        dist_color = "#00ff8c" if dist_pct < 0.5 else "#ffd700" if dist_pct < 1.5 else "#ff4d4d"
        dist_dir = "above" if pr > r.entry_price else "below"
        freshness_rows += _row("Distance from entry", f"{dist_pct:.2f}% {dist_dir}", dist_color)
    if pr:
        freshness_rows += _row("Current price", _f(pr, pr))

    meta_rows = (
        _row("Signal bar (BOS/D)", str(r.signal_index or "–")) +
        (f'<div style="color:#555;font-size:11px;padding:2px 0">bos_index={r.bos_index}</div>'
         if r.bos_index is not None else "") +
        _row("Setup ID", f'<span style="font-size:10px;word-break:break-all">{(r.setup_id or "–")[:60]}…</span>')
    )

    return f"""
<div style="background:#101827;border-radius:8px;padding:16px;border:1px solid {border};
            box-shadow:{glow};margin-bottom:12px">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:10px">
    <div>
      <span style="font-size:18px;font-weight:bold;margin-right:6px">{sym}</span>
      <span style="color:#8899aa;font-size:13px;margin-right:6px">{tf}</span>
      {_signal_badge(signal)}
      {_dir_span(r.direction) if r.direction else ""}
    </div>
    <div style="font-size:11px;color:#8899aa;max-width:400px;text-align:right">
      {r.reason}
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px">
    {_panel("📊 Wave Structure", wave_rows) if wave_rows else ""}
    {_panel("💰 Trade Plan",     tp_rows)   if tp_rows   else ""}
    {_panel("⏱ Freshness",      freshness_rows) if freshness_rows else ""}
    {_panel("🔖 Metadata",       meta_rows)}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Full HTML report
# ---------------------------------------------------------------------------

def generate_html(
    all_results: list,
    meta: dict,
) -> str:
    active   = [r for r in all_results if r["result"].signal in ("LONG_SETUP", "SHORT_SETUP")]
    waiting  = [r for r in all_results if r["result"].signal == "WAITING_FOR_BOS"]
    no_sig   = [r for r in all_results if r["result"].signal == "NO_SIGNAL"]
    invalid_ = [r for r in all_results if r["result"].signal == "INVALID"]

    def _stat(label, val, color):
        return (
            f'<div style="background:#101827;border:1px solid #1e2d40;border-radius:6px;'
            f'padding:12px;text-align:center">'
            f'<div style="font-size:24px;font-weight:bold;color:{color}">{val}</div>'
            f'<div style="font-size:11px;color:#8899aa;margin-top:4px">{label}</div></div>'
        )

    stats_html = (
        _stat("Scanned", meta["total_scanned"], "#8899aa") +
        _stat("LONG_SETUP",      sum(1 for r in active if r["result"].direction=="LONG"),  "#00ff8c") +
        _stat("SHORT_SETUP",     sum(1 for r in active if r["result"].direction=="SHORT"), "#ff4d4d") +
        _stat("WAITING_FOR_BOS", len(waiting),  "#ffd700") +
        _stat("No Signal",       len(no_sig),   "#556677") +
        _stat("Invalid",         len(invalid_), "#334455")
    )

    active_html  = "".join(render_setup_card(r) for r in active)
    waiting_html = "".join(render_setup_card(r) for r in waiting[:10])

    css = (
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{background:#070b12;color:#e6edf7;font-family:'Segoe UI',Arial,sans-serif;font-size:14px}"
        "h1,h2,h3{color:#e6edf7}"
        "th{padding:8px 6px;text-align:left;color:#8899aa;border-bottom:1px solid #1e2d40}"
        "td{padding:7px 6px;border-bottom:1px solid #1a2535}"
        "tr:hover td{background:#0d1520}"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Wave 3 Scanner — {meta.get("data_time","")}</title>
  <style>{css}</style>
</head>
<body>

<div style="background:#0d1520;border-bottom:2px solid #00ff8c;padding:20px 28px">
  <h1 style="font-size:20px;color:#00ff8c;margin-bottom:6px">🌊 Crypto Wave 3 Scanner</h1>
  <div style="color:#8899aa;font-size:12px">
    Report: <strong>{meta.get("report_time","")}</strong> |
    Data: <strong>{meta.get("data_time","")}</strong> |
    {"⚠ HISTORICAL MODE" if meta.get("is_backtest") else "✅ LIVE DATA"} |
    Symbols: <strong>{meta.get("total_symbols",0)}</strong> |
    TF: <strong>{", ".join(meta.get("timeframes",[]))}</strong>
  </div>
  {"<div style='margin-top:8px;padding:8px 12px;background:#1a1a00;border:1px solid #665500;border-radius:4px;color:#ffdd88;font-size:12px'>⚠ Historical scan uses the current top symbols list, but OHLCV data ending at the selected historical timestamp. Prices shown are the last close of the fetched data, not live quotes.</div>" if meta.get("is_backtest") else ""}
</div>

<div style="padding:20px 28px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:6px;margin-bottom:12px">📊 Statistics</h2>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px">
    {stats_html}
  </div>
</div>

<div style="padding:0 28px 20px">
  <h2 style="border-bottom:2px solid #00ff8c;padding-bottom:6px;margin-bottom:14px;color:#00ff8c">
    ✅ Active Setups ({len(active)})
  </h2>
  {active_html if active_html else '<p style="color:#556677">No active setups found.</p>'}
</div>

<div style="padding:0 28px 20px">
  <h2 style="border-bottom:1px solid #ffd700;padding-bottom:6px;margin-bottom:14px;color:#ffd700">
    ⏳ Waiting for BOS ({len(waiting)})
  </h2>
  {waiting_html if waiting_html else '<p style="color:#556677">No structures waiting for BOS.</p>'}
</div>

<div style="background:#0a0f1a;border-top:1px solid #1e2d40;padding:16px 28px;font-size:11px;color:#556677">
  <strong style="color:#8899aa">DISCLAIMER:</strong> Not financial advice. Trading involves risk. |
  Wave 3 Scanner | Generated: {meta.get("report_time","")}
</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Per-symbol scanner worker
# ---------------------------------------------------------------------------

def scan_symbol(sym_info: dict, timeframes: list, end_time_ms=None) -> list:
    """Fetch OHLCV for symbol × timeframes, run analyze_strategy, return results."""
    sym     = sym_info["symbol"]
    results = []

    for tf in timeframes:
        try:
            # Use range fetch for historical mode (hits cache first)
            if end_time_ms is not None:
                tf_duration_ms = _TF_MS.get(tf, 900_000)
                start_ms = end_time_ms - 420 * tf_duration_ms
                df = dt.fetch_candles_range(sym, tf, start_ms, end_time_ms, use_cache=True)
            else:
                df = dt.fetch_candles(sym, tf, end_time_ms=None)
            if df.empty or len(df) < 50:
                continue
            # TASK 1: use the last close of the fetched data as the display price,
            # not sym_info["price"] (which is the live ticker, wrong in historical mode).
            analysis_price = float(df["close"].iloc[-1])
            result = analyze_strategy(df, SCANNER_CONFIG)
            results.append({
                "symbol":    sym,
                "timeframe": tf,
                "price":     analysis_price,
                "n_candles": len(df),
                "result":    result,
            })
        except Exception as e:
            if DEBUG:
                print(f"[SCAN ERROR] {sym} {tf}: {e}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  Crypto Wave 3 Scanner")
    print("=" * 60)

    choice = input("\nDane teraźniejsze czy historyczne? (t=teraz / h=historyczne): ").strip().lower()

    is_backtest  = False
    data_time    = datetime.now().strftime("%Y-%m-%d %H:%M")
    end_time_ms  = None

    if choice == "h":
        is_backtest = True
        date_str = input("Podaj datę i czas (dd/mm/yyyy hh:mm): ").strip()
        try:
            dt_obj      = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
            end_time_ms = int(dt_obj.timestamp() * 1000)
            data_time   = dt_obj.strftime("%Y-%m-%d %H:%M")
            print(f"Tryb backtest: {data_time}")
        except ValueError:
            print("Nieprawidłowy format — używam danych teraźniejszych.")

    try:
        n_str = input(f"Liczba symboli [{DEFAULT_TOP_N}]: ").strip()
        n_symbols = int(n_str) if n_str else DEFAULT_TOP_N
    except ValueError:
        n_symbols = DEFAULT_TOP_N

    tf_input   = input(f"Timeframes [{','.join(DEFAULT_TIMEFRAMES)}]: ").strip()
    timeframes = [normalize_timeframe(t.strip()) for t in tf_input.split(",")] if tf_input else DEFAULT_TIMEFRAMES

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n[1/3] Pobieranie top {n_symbols} symboli...")
    symbols = dt.get_top_symbols(n_symbols, MIN_TURNOVER_USD)
    if not symbols:
        print("Brak symboli. Sprawdź połączenie z siecią.")
        return
    print(f"  Pobrano {len(symbols)} symboli.")

    print(f"[2/3] Skanowanie {len(symbols)} symboli × {len(timeframes)} TF ...")
    all_results  = []
    completed    = 0
    t0           = time.time()

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(scan_symbol, s, timeframes, end_time_ms): s for s in symbols}
        for fut in as_completed(futs):
            try:
                res = fut.result()
                all_results.extend(res)
            except Exception as e:
                if DEBUG:
                    sym = futs[fut].get("symbol", "?")
                    print(f"[SCAN FUTURE ERROR] {sym}: {e}")
            completed += 1
            if completed % 10 == 0 or completed == len(symbols):
                print(f"  {completed}/{len(symbols)}  ({time.time()-t0:.0f}s)")

    active_count = sum(1 for r in all_results if r["result"].signal in ("LONG_SETUP","SHORT_SETUP"))
    wait_count   = sum(1 for r in all_results if r["result"].signal == "WAITING_FOR_BOS")
    print(f"  Aktywnych setupów: {active_count}  |  Oczekujących BOS: {wait_count}")

    print("[3/3] Generowanie raportu HTML...")

    # Sort: LONG_SETUP/SHORT_SETUP first, then WAITING_FOR_BOS
    def _rank(r):
        s = r["result"].signal
        if s in ("LONG_SETUP", "SHORT_SETUP"): return 3
        if s == "WAITING_FOR_BOS": return 2
        return 1

    all_results.sort(key=_rank, reverse=True)

    meta = {
        "report_time":   report_time,
        "data_time":     data_time,
        "is_backtest":   is_backtest,
        "total_symbols": len(symbols),
        "total_scanned": len(all_results),
        "timeframes":    timeframes,
    }
    html = generate_html(all_results, meta)

    ts_str = datetime.now().strftime("%Y_%m_%d_%H%M")
    suffix = "_backtest" if is_backtest else ""
    outfile = os.path.join(OUTPUT_DIR, f"scanner_{ts_str}{suffix}.html")

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Raport: {outfile}")

    abs_path = os.path.abspath(outfile)
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", abs_path])
    except FileNotFoundError:
        try:
            subprocess.Popen(["open", abs_path])
        except Exception as e:
            print(f"   Nie udało się otworzyć przeglądarki: {e}")


if __name__ == "__main__":
    main()
