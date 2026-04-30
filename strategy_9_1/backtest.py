#!/usr/bin/env python3
"""
Backtest v9.1 — limit order simulation.
Użycie: python strategy_9_1/backtest.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import time
import threading
import subprocess
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import strategy as st

_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR   = os.path.join(_THIS_DIR, "cache")
RESULTS_DIR = os.path.join(_THIS_DIR, "results")
os.makedirs(CACHE_DIR,   exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

BT_TIMEFRAMES = ["1H", "15m", "5m"]
MAX_WORKERS   = 20
RATE_SEM      = threading.Semaphore(20)

# ============================================================
# MULTI-BATCH CANDLE FETCHING
# ============================================================

_TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000,
    "1D": 86_400_000, "1W": 604_800_000,
}

def fetch_full_candles(symbol, interval, start_ms, end_ms, max_batches=50):
    """Pobiera wszystkie świece między start_ms a end_ms (+ lookback przed start)."""
    lookback_ms = 400 * _TF_MS.get(interval, 3_600_000)
    fetch_from  = start_ms - lookback_ms

    all_raw     = {}
    current_end = end_ms

    for _ in range(max_batches):
        with RATE_SEM:
            batch = st._bybit_candles_raw(symbol, interval, 200, current_end)
        if not batch:
            break

        for c in batch:
            ts = int(c[0])
            all_raw[ts] = c

        oldest_ts = min(int(c[0]) for c in batch)
        if oldest_ts <= fetch_from:
            break
        current_end = oldest_ts - 1
        time.sleep(0.05)

    candles = []
    for ts_raw, c in all_raw.items():
        if ts_raw >= fetch_from:
            candles.append({
                "time":   ts_raw // 1000,
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })

    candles.sort(key=lambda c: c["time"])
    return candles

def _fetch_symbol_for_bt(args):
    symbol, tfs, start_ms, end_ms = args
    result = {}
    for tf in tfs:
        result[tf] = fetch_full_candles(symbol, tf, start_ms, end_ms)
    return symbol, result

def fetch_and_cache(symbols, start_ms, end_ms, cache_file):
    """Pobiera lub wczytuje z cache dane wszystkich symboli."""
    if os.path.exists(cache_file):
        print(f"  Wczytuję cache: {cache_file}")
        with open(cache_file, "r") as f:
            return json.load(f)

    print(f"  Pobieram dane dla {len(symbols)} symboli × {len(BT_TIMEFRAMES)} TF...")
    all_data = {}
    total    = len(symbols)
    done     = 0
    t0       = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        args = [(sym, BT_TIMEFRAMES, start_ms, end_ms) for sym in symbols]
        futs = {ex.submit(_fetch_symbol_for_bt, a): a[0] for a in args}
        for fut in as_completed(futs):
            sym, data = fut.result()
            if data:
                all_data[sym] = data
            done += 1
            if done % 10 == 0 or done == total:
                print(f"    {done}/{total} — {time.time()-t0:.0f}s")

    with open(cache_file, "w") as f:
        json.dump(all_data, f)
    print(f"  Cache zapisany: {cache_file}")
    return all_data

# ============================================================
# SLICE HELPER
# ============================================================

def slice_at(candles, until_ts):
    """Binary search — zwraca świece dostępne do danego timestampu."""
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["time"] <= until_ts:
            lo = mid + 1
        else:
            hi = mid
    return candles[:lo]

def build_symbol_data(raw_data, sym, until_ts):
    """Buduje dict {tf: sliced_candles} + price dla analyze_symbol."""
    tfs = {}
    for tf, candles in raw_data.get(sym, {}).items():
        if isinstance(candles, list):
            tfs[tf] = slice_at(candles, until_ts)
    price = tfs.get("5m", [{}])[-1].get("close", 0) if tfs.get("5m") else 0
    return {
        "price":      price,
        "turnover":   raw_data[sym].get("_turnover", 999_999_999),
        "timeframes": tfs,
    }

# ============================================================
# LIMIT ORDER — FILL CHECK
# ============================================================

def check_pending_fill(pending, candles_5m_all):
    """
    Szuka świecy 5M która dotknęła ceny entry w oknie (signal_ts, ttl_ts].
    LONG fill: low <= entry.  SHORT fill: high >= entry.
    """
    signal_ts = pending["signal_ts"]
    ttl_ts    = pending["ttl_ts"]
    entry     = pending["entry"]
    direction = pending["direction"]

    for c in candles_5m_all:
        if c["time"] <= signal_ts or c["time"] > ttl_ts:
            continue
        if direction == "LONG"  and c["low"]  <= entry:
            return c
        if direction == "SHORT" and c["high"] >= entry:
            return c
    return None

def activate_trade(pending, fill_candle):
    """Zamienia pending_order w aktywny trade po uruchomieniu limitu."""
    trade = {k: v for k, v in pending.items() if k != "ttl_ts"}
    trade["entry_ts"]   = fill_candle["time"]
    trade["entry_time"] = datetime.fromtimestamp(fill_candle["time"]).strftime("%Y-%m-%d %H:%M")
    trade["result"]     = "open"
    trade["exit_time"]  = None
    trade["exit_ts"]    = None
    trade["exit_price"] = None
    trade["pnl_r"]      = None
    trade["tp1a_hit"]   = False
    return trade

# ============================================================
# TRADE EXIT CHECKER
# ============================================================

def check_exits(trade, candles_5m_all):
    """
    TP1a (1.1R) → SL przesuwa się na BE + 0.2%.
    TP2 (3.0R) → pełne zamknięcie (win).
    SL → −1R loss / 0R BE.
    """
    entry_ts  = trade["entry_ts"]
    direction = trade["direction"]
    entry     = trade["entry"]
    sl_orig   = trade["sl"]
    tp1a      = trade["tp1a"]
    tp2       = trade["tp2"]
    risk      = abs(entry - sl_orig)

    sl_current = sl_orig
    tp1a_hit   = False
    be_active  = False
    fee_buffer = entry * 0.002

    future = [c for c in candles_5m_all if c["time"] > entry_ts]

    for c in future:
        if direction == "LONG":
            if c["low"] <= sl_current:
                pnl_r  = 0.0 if be_active else -1.0
                return _exit_result(c, sl_current, "be_exit" if be_active else "loss", pnl_r, risk, entry, tp1a_hit)
            if not tp1a_hit and c["high"] >= tp1a:
                tp1a_hit   = True
                sl_current = entry + fee_buffer
                be_active  = True
            if c["high"] >= tp2:
                return _exit_result(c, tp2, "win_tp2", (tp2 - entry) / risk, risk, entry, tp1a_hit)
        else:
            if c["high"] >= sl_current:
                pnl_r  = 0.0 if be_active else -1.0
                return _exit_result(c, sl_current, "be_exit" if be_active else "loss", pnl_r, risk, entry, tp1a_hit)
            if not tp1a_hit and c["low"] <= tp1a:
                tp1a_hit   = True
                sl_current = entry - fee_buffer
                be_active  = True
            if c["low"] <= tp2:
                return _exit_result(c, tp2, "win_tp2", (entry - tp2) / risk, risk, entry, tp1a_hit)

    return None

def _exit_result(candle, exit_price, result, pnl_r, risk, entry, tp1a_hit):
    return {
        "exit_time":  datetime.fromtimestamp(candle["time"]).strftime("%Y-%m-%d %H:%M"),
        "exit_ts":    candle["time"],
        "exit_price": exit_price,
        "result":     result,
        "pnl_r":      round(pnl_r, 3),
        "tp1a_hit":   tp1a_hit,
    }

# ============================================================
# MAIN BACKTEST LOOP
# ============================================================

def run_backtest(raw_data, symbols, start_dt, end_dt, scan_interval_h):
    """
    Sygnał → pending limit order (TTL = następny skan).
    Jeśli cena dotknie entry → fill → aktywny trade.
    Jeśli nie → anuluj, szukaj nowego sygnału.
    """
    open_trade    = None
    pending_order = None
    trades        = []
    cancelled     = 0

    btc_raw = raw_data.get("BTCUSDT", {})
    eth_raw = raw_data.get("ETHUSDT", {})

    steps = []
    cur = start_dt
    while cur <= end_dt:
        steps.append(cur)
        cur += timedelta(hours=scan_interval_h)

    scan_sec = int(scan_interval_h * 3600)
    print(f"\n  Kroków symulacji: {len(steps)}  |  Symboli: {len(symbols)}")

    for step_i, step_dt in enumerate(steps):
        ts       = int(step_dt.timestamp())
        next_ts  = int(steps[step_i + 1].timestamp()) if step_i + 1 < len(steps) else ts + scan_sec
        step_str = step_dt.strftime("%Y-%m-%d %H:%M")

        # ── 1. Sprawdź fill pending limitu ────────────────────────────
        if pending_order and not open_trade:
            sym        = pending_order["symbol"]
            candles_5m = slice_at(raw_data.get(sym, {}).get("5m", []), ts)
            fill       = check_pending_fill(pending_order, candles_5m)

            if fill:
                open_trade    = activate_trade(pending_order, fill)
                pending_order = None
                print(f"  ✅ FILL  {open_trade['symbol']:12} {open_trade['direction']:5}"
                      f"  limit={open_trade['entry']:.6g}  filled @ {open_trade['entry_time']}")
            else:
                cancelled += 1
                print(f"  ⛔ CANCEL {pending_order['symbol']:12} limit nie uruchomiony — wygasł")
                pending_order = None

        # ── 2. Sprawdź czy trade się zamknął ──────────────────────────
        if open_trade:
            sym         = open_trade["symbol"]
            candles_5m  = slice_at(raw_data.get(sym, {}).get("5m", []), ts)
            exit_result = check_exits(open_trade, candles_5m)

            if exit_result:
                open_trade.update(exit_result)
                trades.append(open_trade)
                icon = "✅" if "win" in exit_result["result"] else ("⚖" if exit_result["result"] == "be_exit" else "❌")
                print(f"  {icon} EXIT  {open_trade['symbol']:12} {open_trade['direction']:5} "
                      f"{exit_result['result']:10}  {exit_result['pnl_r']:+.2f}R  @ {exit_result['exit_time']}")
                open_trade = None
            else:
                continue

        # ── 3. Szukaj sygnałów ────────────────────────────────────────
        btc_sliced = {tf: slice_at(c, ts) for tf, c in btc_raw.items() if isinstance(c, list)} if btc_raw else {}
        eth_sliced = {tf: slice_at(c, ts) for tf, c in eth_raw.items() if isinstance(c, list)} if eth_raw else {}
        regime = st.market_regime(
            {"timeframes": btc_sliced, "price": 0} if btc_sliced else None,
            {"timeframes": eth_sliced, "price": 0} if eth_sliced else None,
        )

        setups = []
        for sym in symbols:
            data = build_symbol_data(raw_data, sym, ts)
            if not data["price"]:
                continue
            setup = st.analyze_symbol(sym, data, regime)
            if setup:
                setup["ts"]       = st.total_score(setup)
                setup["mps"]      = st.manual_pick_score(setup)
                setup["category"] = st.classify(setup)
                setup["model"]    = st.recommend_model(setup)
                if setup["mps"] >= 0 and setup["ts"] >= 0:
                    setups.append(setup)

        setups.sort(key=lambda s: s["mps"], reverse=True)
        top1 = setups[0] if setups else None

        found_str = f"({len(setups)} sygnałów, TOP MPS={top1['mps']})" if top1 else "(brak sygnałów)"
        print(f"  [{step_i+1:3}/{len(steps)}] {step_str}  regime={regime:8}  {found_str}")

        if top1:
            pending_order = {
                "symbol":      top1["symbol"],
                "direction":   top1["direction"],
                "entry":       top1["entry_mid"],
                "sl":          top1["sl"],
                "tp1a":        top1["tp1a"],
                "tp1b":        top1["tp1b"],
                "tp2":         top1["tp2"],
                "rr":          top1["rr"],
                "risk":        top1["risk"],
                "mps":         top1["mps"],
                "ts_score":    top1["ts"],
                "category":    top1.get("category", ""),
                "model":       top1.get("model", ""),
                "wyckoff":     top1["wyckoff"]["pattern"],
                "status":      top1["status"],
                "macd_5m":     top1.get("macd_5m", "none"),
                "choch_age":   top1["choch_age"],
                "entry_ext":   top1["entry_ext"],
                "signal_time": step_str,
                "signal_ts":   ts,
                "ttl_ts":      next_ts,
                "entry_time":  None,
                "entry_ts":    None,
                "exit_time":   None,
                "exit_ts":     None,
                "exit_price":  None,
                "result":      "pending",
                "pnl_r":       None,
                "tp1a_hit":    False,
            }
            icon = "🔵 LONG " if top1["direction"] == "LONG" else "🔴 SHORT"
            print(f"       → LIMIT {icon}  {top1['symbol']:12}  MPS={top1['mps']}  "
                  f"entry={top1['entry_mid']:.6g}  TTL={datetime.fromtimestamp(next_ts).strftime('%Y-%m-%d %H:%M')}")

    if open_trade:
        open_trade["result"]    = "open_at_end"
        open_trade["exit_time"] = end_dt.strftime("%Y-%m-%d %H:%M")
        open_trade["pnl_r"]     = None
        trades.append(open_trade)
        print(f"  ⏳ OPEN  {open_trade['symbol']}  (otwarty na końcu okresu)")

    if pending_order:
        print(f"  ⛔ PENDING {pending_order['symbol']}  (wygasł na końcu okresu — nie uruchomiony)")

    print(f"\n  Łącznie transakcji: {len(trades)}  |  Anulowane limity: {cancelled}")
    return trades

# ============================================================
# STATYSTYKI
# ============================================================

def compute_stats(trades):
    closed = [t for t in trades if t["result"] not in ("open", "open_at_end")]
    if not closed:
        return {}

    wins     = [t for t in closed if t["result"] == "win_tp2"]
    losses   = [t for t in closed if t["result"] == "loss"]
    be_exits = [t for t in closed if t["result"] == "be_exit"]
    total_r  = sum(t["pnl_r"] for t in closed if t["pnl_r"] is not None)

    equity = [0.0]
    for t in closed:
        equity.append(equity[-1] + (t["pnl_r"] or 0))

    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    avg_win  = sum(t["pnl_r"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(t["pnl_r"] for t in losses) / len(losses) if losses else 0

    return {
        "total":         len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "be_exits":      len(be_exits),
        "win_rate":      len(wins) / len(closed) * 100 if closed else 0,
        "total_r":       round(total_r, 2),
        "avg_win_r":     round(avg_win, 2),
        "avg_loss_r":    round(avg_loss, 2),
        "max_dd_r":      round(max_dd, 2),
        "profit_factor": abs(sum(t["pnl_r"] for t in wins) / sum(t["pnl_r"] for t in losses))
                         if losses and any(t["pnl_r"] for t in losses) else None,
        "equity":        equity,
        "open_trades":   len([t for t in trades if t["result"] in ("open","open_at_end")]),
    }

# ============================================================
# HTML REPORT
# ============================================================

def _f(v):
    if v is None: return "N/A"
    try:
        if abs(v) >= 1000: return f"{v:,.2f}"
        if abs(v) >= 1:    return f"{v:.4f}"
        return f"{v:.6f}"
    except Exception:
        return str(v)

def generate_report(trades, stats, meta):
    result_colors = {
        "win_tp2":    "#00ff8c",
        "be_exit":    "#ffd700",
        "loss":       "#ff4d4d",
        "open":       "#88ccff",
        "open_at_end":"#88ccff",
    }

    eq = stats.get("equity", [0])
    if len(eq) > 1:
        svg_w, svg_h = 700, 120
        min_e = min(eq)
        max_e = max(eq)
        rng   = max_e - min_e if max_e != min_e else 1
        pts   = [f"{int(i/(len(eq)-1)*svg_w)},{int(svg_h-(v-min_e)/rng*svg_h)}" for i, v in enumerate(eq)]
        zero_y = int(svg_h - (0 - min_e) / rng * svg_h)
        equity_svg = (
            f'<svg width="{svg_w}" height="{svg_h}" style="background:#0d1520;border-radius:6px;display:block">'
            f'<line x1="0" y1="{zero_y}" x2="{svg_w}" y2="{zero_y}" stroke="#334" stroke-width="1" stroke-dasharray="4"/>'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="#00ff8c" stroke-width="2"/>'
            f'</svg>'
        )
    else:
        equity_svg = "<div style='color:#556677'>Brak zamkniętych transakcji</div>"

    def stat(label, val, col="#e6edf7"):
        return (f'<div style="background:#101827;border:1px solid #1e2d40;border-radius:6px;'
                f'padding:12px;text-align:center">'
                f'<div style="font-size:22px;font-weight:bold;color:{col}">{val}</div>'
                f'<div style="font-size:11px;color:#8899aa;margin-top:4px">{label}</div></div>')

    total_r  = stats.get("total_r",  "N/A")
    win_rate = stats.get("win_rate", 0)
    wr_col   = "#00ff8c" if win_rate >= 50 else "#ff4d4d"
    tr_col   = "#00ff8c" if (isinstance(total_r, (int,float)) and total_r > 0) else "#ff4d4d"
    pf       = stats.get("profit_factor")
    pf_str   = f"{pf:.2f}" if pf else "N/A"

    stats_html = (
        stat("Total Trades",   stats.get("total", 0)) +
        stat("Wins",           stats.get("wins", 0),     "#00ff8c") +
        stat("Losses",         stats.get("losses", 0),   "#ff4d4d") +
        stat("BE Exits",       stats.get("be_exits", 0), "#ffd700") +
        stat("Win Rate",       f"{win_rate:.1f}%",        wr_col) +
        stat("Total R",        f"{total_r:+.2f}R" if isinstance(total_r,(int,float)) else total_r, tr_col) +
        stat("Avg Win R",      f"+{stats.get('avg_win_r',0):.2f}R", "#00ff8c") +
        stat("Avg Loss R",     f"{stats.get('avg_loss_r',0):.2f}R", "#ff4d4d") +
        stat("Max Drawdown R", f"-{stats.get('max_dd_r',0):.2f}R",  "#ff8844") +
        stat("Profit Factor",  pf_str, "#00ff8c" if pf and pf > 1 else "#ff4d4d") +
        stat("Open at End",    stats.get("open_trades", 0), "#88ccff")
    )

    rows = ""
    for i, t in enumerate(trades, 1):
        rc  = result_colors.get(t["result"], "#8899aa")
        dc  = "#00ff8c" if t["direction"] == "LONG" else "#ff4d4d"
        mc  = "#00ff8c" if t.get("macd_5m") == "yes" else "#888"
        r_s = f'{t["pnl_r"]:+.2f}R' if t["pnl_r"] is not None else "open"
        rows += (
            f'<tr>'
            f'<td>{i}</td>'
            f'<td style="font-weight:bold">{t["symbol"]}</td>'
            f'<td style="color:{dc}">{t["direction"]}</td>'
            f'<td style="color:#8899aa;font-size:11px">{t.get("signal_time") or "—"}</td>'
            f'<td>{t.get("entry_time") or "—"}</td>'
            f'<td>{t.get("exit_time") or "—"}</td>'
            f'<td>{_f(t["entry"])}</td>'
            f'<td style="color:#ff4d4d">{_f(t["sl"])}</td>'
            f'<td>{_f(t["tp1a"])}</td>'
            f'<td style="color:#00ff8c">{_f(t["tp2"])}</td>'
            f'<td>{_f(t.get("exit_price"))}</td>'
            f'<td style="color:{rc};font-weight:bold">{t["result"]}</td>'
            f'<td style="color:{rc};font-weight:bold">{r_s}</td>'
            f'<td>{t["mps"]}</td>'
            f'<td style="color:#8899aa;font-size:11px">{t.get("wyckoff","")}</td>'
            f'<td style="color:#8899aa;font-size:11px">{t.get("status","").replace("_"," ")}</td>'
            f'<td style="color:{mc}">{t.get("macd_5m","")}</td>'
            f'<td>{t.get("choch_age","")}</td>'
            f'</tr>'
        )

    css = ("*{box-sizing:border-box;margin:0;padding:0}"
           "body{background:#070b12;color:#e6edf7;font-family:'Segoe UI',Arial,sans-serif;font-size:14px}"
           "th{padding:8px 6px;text-align:left;color:#8899aa;border-bottom:1px solid #1e2d40;white-space:nowrap}"
           "td{padding:6px;border-bottom:1px solid #1a2535;white-space:nowrap}"
           "tr:hover td{background:#0d1520}")

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <title>Backtest v9.1 — {meta['start']} → {meta['end']}</title>
  <style>{css}</style>
</head>
<body>

<div style="background:#0d1520;border-bottom:2px solid #00ff8c;padding:20px 32px">
  <div style="font-size:11px;color:#8899aa;letter-spacing:1px;margin-bottom:4px">BACKTEST REPORT — strategy v9.1</div>
  <h1 style="font-size:24px;color:#e6edf7;margin-bottom:6px">{meta['start']} → {meta['end']}</h1>
  <div style="font-size:12px;color:#8899aa">
    Skan co <strong>{meta['scan_interval_h']}H</strong> |
    Symboli: <strong>{meta['n_symbols']}</strong> |
    Wygenerowano: <strong>{meta['report_time']}</strong> |
    Model: limit order TOP 1 po MPS | TP2 = 3.0R | SL→BE po TP1a (1.1R)
  </div>
</div>

<div style="padding:20px 32px 0">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:16px">📊 Statystyki</h2>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-bottom:20px">
    {stats_html}
  </div>
</div>

<div style="padding:0 32px 20px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:12px">📈 Equity Curve (R)</h2>
  {equity_svg}
</div>

<div style="padding:0 32px 32px">
  <h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:12px">📋 Dziennik transakcji</h2>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead>
        <tr>
          <th>#</th><th>Symbol</th><th>Dir</th>
          <th>Signal Time</th><th>Fill Time</th><th>Exit Time</th>
          <th>Entry</th><th>SL</th><th>TP1a</th><th>TP2</th><th>Exit Price</th>
          <th>Result</th><th>P&L R</th><th>MPS</th>
          <th>Wyckoff</th><th>Status</th><th>MACD</th><th>ChoCH</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>

<div style="background:#0a0f1a;border-top:1px solid #1e2d40;padding:14px 32px;font-size:11px;color:#556677">
  strategy_9_1 backtest | Dane historyczne z Bybit Linear |
  Wyniki historyczne nie gwarantują przyszłych zysków.
  Wygenerowano: {meta['report_time']}
</div>

</body>
</html>"""

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("  Backtest v9.1")
    print("=" * 60)

    start_str = input("\nData START (dd/mm/yyyy hh:mm): ").strip()
    end_str   = input("Data END   (dd/mm/yyyy hh:mm): ").strip()
    try:
        start_dt = datetime.strptime(start_str, "%d/%m/%Y %H:%M")
        end_dt   = datetime.strptime(end_str,   "%d/%m/%Y %H:%M")
    except ValueError:
        print("Nieprawidłowy format daty. Użyj dd/mm/yyyy hh:mm")
        return

    if end_dt <= start_dt:
        print("END musi być późniejszy niż START.")
        return

    scan_h_str = input("Interwał skanowania w godzinach (domyślnie 4): ").strip()
    scan_h     = int(scan_h_str) if scan_h_str.isdigit() else 4

    n_sym_str = input("Ile top symboli skanować (domyślnie 50): ").strip()
    n_sym     = int(n_sym_str) if n_sym_str.isdigit() else 50

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)

    print(f"\nPrzedział: {start_dt} → {end_dt}")
    print(f"Skan co {scan_h}H | Top {n_sym} symboli")

    print("\n[1/3] Pobieranie listy top symboli...")
    top_crypto = st.get_top_crypto(n_sym)
    if not top_crypto:
        print("Brak symboli. Koniec.")
        return

    symbols = [c["symbol"] for c in top_crypto]
    for must_have in ("BTCUSDT", "ETHUSDT"):
        if must_have not in symbols:
            symbols.insert(0, must_have)

    turnover_map = {c["symbol"]: c["turnover"] for c in top_crypto}

    start_tag  = start_dt.strftime("%Y%m%d%H%M")
    end_tag    = end_dt.strftime("%Y%m%d%H%M")
    cache_file = os.path.join(CACHE_DIR, f"bt_{start_tag}_{end_tag}_top{n_sym}.json")

    print(f"\n[2/3] Dane OHLCV (cache: {os.path.basename(cache_file)})...")
    raw_data = fetch_and_cache(symbols, start_ms, end_ms, cache_file)

    for sym in raw_data:
        raw_data[sym]["_turnover"] = turnover_map.get(sym, 999_999_999)

    print(f"\n[3/3] Symulacja...")
    trades = run_backtest(raw_data, symbols, start_dt, end_dt, scan_h)
    stats  = compute_stats(trades)

    print(f"\n{'='*60}")
    print(f"  Wyniki:")
    print(f"  Transakcji: {stats.get('total',0)}  |  Wins: {stats.get('wins',0)}  |  Losses: {stats.get('losses',0)}")
    print(f"  Win rate:   {stats.get('win_rate',0):.1f}%")
    print(f"  Total R:    {stats.get('total_r',0):+.2f}R")
    print(f"  Max DD:     -{stats.get('max_dd_r',0):.2f}R")

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "start":           start_dt.strftime("%Y-%m-%d %H:%M"),
        "end":             end_dt.strftime("%Y-%m-%d %H:%M"),
        "scan_interval_h": scan_h,
        "n_symbols":       len(symbols),
        "report_time":     report_time,
    }
    html    = generate_report(trades, stats, meta)
    outfile = os.path.join(RESULTS_DIR, f"backtest_{start_tag}_{end_tag}.html")

    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ Raport: {outfile}")

    abs_path = os.path.abspath(outfile)
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", abs_path])
    except FileNotFoundError:
        try:
            subprocess.Popen(["open", abs_path])
        except Exception:
            pass

if __name__ == "__main__":
    main()
