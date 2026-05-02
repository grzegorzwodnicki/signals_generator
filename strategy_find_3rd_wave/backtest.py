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
from utils import normalize_timeframe

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_THIS_DIR, "results", "backtest")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DEBUG = os.environ.get("DEBUG_WAVE3", "0") == "1"

# Default transaction-cost assumptions (applied on entry + exit)
FEE_RATE      = 0.0006   # 0.06% taker fee (Bybit-style)
SLIPPAGE_RATE = 0.0002   # 0.02% estimated slippage

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
    tp_mode: str,           # "t2" or "t1t2"
    fee_rate: float = FEE_RATE,
    slippage_rate: float = SLIPPAGE_RATE,
) -> Dict:
    """
    Simulate a trade forward from signal_bar using SL and TP levels.

    tp_mode "t2"   → single target at target_2, full position.
    tp_mode "t1t2" → 50% off at target_1, 50% off at target_2.

    Costs: cost_r = 2 * entry * (fee_rate + slippage_rate) / risk
    (entry-side + exit-side, symmetric approximation)
    pnl_r_net = pnl_r - cost_r
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

    # Task 2: round-trip cost (entry + exit fees and slippage)
    cost_r    = round(2 * entry * (fee_rate + slippage_rate) / risk, 4)
    pnl_r_net = round(pnl_r - cost_r, 4)

    bars_held = exit_bar - signal_bar

    return _build_trade_row(
        result=result,
        signal_bar=signal_bar,
        df=df,
        exit_bar=exit_bar,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_r=round(pnl_r, 4),
        cost_r=cost_r,
        pnl_r_net=pnl_r_net,
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
        cost_r=None,
        pnl_r_net=None,
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
    cost_r: Optional[float],
    pnl_r_net: Optional[float],
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
        "pnl_r":                pnl_r,        # gross (before fees/slippage)
        "cost_r":               cost_r,
        "pnl_r_net":            pnl_r_net,    # net (after fees/slippage)
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
    allow_overlapping_trades: bool = False,
    fee_rate: float = FEE_RATE,
    slippage_rate: float = SLIPPAGE_RATE,
) -> List[Dict]:
    """
    Replay df_full candle by candle.

    At each bar i (starting at MIN_WINDOW):
      1. Feed df_full.iloc[:i+1] to analyze_strategy()
      2. If signal is LONG_SETUP or SHORT_SETUP AND signal_index == i (fresh):
         a. If allow_overlapping_trades=False, skip while previous trade is open
         b. Check setup_id not already traded
         c. Simulate forward including fees / slippage
    """
    df_full = normalize_ohlcv_dataframe(df_full)
    n = len(df_full)
    trades: List[Dict] = []
    seen_ids: set = set()
    last_exit_bar: int = -1     # used only when allow_overlapping_trades=False

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
        # With max_bars_after_bos=0, signal_index == BOS/D candle == i.
        if result.signal_index != i:
            continue

        # Task 1: one-position-at-a-time gate
        if not allow_overlapping_trades and i <= last_exit_bar:
            continue

        sid = result.setup_id
        if sid is not None and sid in seen_ids:
            continue
        if sid is not None:
            seen_ids.add(sid)

        trade = _simulate_trade(df_full, result, i, tp_mode, fee_rate, slippage_rate)
        trades.append(trade)

        # Update last_exit_bar so next trade waits
        if not allow_overlapping_trades:
            eb = trade.get("exit_bar")
            last_exit_bar = eb if eb is not None else i

        print(
            f"  [{result.signal}] bar={i} "
            f"entry={result.entry_price:.4f} "
            f"sl={result.stop_loss:.4f} "
            f"tp2={result.target_2:.4f} "
            f"→ {trade['exit_reason']} gross={trade['pnl_r']} net={trade['pnl_r_net']}"
        )

    print(f"  Done. Trades found: {len(trades)}")
    return trades


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _pnl_stats(pnls: list, n_total: int) -> dict:
    """Compute a standard set of metrics from a list of closed-trade P&L values."""
    if not pnls:
        return {}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total  = round(sum(pnls), 4)
    ev     = round(total / len(pnls), 4)
    wr     = round(len(wins) / len(pnls) * 100, 1)
    avg_w  = round(sum(wins)   / len(wins),   4) if wins   else 0.0
    avg_l  = round(sum(losses) / len(losses), 4) if losses else 0.0
    gp = sum(wins);  gl = abs(sum(losses))
    pf = round(gp / gl, 4) if gl > 0 else float("inf")
    # Max drawdown
    peak = eq = max_dd = 0.0
    for p in pnls:
        eq += p
        if eq > peak: peak = eq
        dd = peak - eq
        if dd > max_dd: max_dd = dd
    return {
        "win_rate": wr, "total_r": total, "ev_per_trade": ev,
        "avg_win_r": avg_w, "avg_loss_r": avg_l,
        "profit_factor": pf, "max_dd_r": round(max_dd, 4),
    }


def compute_stats(trades: List[Dict]) -> Dict:
    closed = [t for t in trades if t["pnl_r"] is not None and t["exit_reason"] != "open"]
    if not closed:
        return {"n_trades": 0, "n_closed": 0}

    gross_pnls = [t["pnl_r"] for t in closed]
    net_pnls   = [t["pnl_r_net"] for t in closed if t.get("pnl_r_net") is not None]

    gross = _pnl_stats(gross_pnls, len(trades))
    net   = _pnl_stats(net_pnls,   len(trades))

    bars_held_list = [t["bars_held"] for t in closed if t["bars_held"] is not None]
    avg_bars = round(sum(bars_held_list) / len(bars_held_list), 1) if bars_held_list else 0.0

    longs  = [t for t in closed if t["direction"] == "LONG"]
    shorts = [t for t in closed if t["direction"] == "SHORT"]
    long_wr  = round(len([t for t in longs  if t["pnl_r"] > 0]) / len(longs)  * 100, 1) if longs  else 0.0
    short_wr = round(len([t for t in shorts if t["pnl_r"] > 0]) / len(shorts) * 100, 1) if shorts else 0.0

    return {
        "n_trades":      len(trades),
        "n_closed":      len(closed),
        "n_open":        len(trades) - len(closed),
        "n_wins":        len([p for p in gross_pnls if p > 0]),
        "n_losses":      len([p for p in gross_pnls if p <= 0]),
        # gross
        **{k: v for k, v in gross.items()},
        # net (prefixed)
        "net_total_r":      net.get("total_r", 0.0),
        "net_ev_per_trade": net.get("ev_per_trade", 0.0),
        "net_profit_factor": net.get("profit_factor", 0.0),
        "net_win_rate":     net.get("win_rate", 0.0),
        "net_max_dd_r":     net.get("max_dd_r", 0.0),
        # breakdowns
        "avg_bars_held":  avg_bars,
        "n_longs":        len(longs),
        "n_shorts":       len(shorts),
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
    run_params: Optional[Dict] = None,
) -> str:
    rp = run_params or {}
    allow_overlap  = rp.get("allow_overlapping_trades", False)
    fee_rate       = rp.get("fee_rate", FEE_RATE)
    slippage_rate  = rp.get("slippage_rate", SLIPPAGE_RATE)
    cost_total_pct = (fee_rate + slippage_rate) * 2 * 100

    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    def _color_pf(v):
        return "#4caf50" if v >= 1.5 else "#ff9800" if v >= 1.0 else "#f44336"
    def _color_ev(v):
        return "#4caf50" if v > 0 else "#f44336"
    def _color_wr(v):
        return "#4caf50" if v >= 50 else "#f44336"

    gwr = stats.get("win_rate", 0); nwr = stats.get("net_win_rate", 0)
    gpf = stats.get("profit_factor", 0); npf = stats.get("net_profit_factor", 0)
    gev = stats.get("ev_per_trade", 0); nev = stats.get("net_ev_per_trade", 0)

    stats_html = f"""
    <table class="stats-table">
        <tr><th style="color:#aaa;font-weight:normal">Metric</th>
            <th style="color:#90caf9">Gross</th>
            <th style="color:#80cbc4">Net (after costs)</th></tr>
        <tr><td>Trades (total)</td><td colspan="2"><b>{stats.get("n_trades", 0)}</b></td></tr>
        <tr><td>Closed</td><td colspan="2"><b>{stats.get("n_closed", 0)}</b></td></tr>
        <tr><td>Open / Unfilled</td><td colspan="2"><b>{stats.get("n_open", 0)}</b></td></tr>
        <tr><td>Wins / Losses</td><td colspan="2"><b>{stats.get("n_wins",0)} / {stats.get("n_losses",0)}</b></td></tr>
        <tr><td>Win Rate</td>
            <td><b style="color:{_color_wr(gwr)}">{gwr:.1f}%</b></td>
            <td><b style="color:{_color_wr(nwr)}">{nwr:.1f}%</b></td></tr>
        <tr><td>Total R</td>
            <td><b style="color:{_color_ev(stats.get('total_r',0))}">{stats.get('total_r',0):+.2f}R</b></td>
            <td><b style="color:{_color_ev(stats.get('net_total_r',0))}">{stats.get('net_total_r',0):+.2f}R</b></td></tr>
        <tr><td>EV per Trade</td>
            <td><b style="color:{_color_ev(gev)}">{gev:+.4f}R</b></td>
            <td><b style="color:{_color_ev(nev)}">{nev:+.4f}R</b></td></tr>
        <tr><td>Profit Factor</td>
            <td><b style="color:{_color_pf(gpf)}">{gpf:.2f}</b></td>
            <td><b style="color:{_color_pf(npf)}">{npf:.2f}</b></td></tr>
        {_stat_row("Avg Win (gross)", f"{stats.get('avg_win_r', 0):+.4f}R")}
        {_stat_row("Avg Loss (gross)", f"{stats.get('avg_loss_r', 0):+.4f}R")}
        {_stat_row("Max DD (gross)", f"{stats.get('max_dd_r', 0):.4f}R")}
        {_stat_row("Max DD (net)", f"{stats.get('net_max_dd_r', 0):.4f}R")}
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
    cum_gross = 0.0
    cum_net   = 0.0
    for i, t in enumerate(trades, 1):
        pnl      = t.get("pnl_r")
        pnl_net  = t.get("pnl_r_net")
        cost     = t.get("cost_r")
        not_open = t.get("exit_reason") != "open"
        if pnl is not None and not_open:
            cum_gross += pnl
        if pnl_net is not None and not_open:
            cum_net += pnl_net
        pc     = _pnl_color(pnl)
        pc_net = _pnl_color(pnl_net)
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
            <td style="color:#888;font-size:11px">{f"−{cost:.4f}R" if cost is not None else "—"}</td>
            <td style="color:{pc_net}"><b>{_pnl_str(pnl_net)}</b></td>
            <td style="color:{'#4caf50' if cum_net >= 0 else '#f44336'}">{cum_net:+.2f}R</td>
            <td style="font-size:11px;color:#aaa">{reason_short}</td>
        </tr>"""

    position_model_str = (
        "Overlapping trades allowed"
        if allow_overlap else
        "<b>One position at a time</b> — new signals skipped while a trade is open"
    )
    exec_assumptions = f"""
<h2>Execution Assumptions</h2>
<table class="stats-table">
  <tr><td>Entry</td><td><b>BOS candle close</b> (or D-candle close when require_bos=False)</td></tr>
  <tr><td>SL / TP checking</td><td><b>Starts from the next candle</b> after the signal bar</td></tr>
  <tr><td>Intrabar conflict</td><td><b>SL wins</b> — if both SL and TP are touched in the same candle, SL is taken</td></tr>
  <tr><td>Position model</td><td>{position_model_str}</td></tr>
  <tr><td>Deduplication</td><td>One trade per setup_id (wave timestamps); re-entries blocked by setup_id seen-set</td></tr>
  <tr><td>Fee rate</td><td>{fee_rate*100:.3f}% per side (taker)</td></tr>
  <tr><td>Slippage rate</td><td>{slippage_rate*100:.3f}% per side</td></tr>
  <tr><td>Round-trip cost</td><td>≈ {cost_total_pct:.3f}% of entry × 2 sides; expressed in R in each trade row</td></tr>
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
      <th>Gross (R)</th><th>Cost</th><th>Net (R)</th><th>Cum Net</th><th>Setup note</th>
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

def _parse_date(s: str, end_of_day: bool = False) -> datetime:
    """
    Parse a date string.  If end_of_day=True and only a date (no time) is
    provided, the result is set to 23:59:59 UTC so the full day is included.
    """
    date_fmts  = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")
    dt_fmts    = ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S")

    for fmt in dt_fmts:
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    for fmt in date_fmts:
        try:
            dt_obj = datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
            if end_of_day:
                dt_obj = dt_obj.replace(hour=23, minute=59, second=59)
            return dt_obj
        except ValueError:
            continue

    raise ValueError(f"Cannot parse date: {s!r} — use dd/mm/yyyy or dd/mm/yyyy HH:MM")


def main() -> None:
    print("=" * 60)
    print("  Wave 3 Strategy — Backtester")
    print("=" * 60)

    # Symbol
    sym_raw = input("\nSymbol (e.g. BTC or BTCUSDT): ").strip().upper()
    symbol  = sym_raw if sym_raw.endswith("USDT") else sym_raw + "USDT"

    # Timeframe
    tf_raw = input("Timeframe [15m / 1H / 4H / 60 / 240 …] (default 1H): ").strip() or "1H"
    tf = normalize_timeframe(tf_raw)

    # Date range
    start_raw = input("Start date (dd/mm/yyyy or dd/mm/yyyy HH:MM): ").strip()
    end_raw   = input("End date   (dd/mm/yyyy or dd/mm/yyyy HH:MM): ").strip()
    start_dt  = _parse_date(start_raw, end_of_day=False)
    end_dt    = _parse_date(end_raw,   end_of_day=True)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)

    # TP mode
    tp_raw  = input("TP mode: [t2] = full at TP2, [t1t2] = 50/50 (default t2): ").strip().lower()
    tp_mode = tp_raw if tp_raw in ("t2", "t1t2") else "t2"

    # Position model
    ovlp_raw = input("Allow overlapping trades? [y/N] (default N): ").strip().lower()
    allow_overlapping = ovlp_raw == "y"

    run_params = {
        "allow_overlapping_trades": allow_overlapping,
        "fee_rate":      FEE_RATE,
        "slippage_rate": SLIPPAGE_RATE,
    }

    print(f"\nFetching {symbol} {tf} from {start_dt.date()} to {end_dt.date()} …")
    df_raw = dt.fetch_candles_range(symbol, tf, start_ms, end_ms, use_cache=True)

    if df_raw.empty:
        print("ERROR: No data returned. Check symbol / date range.")
        return

    print(f"  Fetched {len(df_raw)} candles.")

    trades = run_backtest(
        df_raw, symbol, tf, BACKTEST_CONFIG, tp_mode,
        allow_overlapping_trades=allow_overlapping,
    )
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

    html = generate_html(trades, stats, symbol, tf, start_dt, end_dt, tp_mode, run_params)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML saved: {html_path}")

    _open_in_browser(html_path)


if __name__ == "__main__":
    main()
