"""
Wave 3 Opportunity Strategy

Detects setups where:
  Wave 1 = impulsive move (X → A)
  Wave 2 = corrective move (A → D) with retracement in valid zone
  D ≈ POC of Wave 1
  Optional AB=CD harmonic pattern
  BOS (Break of Structure) as confirmation

No I/O, no side effects — pure analysis on a DataFrame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

SignalType = Literal[
    "NO_SIGNAL",
    "LONG_SETUP",
    "SHORT_SETUP",
    "WAITING_FOR_BOS",
    "INVALID",
]


@dataclass
class StrategyConfig:
    swing_left: int = 3
    swing_right: int = 3

    min_wave1_atr: float = 2.0

    retracement_min: float = 0.382
    retracement_max: float = 0.886

    preferred_retracement_min: float = 0.5
    preferred_retracement_max: float = 0.786

    poc_distance_atr: float = 0.5

    harmonic_tolerance: float = 0.08

    atr_period: int = 14

    require_bos: bool = True

    min_rr_to_wave1_high: float = 1.5

    poc_bins: int = 50

    # TASK 3 — Signal freshness
    max_bars_after_bos: int = 1

    # TASK 5 — Time limits on wave structure
    max_wave1_bars: int = 100        # A index - X index
    max_correction_bars: int = 150   # D index - A index
    max_bars_after_d: int = 50       # as_of_index - D index

    # TASK 6 — Harmonic optional
    require_harmonic: bool = False


@dataclass
class SwingPoint:
    index: int
    timestamp: pd.Timestamp
    price: float
    kind: Literal["HIGH", "LOW"]
    # index from which this pivot was actually knowable (no lookahead)
    confirmed_index: int


@dataclass
class WaveStructure:
    direction: Literal["LONG", "SHORT"]

    x_index: int
    a_index: int
    b_index: Optional[int]
    c_index: Optional[int]
    d_index: Optional[int]

    x_price: float
    a_price: float
    d_price: Optional[float]

    wave1_size: float
    retracement: Optional[float]

    poc_price: Optional[float]
    distance_to_poc_atr: Optional[float]

    harmonic_valid: bool
    poc_valid: bool
    retracement_valid: bool
    bos_valid: bool

    # extra detail
    b_price: Optional[float] = None
    c_price: Optional[float] = None
    preferred_retracement: bool = False

    # TASK 4 — time-based IDs
    x_time: Optional[pd.Timestamp] = None
    a_time: Optional[pd.Timestamp] = None
    b_time: Optional[pd.Timestamp] = None
    c_time: Optional[pd.Timestamp] = None
    d_time: Optional[pd.Timestamp] = None


@dataclass
class StrategyResult:
    signal: SignalType
    direction: Optional[Literal["LONG", "SHORT"]]

    entry_price: Optional[float]
    stop_loss: Optional[float]

    target_1: Optional[float]
    target_2: Optional[float]
    target_3: Optional[float]

    risk_reward_to_t1: Optional[float]
    risk_reward_to_t2: Optional[float]
    risk_reward_to_t3: Optional[float]

    wave: Optional[WaveStructure]

    reason: str

    # TASK 3 — Signal freshness metadata
    bos_index: Optional[int] = None
    bos_price: Optional[float] = None
    signal_index: Optional[int] = None
    signal_time: Optional[pd.Timestamp] = None

    # TASK 4 — Setup ID
    setup_id: Optional[str] = None


# ---------------------------------------------------------------------------
# TASK 1 — OHLCV normalisation
# ---------------------------------------------------------------------------

def normalize_ohlcv_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise an OHLCV DataFrame so that downstream functions receive a
    consistent layout:
      - Required columns present: open, high, low, close, volume
      - 'timestamp' column exists as pd.Timestamp values (ascending)
      - Index is a plain RangeIndex (0 … N-1)
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"DataFrame is missing required OHLCV columns: {sorted(missing)}"
        )

    df = df.copy()

    # Step 1 — ensure 'timestamp' column exists
    if "timestamp" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            # Capture values as numpy array *before* resetting the index so
            # there is no "timestamp is both an index level and a column label"
            # ambiguity when pandas 2+ enforces uniqueness.
            ts_values = df.index.values
            df = df.reset_index(drop=True)   # RangeIndex, no "timestamp" in index
            df["timestamp"] = pd.to_datetime(ts_values)
        else:
            raise ValueError(
                "DataFrame has no 'timestamp' column and the index is not a DatetimeIndex."
            )
    else:
        ts = df["timestamp"]
        # Numeric → milliseconds (Bybit format)
        if pd.api.types.is_numeric_dtype(ts):
            df["timestamp"] = pd.to_datetime(ts, unit="ms")
        else:
            df["timestamp"] = pd.to_datetime(ts)

    # Step 2 — drop any non-Range index to eliminate further ambiguity
    if not isinstance(df.index, pd.RangeIndex) or df.index.name is not None:
        df = df.reset_index(drop=True)

    # Step 3 — sort ascending and reset to clean RangeIndex
    df = df.sort_values("timestamp").reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """True Range ATR, same formula used across all modules."""
    high = df["high"]
    low = df["low"]
    close = df["close"].shift(1)

    tr = pd.concat([
        high - low,
        (high - close).abs(),
        (low - close).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean()


# ---------------------------------------------------------------------------
# Swing detection — no lookahead
# ---------------------------------------------------------------------------

def detect_swings(
    df: pd.DataFrame,
    left: int,
    right: int,
) -> List[SwingPoint]:
    """
    Pivot high / low detection.

    confirmed_index = i + right  (the candle at which we *know* the pivot)
    We never use a pivot before its confirmed_index.

    Reads timestamps from the 'timestamp' column (always present after
    normalize_ohlcv_dataframe).  Falls back to the index if needed.
    """
    highs = df["high"].values
    lows = df["low"].values

    if "timestamp" in df.columns:
        timestamps = df["timestamp"].values
    elif isinstance(df.index, pd.DatetimeIndex):
        timestamps = df.index
    else:
        timestamps = pd.RangeIndex(len(df))

    swings: List[SwingPoint] = []
    n = len(df)

    for i in range(left, n - right):
        # Swing high
        if all(highs[i] > highs[i - j] for j in range(1, left + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, right + 1)):
            swings.append(SwingPoint(
                index=i,
                timestamp=pd.Timestamp(timestamps[i]),
                price=highs[i],
                kind="HIGH",
                confirmed_index=i + right,
            ))

        # Swing low
        if all(lows[i] < lows[i - j] for j in range(1, left + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, right + 1)):
            swings.append(SwingPoint(
                index=i,
                timestamp=pd.Timestamp(timestamps[i]),
                price=lows[i],
                kind="LOW",
                confirmed_index=i + right,
            ))

    swings.sort(key=lambda s: s.index)
    return swings


# ---------------------------------------------------------------------------
# Volume POC for a candle range
# ---------------------------------------------------------------------------

def calculate_volume_poc(
    df: pd.DataFrame,
    start_index: int,
    end_index: int,
    bins: int = 50,
) -> float:
    """
    Simple volume profile POC between start_index and end_index (inclusive).
    Uses typical_price = (H + L + C) / 3 to assign each candle to a bin.
    Returns the price of the bin centre with the highest cumulative volume.
    """
    sub = df.iloc[start_index : end_index + 1]
    if sub.empty:
        mid = (df.iloc[start_index]["high"] + df.iloc[start_index]["low"]) / 2
        return mid

    typical = (sub["high"] + sub["low"] + sub["close"]) / 3
    price_min = sub["low"].min()
    price_max = sub["high"].max()

    if price_max == price_min:
        return (price_max + price_min) / 2

    bin_size = (price_max - price_min) / bins
    bin_indices = ((typical - price_min) / bin_size).clip(0, bins - 1).astype(int)

    vol_per_bin = np.zeros(bins)
    for idx, vol in zip(bin_indices, sub["volume"].values):
        vol_per_bin[idx] += vol

    best_bin = int(np.argmax(vol_per_bin))
    poc_price = price_min + (best_bin + 0.5) * bin_size
    return poc_price


# ---------------------------------------------------------------------------
# Wave 1 detection
# ---------------------------------------------------------------------------

def _find_wave1_candidates(
    swings: List[SwingPoint],
    direction: Literal["LONG", "SHORT"],
    df: pd.DataFrame,
    atr: pd.Series,
    config: StrategyConfig,
    as_of_index: int,
) -> List[tuple]:
    """
    Returns list of (x, a) SwingPoint pairs that qualify as wave 1.
    Only uses swings confirmed by as_of_index.

    For LONG: pairs each LOW swing with ALL subsequent HIGHs (all pairs logic)
      — this captures impulses where there are intermediate highs or lows
      before the main pivot.
    For SHORT: pairs each HIGH with ALL subsequent LOWs.

    TASK 5: wave1 (A index - X index) must not exceed config.max_wave1_bars.
    """
    confirmed = [s for s in swings if s.confirmed_index <= as_of_index]
    candidates = []

    if direction == "LONG":
        x_kind, a_kind = "LOW", "HIGH"
    else:
        x_kind, a_kind = "HIGH", "LOW"

    for i, x in enumerate(confirmed):
        if x.kind != x_kind:
            continue
        # Find ALL swings of a_kind that come after x and meet the size filter.
        # We keep all of them so find_latest_wave_structure can pick the best
        # structural fit (typically the most recent / largest impulse).
        for a in confirmed[i + 1:]:
            if a.kind != a_kind:
                continue

            # TASK 5 — cap wave 1 duration
            if (a.index - x.index) > config.max_wave1_bars:
                continue

            if direction == "LONG":
                wave_size = a.price - x.price
            else:
                wave_size = x.price - a.price

            if wave_size <= 0:
                continue  # another a_kind swing later may still qualify

            local_atr = atr.iloc[min(a.index, len(atr) - 1)]
            if local_atr > 0 and wave_size >= config.min_wave1_atr * local_atr:
                candidates.append((x, a))

    # Sort so reversed() gives most-recent (highest a.index) first
    candidates.sort(key=lambda pair: (pair[1].index, pair[0].index))
    return candidates


# ---------------------------------------------------------------------------
# Retracement validation
# ---------------------------------------------------------------------------

def validate_retracement(
    wave1_size: float,
    a_price: float,
    d_price: float,
    direction: Literal["LONG", "SHORT"],
    config: StrategyConfig,
) -> tuple[bool, float, bool]:
    """Returns (valid, retracement_ratio, is_preferred_zone)."""
    if direction == "LONG":
        retracement = (a_price - d_price) / wave1_size
    else:
        retracement = (d_price - a_price) / wave1_size

    if retracement < 0:
        return False, retracement, False

    valid = config.retracement_min <= retracement <= config.retracement_max
    preferred = config.preferred_retracement_min <= retracement <= config.preferred_retracement_max
    return valid, retracement, preferred


# ---------------------------------------------------------------------------
# POC touch validation
# ---------------------------------------------------------------------------

def validate_poc_touch(
    d_price: float,
    poc_price: float,
    atr_at_d: float,
    config: StrategyConfig,
) -> tuple[bool, float]:
    """Returns (poc_valid, distance_in_atr)."""
    distance = abs(d_price - poc_price)
    distance_atr = distance / atr_at_d if atr_at_d > 0 else float("inf")
    return distance_atr <= config.poc_distance_atr, distance_atr


# ---------------------------------------------------------------------------
# AB=CD harmonic
# ---------------------------------------------------------------------------

def validate_abcd_harmonic(
    a_price: float,
    b_price: float,
    c_price: float,
    d_price: float,
    direction: Literal["LONG", "SHORT"],
    config: StrategyConfig,
) -> bool:
    """
    AB=CD equality check.
    LONG:  AB = A - B,  CD = C - D
    SHORT: AB = B - A,  CD = D - C
    Accept if |CD/AB - 1| <= harmonic_tolerance.
    """
    if direction == "LONG":
        ab = a_price - b_price
        cd = c_price - d_price
    else:
        ab = b_price - a_price
        cd = d_price - c_price

    if ab <= 0 or cd <= 0:
        return False

    ratio = cd / ab
    return abs(ratio - 1.0) <= config.harmonic_tolerance


# ---------------------------------------------------------------------------
# BOS validation
# ---------------------------------------------------------------------------

def validate_bos(
    df: pd.DataFrame,
    d_index: int,
    c_price: float,
    direction: Literal["LONG", "SHORT"],
    as_of_index: int,
) -> tuple[bool, Optional[int], Optional[float]]:
    """
    Scans candles after d_index up to as_of_index for a BOS candle.
    LONG BOS:  close > c_price
    SHORT BOS: close < c_price
    Returns (bos_found, bos_bar_index, bos_close_price).
    """
    for i in range(d_index + 1, as_of_index + 1):
        if i >= len(df):
            break
        close = df["close"].iloc[i]
        if direction == "LONG" and close > c_price:
            return True, i, close
        if direction == "SHORT" and close < c_price:
            return True, i, close
    return False, None, None


# ---------------------------------------------------------------------------
# TASK 7 — Scored BCD search
# ---------------------------------------------------------------------------

def _score_bcd_candidate(
    a: SwingPoint,
    b: SwingPoint,
    c: SwingPoint,
    d: SwingPoint,
    wave1_size: float,
    poc_price: float,
    atr_at_d: float,
    direction: Literal["LONG", "SHORT"],
    config: StrategyConfig,
    max_d_index: int,
) -> float:
    """
    Score a (B, C, D) candidate combination. Higher is better.

    Components:
      retracement_valid   +20
      preferred_retracement +10
      poc_valid           +15; partial credit for near-misses
      harmonic_valid      +5; partial credit from ratio closeness
      freshness           +5 * ((d.index - a.index) / (max_d_index - a.index))
      estimated RR        +5 * min(rr_to_a / 5, 1.0)
    """
    score = 0.0

    # --- retracement ---
    ret_valid, retracement, preferred = validate_retracement(
        wave1_size, a.price, d.price, direction, config
    )
    if ret_valid:
        score += 20.0
    if preferred:
        score += 10.0

    # --- POC ---
    distance = abs(d.price - poc_price)
    distance_atr = distance / atr_at_d if atr_at_d > 0 else float("inf")
    poc_valid = distance_atr <= config.poc_distance_atr
    if poc_valid:
        score += 15.0
    else:
        # partial credit: linear decay within 5× the threshold
        max_dist = config.poc_distance_atr * 5.0
        partial = max(0.0, 5.0 * (1.0 - distance_atr / max_dist)) if max_dist > 0 else 0.0
        score += partial

    # --- harmonic ---
    if b is not None and c is not None:
        if direction == "LONG":
            ab = a.price - b.price
            cd = c.price - d.price
        else:
            ab = b.price - a.price
            cd = d.price - c.price

        if ab > 0 and cd > 0:
            ratio = cd / ab
            deviation = abs(ratio - 1.0)
            if deviation <= config.harmonic_tolerance:
                score += 5.0
            else:
                # partial credit proportional to closeness
                partial = max(0.0, 5.0 * (1.0 - deviation / max(config.harmonic_tolerance, 1e-9)))
                score += partial

    # --- freshness: prefer more-recent D ---
    if max_d_index > a.index:
        freshness = (d.index - a.index) / (max_d_index - a.index)
        score += 5.0 * freshness

    # --- estimated R:R to A (wave 1 high/low) ---
    if direction == "LONG":
        potential = a.price - d.price
        risk_approx = 0.1 * atr_at_d if atr_at_d > 0 else 1e-9
    else:
        potential = d.price - a.price
        risk_approx = 0.1 * atr_at_d if atr_at_d > 0 else 1e-9

    rr_to_a = potential / risk_approx if risk_approx > 0 else 0.0
    score += 5.0 * min(rr_to_a / 5.0, 1.0)

    return score


def _find_best_bcd(
    post_a: List[SwingPoint],
    a: SwingPoint,
    x: SwingPoint,
    wave1_size: float,
    poc_price: float,
    atr: pd.Series,
    direction: Literal["LONG", "SHORT"],
    config: StrategyConfig,
    as_of_index: int,
) -> Optional[Tuple[SwingPoint, SwingPoint, SwingPoint]]:
    """
    Generate up to 5 B candidates × 5 C per B × 5 D per C (max 125 combinations).
    Apply time-limit filters, score each, return the best (B, C, D) or None.

    TASK 5 filters applied here:
      D.index - A.index <= max_correction_bars
      as_of_index - D.index <= max_bars_after_d
    """
    if direction == "LONG":
        b_kind = "LOW"
        c_kind = "HIGH"
        d_kind = "LOW"
    else:
        b_kind = "HIGH"
        c_kind = "LOW"
        d_kind = "HIGH"

    # Up to 5 B candidates (earliest first among those after A)
    b_pool = [s for s in post_a if s.kind == b_kind][:5]
    if not b_pool:
        return None

    # Maximum D index we'd ever consider
    max_d_index = as_of_index

    best_score = float("-inf")
    best_combo: Optional[Tuple[SwingPoint, SwingPoint, SwingPoint]] = None

    for b in b_pool:
        # Up to 5 C candidates after B
        c_pool = [s for s in post_a if s.kind == c_kind and s.index > b.index][:5]
        for c in c_pool:
            # Up to 5 D candidates after C
            d_pool_raw = [
                s for s in post_a
                if s.kind == d_kind and s.index > c.index
            ]
            d_pool = []
            for d in d_pool_raw:
                # TASK 5 — correction must not be too old or too long
                if (d.index - a.index) > config.max_correction_bars:
                    continue
                if (as_of_index - d.index) > config.max_bars_after_d:
                    continue
                d_pool.append(d)
                if len(d_pool) == 5:
                    break

            for d in d_pool:
                atr_at_d = atr.iloc[min(d.index, len(atr) - 1)]
                s = _score_bcd_candidate(
                    a, b, c, d,
                    wave1_size, poc_price, atr_at_d,
                    direction, config, max_d_index,
                )
                if s > best_score:
                    best_score = s
                    best_combo = (b, c, d)

    return best_combo


# ---------------------------------------------------------------------------
# Core wave structure finder — TASK 6: returns all candidates, scored
# ---------------------------------------------------------------------------

def _score_wave_structure(wave: WaveStructure) -> float:
    """Score a WaveStructure for ranking (higher = preferred)."""
    score = 0.0
    if wave.retracement_valid:
        score += 5.0
    if wave.preferred_retracement:
        score += 10.0
    if wave.poc_valid:
        score += 15.0
    if wave.harmonic_valid:
        score += 5.0
    if wave.distance_to_poc_atr is not None:
        score += max(0.0, 3.0 * (1.0 - wave.distance_to_poc_atr))
    return score


def find_wave_structures(
    df: pd.DataFrame,
    swings: List[SwingPoint],
    atr: pd.Series,
    config: StrategyConfig,
    direction: Literal["LONG", "SHORT"],
    as_of_index: int,
) -> List[WaveStructure]:
    """
    Find ALL wave 1 + wave 2 structures in the given direction.
    Returns a list sorted best-first by _score_wave_structure.

    Uses _find_best_bcd (TASK 7) for scored B/C/D selection within each
    wave-1 candidate.  Enforces max_wave1_bars via _find_wave1_candidates.
    """
    candidates = _find_wave1_candidates(
        swings, direction, df, atr, config, as_of_index
    )
    if not candidates:
        return []

    confirmed = [s for s in swings if s.confirmed_index <= as_of_index]
    structures: List[WaveStructure] = []

    for x, a in reversed(candidates):
        wave1_size = abs(a.price - x.price)
        poc_price = calculate_volume_poc(df, x.index, a.index, config.poc_bins)
        post_a = [s for s in confirmed if s.index > a.index]

        best = _find_best_bcd(
            post_a, a, x, wave1_size, poc_price, atr, direction, config, as_of_index
        )
        if best is None:
            continue

        b, c, d = best

        ret_valid, retracement, preferred = validate_retracement(
            wave1_size, a.price, d.price, direction, config
        )
        atr_at_d = atr.iloc[min(d.index, len(atr) - 1)]
        poc_valid, dist_atr = validate_poc_touch(d.price, poc_price, atr_at_d, config)
        harmonic_valid = validate_abcd_harmonic(
            a.price, b.price, c.price, d.price, direction, config
        )

        def _ts(sp: SwingPoint) -> Optional[pd.Timestamp]:
            if "timestamp" in df.columns:
                return pd.Timestamp(df["timestamp"].iloc[sp.index])
            return sp.timestamp

        wave = WaveStructure(
            direction=direction,
            x_index=x.index, a_index=a.index, b_index=b.index,
            c_index=c.index, d_index=d.index,
            x_price=x.price, a_price=a.price, d_price=d.price,
            b_price=b.price, c_price=c.price,
            wave1_size=wave1_size, retracement=retracement,
            poc_price=poc_price, distance_to_poc_atr=dist_atr,
            harmonic_valid=harmonic_valid, poc_valid=poc_valid,
            retracement_valid=ret_valid, bos_valid=False,
            preferred_retracement=preferred,
            x_time=_ts(x), a_time=_ts(a), b_time=_ts(b),
            c_time=_ts(c), d_time=_ts(d),
        )
        structures.append(wave)

    structures.sort(key=_score_wave_structure, reverse=True)
    return structures


# ---------------------------------------------------------------------------
# RR calculation helpers
# ---------------------------------------------------------------------------

def _long_result(
    wave: WaveStructure,
    entry: float,
    atr_val: float,
    config: StrategyConfig,
    signal: SignalType,
    reason: str,
    bos_index: Optional[int] = None,
    bos_price: Optional[float] = None,
    signal_index: Optional[int] = None,
    signal_time: Optional[pd.Timestamp] = None,
) -> StrategyResult:
    sl = wave.d_price - 0.1 * atr_val
    risk = entry - sl
    setup_id = _make_setup_id(wave)
    if risk <= 0:
        return StrategyResult(
            signal="INVALID", direction="LONG",
            entry_price=entry, stop_loss=sl,
            target_1=None, target_2=None, target_3=None,
            risk_reward_to_t1=None, risk_reward_to_t2=None, risk_reward_to_t3=None,
            wave=wave, reason="Invalid risk calculation",
            bos_index=bos_index, bos_price=bos_price,
            signal_index=signal_index, signal_time=signal_time,
            setup_id=setup_id,
        )
    t1 = wave.a_price
    t2 = wave.a_price + 1.272 * wave.wave1_size
    t3 = wave.a_price + 1.618 * wave.wave1_size

    rr1 = (t1 - entry) / risk
    rr2 = (t2 - entry) / risk
    rr3 = (t3 - entry) / risk

    if rr1 < config.min_rr_to_wave1_high:
        return StrategyResult(
            signal="INVALID", direction="LONG",
            entry_price=entry, stop_loss=sl,
            target_1=t1, target_2=t2, target_3=t3,
            risk_reward_to_t1=rr1, risk_reward_to_t2=rr2, risk_reward_to_t3=rr3,
            wave=wave, reason="Risk reward too low",
            bos_index=bos_index, bos_price=bos_price,
            signal_index=signal_index, signal_time=signal_time,
            setup_id=setup_id,
        )

    return StrategyResult(
        signal=signal, direction="LONG",
        entry_price=entry, stop_loss=sl,
        target_1=t1, target_2=t2, target_3=t3,
        risk_reward_to_t1=rr1, risk_reward_to_t2=rr2, risk_reward_to_t3=rr3,
        wave=wave, reason=reason,
        bos_index=bos_index, bos_price=bos_price,
        signal_index=signal_index, signal_time=signal_time,
        setup_id=setup_id,
    )


def _short_result(
    wave: WaveStructure,
    entry: float,
    atr_val: float,
    config: StrategyConfig,
    signal: SignalType,
    reason: str,
    bos_index: Optional[int] = None,
    bos_price: Optional[float] = None,
    signal_index: Optional[int] = None,
    signal_time: Optional[pd.Timestamp] = None,
) -> StrategyResult:
    sl = wave.d_price + 0.1 * atr_val
    risk = sl - entry
    setup_id = _make_setup_id(wave)
    if risk <= 0:
        return StrategyResult(
            signal="INVALID", direction="SHORT",
            entry_price=entry, stop_loss=sl,
            target_1=None, target_2=None, target_3=None,
            risk_reward_to_t1=None, risk_reward_to_t2=None, risk_reward_to_t3=None,
            wave=wave, reason="Invalid risk calculation",
            bos_index=bos_index, bos_price=bos_price,
            signal_index=signal_index, signal_time=signal_time,
            setup_id=setup_id,
        )
    t1 = wave.a_price
    t2 = wave.a_price - 1.272 * wave.wave1_size
    t3 = wave.a_price - 1.618 * wave.wave1_size

    rr1 = (entry - t1) / risk
    rr2 = (entry - t2) / risk
    rr3 = (entry - t3) / risk

    if rr1 < config.min_rr_to_wave1_high:
        return StrategyResult(
            signal="INVALID", direction="SHORT",
            entry_price=entry, stop_loss=sl,
            target_1=t1, target_2=t2, target_3=t3,
            risk_reward_to_t1=rr1, risk_reward_to_t2=rr2, risk_reward_to_t3=rr3,
            wave=wave, reason="Risk reward too low",
            bos_index=bos_index, bos_price=bos_price,
            signal_index=signal_index, signal_time=signal_time,
            setup_id=setup_id,
        )

    return StrategyResult(
        signal=signal, direction="SHORT",
        entry_price=entry, stop_loss=sl,
        target_1=t1, target_2=t2, target_3=t3,
        risk_reward_to_t1=rr1, risk_reward_to_t2=rr2, risk_reward_to_t3=rr3,
        wave=wave, reason=reason,
        bos_index=bos_index, bos_price=bos_price,
        signal_index=signal_index, signal_time=signal_time,
        setup_id=setup_id,
    )


# ---------------------------------------------------------------------------
# _no_signal helper
# ---------------------------------------------------------------------------

def _no_signal(reason: str, wave: Optional[WaveStructure] = None) -> StrategyResult:
    return StrategyResult(
        signal="NO_SIGNAL", direction=None,
        entry_price=None, stop_loss=None,
        target_1=None, target_2=None, target_3=None,
        risk_reward_to_t1=None, risk_reward_to_t2=None, risk_reward_to_t3=None,
        wave=wave, reason=reason,
    )


# ---------------------------------------------------------------------------
# TASK 4 — Setup ID helper
# ---------------------------------------------------------------------------

def _make_setup_id(wave: WaveStructure) -> str:
    """
    Build a stable setup identifier from wave timestamps (not bar indices).
    Format: {direction}_{x_time}_{a_time}_{b_time}_{c_time}_{d_time}
    """
    return (
        f"{wave.direction}"
        f"_{wave.x_time}"
        f"_{wave.a_time}"
        f"_{wave.b_time}"
        f"_{wave.c_time}"
        f"_{wave.d_time}"
    )


# ---------------------------------------------------------------------------
# TASK 2 — Direction evaluator
# ---------------------------------------------------------------------------

def _signal_rank(signal: SignalType) -> int:
    """Higher rank = preferred result."""
    return {
        "INVALID": 0,
        "NO_SIGNAL": 1,
        "WAITING_FOR_BOS": 2,
        "LONG_SETUP": 3,
        "SHORT_SETUP": 3,
    }.get(signal, 0)


def _validate_and_signal(
    wave: WaveStructure,
    df: pd.DataFrame,
    atr: pd.Series,
    config: StrategyConfig,
    direction: Literal["LONG", "SHORT"],
    as_of_index: int,
) -> StrategyResult:
    """
    Apply all validation gates to a single wave structure and produce a
    StrategyResult.  Called by _evaluate_direction for each candidate wave.
    """
    if not wave.retracement_valid:
        retrace_val = wave.retracement or 0.0
        if retrace_val < config.retracement_min:
            return _no_signal("Wave 1 found, but retracement too shallow", wave)
        return _no_signal("Wave 1 found, but retracement too deep", wave)

    if not wave.poc_valid:
        return _no_signal("Retracement valid, but D is too far from POC", wave)

    # TASK 6 — Harmonic gate (optional)
    if config.require_harmonic and not wave.harmonic_valid:
        return _no_signal("Harmonic pattern required but not found", wave)

    harmonic_note = (
        "POC valid, harmonic valid" if wave.harmonic_valid
        else "POC valid, harmonic invalid"
    )

    c_price = wave.c_price
    if c_price is None:
        return _no_signal("Wave structure incomplete (no C point)", wave)

    atr_val = atr.iloc[min(wave.d_index, len(atr) - 1)]

    if config.require_bos:
        bos_found, bos_idx, bos_close = validate_bos(
            df, wave.d_index, c_price, direction, as_of_index
        )
        if not bos_found:
            wave.bos_valid = False
            return StrategyResult(
                signal="WAITING_FOR_BOS",
                direction=direction,
                entry_price=None, stop_loss=None,
                target_1=None, target_2=None, target_3=None,
                risk_reward_to_t1=None, risk_reward_to_t2=None, risk_reward_to_t3=None,
                wave=wave,
                reason=f"Valid structure, waiting for BOS ({harmonic_note})",
            )

        # TASK 3 — BOS freshness check
        if (as_of_index - bos_idx) > config.max_bars_after_bos:
            wave.bos_valid = False
            return _no_signal(
                f"BOS found but too old ({as_of_index - bos_idx} bars ago)",
                wave,
            )

        wave.bos_valid = True
        entry = bos_close
        signal_label: SignalType = "LONG_SETUP" if direction == "LONG" else "SHORT_SETUP"
        reason = (
            f"{'LONG' if direction == 'LONG' else 'SHORT'} setup confirmed after BOS "
            f"({harmonic_note})"
        )

        # TASK 1 — signal_index/time refer to the BOS candle, not as_of
        signal_index = bos_idx
        signal_time = (
            pd.Timestamp(df["timestamp"].iloc[bos_idx])
            if "timestamp" in df.columns else None
        )

        if direction == "LONG":
            return _long_result(
                wave, entry, atr_val, config, signal_label, reason,
                bos_index=bos_idx, bos_price=bos_close,
                signal_index=signal_index, signal_time=signal_time,
            )
        else:
            return _short_result(
                wave, entry, atr_val, config, signal_label, reason,
                bos_index=bos_idx, bos_price=bos_close,
                signal_index=signal_index, signal_time=signal_time,
            )

    else:
        # No BOS required — entry at close of D candle
        wave.bos_valid = False
        entry = df["close"].iloc[wave.d_index]
        signal_label = "LONG_SETUP" if direction == "LONG" else "SHORT_SETUP"
        reason = (
            f"{'LONG' if direction == 'LONG' else 'SHORT'} setup at D "
            f"(BOS not required, {harmonic_note})"
        )

        # TASK 1 — signal_index/time refer to the D candle
        signal_index = wave.d_index
        signal_time = (
            pd.Timestamp(df["timestamp"].iloc[wave.d_index])
            if "timestamp" in df.columns else None
        )

        if direction == "LONG":
            return _long_result(
                wave, entry, atr_val, config, signal_label, reason,
                signal_index=signal_index, signal_time=signal_time,
            )
        else:
            return _short_result(
                wave, entry, atr_val, config, signal_label, reason,
                signal_index=signal_index, signal_time=signal_time,
            )


def _evaluate_direction(
    df: pd.DataFrame,
    swings: List[SwingPoint],
    atr: pd.Series,
    config: StrategyConfig,
    direction: Literal["LONG", "SHORT"],
    as_of_index: int,
) -> StrategyResult:
    """
    Evaluate one direction independently.

    TASK 6: collects all wave structures, sorts by score, iterates to find the
    best actionable signal (highest _signal_rank wins across all candidates).
    """
    waves = find_wave_structures(df, swings, atr, config, direction, as_of_index)
    if not waves:
        return _no_signal(f"No valid wave 1 found for {direction}")

    best_result: Optional[StrategyResult] = None
    best_rank = 0

    for wave in waves:
        result = _validate_and_signal(wave, df, atr, config, direction, as_of_index)
        rank = _signal_rank(result.signal)
        if rank > best_rank:
            best_rank = rank
            best_result = result
        if best_rank == 3:  # LONG_SETUP / SHORT_SETUP — can't improve
            break

    return best_result or _no_signal(f"No valid wave 1 found for {direction}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_strategy(
    df: pd.DataFrame,
    config: StrategyConfig = None,
) -> StrategyResult:
    """
    Analyse OHLCV DataFrame and return a StrategyResult.

    DataFrame must have columns: open, high, low, close, volume
    (plus either a 'timestamp' column or a DatetimeIndex).

    Analysis is performed as-of the last available candle — no lookahead.

    TASK 1: normalize_ohlcv_dataframe() is applied first.
    TASK 2: LONG and SHORT are evaluated independently; the better result wins.
    """
    if config is None:
        config = StrategyConfig()

    # TASK 1 — normalise input
    df = normalize_ohlcv_dataframe(df)

    if len(df) < config.atr_period + config.swing_left + config.swing_right + 5:
        return _no_signal("Not enough candles")

    atr = calculate_atr(df, config.atr_period)

    # as_of = last confirmed candle (no lookahead — we use ALL candles here
    # since this represents the live edge of the chart)
    as_of_index = len(df) - 1

    swings = detect_swings(df, config.swing_left, config.swing_right)
    if len(swings) < 4:
        return _no_signal("Not enough swing points detected")

    # TASK 2 — evaluate both directions independently, return the best
    long_result = _evaluate_direction(df, swings, atr, config, "LONG", as_of_index)
    short_result = _evaluate_direction(df, swings, atr, config, "SHORT", as_of_index)

    long_rank = _signal_rank(long_result.signal)
    short_rank = _signal_rank(short_result.signal)

    if long_rank > short_rank:
        return long_result
    if short_rank > long_rank:
        return short_result

    # Tied rank: for SETUP signals, prefer the one with higher RR to T2
    if long_rank == short_rank and long_rank == 3:
        # Both are active setups — pick by RR to T2
        long_rr = long_result.risk_reward_to_t2 or 0.0
        short_rr = short_result.risk_reward_to_t2 or 0.0
        if short_rr > long_rr:
            return short_result
        return long_result

    # For all other ties (NO_SIGNAL vs NO_SIGNAL, etc.) return long by convention
    return long_result
