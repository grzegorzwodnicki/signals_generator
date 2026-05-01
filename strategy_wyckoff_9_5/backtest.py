#!/usr/bin/env python3
"""
Backtest v9.1 strict — multi-config × multi-model × conservative/optimistic.

Configurations:
  TOP1       — best 1 active signal per scan (basket_size=1)
  TOP3       — up to 3 concurrent active signals (basket_size=3)
  PREMIUM    — premium_setup only (basket_size=1)
  HQ         — high_quality only (basket_size=1)
  PREMIUM_HQ — premium_setup OR high_quality (basket_size=1)

Models: A / B / Auto
Intrabar modes: conservative (SL wins on conflict) / optimistic (TP wins on conflict)

Limit order TTL = next scan step. Unfilled orders cancelled.

Użycie: python strategy_wyckoff_9_5/backtest.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import csv
import json
import math
import time
import threading
import subprocess
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import strategy as st
import execution as ex

_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR   = os.path.join(_THIS_DIR, "cache")
RESULTS_DIR = os.path.join(_THIS_DIR, "results")
os.makedirs(CACHE_DIR,   exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

# 30m added for Wyckoff Phase D confirmation context
BT_TIMEFRAMES = ["1H", "30m", "15m", "5m"]
MAX_WORKERS   = 20
RATE_SEM      = threading.Semaphore(20)

_TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000,
    "1D": 86_400_000, "1W": 604_800_000,
}

# Only active categories enter the trade queue; watchlist/rejected are excluded
_ACTIVE = {"premium_setup", "high_quality", "secondary_quality"}

# ============================================================
# BACKTEST CONFIG DEFINITIONS
# ============================================================

CONFIGS = {
    "TOP1":       {"top_n": 1,  "categories": _ACTIVE,                          "basket_size": 1, "core_v93": False},
    "TOP3":       {"top_n": 3,  "categories": _ACTIVE,                          "basket_size": 3, "core_v93": False},
    "PREMIUM":    {"top_n": 99, "categories": {"premium_setup"},                 "basket_size": 1, "core_v93": False},
    "HQ":         {"top_n": 99, "categories": {"high_quality"},                  "basket_size": 1, "core_v93": False},
    "PREMIUM_HQ": {"top_n": 99, "categories": {"premium_setup", "high_quality"}, "basket_size": 1, "core_v93": False},
    "CORE_V93":   {"top_n": 99, "categories": _ACTIVE,                          "basket_size": 1, "core_v93": True},
}

MODELS = {
    "A":          ex.simulate_model_a,
    "B":          ex.simulate_model_b,
    "Auto":       ex.simulate_auto_model,
    "FIXED_2R":   ex.simulate_fixed_2r,
    "FIXED_1_5R": ex.simulate_fixed_15r,
    "FIXED_3R":   ex.simulate_fixed_3r,
}

INTRABAR_MODES = {
    "conservative": True,
    "optimistic":   False,
}

# CORE_V93 hard filter — strictest diagnostic subset
def _passes_core_v93(setup):
    fvg_filled = bool((setup.get("fvg") or {}).get("filled", False))
    return (
        setup.get("status")    in {"at_fvg", "at_fvg_and_order_block"}
        and setup.get("entry_ext", 1.0) <= 0.25
        and setup.get("choch_age", 99)  <= 2
        and "Engulfing"        in setup.get("pattern_type", "")
        and setup.get("macd_5m")        != "against"
        and setup.get("ts",    0)       >= 73
        and setup.get("mps",   0)       >= 70
        and not fvg_filled
        and setup.get("rr",    0)       >= 2.0
    )

# ============================================================
# MULTI-BATCH CANDLE FETCHING
# ============================================================

def fetch_full_candles(symbol, interval, start_ms, end_ms, max_batches=50):
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
            all_raw[int(c[0])] = c
        oldest_ts = min(int(c[0]) for c in batch)
        if oldest_ts <= fetch_from:
            break
        current_end = oldest_ts - 1
        time.sleep(0.05)

    candles = [
        {
            "time":   ts // 1000,
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        }
        for ts, c in all_raw.items()
        if ts >= fetch_from
    ]
    candles.sort(key=lambda c: c["time"])
    return candles


def _fetch_symbol(args):
    symbol, tfs, start_ms, end_ms = args
    return symbol, {tf: fetch_full_candles(symbol, tf, start_ms, end_ms) for tf in tfs}


def fetch_and_cache(symbols, start_ms, end_ms, cache_file):
    if os.path.exists(cache_file):
        print(f"  Wczytuję cache: {cache_file}")
        with open(cache_file) as f:
            return json.load(f)

    print(f"  Pobieram dane dla {len(symbols)} symboli × {len(BT_TIMEFRAMES)} TF...")
    all_data = {}
    total, done, t0 = len(symbols), 0, time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(_fetch_symbol, (sym, BT_TIMEFRAMES, start_ms, end_ms)): sym
                for sym in symbols}
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
# SLICE HELPERS
# ============================================================

def slice_at(candles, until_ts):
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["time"] <= until_ts:
            lo = mid + 1
        else:
            hi = mid
    return candles[:lo]


def build_symbol_data(raw_data, sym, until_ts):
    tfs = {
        tf: slice_at(candles, until_ts)
        for tf, candles in raw_data.get(sym, {}).items()
        if isinstance(candles, list)
    }
    price = tfs["5m"][-1]["close"] if tfs.get("5m") else 0
    return {
        "price":      price,
        "turnover":   raw_data.get(sym, {}).get("_turnover", 999_999_999),
        "timeframes": tfs,
    }

# ============================================================
# SIGNAL RANKING  (matches scanner.py order)
# ============================================================

_CAT_TIER = {"premium_setup": 4, "high_quality": 3, "secondary_quality": 2, "watchlist": 1}


def _rank_key(s):
    return (
        s["mps"],
        s["ts"],
        1 if s.get("macd_5m") == "yes" else 0,
        -s.get("entry_ext", 1.0),
        _CAT_TIER.get(s.get("category", ""), 0),
    )

# ============================================================
# LIMIT ORDER — FILL CHECK
# ============================================================

def check_pending_fill(pending, candles_5m_sliced):
    signal_ts = pending["signal_ts"]
    ttl_ts    = pending["ttl_ts"]
    entry     = pending["entry"]
    direction = pending["direction"]
    for c in candles_5m_sliced:
        if c["time"] <= signal_ts or c["time"] > ttl_ts:
            continue
        if direction == "LONG"  and c["low"]  <= entry:
            return c
        if direction == "SHORT" and c["high"] >= entry:
            return c
    return None

# ============================================================
# SINGLE-CONFIG BACKTEST LOOP  (basket-aware)
# ============================================================

def _trade_record(top, step_dt, ts, next_ts, entry_mode):
    """Build the shared trade/order dict from a setup."""
    return {
        "symbol":      top["symbol"],
        "direction":   top["direction"],
        "entry":       top["entry_mid"],
        "entry_mid":   top["entry_mid"],
        "sl":          top["sl"],
        "tp1a":        top.get("tp1a"),
        "tp1b":        top.get("tp1b"),
        "tp2":         top.get("tp2"),
        "tp3":         top.get("tp3"),
        "rr":          top["rr"],
        "risk":        top.get("risk"),
        "mps":         top["mps"],
        "ts_score":    top["ts"],
        "category":    top["category"],
        "model":       top["model"],
        "wyckoff":     top["wyckoff"]["pattern"],
        "status":      top["status"],
        "macd_5m":     top.get("macd_5m", "none"),
        "choch_age":   top["choch_age"],
        "entry_ext":   top["entry_ext"],
        "pattern_type":             top.get("pattern_type", ""),
        "target_feasibility_score":        top.get("target_feasibility_score"),
        "verdict":                         top.get("verdict"),
        "wyckoff_cause_score":             top.get("wyckoff_cause_score"),
        "base_strength_label":             top.get("base_strength_label"),
        "range_duration_hours":            top.get("range_duration_hours"),
        "range_atr_ratio":                 top.get("range_atr_ratio"),
        "sos_sow_displacement_atr":        top.get("sos_sow_displacement_atr"),
        "phase_d_age_bars":                top.get("phase_d_age_bars"),
        "pullback_depth_percent_of_range": top.get("pullback_depth_percent_of_range"),
        "signal_time": step_dt.strftime("%Y-%m-%d %H:%M"),
        "signal_ts":   ts,
        "ttl_ts":      next_ts,
        "entry_mode":  entry_mode,
        "result":      "pending",
        "pnl_r":       None,
        "_setup":      top,
    }


def run_config(
    raw_data, symbols, start_dt, end_dt, scan_interval_h,
    cfg_name, model_name, conservative=True, entry_mode="limit",
    debug_wcs=False,
):
    cfg         = CONFIGS[cfg_name]
    sim_fn      = MODELS[model_name]
    top_n       = cfg["top_n"]
    allowed     = cfg["categories"]
    basket_size = cfg["basket_size"]
    use_core    = cfg.get("core_v93", False)
    market_mode = (entry_mode == "immediate")
    _wcs_printed = 0

    active_trades   = []
    pending_orders  = []
    trades          = []
    concurrent_hist = []

    orders = {"created": 0, "filled": 0, "cancelled": 0}

    btc_raw = raw_data.get("BTCUSDT", {})
    eth_raw = raw_data.get("ETHUSDT", {})

    steps = []
    cur = start_dt
    scan_sec = int(scan_interval_h * 3600)
    while cur <= end_dt:
        steps.append(cur)
        cur += timedelta(hours=scan_interval_h)

    for step_i, step_dt in enumerate(steps):
        ts      = int(step_dt.timestamp())
        next_ts = (int(steps[step_i + 1].timestamp())
                   if step_i + 1 < len(steps)
                   else ts + scan_sec)

        # ── 1. Check fills for pending limit orders (TTL = this step) ──
        newly_active = []
        for p in pending_orders:
            c5m = raw_data.get(p["symbol"], {}).get("5m", [])
            fill = check_pending_fill(p, slice_at(c5m, ts)) if isinstance(c5m, list) else None
            if fill:
                orders["filled"] += 1
                newly_active.append({
                    **{k: v for k, v in p.items() if k != "ttl_ts"},
                    "entry_ts":   fill["time"],
                    "entry_time": datetime.fromtimestamp(fill["time"]).strftime("%Y-%m-%d %H:%M"),
                })
            else:
                orders["cancelled"] += 1
        pending_orders = []
        active_trades.extend(newly_active)

        # ── 2. Check exits for all active trades ─────────────────────
        still_open = []
        for t in active_trades:
            c5m_all = raw_data.get(t["symbol"], {}).get("5m", [])
            after_fill = (
                [c for c in c5m_all if t["entry_ts"] < c["time"] <= ts]
                if isinstance(c5m_all, list) else []
            )
            if after_fill:
                res = sim_fn(t["_setup"], after_fill, conservative)
                if res["exit_reason"] != "open":
                    bar_idx = min(res["bars_held"] - 1, len(after_fill) - 1)
                    t.update({
                        "exit_reason":           res["exit_reason"],
                        "pnl_r":                 res["pnl_r"],
                        "mfe_r":                 res["mfe_r"],
                        "mae_r":                 res["mae_r"],
                        "bars_held":             res["bars_held"],
                        "conservative_intrabar": res["conservative_intrabar"],
                        "exec_model":            res["model"],
                        "exit_time":             datetime.fromtimestamp(
                            after_fill[bar_idx]["time"]
                        ).strftime("%Y-%m-%d %H:%M"),
                    })
                    trades.append(t)
                    continue
            still_open.append(t)
        active_trades = still_open

        concurrent_hist.append(len(active_trades))

        # ── 3. Fill free basket slots ────────────────────────────────
        slots_free = basket_size - len(active_trades)
        if slots_free <= 0:
            continue

        busy = {t["symbol"] for t in active_trades}

        btc_sl = {tf: slice_at(c, ts) for tf, c in btc_raw.items() if isinstance(c, list)} if btc_raw else {}
        eth_sl = {tf: slice_at(c, ts) for tf, c in eth_raw.items() if isinstance(c, list)} if eth_raw else {}
        regime = st.market_regime(
            {"timeframes": btc_sl, "price": 0} if btc_sl else None,
            {"timeframes": eth_sl, "price": 0} if eth_sl else None,
        )

        setups = []
        for sym in symbols:
            if sym in busy:
                continue
            data = build_symbol_data(raw_data, sym, ts)
            if not data["price"]:
                continue
            setup = st.analyze_symbol(sym, data, regime)
            if not setup:
                continue
            setup["ts"]       = st.total_score(setup)
            setup["mps"]      = st.manual_pick_score(setup)
            setup["category"] = st.classify(setup)
            setup["model"]    = st.recommend_model(setup)
            if debug_wcs and _wcs_printed < 5:
                _wcs_printed += 1
                print(
                    f"\n  [WCS] {setup['symbol']} {setup['direction']}"
                    f"  rH={setup['wyckoff'].get('range_high'):.4f}"
                    f"  rL={setup['wyckoff'].get('range_low'):.4f}"
                    f"  dur={setup.get('range_duration_hours')}h"
                    f"  disp_atr={setup.get('sos_sow_displacement_atr')}"
                    f"  pd_age={setup.get('phase_d_age_bars')}"
                    f"  wcs={setup.get('wyckoff_cause_score')}/15"
                    f"  label={setup.get('base_strength_label')}"
                    f"\n         note: {setup.get('wyckoff_cause_note')}"
                )
            if setup["category"] not in allowed:
                continue
            if use_core and not _passes_core_v93(setup):
                continue
            setups.append(setup)

        setups.sort(key=_rank_key, reverse=True)
        candidates = setups[:min(top_n, slots_free)]

        for top in candidates:
            rec = _trade_record(top, step_dt, ts, next_ts, entry_mode)
            orders["created"] += 1

            if market_mode:
                # Immediate fill at entry_mid (signal candle close)
                orders["filled"] += 1
                rec["entry_ts"]   = ts
                rec["entry_time"] = step_dt.strftime("%Y-%m-%d %H:%M")
                active_trades.append(rec)
            else:
                pending_orders.append(rec)

    # End of period — close still-running trades at last 5m close (close_at_end)
    end_ts = int(end_dt.timestamp())
    for t in active_trades:
        c5m_all = raw_data.get(t["symbol"], {}).get("5m", [])
        pnl_r = None
        exit_reason = "open_at_end"
        if isinstance(c5m_all, list):
            last_sliced = slice_at(c5m_all, end_ts)
            if last_sliced and t.get("entry_ts"):
                after = [c for c in last_sliced if c["time"] > t["entry_ts"]]
                if after:
                    last_price = after[-1]["close"]
                    entry = t["entry"]
                    risk = abs(entry - t["sl"]) if t.get("sl") else 0
                    if risk > 0:
                        if t["direction"] == "LONG":
                            pnl_r = round((last_price - entry) / risk, 4)
                        else:
                            pnl_r = round((entry - last_price) / risk, 4)
                        exit_reason = "close_at_end"
        t["exit_reason"] = exit_reason
        t["pnl_r"]       = pnl_r
        t["exit_time"]   = end_dt.strftime("%Y-%m-%d %H:%M")
        trades.append(t)

    # Pending orders placed at last step but never checked → cancelled
    orders["cancelled"] += len(pending_orders)

    return trades, orders, concurrent_hist

# ============================================================
# STATISTICS
# ============================================================

def compute_stats(trades, concurrent_hist=None, orders=None):
    closed = [t for t in trades if t.get("exit_reason") not in ("open", "open_at_end", None) and t.get("pnl_r") is not None]
    if not closed:
        return {}

    wins_full     = [t for t in closed if t.get("exit_reason") == "tp_full"]
    wins_be       = [t for t in closed if t.get("exit_reason") == "be_exit"]
    positive_wins = [t for t in closed if (t.get("pnl_r") or 0) > 0]
    losses        = [t for t in closed if (t.get("pnl_r") or 0) < 0]
    no_exit       = [t for t in trades  if t.get("exit_reason") in ("open", "open_at_end")]

    pnls      = [t["pnl_r"] for t in closed if t.get("pnl_r") is not None]
    total_r   = sum(pnls)

    gross_win  = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf         = gross_win / gross_loss if gross_loss > 0 else None
    ev         = total_r / len(closed) if closed else 0

    avg_mfe = (sum(t.get("mfe_r", 0) or 0 for t in closed) / len(closed)) if closed else 0
    avg_mae = (sum(t.get("mae_r", 0) or 0 for t in closed) / len(closed)) if closed else 0

    equity = [0.0]
    for t in closed:
        equity.append(equity[-1] + (t.get("pnl_r") or 0))

    peak = max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak - e > max_dd:
            max_dd = peak - e

    # Z-score (runs test) — based on pnl_r sign, not exit_reason
    n        = len(pnls)
    wins_n   = sum(1 for p in pnls if p > 0)
    losses_n = sum(1 for p in pnls if p <= 0)
    runs     = 1 + sum(1 for i in range(1, n) if (pnls[i] > 0) != (pnls[i-1] > 0))
    if wins_n > 0 and losses_n > 0 and n > 1:
        exp_r = 2 * wins_n * losses_n / n + 1
        var_r = (2 * wins_n * losses_n * (2 * wins_n * losses_n - n)) / (n * n * (n - 1))
        z_score = (runs - exp_r) / math.sqrt(var_r) if var_r > 0 else 0.0
    else:
        z_score = 0.0

    avg_win_r  = sum(t["pnl_r"] for t in positive_wins) / len(positive_wins) if positive_wins else 0.0
    avg_loss_r = sum(t["pnl_r"] for t in losses)        / len(losses)        if losses        else 0.0
    payoff_ratio  = avg_win_r / abs(avg_loss_r) if avg_loss_r < 0 else None
    breakeven_wr  = round(1 / (1 + payoff_ratio) * 100, 1) if payoff_ratio else None
    win_rate_dec  = len(positive_wins) / len(closed) if closed else 0.0
    expectancy    = win_rate_dec * avg_win_r + (1 - win_rate_dec) * avg_loss_r

    stats = {
        "total":          len(closed),
        "positive_wins":  len(positive_wins),
        "full_wins":      len(wins_full),
        "partial_wins":   len(wins_be),
        "losses":         len(losses),
        "no_exit":        len(no_exit),
        "win_rate":       win_rate_dec * 100,
        "total_r":        round(total_r, 3),
        "ev_per_trade":   round(ev, 3),
        "profit_factor":  round(pf, 3) if pf else None,
        "avg_win_r":      round(avg_win_r,  3),
        "avg_loss_r":     round(avg_loss_r, 3),
        "payoff_ratio":   round(payoff_ratio, 3) if payoff_ratio else None,
        "breakeven_wr":   breakeven_wr,
        "expectancy":     round(expectancy, 3),
        "avg_mfe_r":      round(avg_mfe, 3),
        "avg_mae_r":      round(avg_mae, 3),
        "max_dd_r":       round(max_dd, 3),
        "z_score":        round(z_score, 2),
        "equity":         equity,
    }

    if orders:
        created  = orders.get("created", 0)
        filled   = orders.get("filled",  0)
        stats["orders_created"]   = created
        stats["orders_filled"]    = filled
        stats["orders_cancelled"] = orders.get("cancelled", 0)
        stats["fill_rate"]        = round(filled / created * 100, 1) if created > 0 else 0.0

    if concurrent_hist:
        dist = {}
        for c in concurrent_hist:
            k = min(c, 3)
            dist[k] = dist.get(k, 0) + 1
        stats["max_concurrent"]  = max(concurrent_hist)
        stats["avg_concurrent"]  = round(sum(concurrent_hist) / len(concurrent_hist), 2)
        stats["concurrent_dist"] = {i: dist.get(i, 0) for i in range(4)}

    return stats


def _breakdown(trades, key_fn):
    groups: dict = {}
    for t in trades:
        if t.get("exit_reason") in ("open", "open_at_end", None) or t.get("pnl_r") is None:
            continue
        k = key_fn(t)
        groups.setdefault(k, []).append(t)
    rows = []
    for k, ts in sorted(groups.items(), key=lambda x: -len(x[1])):
        pnls  = [t["pnl_r"] for t in ts if t.get("pnl_r") is not None]
        wins  = [p for p in pnls if p > 0]
        losss = [p for p in pnls if p < 0]
        n     = len(ts)
        wr    = len(wins) / n * 100 if n else 0
        ev    = sum(pnls) / n if n and pnls else 0
        gw    = sum(wins)
        gl    = abs(sum(losss))
        pf    = gw / gl if gl > 0 else None
        aw    = sum(wins) / len(wins)   if wins  else 0.0
        al    = sum(losss) / len(losss) if losss else 0.0
        po    = aw / abs(al) if al < 0 else None
        rows.append({
            "label":         str(k),
            "n":             n,
            "wins":          len(wins),
            "wr":            f"{wr:.0f}%",
            "total_r":       f"{sum(pnls):+.2f}R" if pnls else "—",
            "ev":            f"{ev:+.3f}R",
            "pf":            f"{pf:.2f}" if pf else "—",
            "avg_win":       f"+{aw:.3f}R" if wins  else "—",
            "avg_loss":      f"{al:.3f}R"  if losss else "—",
            "payoff":        f"{po:.2f}"   if po    else "—",
            # raw values for edge filtering
            "_ev":           ev,
            "_pf":           pf or 0.0,
            "_n":            n,
        })
    return rows


def _edge_groups(all_breakdowns):
    """Flatten all breakdown rows and return (candidates, killers)."""
    candidates, killers = [], []
    for label, rows in all_breakdowns:
        for r in rows:
            if r["_n"] < 20:
                continue
            entry = {"group": label, **r}
            if r["_ev"] > 0 and r["_pf"] > 1.2:
                candidates.append(entry)
            elif r["_ev"] < 0 and r["_pf"] < 1.0:
                killers.append(entry)
    candidates.sort(key=lambda x: -x["_ev"])
    killers.sort(key=lambda x: x["_ev"])
    return candidates, killers

# ============================================================
# HTML REPORT HELPERS
# ============================================================

def _f(v):
    if v is None:
        return "N/A"
    try:
        if abs(v) >= 1000: return f"{v:,.2f}"
        if abs(v) >= 1:    return f"{v:.4f}"
        return f"{v:.6f}"
    except Exception:
        return str(v)


def _stat_card(label, val, col="#e6edf7"):
    return (
        f'<div style="background:#101827;border:1px solid #1e2d40;border-radius:6px;'
        f'padding:12px;text-align:center">'
        f'<div style="font-size:20px;font-weight:bold;color:{col}">{val}</div>'
        f'<div style="font-size:11px;color:#8899aa;margin-top:4px">{label}</div></div>'
    )


def _equity_svg(equity, width=700, height=120):
    if len(equity) < 2:
        return "<div style='color:#556677'>Brak zamkniętych transakcji</div>"
    min_e, max_e = min(equity), max(equity)
    rng = max_e - min_e if max_e != min_e else 1
    pts = " ".join(
        f"{int(i / (len(equity) - 1) * width)},{int(height - (v - min_e) / rng * height)}"
        for i, v in enumerate(equity)
    )
    zero_y = int(height - (0 - min_e) / rng * height)
    return (
        f'<svg width="{width}" height="{height}" '
        f'style="background:#0d1520;border-radius:6px;display:block">'
        f'<line x1="0" y1="{zero_y}" x2="{width}" y2="{zero_y}" '
        f'stroke="#334" stroke-width="1" stroke-dasharray="4"/>'
        f'<polyline points="{pts}" fill="none" stroke="#00ff8c" stroke-width="2"/>'
        f'</svg>'
    )


def _breakdown_table(rows, title):
    if not rows:
        return ""
    headers = ["Group", "N", "Wins", "WR", "Total R", "EV/trade", "PF", "Avg Win", "Avg Loss", "Payoff"]
    th = "".join(f'<th>{h}</th>' for h in headers)
    tr_html = "".join(
        f'<tr>'
        f'<td>{r["label"]}</td><td>{r["n"]}</td><td>{r["wins"]}</td>'
        f'<td>{r["wr"]}</td>'
        f'<td style="color:{("#00ff8c" if r["total_r"].startswith("+") else "#ff4d4d")}">{r["total_r"]}</td>'
        f'<td style="color:{("#00ff8c" if r["_ev"]>0 else "#ff4d4d")}">{r["ev"]}</td>'
        f'<td>{r["pf"]}</td>'
        f'<td style="color:#00ff8c">{r["avg_win"]}</td>'
        f'<td style="color:#ff4d4d">{r["avg_loss"]}</td>'
        f'<td>{r["payoff"]}</td>'
        f'</tr>'
        for r in rows
    )
    return (
        f'<h3 style="margin:16px 0 8px;color:#8899aa;font-size:12px;'
        f'letter-spacing:1px;text-transform:uppercase">{title}</h3>'
        f'<div style="overflow-x:auto">'
        f'<table style="width:auto;border-collapse:collapse;font-size:12px;margin-bottom:16px">'
        f'<thead><tr>{th}</tr></thead><tbody>{tr_html}</tbody></table></div>'
    )


def _edge_table(groups, accent):
    if not groups:
        return f'<div style="color:#556677;font-size:12px">Brak grup (min 20 trade\'ów).</div>'
    headers = ["Group", "Dimension", "N", "WR", "EV/trade", "PF", "Avg Win", "Avg Loss", "Payoff"]
    th = "".join(f'<th>{h}</th>' for h in headers)
    rows = "".join(
        f'<tr>'
        f'<td style="color:{accent};font-weight:bold">{g["label"]}</td>'
        f'<td style="color:#8899aa">{g["group"]}</td>'
        f'<td>{g["n"]}</td>'
        f'<td>{g["wr"]}</td>'
        f'<td style="color:{accent}">{g["ev"]}</td>'
        f'<td>{g["pf"]}</td>'
        f'<td style="color:#00ff8c">{g["avg_win"]}</td>'
        f'<td style="color:#ff4d4d">{g["avg_loss"]}</td>'
        f'<td>{g["payoff"]}</td>'
        f'</tr>'
        for g in groups
    )
    return (
        f'<div style="overflow-x:auto">'
        f'<table style="width:100%;border-collapse:collapse;font-size:12px">'
        f'<thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def _config_comparison_table(config_results, scan_interval_h):
    rows_html = ""
    for (cfg, model, cons_label, entry_mode), (trades, stats) in sorted(config_results.items()):
        if not stats:
            continue
        tr_col  = "#00ff8c" if (stats.get("total_r") or 0) > 0 else "#ff4d4d"
        wr_col  = "#00ff8c" if stats.get("win_rate", 0) >= 50 else "#ff4d4d"
        pf      = stats.get("profit_factor")
        fr      = stats.get("fill_rate", 0)
        cons_c  = "#88ccff" if cons_label == "conservative" else "#ffd700"
        em_c    = "#00ff8c" if entry_mode == "immediate" else "#aabbcc"
        exp_v   = stats.get("expectancy", 0)
        exp_c   = "#00ff8c" if exp_v > 0 else "#ff4d4d"
        rows_html += (
            f'<tr>'
            f'<td style="font-weight:bold">{cfg}</td>'
            f'<td>{model}</td>'
            f'<td style="color:{cons_c}">{cons_label}</td>'
            f'<td style="color:{em_c}">{entry_mode}</td>'
            f'<td>{CONFIGS[cfg]["basket_size"]}</td>'
            f'<td>{stats.get("total", 0)}</td>'
            f'<td style="color:{wr_col}">{stats.get("win_rate", 0):.1f}%</td>'
            f'<td style="color:{tr_col}">{stats.get("total_r", 0):+.3f}R</td>'
            f'<td style="color:{exp_c}">{exp_v:+.3f}R</td>'
            f'<td>{stats.get("ev_per_trade", 0):+.3f}R</td>'
            f'<td>{f"{pf:.2f}" if pf else "—"}</td>'
            f'<td>-{stats.get("max_dd_r", 0):.2f}R</td>'
            f'<td>{stats.get("z_score", 0):+.2f}</td>'
            f'<td>{fr:.1f}%</td>'
            f'</tr>'
        )
    headers = ["Config", "Model", "Intrabar", "Entry", "Basket",
               "Trades", "Win Rate", "Total R", "Expectancy", "EV/Trade",
               "PF", "Max DD", "Z-Score", "Fill Rate"]
    th_html = "".join(f'<th>{h}</th>' for h in headers)
    ttl_note = (
        f'<p style="font-size:11px;color:#556677;margin-bottom:12px">'
        f'⚠ Limit order TTL = {scan_interval_h}h. '
        f'Immediate entry = instant fill at entry_mid. '
        f'close_at_end = open trades closed at last 5m candle price. '
        f'Fill Rate = filled / created (limit mode only).</p>'
    )
    return (
        f'<h2 style="border-bottom:1px solid #1e2d40;padding-bottom:8px;margin-bottom:8px">'
        f'⚖ Porównanie konfiguracji</h2>'
        f'{ttl_note}'
        f'<div style="overflow-x:auto;margin-bottom:32px">'
        f'<table style="width:100%;border-collapse:collapse;font-size:12px">'
        f'<thead><tr>{th_html}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>'
    )


def _trade_log_table(trades, label):
    rc_map = {
        "tp_full":    "#00ff8c",
        "be_exit":    "#ffd700",
        "sl":         "#ff4d4d",
        "open":       "#88ccff",
        "open_at_end":"#88ccff",
    }
    rows = ""
    for i, t in enumerate(trades, 1):
        rc  = rc_map.get(t.get("exit_reason", ""), "#8899aa")
        dc  = "#00ff8c" if t["direction"] == "LONG" else "#ff4d4d"
        mc  = "#00ff8c" if t.get("macd_5m") == "yes" else "#888"
        pnl = t.get("pnl_r")
        r_s = f'{pnl:+.3f}R' if pnl is not None else "open"
        mfe = t.get("mfe_r")
        mae = t.get("mae_r")
        ci  = "★" if t.get("conservative_intrabar") else ""
        rows += (
            f'<tr>'
            f'<td>{i}</td>'
            f'<td style="font-weight:bold">{t["symbol"]}</td>'
            f'<td style="color:{dc}">{t["direction"]}</td>'
            f'<td style="color:#8899aa;font-size:10px">{t.get("signal_time","—")}</td>'
            f'<td style="font-size:10px">{t.get("entry_time","—")}</td>'
            f'<td style="font-size:10px">{t.get("exit_time","—")}</td>'
            f'<td>{_f(t["entry"])}</td>'
            f'<td style="color:#ff4d4d">{_f(t["sl"])}</td>'
            f'<td style="color:#00ff8c">{_f(t.get("tp2"))}</td>'
            f'<td style="color:{rc};font-weight:bold">{t.get("exit_reason","?")}</td>'
            f'<td style="color:{rc};font-weight:bold">{r_s}</td>'
            f'<td style="color:#88ccff">{f"+{mfe:.2f}R" if mfe is not None else "—"}</td>'
            f'<td style="color:#ff8844">{f"{mae:.2f}R" if mae is not None else "—"}</td>'
            f'<td>{t.get("bars_held","—")}</td>'
            f'<td>{t.get("exec_model","?")}</td>'
            f'<td style="color:#ffaa00">{ci}</td>'
            f'<td>{t.get("mps","—")}</td>'
            f'<td style="color:{mc}">{t.get("macd_5m","")}</td>'
            f'<td>{t.get("choch_age","")}</td>'
            f'<td style="color:#8899aa;font-size:10px">{t.get("category","").replace("_"," ")}</td>'
            f'</tr>'
        )
    headers = ["#", "Symbol", "Dir", "Signal", "Fill", "Exit",
               "Entry", "SL", "TP2", "Result", "P&L R",
               "MFE", "MAE", "Bars", "Mdl", "CI★", "MPS", "MACD", "ChoCH", "Category"]
    th = "".join(f'<th>{h}</th>' for h in headers)
    return (
        f'<details style="margin-bottom:24px">'
        f'<summary style="cursor:pointer;color:#8899aa;font-size:13px;margin-bottom:8px">'
        f'📋 {label} — {len(trades)} transakcji  '
        f'<span style="font-size:10px;color:#556677">(★ = conservative intrabar triggered)</span>'
        f'</summary>'
        f'<div style="overflow-x:auto">'
        f'<table style="width:100%;border-collapse:collapse;font-size:11px">'
        f'<thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div></details>'
    )

# ============================================================
# WYCKOFF CAUSE BUCKET HELPERS
# ============================================================

def _bucket_wyckoff_cause(v):
    if v is None: return "N/A"
    if v <= 4:    return "0–4"
    if v <= 8:    return "5–8"
    if v <= 12:   return "9–12"
    return "13–15"


def _bucket_range_duration(hours):
    if hours is None: return "N/A"
    if hours < 8:     return "<8h"
    if hours < 24:    return "8–24h"
    if hours <= 72:   return "24–72h"
    if hours <= 168:  return "72–168h"
    return ">168h"


def _bucket_phase_d_age(v):
    if v is None: return "N/A"
    if v <= 6:    return "0–6"
    if v <= 24:   return "7–24"
    return ">24"


# ============================================================
# CSV EXPORT
# ============================================================

_CSV_FIELDS = [
    "config", "model_name", "intrabar", "entry_mode",
    "symbol", "direction", "signal_time", "entry_time", "exit_time",
    "entry_mid", "sl", "tp2", "tp3", "rr", "risk",
    "category", "wyckoff", "status", "macd_5m",
    "choch_age", "entry_ext", "pattern_type",
    "mps", "ts_score",
    "target_feasibility_score", "verdict",
    "wyckoff_cause_score", "base_strength_label",
    "range_duration_hours", "range_atr_ratio",
    "sos_sow_displacement_atr", "phase_d_age_bars",
    "pullback_depth_percent_of_range",
    "exec_model", "exit_reason", "pnl_r", "mfe_r", "mae_r",
    "bars_held", "conservative_intrabar",
]


def _export_csv(config_results, path):
    rows = []
    for (cfg, model_name, cons_label, entry_mode), (trades, _) in sorted(config_results.items()):
        for t in trades:
            rows.append({
                "config":      cfg,
                "model_name":  model_name,
                "intrabar":    cons_label,
                "entry_mode":  entry_mode,
                "symbol":      t.get("symbol"),
                "direction":   t.get("direction"),
                "signal_time": t.get("signal_time"),
                "entry_time":  t.get("entry_time"),
                "exit_time":   t.get("exit_time"),
                "entry_mid":   t.get("entry_mid"),
                "sl":          t.get("sl"),
                "tp2":         t.get("tp2"),
                "tp3":         t.get("tp3"),
                "rr":          t.get("rr"),
                "risk":        t.get("risk"),
                "category":    t.get("category"),
                "wyckoff":     t.get("wyckoff"),
                "status":      t.get("status"),
                "macd_5m":     t.get("macd_5m"),
                "choch_age":   t.get("choch_age"),
                "entry_ext":   t.get("entry_ext"),
                "pattern_type":             t.get("pattern_type"),
                "mps":         t.get("mps"),
                "ts_score":    t.get("ts_score"),
                "target_feasibility_score": t.get("target_feasibility_score"),
                "verdict":     t.get("verdict"),
                "wyckoff_cause_score":             t.get("wyckoff_cause_score"),
                "base_strength_label":             t.get("base_strength_label"),
                "range_duration_hours":            t.get("range_duration_hours"),
                "range_atr_ratio":                 t.get("range_atr_ratio"),
                "sos_sow_displacement_atr":        t.get("sos_sow_displacement_atr"),
                "phase_d_age_bars":                t.get("phase_d_age_bars"),
                "pullback_depth_percent_of_range": t.get("pullback_depth_percent_of_range"),
                "exec_model":  t.get("exec_model"),
                "exit_reason": t.get("exit_reason"),
                "pnl_r":       t.get("pnl_r"),
                "mfe_r":       t.get("mfe_r"),
                "mae_r":       t.get("mae_r"),
                "bars_held":   t.get("bars_held"),
                "conservative_intrabar": t.get("conservative_intrabar"),
            })
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ============================================================
# MAIN REPORT GENERATOR
# ============================================================

def generate_report(config_results, meta):
    css = (
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{background:#070b12;color:#e6edf7;font-family:'Segoe UI',Arial,sans-serif;font-size:14px}"
        "th{padding:8px 6px;text-align:left;color:#8899aa;border-bottom:1px solid #1e2d40;white-space:nowrap}"
        "td{padding:5px 6px;border-bottom:1px solid #1a2535;white-space:nowrap}"
        "tr:hover td{background:#0d1520}"
        "details summary{list-style:none}"
        "details summary::-webkit-details-marker{display:none}"
    )

    header = (
        f'<div style="background:#0d1520;border-bottom:2px solid #00ff8c;padding:20px 32px">'
        f'<div style="font-size:11px;color:#8899aa;letter-spacing:1px;margin-bottom:4px">'
        f'BACKTEST REPORT — Crypto Wyckoff + SMC v9.1 strict</div>'
        f'<h1 style="font-size:24px;color:#e6edf7;margin-bottom:6px">'
        f'{meta["start"]} → {meta["end"]}</h1>'
        f'<div style="font-size:12px;color:#8899aa">'
        f'Skan co <strong>{meta["scan_interval_h"]}H</strong> | '
        f'Symboli: <strong>{meta["n_symbols"]}</strong> | '
        f'Wygenerowano: <strong>{meta["report_time"]}</strong>'
        f'</div>'
        f'<div style="margin-top:8px;font-size:11px;color:#556677">'
        f'Limit order TTL = {meta["scan_interval_h"]}h. '
        f'TF: 1H / 30m / 15m / 5m. '
        f'conservative mode: SL wins intrabar conflict. '
        f'optimistic mode: TP wins intrabar conflict. '
        f'★ in trade log = conservative intrabar triggered.</div>'
        f'<div style="margin-top:8px;font-size:11px;background:#0a1525;border-left:3px solid '
        f'{"#00ff8c" if meta["fuel_label"] == "fuel_ON" else "#ff4d4d"}'
        f';padding:6px 10px;display:inline-block">'
        f'Target Feasibility Filter: '
        f'<strong style="color:{"#00ff8c" if meta["fuel_label"] == "fuel_ON" else "#ff4d4d"}">'
        f'{"ON — TFS affects classify(), MPS ranking (v9.3)" if meta["fuel_label"] == "fuel_ON" else "OFF — TFS diagnostic only, no effect on classify() or MPS (baseline v9.1)"}'
        f'</strong></div>'
        f'</div>'
    )

    comparison = _config_comparison_table(config_results, meta["scan_interval_h"])

    # Collect all breakdowns for edge diagnostics (across all runs)
    all_breakdowns_global = []

    sections = ""
    for (cfg, model, cons_label, entry_mode), (trades, stats) in sorted(config_results.items()):
        if not stats:
            continue

        basket_size = CONFIGS[cfg]["basket_size"]
        eq     = stats.get("equity", [0])
        wr     = stats.get("win_rate", 0)
        tr     = stats.get("total_r", 0)
        wr_c   = "#00ff8c" if wr >= 50 else "#ff4d4d"
        tr_c   = "#00ff8c" if tr > 0 else "#ff4d4d"
        pf     = stats.get("profit_factor")
        fr     = stats.get("fill_rate", 0)
        exp_v  = stats.get("expectancy", 0)
        po     = stats.get("payoff_ratio")
        bew    = stats.get("breakeven_wr")
        cons_c = "#88ccff" if cons_label == "conservative" else "#ffd700"
        em_c   = "#00ff8c" if entry_mode == "immediate" else "#aabbcc"

        cards = (
            _stat_card("Trades",        stats.get("total", 0)) +
            _stat_card("Positive",      stats.get("positive_wins", 0), "#00ff8c") +
            _stat_card("Full TP",       stats.get("full_wins",     0), "#00cc6a") +
            _stat_card("Partial TP",    stats.get("partial_wins",  0), "#ffd700") +
            _stat_card("Losses",        stats.get("losses",        0), "#ff4d4d") +
            _stat_card("Open/end",      stats.get("no_exit",       0), "#88ccff") +
            _stat_card("Win Rate",      f"{wr:.1f}%", wr_c) +
            _stat_card("Breakeven WR",  f"{bew:.1f}%" if bew else "—") +
            _stat_card("Total R",       f"{tr:+.3f}R", tr_c) +
            _stat_card("Expectancy",    f"{exp_v:+.3f}R",
                       "#00ff8c" if exp_v > 0 else "#ff4d4d") +
            _stat_card("EV/Trade",      f'{stats.get("ev_per_trade", 0):+.3f}R') +
            _stat_card("Profit Factor", f'{pf:.2f}' if pf else "—",
                       "#00ff8c" if pf and pf > 1 else "#ff4d4d") +
            _stat_card("Avg Win",       f'+{stats.get("avg_win_r",  0):.3f}R', "#00ff8c") +
            _stat_card("Avg Loss",      f'{stats.get("avg_loss_r",  0):.3f}R', "#ff4d4d") +
            _stat_card("Payoff Ratio",  f'{po:.2f}' if po else "—") +
            _stat_card("Max DD",        f'-{stats.get("max_dd_r", 0):.2f}R', "#ff8844") +
            _stat_card("Avg MFE",       f'+{stats.get("avg_mfe_r", 0):.2f}R', "#88ccff") +
            _stat_card("Avg MAE",       f'{stats.get("avg_mae_r", 0):.2f}R',  "#ff8844") +
            _stat_card("Z-Score",       f'{stats.get("z_score", 0):+.2f}') +
            _stat_card("Orders Created",  stats.get("orders_created",  "—")) +
            _stat_card("Orders Filled",   stats.get("orders_filled",   "—"), "#00ff8c") +
            _stat_card("Orders Cancelled",stats.get("orders_cancelled","—"), "#ff4d4d") +
            _stat_card("Fill Rate",       f"{fr:.1f}%",
                       "#00ff8c" if fr >= 50 else "#ff8844")
        )

        # Basket stats for TOP3
        if basket_size > 1 and "max_concurrent" in stats:
            dist = stats["concurrent_dist"]
            cards += (
                _stat_card("Max Concurrent", stats["max_concurrent"]) +
                _stat_card("Avg Concurrent", stats["avg_concurrent"]) +
                _stat_card("Steps 0 active", dist.get(0, 0), "#8899aa") +
                _stat_card("Steps 1 active", dist.get(1, 0)) +
                _stat_card("Steps 2 active", dist.get(2, 0)) +
                _stat_card("Steps 3 active", dist.get(3, 0))
            )

        bd_cat    = _breakdown(trades, lambda t: t.get("category",  "?"))
        bd_status = _breakdown(trades, lambda t: t.get("status",    "?"))
        bd_macd   = _breakdown(trades, lambda t: t.get("macd_5m",   "?"))
        bd_dir    = _breakdown(trades, lambda t: t.get("direction",  "?"))
        bd_choch  = _breakdown(trades, lambda t: (
            "0–1" if (t.get("choch_age") or 99) <= 1 else
            "2"   if (t.get("choch_age") or 99) == 2 else
            "3"   if (t.get("choch_age") or 99) == 3 else "4+"
        ))
        bd_ext    = _breakdown(trades, lambda t: (
            "≤10%"    if (t.get("entry_ext") or 0) <= 0.10 else
            "10–25%"  if (t.get("entry_ext") or 0) <= 0.25 else
            "25–50%"  if (t.get("entry_ext") or 0) <= 0.50 else ">50%"
        ))
        bd_entry  = _breakdown(trades, lambda t: t.get("entry_mode", "limit"))
        bd_tfs    = _breakdown(trades, lambda t: (
            "<40"   if (t.get("target_feasibility_score") or 0) < 40  else
            "40–54" if (t.get("target_feasibility_score") or 0) < 55  else
            "55–69" if (t.get("target_feasibility_score") or 0) < 70  else
            "70–84" if (t.get("target_feasibility_score") or 0) < 85  else "85+"
        ))
        bd_verdict   = _breakdown(trades, lambda t: t.get("verdict", "?"))
        bd_wcs       = _breakdown(trades, lambda t: _bucket_wyckoff_cause(t.get("wyckoff_cause_score")))
        bd_base_str  = _breakdown(trades, lambda t: t.get("base_strength_label") or "UNKNOWN_BASE")
        bd_range_dur = _breakdown(trades, lambda t: _bucket_range_duration(t.get("range_duration_hours")))
        bd_pd_age    = _breakdown(trades, lambda t: _bucket_phase_d_age(t.get("phase_d_age_bars")))

        bds_html = (
            _breakdown_table(bd_cat,      "By Category") +
            _breakdown_table(bd_status,   "By Status") +
            _breakdown_table(bd_macd,     "By MACD") +
            _breakdown_table(bd_dir,      "Long vs Short") +
            _breakdown_table(bd_choch,    "By ChoCH Age") +
            _breakdown_table(bd_ext,      "By Entry Extension") +
            _breakdown_table(bd_entry,    "By Entry Mode") +
            _breakdown_table(bd_tfs,      "By Target Feasibility Score") +
            _breakdown_table(bd_verdict,  "By TFS Verdict") +
            _breakdown_table(bd_wcs,      "By Wyckoff Cause Score") +
            _breakdown_table(bd_base_str, "By Base Strength") +
            _breakdown_table(bd_range_dur,"By Range Duration") +
            _breakdown_table(bd_pd_age,   "By Phase D Age (bars)")
        )

        # Accumulate for global edge section
        run_tag = f"{cfg}/{model}/{cons_label}/{entry_mode}"
        for dim_label, rows in [
            ("Category", bd_cat), ("Status", bd_status), ("MACD", bd_macd),
            ("Direction", bd_dir), ("ChoCH", bd_choch), ("ExtBucket", bd_ext),
            ("EntryMode", bd_entry), ("TFS bucket", bd_tfs), ("TFS verdict", bd_verdict),
            ("WyckoffCause", bd_wcs), ("BaseStrength", bd_base_str),
            ("RangeDuration", bd_range_dur), ("PhaseDAge", bd_pd_age),
        ]:
            for r in rows:
                all_breakdowns_global.append((f"{run_tag} · {dim_label}", [r]))

        label = f"{cfg} / Model {model} / {cons_label} / {entry_mode}"
        basket_tag = "  (basket 3)" if basket_size > 1 else ""
        sections += (
            f'<div style="border-top:2px solid #1e2d40;margin:32px 0 0;padding:24px 32px 0">'
            f'<h2 style="color:#e6edf7;margin-bottom:4px">{label}{basket_tag}</h2>'
            f'<div style="font-size:11px;margin-bottom:4px">'
            f'<span style="color:{cons_c}">intrabar → {"SL wins" if cons_label == "conservative" else "TP wins"}</span>'
            f'  |  <span style="color:{em_c}">entry → {entry_mode}</span></div>'
            f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));'
            f'gap:8px;margin-bottom:20px">{cards}</div>'
            f'<div style="margin-bottom:16px">{_equity_svg(eq)}</div>'
            f'{bds_html}'
            f'{_trade_log_table(trades, label)}'
            f'</div>'
        )

    # ── Global Edge Diagnostics ──────────────────────────────────
    candidates, killers = _edge_groups(all_breakdowns_global)
    edge_section = (
        f'<div style="border-top:2px solid #ffd700;margin:32px 0 0;padding:24px 32px 0">'
        f'<h2 style="color:#ffd700;margin-bottom:16px">🔬 Edge Diagnostics</h2>'
        f'<p style="font-size:11px;color:#556677;margin-bottom:20px">'
        f'Grupuje wszystkie wymiary (status, MACD, direction, ChoCH, entry_ext) we wszystkich konfiguracjach. '
        f'Minimalna próbka: 20 trade\'ów.</p>'
        f'<h3 style="color:#00ff8c;margin-bottom:8px">✅ EDGE CANDIDATES '
        f'<span style="font-size:11px;font-weight:normal;color:#556677">'
        f'(N≥20, EV>0, PF>1.2)</span></h3>'
        f'{_edge_table(candidates, "#00ff8c")}'
        f'<h3 style="color:#ff4d4d;margin:20px 0 8px">❌ EDGE KILLERS '
        f'<span style="font-size:11px;font-weight:normal;color:#556677">'
        f'(N≥20, EV&lt;0, PF&lt;1.0)</span></h3>'
        f'{_edge_table(killers, "#ff4d4d")}'
        f'</div>'
    )
    sections += edge_section

    footer = (
        f'<div style="background:#0a0f1a;border-top:1px solid #1e2d40;'
        f'padding:14px 32px;font-size:11px;color:#556677">'
        f'strategy_wyckoff_9_5 strict backtest | Dane historyczne z Bybit Linear | '
        f'Wyniki historyczne nie gwarantują przyszłych zysków. '
        f'Wygenerowano: {meta["report_time"]}</div>'
    )

    return (
        f'<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8">'
        f'<title>Backtest v9.1 — {meta["start"]} → {meta["end"]}</title>'
        f'<style>{css}</style></head><body>'
        f'{header}'
        f'<div style="padding:20px 32px">{comparison}</div>'
        f'{sections}'
        f'{footer}</body></html>'
    )

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fuel-filter", choices=["on", "off"], default="on",
                        help="Enable/disable Target Feasibility Filter in classify()")
    args, _ = parser.parse_known_args()

    st.USE_TARGET_FEASIBILITY_FILTER = (args.fuel_filter == "on")
    fuel_label = "fuel_ON" if st.USE_TARGET_FEASIBILITY_FILTER else "fuel_OFF"

    print("=" * 60)
    print("  Backtest v9.1 strict")
    print("  Configs: TOP1 / TOP3 / PREMIUM / HQ / PREMIUM_HQ")
    print("  Models:  A / B / Auto")
    print("  Modes:   conservative / optimistic")
    print(f"  Fuel Filter (TFS): {'ON' if st.USE_TARGET_FEASIBILITY_FILTER else 'OFF'}")
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

    print(f"\nKonfiguracje: {', '.join(CONFIGS)}")
    cfg_input = input("Które konfiguracje? (Enter = wszystkie, np. TOP1,CORE_V93): ").strip()
    chosen_cfgs = (
        [c.strip() for c in cfg_input.split(",") if c.strip() in CONFIGS]
        if cfg_input else list(CONFIGS)
    )

    print("Modele: A, B, Auto, FIXED_2R, FIXED_1_5R, FIXED_3R")
    mdl_input = input("Które modele? (Enter = wszystkie): ").strip()
    chosen_models = (
        [m.strip() for m in mdl_input.split(",") if m.strip() in MODELS]
        if mdl_input else list(MODELS)
    )

    print("Tryb intrabar: c = conservative  o = optimistic  co = oba")
    cons_input = input("Tryb (Enter = conservative): ").strip().lower()
    if cons_input in ("o", "optimistic"):
        chosen_cons = [("optimistic", False)]
    elif cons_input in ("co", "oba", "both"):
        chosen_cons = [("conservative", True), ("optimistic", False)]
    else:
        chosen_cons = [("conservative", True)]

    print("Tryb wejścia: l = limit  i = immediate  li = oba")
    entry_input = input("Entry (Enter = limit): ").strip().lower()
    if entry_input in ("i", "immediate"):
        chosen_entries = ["immediate"]
    elif entry_input in ("li", "oba", "both"):
        chosen_entries = ["limit", "immediate"]
    else:
        chosen_entries = ["limit"]

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)

    print(f"\nPrzedział:    {start_dt} → {end_dt}")
    print(f"Skan:         co {scan_h}H | top {n_sym} symboli")
    print(f"Konfiguracje: {chosen_cfgs}")
    print(f"Modele:       {chosen_models}")
    print(f"Tryby:        {[c for c, _ in chosen_cons]}")
    print(f"Entry:        {chosen_entries}")

    print("\n[1/3] Pobieranie listy top symboli...")
    top_crypto = st.get_top_crypto(n_sym)
    if not top_crypto:
        print("Brak symboli. Koniec.")
        return

    symbols      = [c["symbol"] for c in top_crypto]
    turnover_map = {c["symbol"]: c["turnover"] for c in top_crypto}
    for must_have in ("BTCUSDT", "ETHUSDT"):
        if must_have not in symbols:
            symbols.insert(0, must_have)

    start_tag  = start_dt.strftime("%Y%m%d%H%M")
    end_tag    = end_dt.strftime("%Y%m%d%H%M")
    cache_file = os.path.join(CACHE_DIR, f"bt_{start_tag}_{end_tag}_top{n_sym}.json")

    print(f"\n[2/3] Dane OHLCV — TF: {BT_TIMEFRAMES}")
    print(f"      Cache: {os.path.basename(cache_file)}")
    raw_data = fetch_and_cache(symbols, start_ms, end_ms, cache_file)

    for sym in raw_data:
        raw_data[sym]["_turnover"] = turnover_map.get(sym, 999_999_999)

    n_runs = len(chosen_cfgs) * len(chosen_models) * len(chosen_cons) * len(chosen_entries)
    print(f"\n[3/3] Symulacja — {n_runs} przebiegów...")
    config_results = {}
    _first_run = True

    for cfg_name in chosen_cfgs:
        for model_name in chosen_models:
            for cons_label, cons_val in chosen_cons:
                for entry_mode in chosen_entries:
                    basket = CONFIGS[cfg_name]["basket_size"]
                    label  = f"{cfg_name}/{model_name}/{cons_label}/{entry_mode}"
                    print(f"  → {label} (basket={basket}) ...", end="", flush=True)

                    trades, orders, concurrent_hist = run_config(
                        raw_data, symbols, start_dt, end_dt, scan_h,
                        cfg_name, model_name, cons_val, entry_mode,
                        debug_wcs=_first_run,
                    )
                    _first_run = False
                    stats = compute_stats(
                        trades,
                        concurrent_hist if basket > 1 else None,
                        orders,
                    )
                    stats["basket_size"] = basket
                    config_results[(cfg_name, model_name, cons_label, entry_mode)] = (trades, stats)

                    fr = stats.get("fill_rate", 0)
                    tr = stats.get("total_r", "—")
                    wr = stats.get("win_rate", 0)
                    ev = stats.get("expectancy", 0)
                    print(
                        f"  {stats.get('total',0)} trades | "
                        f"WR={wr:.1f}% | R={tr} | EV={ev:+.3f}R | "
                        f"fill={fr:.0f}% ({orders['filled']}/{orders['created']})"
                    )

    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "start":           start_dt.strftime("%Y-%m-%d %H:%M"),
        "end":             end_dt.strftime("%Y-%m-%d %H:%M"),
        "scan_interval_h": scan_h,
        "n_symbols":       len(symbols),
        "report_time":     report_time,
        "fuel_label":      fuel_label,
    }

    html    = generate_report(config_results, meta)
    outfile = os.path.join(RESULTS_DIR, f"backtest_{start_tag}_{end_tag}_{fuel_label}.html")
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ Raport: {outfile}")

    csv_file = os.path.join(RESULTS_DIR, f"backtest_{start_tag}_{end_tag}_{fuel_label}_trades.csv")
    n_rows = _export_csv(config_results, csv_file)
    print(f"✅ CSV:   {csv_file}  ({n_rows} wierszy)")

    try:
        subprocess.Popen(["open", "-a", "Google Chrome", os.path.abspath(outfile)])
    except FileNotFoundError:
        try:
            subprocess.Popen(["open", os.path.abspath(outfile)])
        except Exception:
            pass


if __name__ == "__main__":
    main()
