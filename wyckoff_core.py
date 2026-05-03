#!/usr/bin/env python3
"""
Shared Wyckoff analysis core.
Imported by analyze_instrument.py and wyckoff_phase_scanner.py.

Event label conventions:
  verbose=False  →  short scanner labels  ("SOS", "Spring", ...)
  verbose=True   →  descriptive labels    ("SOS — break above range high", ...)
"""

# ── Tuning constants (Problem H) ──────────────────────────────────────────────

RANGE_SPAN_MAX      = 0.20   # (high − low) / avg < this → qualifies as a range
RANGE_SLOPE_MAX     = 0.008  # |linreg slope| / mean_price per bar < this
RANGE_SPAN_GROW_MAX = 1.5    # span jump factor (Problem A2): >50% growth + above floor → boundary
RANGE_SPAN_FLOOR    = 0.05   # absolute span floor before growth heuristic kicks in
RANGE_BOUNDS_DRIFT  = 0.03   # bounds may not drift >3% of avg from initial-window bounds
PHASE_B_SPAN_MAX    = 0.10   # tighter span for Phase B label (current window)
PHASE_B_SLOPE_MAX   = 0.003
SPRING_HEIGHT_RATIO = 0.20   # spring/UTAD pierce ≥ 20% of range height below/above boundary
SPRING_PIERCE_MIN   = 0.002  # absolute floor: ≥ 0.2% of rl/rh price
SC_BC_VOL_MULT      = 2.0    # SC/BC: peak volume must be ≥ this × rolling mean
SC_LOW_TOL          = 1.005  # SC: candle low ≤ rl × this
BC_HIGH_TOL         = 0.995  # BC: candle high ≥ rh × this
LPS_RH_TOL          = 1.015  # LPS: close ≤ rh × this
LPSY_RL_TOL         = 0.985  # LPSY: close ≥ rl × this
SOS_SOW_BUFFER      = 0.002  # SOS: close > rh × (1+buf); SOW: close < rl × (1−buf)
                              # mirrors Spring/UTAD pierce — filters candles testing
                              # the range boundary without decisively breaking out

_RANGE_WIN_MIN   = 10   # minimum window length for adaptive range search
_RANGE_WIN_MAX   = 40   # maximum window length for adaptive range search
_TAIL_WIN        = 15   # tail window for SOS/SOW/Spring/UTAD detection
_MIN_CANDLES     = 30   # absolute floor — fewer returns Insufficient data

# Graduated confidence caps (Problem D):  (min_candles, max_achievable_confidence)
_CONF_CAPS = [(70, "HIGH"), (45, "MEDIUM"), (0, "LOW")]


# ── Event label sets ──────────────────────────────────────────────────────────

_LABEL = {
    False: {
        "sc":     "SC (Selling Climax)",
        "bc":     "BC (Buying Climax)",
        "spring": "Spring",
        "utad":   "UTAD",
        "sos":    "SOS",
        "sow":    "SOW",
        "lps":    "LPS",
        "lpsy":   "LPSY",
        "none":   "No clear events",
    },
    True: {
        "sc":     "Possible SC (Selling Climax)",
        "bc":     "Possible BC (Buying Climax)",
        "spring": "Spring (test below support)",
        "utad":   "UTAD (test above resistance)",
        "sos":    "SOS — break above range high",
        "sow":    "SOW — break below range low",
        "lps":    "LPS — pullback after SOS",
        "lpsy":   "LPSY — pullback after SOW",
        "none":   "No clear Wyckoff events",
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _trend(candles, lookback=30):
    if len(candles) < lookback:
        return "unclear"
    c = [x["close"] for x in candles[-lookback:]]
    mid = lookback // 2
    f, s = sum(c[:mid]) / mid, sum(c[mid:]) / mid
    if s > f * 1.015:  return "bullish"
    if s < f * 0.985:  return "bearish"
    return "neutral"


def _linreg_slope_pct(candles):
    """Linear regression slope as fraction of mean price per bar (signed)."""
    closes = [c["close"] for c in candles]
    n = len(closes)
    if n < 3:
        return 0.0
    mean_c = sum(closes) / n
    if mean_c == 0:
        return 0.0
    mean_i = (n - 1) / 2.0
    num = sum((i - mean_i) * (cl - mean_c) for i, cl in enumerate(closes))
    den = sum((i - mean_i) ** 2 for i in range(n))
    return (num / den / mean_c) if den > 0 else 0.0


def _find_range_window(candles):
    """
    Adaptive range detection (Problem A2 — natural boundary).

    Iterates from the SHORTEST (_RANGE_WIN_MIN) to the LONGEST (_RANGE_WIN_MAX)
    candidate window, extending as long as the window remains a coherent range.
    Stops and returns the LAST valid window when:
      - whole-window span or |slope| exceeds the range thresholds, OR
      - span jumps by >RANGE_SPAN_GROW_MAX (default 50%) AND is above
        RANGE_SPAN_FLOOR — sharp transition (e.g., flat range then sudden trend), OR
      - cumulative bounds drift exceeds RANGE_BOUNDS_DRIFT — the gradual case
        (markdown bleeding in candle by candle, each adding <50% span growth
        but inflating rh/rl over many extensions).

    Why grow-from-shortest, not shrink-from-longest:  In a markdown→range
    structure, the longest qualifying window can dilute the prior trend's
    slope below RANGE_SLOPE_MAX while the high/low still reflects the trend's
    extremes (a "pseudo-range").  Growing from the most-recent end and
    anchoring against the initial bounds finds the natural range boundary.

    Returns (window_candles, range_high, range_low) or (None, None, None).
    """
    n = len(candles)
    last_win = last_h = last_l = None
    last_span = None
    init_h = init_l = None  # bounds from the smallest valid window — the "true range"
    for win_len in range(_RANGE_WIN_MIN, min(_RANGE_WIN_MAX, n) + 1):
        win = candles[-win_len:]
        h   = max(c["high"] for c in win)
        l   = min(c["low"]  for c in win)
        avg = sum(c["close"] for c in win) / win_len
        if avg <= 0:
            continue
        span = (h - l) / avg
        slp  = abs(_linreg_slope_pct(win))
        # Hard threshold: window no longer qualifies as a range — return previous
        if span >= RANGE_SPAN_MAX or slp >= RANGE_SLOPE_MAX:
            return (last_win, last_h, last_l) if last_win else (None, None, None)
        # Sharp transition: span jumps >50% — likely crossed into prior trend
        if (last_span is not None
                and span > last_span * RANGE_SPAN_GROW_MAX
                and span > RANGE_SPAN_FLOOR):
            return (last_win, last_h, last_l)
        # Gradual contamination: bounds drifted >RANGE_BOUNDS_DRIFT from initial
        # (the smallest valid window's bounds = the genuine range bounds)
        if init_h is not None and (
                (h - init_h) / avg > RANGE_BOUNDS_DRIFT
                or (init_l - l) / avg > RANGE_BOUNDS_DRIFT):
            return (last_win, last_h, last_l)
        last_win, last_h, last_l, last_span = win, h, l, span
        if init_h is None:
            init_h, init_l = h, l
    return last_win, last_h, last_l


def _cap_conf(confidence, n_candles):
    """Graduated confidence cap — prevents HIGH/MEDIUM on thin history (Problem D)."""
    # _CONF_CAPS includes (0, "LOW") so the loop always finds a match
    for threshold, cap in _CONF_CAPS:
        if n_candles >= threshold:
            if cap == "HIGH":
                return confidence
            if cap == "MEDIUM":
                return confidence if confidence != "HIGH" else "MEDIUM"
            return "LOW"
    return "LOW"  # safety net if _CONF_CAPS were ever cleared


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze_wyckoff(candles, verbose=False):
    """
    Classifies Wyckoff market structure for a single timeframe candle list.

    Design decisions (referencing original bug-report problem numbers):
      #1+#4  has_range: adaptive window via _find_range_window on candles[:-_TAIL_WIN].
             SOS/SOW/Spring/UTAD/SC/BC are ALL gated — suppressed in trending markets.
      #2     Spring/UTAD are mutually exclusive (most-recent wins).
             Pierce threshold = max(20% of range height, 0.2% of price).
             This prevents normal range oscillations from triggering Spring/UTAD.
             Both events share the _TAIL_WIN window with SOS/SOW (Problem B fix).
      #3     SC/BC: peak volume must be ≥ SC_BC_VOL_MULT × rolling mean.
      #5     < _MIN_CANDLES (30) → Insufficient.  Graduated caps via _cap_conf.
      #6     neutral struct_trend → Reaccumulation (SOS) / Redistribution (SOW).
      #7     Symmetric SOS/SOW; tie on same bar → UNCLEAR/D/LOW.
      ProbA  Adaptive window finds longest genuine range; avoids mixing prior trend.
      ProbA2 Range detection grows from MIN to MAX, stopping at the natural
             boundary (span jumps >50% OR cumulative bounds drift >3%).
             Prevents diluting a markdown's slope below threshold by appending
             range candles.
      ProbA3 Span/slope thresholds relaxed (0.20/0.008) — works with A2's
             boundary detection; tighter thresholds were over-rejecting on HTF.
      v5     SC/BC window moved to pre_tail[-_TAIL_WIN:] — Phase A events must
             not fire on the high-volume SOS/SOW candle in the breakout tail.
             SOS/SOW: close must exceed rh/rl by SOS_SOW_BUFFER (0.2%) to
             filter candles that merely test the range boundary.
      ProbB  Spring/UTAD detection window = same _TAIL_WIN as SOS/SOW (not wider 25c).
      ProbC  Fallback rh/rl = full data min/max (no fake percentile range).
      ProbD  Graduated confidence thresholds (not binary 59 vs 60).
      ProbE  Phase B requires CURRENT ranging (not just past has_range), so a
             post-breakout state without SOS/SOW falls through to Trend.
      H      All thresholds in named constants at module top.

    Returns dict: structure, phase, events, confidence, range_high, range_low,
                  full_trend, recent_trend, ranging
    """
    lbl = _LABEL[bool(verbose)]
    n   = len(candles)

    if not candles or n < _MIN_CANDLES:
        return {
            "structure":    "UNCLEAR",
            "phase":        "Unclear",
            "events":       [lbl["none"]],
            "confidence":   "LOW",
            "range_high":   None,
            "range_low":    None,
            "full_trend":   "unclear",
            "recent_trend": "unclear",
            "ranging":      False,
        }

    # ── Trend analysis ────────────────────────────────────────────────────────
    full_trend = _trend(candles, min(60, n))
    recent_tnd = _trend(candles, min(15, n))

    # ── Range detection (Problem A) ───────────────────────────────────────────
    # Run _find_range_window on candles BEFORE the _TAIL_WIN so the breakout
    # candle cannot inflate rh or deflate rl, which would make SOS impossible.
    pre_tail = candles[:-_TAIL_WIN] if n > _TAIL_WIN + _RANGE_WIN_MIN else candles
    range_win, rh, rl = _find_range_window(pre_tail)
    has_range = range_win is not None

    if not has_range:
        # Problem C: use full span for display; no Phase D/C events will fire
        rh = max(c["high"] for c in candles)
        rl = min(c["low"]  for c in candles)

    last = candles[-1]["close"]

    # Phase B gate: is price currently in a tight consolidation?
    cur_b   = candles[-min(30, n):]
    cur_b_h = max(c["high"]  for c in cur_b)
    cur_b_l = min(c["low"]   for c in cur_b)
    cur_avg = sum(c["close"] for c in cur_b) / len(cur_b)
    ranging = (cur_avg > 0
               and (cur_b_h - cur_b_l) / cur_avg < PHASE_B_SPAN_MAX
               and abs(_linreg_slope_pct(cur_b)) < PHASE_B_SLOPE_MAX)

    # ── SOS / SOW — gated by has_range ────────────────────────────────────────
    # Buffer mirrors Spring/UTAD pierce: a candle merely testing the range
    # boundary (close = rh exactly) does not qualify as a breakout.
    sos_last = sow_last = -1
    if has_range:
        tail = candles[-_TAIL_WIN:]
        sos_thr = rh * (1 + SOS_SOW_BUFFER)
        sow_thr = rl * (1 - SOS_SOW_BUFFER)
        sos_last = max((i for i, c in enumerate(tail)
                        if c["close"] > sos_thr and c["close"] > c["open"]), default=-1)
        sow_last = max((i for i, c in enumerate(tail)
                        if c["close"] < sow_thr and c["close"] < c["open"]), default=-1)
    sos = sos_last >= 0
    sow = sow_last >= 0

    # ── Spring / UTAD — gated by has_range; same _TAIL_WIN; mutually exclusive ─
    # Problem B: using the same tail window as SOS/SOW means Spring/UTAD can only
    # fire on candles AFTER the range (not within it).
    # Dynamic pierce = max(SPRING_HEIGHT_RATIO × range_height, SPRING_PIERCE_MIN × price)
    # prevents normal intra-range oscillations from being misclassified as Springs.
    spring = utad = False
    if has_range:
        range_height  = rh - rl
        spring_pierce = max(range_height * SPRING_HEIGHT_RATIO, rl * SPRING_PIERCE_MIN)
        utad_pierce   = max(range_height * SPRING_HEIGHT_RATIO, rh * SPRING_PIERCE_MIN)

        tail = candles[-_TAIL_WIN:]
        spring_last = max((i for i, c in enumerate(tail)
                           if c["low"] < rl - spring_pierce and c["close"] > rl), default=-1)
        utad_last   = max((i for i, c in enumerate(tail)
                           if c["high"] > rh + utad_pierce  and c["close"] < rh), default=-1)
        if spring_last >= 0 and utad_last >= 0:
            spring = spring_last >= utad_last   # most recent wins; Spring wins tie
            utad   = not spring
        else:
            spring = spring_last >= 0
            utad   = utad_last   >= 0

    # ── SC / BC — in the last _TAIL_WIN candles of pre_tail ──────────────────
    # SC/BC are Phase A events (climax at the START of a range), not breakout
    # events.  Searching the breakout tail wrongly flags the high-volume SOS/SOW
    # candle as BC/SC.  Searching in pre_tail[-_TAIL_WIN:] targets the end of
    # the consolidation where the climactic reversal actually occurs.
    sc = bc = False
    if has_range and len(pre_tail) >= _TAIL_WIN:
        sc_bc_win = pre_tail[-_TAIL_WIN:]
        avg_vol = sum(c["volume"] for c in sc_bc_win) / len(sc_bc_win)
        if avg_vol > 0:
            mvc = max(sc_bc_win, key=lambda c: c["volume"])
            if mvc["volume"] >= avg_vol * SC_BC_VOL_MULT:
                sc = mvc["close"] < mvc["open"] and mvc["low"]  <= rl * SC_LOW_TOL
                bc = mvc["close"] > mvc["open"] and mvc["high"] >= rh * BC_HIGH_TOL

    lps  = sos and last <= rh * LPS_RH_TOL  and last >= rl
    lpsy = sow and last >= rl * LPSY_RL_TOL and last <= rh

    events = []
    if sc:     events.append(lbl["sc"])
    if bc:     events.append(lbl["bc"])
    if spring: events.append(lbl["spring"])
    if utad:   events.append(lbl["utad"])
    if sos:    events.append(lbl["sos"])
    if sow:    events.append(lbl["sow"])
    if lps:    events.append(lbl["lps"])
    if lpsy:   events.append(lbl["lpsy"])
    if not events:
        events.append(lbl["none"])

    # struct_trend: measured BEFORE the tail so the breakout itself cannot
    # reclassify Accumulation → Reaccumulation between consecutive scans.
    pre_struct   = candles[:-_TAIL_WIN] if n > _TAIL_WIN + 20 else candles
    struct_trend = _trend(pre_struct, min(120, len(pre_struct)))

    # ── Phase determination ───────────────────────────────────────────────────

    if sos and sow and sos_last == sow_last:
        structure, phase = "UNCLEAR", "D"
        confidence = "LOW"

    elif sos and (not sow or sos_last > sow_last):
        # Bearish prior → fresh bottom → Accumulation
        # Neutral or bullish → continuation/retest → Reaccumulation
        structure  = "Accumulation" if struct_trend == "bearish" else "Reaccumulation"
        phase      = "D"
        confidence = "HIGH" if (spring and lps) else ("MEDIUM" if (spring or lps) else "LOW")

    elif sow and (not sos or sow_last > sos_last):
        # Bullish prior → fresh top → Distribution
        # Neutral or bearish → continuation → Redistribution
        structure  = "Distribution" if struct_trend == "bullish" else "Redistribution"
        phase      = "D"
        confidence = "HIGH" if (utad and lpsy) else ("MEDIUM" if (utad or lpsy) else "LOW")

    elif spring:
        structure  = "Accumulation" if struct_trend == "bearish" else "Reaccumulation"
        phase      = "C"
        confidence = "MEDIUM"

    elif utad:
        structure  = "Distribution" if struct_trend == "bullish" else "Redistribution"
        phase      = "C"
        confidence = "MEDIUM"

    elif ranging:
        # Phase B requires CURRENT consolidation.  has_range alone (a past range
        # in pre_tail) without current tightness means the structure has broken
        # out without firing SOS/SOW/Spring/UTAD — fall through to Trend/Unclear.
        structure  = ("Accumulation" if full_trend == "bearish" else
                      "Distribution" if full_trend == "bullish" else "UNCLEAR")
        phase      = "B"
        confidence = "LOW"

    else:
        structure  = ("Trend (Bullish)" if full_trend == "bullish" else
                      "Trend (Bearish)" if full_trend == "bearish" else "UNCLEAR")
        phase      = "E" if full_trend != "unclear" else "Unclear"
        confidence = "MEDIUM" if full_trend != "unclear" else "LOW"

    # Apply graduated confidence cap based on available candle count
    confidence = _cap_conf(confidence, n)

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
