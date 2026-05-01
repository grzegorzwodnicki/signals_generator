#!/usr/bin/env python3
"""
Wave 3 Backtester

Usage:
    python strategy_find_3rd_wave/backtest.py

Interactive:
  - symbol (e.g. BTC or BTCUSDT)
  - timeframe (e.g. 15m, 1H, 4H)
  - start / end date  (dd/mm/yyyy)
  - TP mode: t2 only, or t1 + t2 (50% each)

Output:
  - results/backtest/trades_{symbol}_{tf}_{start}_{end}.csv
  - results/backtest/backtest_{symbol}_{tf}_{start}_{end}.html  (auto-opens)
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import csv
import subprocess
import webbrowser
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

import data as dt
from strategy import (
    StrategyConfig,
    StrategyResult,
    analyze_strategy,
    normalize_ohlcv_dataframe,
)

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_THIS_DIR, "results", "backtest")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DEBUG = os.environ.get("DEBUG_WAVE3", "0") == "1"

# ---------------------------------------------------------------------------
# Config used for backtest runs
# ---------------------------------------------------------------------------

BACKTEST_CONFIG = StrategyConfig(
    swing_left=3,
    swing_right=3,
    min_wave1_atr=2.0,
    poc_distance_atr=1.0,
    require_bos=True,
    max_bars_after_bos=0,   # TASK 4: enter only on the exact BOS candle
    max_wave1_bars=100,
    max_correction_bars=150,
    max_bars_after_d=50,
    require_harmonic=False,
)

MIN_WINDOW = 100   # minimum candles before we start evaluating


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def _simulate_trade(
    df: pd.DataFrame,
    result: StrategyResult,
    signal_bar: int,
    tp_mode: str,          # "t2" or "t1t2"
) -> Dict:
    """
    Simulate a trade forward from signal_bar using SL and TP levels.
    Returns a trade dict.

    tp_mode "t2"   → single target at target_2, full position.
    tp_mode "t1t2" → 50% off at target_1, 50% off at target_2.
    """
    entry  = result.entry_price
    sl     = result.stop_loss
    tp1    = result.target_1
    tp2    = result.target_2
    direction = result.direction

    if entry is None or sl is None or tp2 is None:
        return _open_trade_row(result, signal_bar, df)

    risk = abs(entry - sl)
    if risk == 0:
        return _open_trade_row(result, signal_bar, df)

    partial_hit = False
    partial_r   = 0.0
    exit_bar    = None
    exit_price  = None
    exit_reason = "open"

    for i in range(signal_bar + 1, len(df)):
        bar_high = df["high"].iloc[i]
        bar_low  = df["low"].iloc[i]

        if direction == "LONG":
            sl_hit  = bar_low  <= sl
            tp1_hit = (tp1 is not None) and (bar_high >= tp1)
            tp2_hit = bar_high >= tp2
        else:
            sl_hit  = bar_high >= sl
            tp1_hit = (tp1 is not None) and (bar_low  <= tp1)
            tp2_hit = bar_low  <= tp2

        # TASK 2: SL always wins — check SL before any TP
        if sl_hit:
            exit_bar    = i
            exit_price  = sl
            exit_reason = "sl"
            break

        # TP2 hit (full exit)
        if tp2_hit:
            exit_bar    = i
            exit_price  = tp2
            exit_reason = "tp2"
            break

        # Partial TP1 (t1t2 mode) — only reached if SL and TP2 not hit this bar
        if tp_mode == "t1t2" and tp1_hit and not partial_hit:
            partial_hit = True
            partial_r   = abs(tp1 - entry) / risk * 0.5  # 50% of position

    # If we never closed, mark open
    if exit_reason == "open":
        exit_bar    = len(df) - 1
        exit_price  = df["close"].iloc[-1]
        exit_reason = "close_at_end"

    # Calculate P&L in R
    full_r = abs(exit_price - entry) / risk
    if direction == "LONG":
        won = exit_price > entry
    else:
        won = exit_price < entry

    if exit_reason == "sl":
        pnl_r = -1.0
    elif exit_reason in ("tp2", "close_at_end"):
        sign  = 1.0 if won else -1.0
        pnl_r = sign * full_r
    else:
        pnl_r = 0.0

    if tp_mode == "t1t2" and partial_hit:
        # TP1 was taken on 50%; pnl_r covers the remaining 50%
        remaining_r = pnl_r * 0.5
        pnl_r = partial_r + remaining_r
    # If tp_mode == "t1t2" but no partial was hit, pnl_r is already the full-position result

    bars_held = exit_bar - signal_bar

    return _build_trade_row(
        result=result,
        signal_bar=signal_bar,
        df=df,
        exit_bar=exit_bar,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_r=round(pnl_r, 4),
        bars_held=bars_held,
    )


def _open_trade_row(result: StrategyResult, signal_bar: int, df: pd.DataFrame) -> Dict:
    return _build_trade_row(
        result=result,
        signal_bar=signal_bar,
        df=df,
        exit_bar=None,
        exit_price=None,
        exit_reason="open",
        pnl_r=None,
        bars_held=None,
    )


def _build_trade_row(
    result: StrategyResult,
    signal_bar: int,
    df: pd.DataFrame,
    exit_bar: Optional[int],
    exit_price: Optional[float],
    exit_reason: str,
    pnl_r: Optional[float],
    bars_held: Optional[int],
) -> Dict:
    sig_ts = df["timestamp"].iloc[signal_bar] if signal_bar < len(df) else None
    exit_ts = df["timestamp"].iloc[exit_bar] if exit_bar is not None and exit_bar < len(df) else None
    wave = result.wave

    return {
        "setup_id":             result.setup_id,
        "signal":               result.signal,
        "direction":            result.direction,
        "signal_bar":           signal_bar,           # backtest bar where trade opened
        "signal_time":          str(sig_ts),          # timestamp of that bar
        "signal_index":         result.signal_index,  # TASK 3: BOS/D candle from strategy
        "strategy_signal_time": str(result.signal_time) if result.signal_time is not None else "",
        "exit_bar":             exit_bar,
        "exit_time":            str(exit_ts) if exit_ts is not None else "",
        "entry_price":          result.entry_price,
        "stop_loss":            result.stop_loss,
        "target_1":             result.target_1,
        "target_2":             result.target_2,
        "target_3":             result.target_3,
        "exit_price":           exit_price,
        "exit_reason":          exit_reason,
        "pnl_r":                pnl_r,
        "bars_held":            bars_held,
        "rr_t1":                result.risk_reward_to_t1,
        "rr_t2":                result.risk_reward_to_t2,
        "bos_index":            result.bos_index,
        "bos_price":            result.bos_price,
        "reason":               result.reason,
        "x_price":              wave.x_price      if wave else None,
        "a_price":              wave.a_price      if wave else None,
        "d_price":              wave.d_price      if wave else None,
        "retracement":          wave.retracement  if wave else None,
        "harmonic_valid":       wave.harmonic_valid if wave else None,
        "poc_price":            wave.poc_price    if wave else None,
    }


# ---------------------------------------------------------------------------
# Candle-by-candle replay
# ---------------------------------------------------------------------------

def run_backtest(
    df_full: pd.DataFrame,
    symbol: str,
    tf: str,
    config: StrategyConfig,
    tp_mode: str = "t2",
) -> List[Dict]:
    """
    Replay df_full candle by candle.

    At each bar i (starting at MIN_WINDOW):
      1. Feed df_full.iloc[:i+1] to analyze_strategy()
      2. If signal is LONG_SETUP or SHORT_SETUP AND signal_index == i (fresh):
         a. Check setup_id not already traded
         b. Simulate forward
    """
    df_full = normalize_ohlcv_dataframe(df_full)
    n = len(df_full)
    trades: List[Dict] = []
    seen_ids: set = set()

    print(f"\nReplaying {n} candles for {symbol} {tf} …")

    for i in range(MIN_WINDOW, n):
        if i % 500 == 0:
            print(f"  bar {i}/{n} …")

        window = df_full.iloc[: i + 1].copy()

        try:
            result = analyze_strategy(window, config)
        except Exception as e:
            if DEBUG:
                print(f"  [ERROR] bar={i}: {e}")
            continue

        if result.signal not in ("LONG_SETUP", "SHORT_SETUP"):
            continue

        # Enter only on the exact signal candle.
        # With max_bars_after_bos=0, signal_index == the BOS/D candle == i
        # when the signal is fresh, so this check is always safe.
        if result.signal_index != i:
            continue

        sid = result.setup_id
        if sid is not None and sid in seen_ids:
            continue
        if sid is not None:
            seen_ids.add(sid)

        trade = _simulate_trade(df_full, result, i, tp_mode)
        trades.append(trade)
        print(
            f"  [{result.signal}] bar={i} "
            f"entry={result.entry_price:.4f} "
            f"sl={result.stop_loss:.4f} "
            f"tp2={result.target_2:.4f} "
            f"→ {trade['exit_reason']} pnl={trade['pnl_r']}"
        )

    print(f"  Done. Trades found: {len(trades)}")
    return trades


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_stats(trades: List[Dict]) -> Dict:
    closed = [t for t in trades if t["pnl_r"] is not None and t["exit_reason"] != "open"]
    if not closed:
        return {"n_trades": 0, "n_closed": 0}

    pnls = [t["pnl_r"] for t in closed]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_r  = round(sum(pnls), 4)
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
    avg_win  = round(sum(wins)   / len(wins),   4) if wins   else 0.0
    avg_loss = round(sum(losses) / len(losses), 4) if losses else 0.0
    ev       = round(total_r / len(closed), 4)

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    pf = round(gross_profit / gross_loss, 4) if gross_loss > 0 else float("inf")

    # Max drawdown (running sum)
    peak = 0.0
    eq   = 0.0
    max_dd = 0.0
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd

    bars_held_list = [t["bars_held"] for t in closed if t["bars_held"] is not None]
    avg_bars = round(sum(bars_held_list) / len(bars_held_list), 1) if bars_held_list else 0.0

    longs  = [t for t in closed if t["direction"] == "LONG"]
    shorts = [t for t in closed if t["direction"] == "SHORT"]
    long_wr  = round(len([t for t in longs  if t["pnl_r"] > 0]) / len(longs)  * 100, 1) if longs  else 0.0
    short_wr = round(len([t for t in shorts if t["pnl_r"] > 0]) / len(shorts) * 100, 1) if shorts else 0.0

    return {
        "n_trades":     len(trades),
        "n_closed":     len(closed),
        "n_open":       len(trades) - len(closed),
        "n_wins":       len(wins),
        "n_losses":     len(losses),
        "win_rate":     win_rate,
        "total_r":      total_r,
        "ev_per_trade": ev,
        "avg_win_r":    avg_win,
        "avg_loss_r":   avg_loss,
        "profit_factor": pf,
        "max_dd_r":     round(max_dd, 4),
        "avg_bars_held": avg_bars,
        "n_longs":      len(longs),
        "n_shorts":     len(shorts),
        "long_win_rate":  long_wr,
        "short_win_rate": short_wr,
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def save_csv(trades: List[Dict], path: str) -> None:
    if not trades:
        print(f"  No trades to save.")
        return
    fieldnames = list(trades[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trades)
    print(f"  CSV saved: {path}")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def _stat_row(label: str, value) -> str:
    return f"<tr><td>{label}</td><td><b>{value}</b></td></tr>"


def generate_html(
    trades: List[Dict],
    stats: Dict,
    symbol: str,
    tf: str,
    start_dt: datetime,
    end_dt: datetime,
    tp_mode: str,
) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    pf_color = "#4caf50" if stats.get("profit_factor", 0) >= 1.5 else \
               "#ff9800" if stats.get("profit_factor", 0) >= 1.0 else "#f44336"
    wr_color = "#4caf50" if stats.get("win_rate", 0) >= 50 else "#f44336"
    ev_color = "#4caf50" if stats.get("ev_per_trade", 0) > 0 else "#f44336"

    stats_html = f"""
    <table class="stats-table">
        {_stat_row("Trades (total)", stats.get("n_trades", 0))}
        {_stat_row("Closed", stats.get("n_closed", 0))}
        {_stat_row("Open / Unfilled", stats.get("n_open", 0))}
        {_stat_row("Wins", stats.get("n_wins", 0))}
        {_stat_row("Losses", stats.get("n_losses", 0))}
        <tr><td>Win Rate</td><td><b style="color:{wr_color}">{stats.get("win_rate", 0):.1f}%</b></td></tr>
        {_stat_row("Total R", f"{stats.get('total_r', 0):+.2f}R")}
        <tr><td>EV per Trade</td><td><b style="color:{ev_color}">{stats.get('ev_per_trade', 0):+.4f}R</b></td></tr>
        <tr><td>Profit Factor</td><td><b style="color:{pf_color}">{stats.get('profit_factor', 0):.2f}</b></td></tr>
        {_stat_row("Avg Win", f"{stats.get('avg_win_r', 0):+.4f}R")}
        {_stat_row("Avg Loss", f"{stats.get('avg_loss_r', 0):+.4f}R")}
        {_stat_row("Max Drawdown", f"{stats.get('max_dd_r', 0):.4f}R")}
        {_stat_row("Avg Bars Held", stats.get("avg_bars_held", 0))}
        {_stat_row("Longs", f"{stats.get('n_longs', 0)} ({stats.get('long_win_rate', 0):.1f}% WR)")}
        {_stat_row("Shorts", f"{stats.get('n_shorts', 0)} ({stats.get('short_win_rate', 0):.1f}% WR)")}
    </table>
    """

    # Trade rows
    def _pnl_color(pnl):
        if pnl is None:
            return "#888"
        return "#4caf50" if pnl > 0 else "#f44336" if pnl < 0 else "#ff9800"

    def _pnl_str(pnl):
        if pnl is None:
            return "—"
        return f"{pnl:+.4f}R"

    trade_rows = ""
    cumulative_r = 0.0
    for i, t in enumerate(trades, 1):
        pnl = t.get("pnl_r")
        if pnl is not None and t.get("exit_reason") != "open":
            cumulative_r += pnl
        pc = _pnl_color(pnl)
        reason_short = (t.get("reason") or "")[:60]
        trade_rows += f"""
        <tr>
            <td>{i}</td>
            <td>{t.get("signal_time", "")[:16]}</td>
            <td>{'🔼' if t.get("direction")=="LONG" else '🔽'} {t.get("direction","")}</td>
            <td>{t.get("entry_price", "")}</td>
            <td>{t.get("stop_loss", "")}</td>
            <td>{t.get("target_2", "")}</td>
            <td>{t.get("exit_price", "") or "—"}</td>
            <td>{t.get("exit_time", "")[:16]}</td>
            <td>{t.get("exit_reason", "")}</td>
            <td style="color:{pc}"><b>{_pnl_str(pnl)}</b></td>
            <td style="color:{'#4caf50' if cumulative_r >= 0 else '#f44336'}">{cumulative_r:+.2f}R</td>
            <td style="font-size:11px;color:#aaa">{reason_short}</td>
        </tr>"""

    exec_assumptions = """
<h2>Execution Assumptions</h2>
<table class="stats-table">
  <tr><td>Entry</td><td><b>BOS candle close</b> (or D-candle close when require_bos=False)</td></tr>
  <tr><td>SL / TP checking</td><td><b>Starts from the next candle</b> after the signal bar</td></tr>
  <tr><td>Intrabar conflict</td><td><b>SL wins</b> — if both SL and TP are touched in the same candle, SL is taken</td></tr>
  <tr><td>Deduplication</td><td>One trade per setup_id (wave timestamps); re-entries blocked by setup_id seen-set</td></tr>
  <tr><td>Open trades at end</td><td>Closed at last candle close, marked <i>close_at_end</i></td></tr>
</table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Wave 3 Backtest — {symbol} {tf}</title>
<style>
  body {{ background:#0d0d0d; color:#e0e0e0; font-family:'Segoe UI',Arial,sans-serif;
          margin:0; padding:20px; }}
  h1   {{ color:#90caf9; margin-bottom:4px; }}
  h2   {{ color:#80cbc4; margin-top:28px; }}
  .meta {{ color:#888; font-size:13px; margin-bottom:24px; }}
  .stats-table {{ border-collapse:collapse; min-width:360px; margin-bottom:32px; }}
  .stats-table td {{ padding:6px 14px; border-bottom:1px solid #222; }}
  .stats-table td:first-child {{ color:#aaa; }}
  .trade-table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  .trade-table th {{ background:#1a1a2e; color:#90caf9; padding:8px 10px;
                     text-align:left; border-bottom:2px solid #333; }}
  .trade-table td {{ padding:6px 10px; border-bottom:1px solid #1a1a1a; }}
  .trade-table tr:hover {{ background:#111; }}
  .no-trades {{ color:#888; font-style:italic; margin:24px 0; }}
</style>
</head>
<body>
<h1>Wave 3 Backtest — {symbol} {tf}</h1>
<div class="meta">
  Period: {start_str} → {end_str} &nbsp;|&nbsp;
  TP mode: {tp_mode} &nbsp;|&nbsp;
  Generated: {now_str}
</div>

{exec_assumptions}

<h2>Summary</h2>
{stats_html if stats.get("n_closed", 0) > 0 else '<p class="no-trades">No closed trades in this period.</p>'}

<h2>Trade Log</h2>
{'<p class="no-trades">No trades found.</p>' if not trades else f"""
<table class="trade-table">
  <thead>
    <tr>
      <th>#</th><th>Signal Time</th><th>Dir</th>
      <th>Entry</th><th>SL</th><th>TP2</th>
      <th>Exit</th><th>Exit Time</th><th>Reason</th>
      <th>PnL (R)</th><th>Cum R</th><th>Setup note</th>
    </tr>
  </thead>
  <tbody>{trade_rows}</tbody>
</table>"""}

</body>
</html>"""


# ---------------------------------------------------------------------------
# Open in browser
# ---------------------------------------------------------------------------

def _open_in_browser(path: str) -> None:
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", path])
        return
    except Exception:
        pass
    try:
        webbrowser.open(f"file://{path}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> datetime:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r} — use dd/mm/yyyy")


def main() -> None:
    print("=" * 60)
    print("  Wave 3 Strategy — Backtester")
    print("=" * 60)

    # Symbol
    sym_raw = input("\nSymbol (e.g. BTC or BTCUSDT): ").strip().upper()
    symbol  = sym_raw if sym_raw.endswith("USDT") else sym_raw + "USDT"

    # Timeframe
    tf = input("Timeframe [15m / 1H / 4H] (default 1H): ").strip() or "1H"

    # Date range
    start_raw = input("Start date (dd/mm/yyyy): ").strip()
    end_raw   = input("End date   (dd/mm/yyyy): ").strip()
    start_dt  = _parse_date(start_raw)
    end_dt    = _parse_date(end_raw)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)

    # TP mode
    tp_raw  = input("TP mode: [t2] = full at TP2, [t1t2] = 50/50 (default t2): ").strip().lower()
    tp_mode = tp_raw if tp_raw in ("t2", "t1t2") else "t2"

    print(f"\nFetching {symbol} {tf} from {start_dt.date()} to {end_dt.date()} …")
    df_raw = dt.fetch_candles_range(symbol, tf, start_ms, end_ms, use_cache=True)

    if df_raw.empty:
        print("ERROR: No data returned. Check symbol / date range.")
        return

    print(f"  Fetched {len(df_raw)} candles.")

    trades = run_backtest(df_raw, symbol, tf, BACKTEST_CONFIG, tp_mode)
    stats  = compute_stats(trades)

    # Display summary
    print("\n── Backtest Summary ──────────────────────────────────────")
    for k, v in stats.items():
        print(f"  {k:<20} {v}")
    print("──────────────────────────────────────────────────────────")

    # File names
    start_tag = start_dt.strftime("%Y%m%d")
    end_tag   = end_dt.strftime("%Y%m%d")
    base_name = f"{symbol}_{tf}_{start_tag}_{end_tag}"
    csv_path  = os.path.join(OUTPUT_DIR, f"trades_{base_name}.csv")
    html_path = os.path.join(OUTPUT_DIR, f"backtest_{base_name}.html")

    save_csv(trades, csv_path)

    html = generate_html(trades, stats, symbol, tf, start_dt, end_dt, tp_mode)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML saved: {html_path}")

    _open_in_browser(html_path)


if __name__ == "__main__":
    main()
