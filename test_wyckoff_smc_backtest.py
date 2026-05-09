#!/usr/bin/env python3
"""
Walk-forward directional accuracy backtest for wyckoff_core.py.

Tests whether Wyckoff structure detection + new SMC/volume features
correctly predict price direction over a forward window.

Method:
  For each symbol × each historical time window:
  1. Fetch 400 1H candles ending at T (detection window)
  2. Fetch candles ending at T + FORWARD_HOURS (forward window)
  3. Detect Wyckoff structure at T
  4. Record: structure, phase, confidence, WVR, FVG bias, HL score
  5. Measure actual forward return
  6. Score: bullish structure + positive return = correct; bearish + negative = correct

Metrics per group:
  - Directional Accuracy (%)
  - Avg forward return when structure active
  - Hit rate breakdown by Phase, Confidence, WVR, FVG bias, HL score

Usage:
  python test_wyckoff_smc_backtest.py [--symbols N] [--windows N] [--forward N]
"""

import sys, os, time, concurrent.futures, argparse
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_wyckoff_9_5"))
import strategy as st
import wyckoff_core as wc

# ── Config ────────────────────────────────────────────────────────────────────

TIMEFRAME       = "1H"
FORWARD_HOURS   = 72       # bars forward for outcome measurement
STEP_HOURS      = 72       # step between windows (avoid overlap)
LOOKBACK_DAYS   = 90       # how far back to go
WORKERS         = 10
REQUIRE_RSI     = False    # soft mode (NoRSI — empirically better)

BULLISH_STRUCTS = {"Accumulation", "Reaccumulation"}
BEARISH_STRUCTS = {"Distribution", "Redistribution"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_candles_at(symbol, end_time_ms):
    """Fetch 400 1H candles ending at end_time_ms (ms).
    Candle times returned by strategy.py are in SECONDS (//1000 applied in _parse_candles).
    """
    try:
        return st.get_400_candles(symbol, TIMEFRAME, end_time_ms=end_time_ms)
    except Exception:
        return None


def get_forward_price(candles_forward, detection_time_ms, forward_hours):
    """Get price at detection_time + forward_hours from a fetched forward window."""
    target_ms = detection_time_ms + forward_hours * 3600 * 1000
    # Find the closest candle at or after target_ms
    close_at_target = None
    entry_close = None
    for c in candles_forward:
        if c.get("time", 0) >= target_ms and close_at_target is None:
            close_at_target = c["close"]
        if c.get("time", 0) >= detection_time_ms and entry_close is None:
            entry_close = c["close"]
    return entry_close, close_at_target


def analyze_one(symbol, end_ms, forward_ms):
    """Detect structure at end_ms, measure forward return up to forward_ms."""
    candles = fetch_candles_at(symbol, end_ms)
    if not candles or len(candles) < 50:
        return None

    r = wc.analyze_wyckoff(candles, verbose=False, require_rsi_climax=REQUIRE_RSI)
    struct = r["structure"]

    # Only record structures we can score directionally
    if struct not in BULLISH_STRUCTS and struct not in BEARISH_STRUCTS:
        return None

    pts  = r.get("_points", {})
    acc  = pts.get("acc", {})
    dist = pts.get("dist", {})
    active = acc if struct in BULLISH_STRUCTS else dist

    # Forward candles
    fwd_candles = fetch_candles_at(symbol, forward_ms)
    if not fwd_candles:
        return None

    entry_close   = candles[-1]["close"]
    # Forward price = last candle of the forward-shifted window.
    # fwd_candles fetched ending at end_ms + FORWARD_HOURS, so last bar IS the forward bar.
    forward_close = fwd_candles[-1]["close"] if fwd_candles else None
    if forward_close is None or entry_close == 0:
        return None

    fwd_return = (forward_close / entry_close) - 1.0

    wvr  = active.get("_wave_vol_ratio", 1.0)
    rvs  = active.get("_range_vol_slope", 0.0)
    fvg  = active.get("_fvg_bias", 0.0)
    hlb  = active.get("_hl_score_bull", 0)
    hlbr = active.get("_hl_score_bear", 0)
    ar_choch      = active.get("AR", {}).get("choch_strength", 1.0) if "AR" in active else 1.0
    ar_vol_slope  = active.get("AR", {}).get("vol_slope", 0.0)     if "AR" in active else 0.0
    ar_vol_anatomy = active.get("AR", {}).get("vol_anatomy", 1.0)  if "AR" in active else 1.0
    # Book Ch.16: ar_confirmed = SC/BC was confirmed by AR (ChoCH)
    climax_key = "SC" if struct in BULLISH_STRUCTS else "BC"
    ar_confirmed  = active.get(climax_key, {}).get("ar_confirmed", True)
    st_above_bc   = active.get("_st_above_bc", False)   # dist path only
    st_below_sc   = active.get("_st_below_sc", False)   # acc path only
    spring_type   = active.get("Spring", {}).get("spring_type", None) if "Spring" in active else None
    st_pos        = active.get("ST", {}).get("position", None) if "ST" in active else None
    hh_hl_sos     = active.get("SOS", {}).get("hh_hl", None) if "SOS" in active else None

    expected = "bullish" if struct in BULLISH_STRUCTS else "bearish"
    correct  = (fwd_return > 0) if expected == "bullish" else (fwd_return < 0)

    return {
        "symbol":       symbol,
        "end_ms":       end_ms,
        "structure":    struct,
        "phase":        r["phase"],
        "confidence":   r["confidence"],
        "fwd_return":   fwd_return,
        "correct":      correct,
        "expected":     expected,
        # Volume + SMC features
        "wvr":          wvr,
        "rvs":          rvs,
        "fvg_bias":     fvg,
        "hl_bull":      hlb,
        "hl_bear":      hlbr,
        "ar_choch":       ar_choch,
        "ar_vol_slope":   ar_vol_slope,
        "ar_vol_anatomy": ar_vol_anatomy,
        "ar_confirmed":   ar_confirmed,
        "st_above_bc":    st_above_bc,
        "st_below_sc":    st_below_sc,
        "spring_type":    spring_type,
        "st_position":    st_pos,
        "hh_hl_sos":      hh_hl_sos,
    }


# ── Reporting helpers ─────────────────────────────────────────────────────────

def acc_rate(records):
    if not records: return 0.0, 0
    n = len(records)
    c = sum(1 for r in records if r["correct"])
    return 100 * c / n, n

def avg_return(records):
    if not records: return 0.0
    return 100 * sum(r["fwd_return"] for r in records) / len(records)

def print_group(label, records):
    rate, n = acc_rate(records)
    avr = avg_return(records)
    stars = "★" if rate >= 60 else ("·" if rate >= 50 else "✗")
    print(f"  {stars} {label:<42} n={n:>4}  acc={rate:5.1f}%  avgRet={avr:+6.2f}%")

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, default=30)
    parser.add_argument("--windows", type=int, default=8)
    parser.add_argument("--forward", type=int, default=FORWARD_HOURS)
    args = parser.parse_args()

    forward_hours = args.forward

    print(f"\n{'='*70}")
    print(f"  Wyckoff + SMC directional backtest")
    print(f"  TF={TIMEFRAME}  forward={forward_hours}h  NoRSI={not REQUIRE_RSI}")
    print(f"  symbols={args.symbols}  windows={args.windows}  step={STEP_HOURS}h")
    print(f"{'='*70}\n")

    # Time windows: from LOOKBACK_DAYS ago to FORWARD_HOURS ago
    now_ms   = int(time.time() * 1000)
    step_ms  = STEP_HOURS * 3600 * 1000
    fwd_ms   = forward_hours * 3600 * 1000
    # Generate N evenly spaced windows starting from (now - lookback + margin)
    lookback_ms = min(LOOKBACK_DAYS * 24 * 3600 * 1000, args.windows * step_ms * 2)
    # Newest detection window must be at least FORWARD_HOURS + 2h behind now
    # so forward candles are all closed (we fetch at end_ms + fwd_ms, last bar = forward price)
    margin_ms   = fwd_ms + 2 * 3600 * 1000
    latest_safe = now_ms - margin_ms
    start_ms    = latest_safe - (args.windows - 1) * step_ms
    windows     = [start_ms + i * step_ms for i in range(args.windows)
                   if start_ms + i * step_ms <= latest_safe]
    if len(windows) == 0:
        windows = [latest_safe]

    print(f"Time windows: {len(windows)}")

    # Fetch symbols
    print("Fetching symbol list...")
    symbols_raw = st.get_top_crypto(args.symbols)
    symbols = [s["symbol"] for s in symbols_raw if isinstance(s, dict) and "symbol" in s][:args.symbols]
    print(f"  {len(symbols)} symbols\n")

    # Build work items
    work = [(sym, end_ms, end_ms + fwd_ms)
            for sym in symbols for end_ms in windows]
    print(f"Total work items: {len(work)}")

    records = []
    errors  = 0
    done    = 0

    def do_work(item):
        sym, end_ms, fwd_ms = item
        return analyze_one(sym, end_ms, fwd_ms)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(do_work, item): item for item in work}
        for f in concurrent.futures.as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(work)} done, {len(records)} records...")
            r = f.result()
            if r is not None:
                records.append(r)
            else:
                errors += 1

    print(f"\n{len(records)} records collected ({errors} errors / {len(work)} total)\n")

    if not records:
        print("No records — cannot produce report.")
        return

    # ── Overall accuracy ──────────────────────────────────────────────────────
    total_rate, total_n = acc_rate(records)
    print(f"{'='*70}")
    print(f"  OVERALL DIRECTIONAL ACCURACY: {total_rate:.1f}%  (n={total_n})")
    print(f"  Avg forward return: {avg_return(records):+.2f}%")
    print(f"{'='*70}\n")

    # ── By structure ──────────────────────────────────────────────────────────
    print("BY STRUCTURE:")
    for s in sorted(set(r["structure"] for r in records)):
        grp = [r for r in records if r["structure"] == s]
        print_group(s, grp)

    # ── By phase ─────────────────────────────────────────────────────────────
    print("\nBY PHASE:")
    for ph in sorted(set(r["phase"] for r in records)):
        grp = [r for r in records if r["phase"] == ph]
        print_group(f"Phase {ph}", grp)

    # ── By confidence ─────────────────────────────────────────────────────────
    print("\nBY CONFIDENCE:")
    for cf in ["HIGH", "MEDIUM", "LOW"]:
        grp = [r for r in records if r["confidence"] == cf]
        print_group(cf, grp)

    # ── WVR buckets ───────────────────────────────────────────────────────────
    print("\nBY WAVE VOL RATIO (WVR):")
    wvr_buckets = [
        ("WVR < 0.8  (bear wave dominance)",     lambda r: r["wvr"] < 0.8),
        ("WVR 0.8–1.2 (balanced)",               lambda r: 0.8 <= r["wvr"] < 1.2),
        ("WVR 1.2–1.5 (mild bull)",              lambda r: 1.2 <= r["wvr"] < 1.5),
        ("WVR > 1.5  (strong bull wave)",         lambda r: r["wvr"] >= 1.5),
    ]
    for lbl, pred in wvr_buckets:
        print_group(lbl, [r for r in records if pred(r)])

    # ── FVG bias buckets ──────────────────────────────────────────────────────
    print("\nBY FVG BIAS:")
    fvg_buckets = [
        ("FVG < -0.002 (bearish FVG dominance)",  lambda r: r["fvg_bias"] < -0.002),
        ("FVG -0.002..+0.002 (neutral)",           lambda r: -0.002 <= r["fvg_bias"] <= 0.002),
        ("FVG > +0.002 (bullish FVG dominance)",   lambda r: r["fvg_bias"] > 0.002),
    ]
    for lbl, pred in fvg_buckets:
        print_group(lbl, [r for r in records if pred(r)])

    # ── HH/HL internal structure ──────────────────────────────────────────────
    print("\nBY INTERNAL HL STRUCTURE:")
    hl_buckets = [
        ("LH/LL > HH/HL (bearish internal)",  lambda r: r["hl_bear"] > r["hl_bull"]),
        ("HH/HL = LH/LL (neutral)",            lambda r: r["hl_bear"] == r["hl_bull"]),
        ("HH/HL > LH/LL (bullish internal)",   lambda r: r["hl_bull"] > r["hl_bear"]),
    ]
    for lbl, pred in hl_buckets:
        print_group(lbl, [r for r in records if pred(r)])

    # ── AR ChoCH strength ─────────────────────────────────────────────────────
    print("\nBY AR CHOCH STRENGTH (Ch.16 — size vs prior swings):")
    choch_buckets = [
        ("AR ChoCH < 0.6  (weak → suspect opposite struct)", lambda r: r["ar_choch"] < 0.60),
        ("AR ChoCH 0.6–1.0 (moderate ChoCH)",               lambda r: 0.60 <= r["ar_choch"] < 1.00),
        ("AR ChoCH 1.0–1.5 (good ChoCH)",                   lambda r: 1.00 <= r["ar_choch"] < 1.50),
        ("AR ChoCH > 1.5  (strong ChoCH confirmed)",         lambda r: r["ar_choch"] >= 1.50),
    ]
    for lbl, pred in choch_buckets:
        print_group(lbl, [r for r in records if pred(r)])

    # ── AR volume anatomy (first-half vs second-half) ────────────────────────
    print("\nBY AR VOLUME ANATOMY (Ch.16 — first-half/second-half avg vol ratio):")
    ar_anat_buckets = [
        ("AR anatomy < 0.8  (rising vol = unhealthy AR)",    lambda r: r["ar_vol_anatomy"] < 0.80),
        ("AR anatomy 0.8–1.5 (roughly flat)",                lambda r: 0.80 <= r["ar_vol_anatomy"] < 1.50),
        ("AR anatomy > 1.5  (declining = healthy book anatomy)", lambda r: r["ar_vol_anatomy"] >= 1.50),
    ]
    for lbl, pred in ar_anat_buckets:
        print_group(lbl, [r for r in records if pred(r)])

    # ── AR confirmed (Ch.16: AR ChoCH confirms SC/BC) ────────────────────────
    print("\nBY AR-CONFIRMED (Ch.16 — SC/BC confirmed by AR ChoCH >= 0.3):")
    for val, lbl in [(True, "SC/BC confirmed by AR ChoCH"), (False, "SC/BC NOT confirmed (AR too weak)")]:
        grp = [r for r in records if r["ar_confirmed"] is val]
        print_group(lbl, grp)

    # ── ST above BC (dist reaccumulation signal) ──────────────────────────────
    print("\nBY ST-ABOVE-BC (dist/redist only — Ch.16 reaccumulation signal):")
    for val, lbl in [(True, "ST above BC = Reaccumulation suspected"), (False, "ST below BC = Distribution confirmed")]:
        grp = [r for r in records if r["structure"] in BEARISH_STRUCTS and r["st_above_bc"] is val]
        print_group(lbl, grp)

    # ── ST below SC (acc redistribution signal) ───────────────────────────────
    print("\nBY ST-BELOW-SC (acc/reacc only — Ch.16 redistribution signal):")
    for val, lbl in [(True, "ST below SC = Redistribution suspected"), (False, "ST holds above SC = Accumulation confirmed")]:
        grp = [r for r in records if r["structure"] in BULLISH_STRUCTS and r["st_below_sc"] is val]
        print_group(lbl, grp)

    # ── AR combined quality (size + vol anatomy) ──────────────────────────────
    print("\nAR COMBINED QUALITY (choch_strength > 0.8 AND vol_anatomy > 1.2):")
    grp_good = [r for r in records if r["ar_choch"] > 0.8 and r["ar_vol_anatomy"] > 1.2]
    grp_weak = [r for r in records if r["ar_choch"] <= 0.8 or  r["ar_vol_anatomy"] <= 1.2]
    print_group("AR quality: GOOD (size ✓ + declining vol ✓)", grp_good)
    print_group("AR quality: WEAK (size or vol anatomy fails)", grp_weak)

    # ── Spring type ───────────────────────────────────────────────────────────
    print("\nBY SPRING TYPE (acc structures only):")
    for t in [1, 2, 3]:
        grp = [r for r in records if r["structure"] in BULLISH_STRUCTS and r["spring_type"] == t]
        print_group(f"Spring Type {t}", grp)

    # ── ST position ──────────────────────────────────────────────────────────
    print("\nBY ST POSITION (acc structures only):")
    for pos in ["upper", "lower"]:
        grp = [r for r in records if r["structure"] in BULLISH_STRUCTS and r["st_position"] == pos]
        print_group(f"ST position={pos}", grp)

    # ── HH/HL before SOS ─────────────────────────────────────────────────────
    print("\nBY SOS HH/HL CONFIRMATION (acc structures only):")
    for val in [True, False]:
        grp = [r for r in records if r["structure"] in BULLISH_STRUCTS and r["hh_hl_sos"] is val]
        print_group(f"SOS hh_hl={val}", grp)

    # ── Combined filters — "best" configurations ─────────────────────────────
    print("\nCOMBINED FILTERS — HIGH-CONFIDENCE SUBSETS:")
    combos = [
        ("Acc/Reacc + WVR>1.2 + FVG>0",
            lambda r: r["structure"] in BULLISH_STRUCTS and r["wvr"] > 1.2 and r["fvg_bias"] > 0),
        ("Acc/Reacc + HH/HL dominant + Phase D",
            lambda r: r["structure"] in BULLISH_STRUCTS and r["hl_bull"] > r["hl_bear"] and r["phase"] == "D"),
        ("Acc/Reacc + conf=HIGH or MEDIUM + WVR>1.2",
            lambda r: r["structure"] in BULLISH_STRUCTS and r["confidence"] in ("HIGH","MEDIUM") and r["wvr"] > 1.2),
        ("Dist/Redist + WVR<0.8 + FVG<0",
            lambda r: r["structure"] in BEARISH_STRUCTS and r["wvr"] < 0.8 and r["fvg_bias"] < 0),
        ("Dist/Redist + Phase D + WVR<1.0",
            lambda r: r["structure"] in BEARISH_STRUCTS and r["phase"] == "D" and r["wvr"] < 1.0),
        ("Dist/Redist + conf=HIGH or MEDIUM + WVR<1.2",
            lambda r: r["structure"] in BEARISH_STRUCTS and r["confidence"] in ("HIGH","MEDIUM") and r["wvr"] < 1.2),
        ("Any structure + conf=HIGH",
            lambda r: r["confidence"] == "HIGH"),
    ]
    for lbl, pred in combos:
        print_group(lbl, [r for r in records if pred(r)])

    print(f"\n{'='*70}")
    print("LEGEND: ★ ≥60% directional accuracy  · 50-60%  ✗ <50%")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
