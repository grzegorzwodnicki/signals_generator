#!/usr/bin/env python3
"""
Wyckoff Core v2 — pivot + RSI + volume based detection.

Logika wzorowana na Pine Script indicator "Wyckoff Accumulation Distribution".
Sekwencyjna detekcja: SC → AR → ST (akumulacja) / BC → AR → ST (dystrybucja).
RSI używany jako filtr trendu i kontekstu, pivot jako lokalizacja punktów.
Wolumen jako kwalifikator SC/BC (wysoki) i ST (niski).

Eksportuje:
  analyze_wyckoff(candles, verbose=False) → dict
  find_wyckoff_points(candles) → dict  ← nowa funkcja punktów
"""

# ── Tuneable constants ────────────────────────────────────────────────────────

PIVOT_LEN       = 5      # świec po każdej stronie dla pivot high/low
RSI_LEN         = 14     # okres RSI
RSI_SENS        = 20     # strefa neutralna: 50±RSI_SENS (domyślnie 30–70)
                         # bull = RSI > 50+sens, bear = RSI < 50-sens

# Wolumen
SC_VOL_MULT      = 1.5   # SC (strict mode, require_rsi_climax=True): volume >= X * sma(vol, 20)
BC_VOL_MULT      = 1.5   # BC (strict mode): j.w.
SC_VOL_MULT_SOFT = 1.0   # SC (soft mode, require_rsi_climax=False): catches exhaustion-type SC
BC_VOL_MULT_SOFT = 1.0   # BC (soft mode): catches exhaustion-type BC (Book Ch.15: SC/BC of Exhaustion)
ST_VOL_RATIO    = 0.80   # ST: wolumen <= X * sma(vol, 20)  (niższy od średniej)

# Zasięgi zakresu
RANGE_SPAN_MAX  = 0.25   # (high-low)/avg < tego — kwalifikuje jako zakres
RANGE_SLOPE_MAX = 0.010  # |slope| per bar / avg_price

# Konfidencja
_CONF_CAPS = [(70, "HIGH"), (45, "MEDIUM"), (0, "LOW")]

# Minimalna liczba świec
_MIN_CANDLES = 30

# Walidacja wzorca — czy Wyckoff jest nadal aktualny?
# Sprawdzane względem range_height = AR_high − SC_low (lub BC_high − AR_low).
#
# VALIDITY_PHASE_E_MULT: cena > AR + N×range_height → wzorzec zakończony,
#   cena jest już w fazie E (markup/markdown).  Domyślnie 2.0 — tzn. cena
#   musi być 2 zakresy powyżej AR żeby uznać że akumulacja już "grała".
#
# VALIDITY_INVALIDATE_MULT: cena < SC − N×range_height → wzorzec unieważniony
#   (akumulacja nie trzyma, nowe dołki; analogicznie dla dystrybucji).
#   Domyślnie 0.5 — tzn. cena 50% zakresu poniżej SC = wyraźny breakdown.
VALIDITY_PHASE_E_MULT    = 2.0
VALIDITY_INVALIDATE_MULT = 0.5

# Detekcja klimaksu — jakość SC/BC
# SC/BC muszą pojawić się po trendzie zgodnym z kierunkiem (SC po bearish, BC po bullish).
# Odrzucamy BC w środku trendu spadkowego i SC w środku trendu wzrostowego.
SC_BC_PRETREND_BARS = 20   # liczba świec PRZED SC/BC do oceny poprzedniego trendu
# Spring/UTAD — minimalne przebicie granicy zakresu
UTAD_SPRING_MIN_PCT = 0.003  # Spring/UTAD musi przekroczyć granicę o co najmniej 0.3%
# Dystrybucja — minimalne wymagania jakości
BC_SOFT_RSI_MIN  = 60     # przy require_rsi_climax=False: BC wymaga RSI >= 60 (miękki próg)
SC_SOFT_RSI_MAX  = 50     # przy require_rsi_climax=False: SC wymaga RSI < 50 (poniżej środka)
                          # RSI >= 50 przy "SC" = normalny pullback w uptrend, nie klimaks wyczerpania
SOW_MIN_BELOW_AR = 0.98   # SOW musi zamknąć się >= 2% poniżej AR low (zamiast 1%)
AR_CHOCH_MIN_CONFIRM = 0.30  # minimalna siła ChoCH AR, żeby potwierdzić SC/BC jako prawdziwy

# ── RSI ───────────────────────────────────────────────────────────────────────

def _rsi(closes, period=14):
    """Oblicza RSI dla listy cen zamknięcia. Zwraca listę tej samej długości (pierwsze period wartości = None)."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    result = [None] * period
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if avg_l == 0:
            result.append(100.0)
        else:
            rs = avg_g / avg_l
            result.append(100 - 100 / (1 + rs))
        if i < len(gains):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
    return result

# ── Pivot ─────────────────────────────────────────────────────────────────────

def _pivot_lows(candles, lb=PIVOT_LEN):
    """Zwraca listę indeksów świec będących pivot low (lb świec z każdej strony)."""
    result = []
    n = len(candles)
    for i in range(lb, n - lb):
        lo = candles[i]["low"]
        if all(lo <= candles[i - j]["low"] for j in range(1, lb + 1)) and \
           all(lo <= candles[i + j]["low"] for j in range(1, lb + 1)):
            result.append(i)
    return result

def _pivot_highs(candles, lb=PIVOT_LEN):
    """Zwraca listę indeksów świec będących pivot high."""
    result = []
    n = len(candles)
    for i in range(lb, n - lb):
        hi = candles[i]["high"]
        if all(hi >= candles[i - j]["high"] for j in range(1, lb + 1)) and \
           all(hi >= candles[i + j]["high"] for j in range(1, lb + 1)):
            result.append(i)
    return result

# ── Volume SMA ────────────────────────────────────────────────────────────────

def _vol_sma(candles, period=20):
    """Zwraca listę sma(volume, period) tej samej długości co candles."""
    vols = [c["volume"] for c in candles]
    result = []
    for i in range(len(vols)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(vols[i - period + 1: i + 1]) / period)
    return result

# ── Volume analysis helpers ───────────────────────────────────────────────────

def _range_vol_slope(candles, start_idx, end_idx):
    """
    Normalized linear regression slope of volume across a range window.
    Negative = declining volume (accumulation/reaccumulation signature, Villahermosa Ch.9).
    Positive = flat/rising volume (distribution signature — high constant volume throughout range).
    Returns slope per bar, normalized by mean volume.
    """
    end_idx = min(end_idx, len(candles) - 1)
    if end_idx <= start_idx:
        return 0.0
    vols = [candles[i]["volume"] for i in range(start_idx, end_idx + 1)]
    n = len(vols)
    if n < 5:
        return 0.0
    mean_v = sum(vols) / n
    if mean_v == 0:
        return 0.0
    mean_i = (n - 1) / 2.0
    denom = sum((i - mean_i) ** 2 for i in range(n))
    if denom == 0:
        return 0.0
    numer = sum((i - mean_i) * (vols[i] - mean_v) for i in range(n))
    return (numer / denom) / mean_v


def _wave_vol_ratio(candles, pivots):
    """
    Weis wave analysis: ratio of avg cumulative volume on upward legs vs downward legs.
    pivots: list of (idx, direction) where direction is 'H' or 'L', sorted by idx.
    Ratio > 1.0 = up-legs carry more volume (accumulation / bullish bias).
    Ratio < 1.0 = down-legs carry more volume (distribution / bearish bias).
    Returns 1.0 when insufficient data.
    """
    up_vols, down_vols = [], []
    for k in range(len(pivots) - 1):
        a_idx, a_dir = pivots[k]
        b_idx, b_dir = pivots[k + 1]
        if a_idx >= b_idx:
            continue
        wave_vol = sum(candles[j]["volume"] for j in range(a_idx, min(b_idx + 1, len(candles))))
        if a_dir == 'L' and b_dir == 'H':
            up_vols.append(wave_vol)
        elif a_dir == 'H' and b_dir == 'L':
            down_vols.append(wave_vol)
    if not up_vols or not down_vols:
        return 1.0
    return (sum(up_vols) / len(up_vols)) / (sum(down_vols) / len(down_vols))


def _check_hh_hl(candles, idx, lookback=10):
    """
    Check if bars before idx show a rising low pattern (at least half of consecutive
    lows are higher than the previous). Confirms structure building before SOS.
    """
    start = max(0, idx - lookback)
    lows = [candles[j]["low"] for j in range(start, idx)]
    if len(lows) < 3:
        return False
    rising = sum(1 for k in range(1, len(lows)) if lows[k] > lows[k - 1])
    return rising >= len(lows) // 2


def _fvg_bias_in_range(candles, start_idx, end_idx):
    """
    Net FVG (Fair Value Gap / SMC imbalance) bias within a window.
    Bullish FVG: candle[i].low > candle[i-2].high  (gap up — unfilled demand zone).
    Bearish FVG: candle[i].high < candle[i-2].low  (gap down — unfilled supply zone).
    Returns normalized net bias (positive = bullish FVGs dominate, negative = bearish).
    A strong positive bias inside an accumulation range confirms genuine demand absorption.
    A strong negative bias inside a distribution range confirms genuine supply.
    """
    end_idx = min(end_idx, len(candles) - 1)
    if end_idx - start_idx < 2:
        return 0.0
    bull_size = 0.0
    bear_size = 0.0
    for i in range(start_idx + 2, end_idx + 1):
        gap_up   = candles[i]["low"]   - candles[i - 2]["high"]
        gap_down = candles[i - 2]["low"] - candles[i]["high"]
        if gap_up > 0:
            bull_size += gap_up
        if gap_down > 0:
            bear_size += gap_down
    mid_price = (candles[start_idx]["close"] + candles[end_idx]["close"]) / 2
    if mid_price == 0:
        return 0.0
    return (bull_size - bear_size) / mid_price


def _range_hl_score(candles, start_idx, end_idx):
    """
    Internal range structure: count pivot Higher Highs + Higher Lows (bull_score)
    vs Lower Highs + Lower Lows (bear_score) within the trading range.
    bull_score > bear_score → price building ascending structure inside range (Acc/Reacc).
    bear_score > bull_score → price building descending structure inside range (Dist/Redist).
    Uses a 3-bar pivot lookback (tighter than the main PIVOT_LEN for intra-range resolution).
    """
    end_idx = min(end_idx, len(candles) - 1)
    lb = 3
    window = candles[start_idx:end_idx + 1]
    n = len(window)
    if n < lb * 2 + 3:
        return 0, 0
    highs, lows = [], []
    for i in range(lb, n - lb):
        h = window[i]["high"]
        l = window[i]["low"]
        if all(h >= window[i - j]["high"] and h >= window[i + j]["high"] for j in range(1, lb + 1)):
            highs.append(h)
        if all(l <= window[i - j]["low"]  and l <= window[i + j]["low"]  for j in range(1, lb + 1)):
            lows.append(l)
    bull = (sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1]) +
            sum(1 for i in range(1, len(lows))  if lows[i]  > lows[i - 1]))
    bear = (sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1]) +
            sum(1 for i in range(1, len(lows))  if lows[i]  < lows[i - 1]))
    return bull, bear


def _ar_choch_strength(candles, climax_idx, ar_idx, is_acc=True):
    """
    AR (Automatic Rally/Reaction) as Change of Character strength (Villahermosa Ch.16).
    Compares the AR distance to the average swing size in the 60 bars before the climax.
    Book: "an AR of 100pts in a market with avg 50pt swings suggests a stronger bottom."
    Weak AR (intertwined, short distance) suggests the opposite structure:
      acc → suspect redistribution; dist → suspect reaccumulation.
    Returns ratio ≥ 1.0 = strong ChoCH, < 0.6 = weak ChoCH.
    """
    if is_acc:
        ar_size = candles[ar_idx]["high"] - candles[climax_idx]["low"]
        ref     = candles[climax_idx]["low"]
    else:
        ar_size = candles[climax_idx]["high"] - candles[ar_idx]["low"]
        ref     = candles[climax_idx]["high"]

    if ref <= 0 or ar_size <= 0:
        return 1.0

    ar_pct = ar_size / ref

    # Average 10-bar high-low range in the 60 bars before climax = proxy for typical swing
    lookback = 60
    start = max(0, climax_idx - lookback)
    window = candles[start:climax_idx]
    n = len(window)
    if n < 10:
        return 1.0

    swing_pcts = []
    for k in range(0, n - 9, 5):
        sub = window[k:k + 10]
        hi  = max(c["high"] for c in sub)
        lo  = min(c["low"]  for c in sub)
        mid = (hi + lo) / 2
        if mid > 0:
            swing_pcts.append((hi - lo) / mid)

    if not swing_pcts:
        return 1.0
    avg_swing = sum(swing_pcts) / len(swing_pcts)
    return (ar_pct / avg_swing) if avg_swing > 0 else 1.0


def _ar_vol_anatomy(candles, climax_idx, ar_idx):
    """
    AR volume anatomy: first-half avg volume vs second-half avg volume ratio.
    Book Ch.16: healthy AR starts with climax residual (high vol) → declines naturally.
    > 1.0 = declining anatomy (healthy, interest fading).
    < 1.0 = rising/flat anatomy (forced move, unnatural).
    Better than linear slope — captures the start-high / end-low pattern directly.
    """
    n = ar_idx - climax_idx
    if n < 4:
        return 1.0
    mid = climax_idx + n // 2
    first_vols  = [candles[i]["volume"] for i in range(climax_idx + 1, mid + 1)]
    second_vols = [candles[i]["volume"] for i in range(mid + 1, ar_idx + 1)]
    if not first_vols or not second_vols:
        return 1.0
    avg_first  = sum(first_vols)  / len(first_vols)
    avg_second = sum(second_vols) / len(second_vols)
    return (avg_first / avg_second) if avg_second > 0 else 1.0


# ── Linear regression slope ───────────────────────────────────────────────────

def _slope_pct(closes):
    n = len(closes)
    if n < 3:
        return 0.0
    mean_c = sum(closes) / n
    if mean_c == 0:
        return 0.0
    mean_i = (n - 1) / 2.0
    num = sum((i - mean_i) * (c - mean_c) for i, c in enumerate(closes))
    den = sum((i - mean_i) ** 2 for i in range(n))
    return (num / den / mean_c) if den > 0 else 0.0

# ── Trend ─────────────────────────────────────────────────────────────────────

def _trend(candles, lookback=30):
    if len(candles) < lookback:
        return "unclear"
    c = [x["close"] for x in candles[-lookback:]]
    mid = lookback // 2
    f = sum(c[:mid]) / mid
    s = sum(c[mid:]) / mid
    if s > f * 1.015:  return "bullish"
    if s < f * 0.985:  return "bearish"
    return "neutral"

def _multi_trend(candles, skip_last=5):
    """
    Multi-window trend consensus: checks 30, 60, 120 bars, skipping the last
    skip_last bars (to exclude the climax itself from biasing the result).
    Returns 'bearish', 'bullish', or 'neutral'.
    Requires at least 2/3 windows to agree for a clear signal.
    """
    win_end = len(candles) - skip_last
    if win_end < 20:
        return "neutral"
    segment = candles[:win_end]
    n = len(segment)
    votes = []
    for w in [30, 60, 120]:
        if n >= w:
            votes.append(_trend(segment, w))
    if not votes:
        return "neutral"
    bear = votes.count("bearish")
    bull = votes.count("bullish")
    if bear >= 2:
        return "bearish"
    if bull >= 2:
        return "bullish"
    return "neutral"

# ── Confidence cap ────────────────────────────────────────────────────────────

def _cap_conf(confidence, n_candles):
    for threshold, cap in _CONF_CAPS:
        if n_candles >= threshold:
            if cap == "HIGH":   return confidence
            if cap == "MEDIUM": return confidence if confidence != "HIGH" else "MEDIUM"
            return "LOW"
    return "LOW"

# ── Range detection ───────────────────────────────────────────────────────────

def _detect_range(candles):
    """
    Wykrywa zakres (range) w ostatnich świecach.
    Zwraca (range_high, range_low) lub (None, None).
    """
    n = len(candles)
    if n < 10:
        return None, None
    closes = [c["close"] for c in candles]
    h = max(c["high"] for c in candles)
    l = min(c["low"]  for c in candles)
    avg = sum(closes) / n
    if avg <= 0:
        return None, None
    span  = (h - l) / avg
    slope = abs(_slope_pct(closes))
    if span < RANGE_SPAN_MAX and slope < RANGE_SLOPE_MAX:
        return h, l
    return None, None

# ── RSI zone classification ───────────────────────────────────────────────────

def _rsi_zone(rsi_val, sens=RSI_SENS):
    """Zwraca 'bull', 'bear' lub 'side' na podstawie wartości RSI."""
    if rsi_val is None:
        return "side"
    if rsi_val > 50 + sens:  return "bull"
    if rsi_val < 50 - sens:  return "bear"
    return "side"

# ══════════════════════════════════════════════════════════════════════════════
# GŁÓWNA FUNKCJA — DETEKCJA PUNKTÓW WYCKOFFA
# ══════════════════════════════════════════════════════════════════════════════

def find_wyckoff_points(candles, pivot_len=PIVOT_LEN, rsi_sens=RSI_SENS, require_rsi_climax=True):
    """
    Wykrywa sekwencyjnie punkty Wyckoffa na liście świec.

    Akumulacja:  [PS] → SC → AR → ST → [Spring] → SOS → LPS
    Dystrybucja: [PSY] → BC → AR → ST → [UTAD]  → SOW → LPSY

    Tryb SC/BC (require_rsi_climax):
      True  — strict: RSI<30 dla SC / RSI>70 dla BC, volume >= 1.5x SMA (klasyczny klimaks)
      False — soft:   brak RSI gate dla SC (RSI>=60 dla BC), volume >= 1.0x SMA
              Wykrywa również SC/BC wyczerpaniowe (Exhaustion Climax wg. Villahermosa Ch.15)

    Parametry:
      candles   – lista dict {open, high, low, close, volume, time}
      pivot_len – liczba świec po każdej stronie dla pivot
      rsi_sens  – strefa neutralna RSI (domyślnie 20 → 30–70)

    Zwraca dict:
      "acc"  → dict z punktami akumulacji (PS, SC, AR, ST, Spring, SOS, LPS)
      "dist" → dict z punktami dystrybucji (PSY, BC, AR, ST, UTAD, SOW, LPSY)
      "rsi"  → lista wartości RSI
      "vol_sma" → lista vol SMA(20)
      "range_high", "range_low" → granice zakresu (lub None)
      "context" → "accumulation" / "distribution" / "side" / "unclear"
    """
    n = len(candles)
    if n < _MIN_CANDLES:
        return {"acc": {}, "dist": {}, "context": "unclear",
                "range_high": None, "range_low": None, "rsi": [], "vol_sma": []}

    closes  = [c["close"] for c in candles]
    rsi_arr = _rsi(closes, RSI_LEN)
    vsma    = _vol_sma(candles, 20)

    p_lows  = _pivot_lows(candles,  lb=pivot_len)
    p_highs = _pivot_highs(candles, lb=pivot_len)
    p_low_set  = set(p_lows)
    p_high_set = set(p_highs)

    rsi_low  = 50 - rsi_sens   # np. 30
    rsi_high = 50 + rsi_sens   # np. 70

    # ── Kontekst RSI (ostatnia wartość) ──────────────────────────────────────
    last_rsi = next((r for r in reversed(rsi_arr) if r is not None), 50.0)
    context_zone = _rsi_zone(last_rsi, rsi_sens)

    # ── Zakres cenowy (last 40 świec bez ostatnich 10) ────────────────────────
    range_win = candles[max(0, n-50): max(0, n-10)]
    rh, rl = _detect_range(range_win) if len(range_win) >= 10 else (None, None)

    # ════════════════════════════════════════════════════════════════════════
    # AKUMULACJA: SC → AR → ST → Spring → SOS → LPS
    # ════════════════════════════════════════════════════════════════════════
    acc = {}

    # SC: pivot low + RSI gate + wolumen
    # Strict mode (require_rsi_climax=True):  RSI < 30 + volume >= 1.5x  (classic climax)
    # Soft mode  (require_rsi_climax=False):  RSI < 50 + volume >= 1.0x  (exhaustion SC per book Ch.15)
    _sc_vol_thresh = SC_VOL_MULT if require_rsi_climax else SC_VOL_MULT_SOFT
    sc_idx = None
    for i in p_lows:
        r = rsi_arr[i]
        v = vsma[i]
        if r is None or v is None:
            continue
        # RSI gate:
        #   strict: RSI < 30 (wyraźnie oversold)
        #   soft:   RSI < 50 (przynajmniej poniżej środka — klimaks wyczerpania nie może być w bullish zone)
        if require_rsi_climax:
            if r >= rsi_low:        # strict: RSI >= 30 → odrzuć
                continue
        else:
            if r >= SC_SOFT_RSI_MAX:  # soft: RSI >= 50 → odrzuć (nie SC, a normalny pullback)
                continue
        # Wolumen >= threshold * sma(vol,20)
        if candles[i]["volume"] < v * _sc_vol_thresh:
            continue
        # Świeca spadkowa (close < open) lub długi dolny cień
        c = candles[i]
        is_bearish_candle = c["close"] < c["open"] or (c["open"] - c["low"]) > (c["high"] - c["low"]) * 0.4
        if not is_bearish_candle:
            continue
        # SC musi pojawić się po trendzie bearish/neutral — nie może być w środku rajdu
        pre = _trend(candles[max(0, i - SC_BC_PRETREND_BARS):i], SC_BC_PRETREND_BARS)
        if pre == "bullish":
            continue
        sc_idx = i  # bierzemy ostatni spełniający (najbardziej aktualny)

    if sc_idx is not None:
        # PS: ostatni pivot low PRZED SC z podwyższonym wolumenem
        # (Preliminary Support — pierwsze ślady popytu wchodzącego podczas spadku)
        for i in reversed([j for j in p_lows if j < sc_idx]):
            v = vsma[i]
            if v is None:
                continue
            c = candles[i]
            if c["low"] <= candles[sc_idx]["low"]:  # PS musi być wyżej niż SC
                continue
            if c["volume"] < v * 0.8:
                continue
            acc["PS"] = {"idx": i, "price": c["low"],
                         "rsi": rsi_arr[i], "volume": c["volume"]}
            break

        acc["SC"] = {"idx": sc_idx, "price": candles[sc_idx]["low"],
                     "rsi": rsi_arr[sc_idx], "volume": candles[sc_idx]["volume"],
                     "ar_confirmed": False}  # default; updated below if AR is genuine ChoCH

        # AR: pierwsza pivot high PO SC, RSI wychodzi ze strefy bear
        ar_idx = None
        for i in p_highs:
            if i <= sc_idx:
                continue
            r = rsi_arr[i]
            if r is None:
                continue
            # RSI już nie jest głęboko oversold (wychodzi z bear zone)
            if r < rsi_low:
                continue
            ar_idx = i
            break  # pierwszy pivot high po SC

        if ar_idx is not None:
            _choch_acc = _ar_choch_strength(candles, sc_idx, ar_idx, is_acc=True)
            # Book Ch.16: AR (ChoCH) is the primary confirmation of SC genuineness.
            # SC is only "confirmed" if AR is strong enough to represent a real ChoCH.
            acc["SC"]["ar_confirmed"] = (_choch_acc >= AR_CHOCH_MIN_CONFIRM)
            # Volume slope within the AR window (skip SC bar to avoid climax-volume bias).
            _ar_vol_sl_acc = _range_vol_slope(candles, sc_idx + 1, ar_idx) if ar_idx > sc_idx + 2 else 0.0
            # Volume anatomy: first-half vs second-half avg vol (better than linear slope).
            _ar_vol_anat_acc = _ar_vol_anatomy(candles, sc_idx, ar_idx)
            acc["AR"] = {"idx": ar_idx, "price": candles[ar_idx]["high"],
                         "rsi": rsi_arr[ar_idx], "volume": candles[ar_idx]["volume"],
                         "choch_strength": _choch_acc,
                         "vol_slope":      _ar_vol_sl_acc,
                         "vol_anatomy":    _ar_vol_anat_acc}

            # Wyznacz granice zakresu z SC i AR
            cr_low  = candles[sc_idx]["low"]
            cr_high = candles[ar_idx]["high"]
            if rh is None:
                rh, rl = cr_high, cr_low

            # ST: pierwsza pivot low PO AR + RSI nie schodzi poniżej rsi_low + niski wolumen
            st_idx = None
            for i in p_lows:
                if i <= ar_idx:
                    continue
                r = rsi_arr[i]
                v = vsma[i]
                if r is None or v is None:
                    continue
                # RSI nie jest tak głęboko jak przy SC (brak nowej kulminacji)
                if r < rsi_low:
                    # to byłby nowy SC, nie ST — reset struktury
                    break
                # Wolumen niższy niż średnia (słaba podaż)
                low_vol = candles[i]["volume"] <= v * ST_VOL_RATIO
                # Cena nie spada poniżej SC (lub tylko nieznacznie)
                price_ok = candles[i]["low"] >= cr_low * 0.995
                if price_ok:  # niski wolumen preferowany ale nie wymuszony
                    st_idx = i
                    break

            if st_idx is not None:
                # ST position within range: upper half = bullish strength, lower = weakness
                _range_mid = (cr_high + cr_low) / 2
                _st_pos = "upper" if candles[st_idx]["low"] >= _range_mid else "lower"
                # ST volume vs SC volume (book: ST should be lower volume than SC)
                _sc_vol = candles[sc_idx]["volume"]
                _st_vs_sc_ok = candles[st_idx]["volume"] < _sc_vol * 0.90
                acc["ST"] = {"idx": st_idx, "price": candles[st_idx]["low"],
                             "rsi": rsi_arr[st_idx], "volume": candles[st_idx]["volume"],
                             "low_volume": vsma[st_idx] is not None and
                                           candles[st_idx]["volume"] <= (vsma[st_idx] or 1) * ST_VOL_RATIO,
                             "position": _st_pos,
                             "vol_vs_sc_ok": _st_vs_sc_ok}

            # Book Ch.16: "Secondary Test ending below SC adds strength to redistribution scenario."
            # Normal ST detection requires low >= SC * 0.995, so a genuine "below SC" test
            # never qualifies as ST in our system — detect it with a separate scan of the
            # first pivot low after AR. If it breaks SC by > 1%, bears are still in control.
            _st_below_sc = False
            for i in p_lows:
                if i <= ar_idx:
                    continue
                if candles[i]["low"] < cr_low * 0.99:  # 1% break below SC = failed test
                    _st_below_sc = True
                break  # only first pivot low after AR matters
            acc["_st_below_sc"] = _st_below_sc

            # Spring: pivot low PO ST który przebija rl ale zamyka powyżej
            # Niski wolumen, RSI crossover z bear na side
            if st_idx is not None or ar_idx is not None:
                spring_start = (st_idx or ar_idx)
                for i in p_lows:
                    if i <= spring_start:
                        continue
                    r = rsi_arr[i]
                    v = vsma[i]
                    if r is None:
                        continue
                    c = candles[i]
                    # Fałszywe wybicie: low musi przekroczyć cr_low o min. UTAD_SPRING_MIN_PCT
                    if c["low"] < cr_low * (1 - UTAD_SPRING_MIN_PCT) and c["close"] > cr_low:
                        low_vol = v is None or c["volume"] <= v * 0.9
                        # Spring Type (Villahermosa): 1=high-vol shakeout (needs test),
                        # 2=moderate, 3=exhaustion/low-vol (strongest — direct entry signal)
                        _sv = v or 1
                        _penetration = (cr_low - c["low"]) / cr_low if cr_low > 0 else 0
                        if _penetration > 0.01 and c["volume"] > _sv * 1.3:
                            spring_type = 1
                        elif c["volume"] > _sv * 0.8:
                            spring_type = 2
                        else:
                            spring_type = 3
                        acc["Spring"] = {"idx": i, "price": c["low"],
                                         "close": c["close"],
                                         "rsi": r,
                                         "volume": c["volume"],
                                         "low_volume": low_vol,
                                         "spring_type": spring_type}
                        break

            # SOS: pivot high PO (Spring lub ST) + RSI wchodzi w bull zone + wysoki wolumen
            # + zamknięcie powyżej CR high (cr_high)
            sos_start = acc.get("Spring", {}).get("idx") or st_idx or ar_idx
            if sos_start is not None:
                for i in p_highs:
                    if i <= sos_start:
                        continue
                    r = rsi_arr[i]
                    v = vsma[i]
                    if r is None:
                        continue
                    c = candles[i]
                    above_cr = c["close"] > cr_high * 1.01
                    bull_rsi  = r > 55  # RSI przekracza środek (stricter)
                    hi_vol    = v is None or c["volume"] >= v * 1.0
                    if above_cr and bull_rsi:
                        acc["SOS"] = {"idx": i, "price": c["high"],
                                      "close": c["close"],
                                      "rsi": r,
                                      "volume": c["volume"],
                                      "high_volume": hi_vol,
                                      "hh_hl": _check_hh_hl(candles, i)}
                        break

            # LPS: pivot low PO SOS + cena powyżej CR (stary resistance = nowe wsparcie)
            # + niski wolumen + RSI nie spada pod 40
            if "SOS" in acc:
                sos_i = acc["SOS"]["idx"]
                for i in p_lows:
                    if i <= sos_i:
                        continue
                    r = rsi_arr[i]
                    v = vsma[i]
                    if r is None:
                        continue
                    c = candles[i]
                    above_support = c["low"] >= cr_high * 0.985  # blisko starego resistance
                    rsi_ok = r >= 40  # RSI nie wraca w oversold
                    low_vol = v is None or c["volume"] <= v * ST_VOL_RATIO
                    if above_support and rsi_ok:
                        acc["LPS"] = {"idx": i, "price": c["low"],
                                      "rsi": r,
                                      "volume": c["volume"],
                                      "low_volume": low_vol}
                        break

        # ── Volume + SMC analysis across accumulation range ──────────────────
        if "SC" in acc:
            _sc_i = acc["SC"]["idx"]
            _latest_i = max((v["idx"] for v in acc.values() if isinstance(v, dict) and "idx" in v), default=_sc_i)
            # Measure from AR onwards (Phase B start) to capture intra-range vol trend.
            # SC itself has climactic high vol — including it biases slope downward always.
            _slope_start = acc.get("AR", {}).get("idx") or _sc_i
            acc["_range_vol_slope"] = _range_vol_slope(candles, _slope_start, _latest_i)

            # Weis wave: L for lows (SC, ST, Spring, LPS), H for highs (AR, SOS)
            _pivots = []
            for _key, _dir in [("SC", 'L'), ("AR", 'H'), ("ST", 'L'),
                                ("Spring", 'L'), ("SOS", 'H'), ("LPS", 'L')]:
                if _key in acc:
                    _pivots.append((acc[_key]["idx"], _dir))
            _pivots.sort()
            acc["_wave_vol_ratio"] = _wave_vol_ratio(candles, _pivots) if len(_pivots) >= 2 else 1.0

            # SMC: FVG bias and internal HH/HL structure within the range
            acc["_fvg_bias"] = _fvg_bias_in_range(candles, _sc_i, _latest_i)
            _hl_bull, _hl_bear = _range_hl_score(candles, _sc_i, _latest_i)
            acc["_hl_score_bull"] = _hl_bull
            acc["_hl_score_bear"] = _hl_bear

    # ════════════════════════════════════════════════════════════════════════
    # DYSTRYBUCJA: BC → AR → ST → UTAD → SOW → LPSY
    # ════════════════════════════════════════════════════════════════════════
    dist = {}

    # BC: pivot high + RSI > rsi_high + wysoki wolumen + świeca wzrostowa
    # Strict mode (require_rsi_climax=True):  RSI > 70 + volume >= 1.5x  (classic climax)
    # Soft mode  (require_rsi_climax=False): RSI >= 60 + volume >= 1.0x  (catches exhaustion BC)
    _bc_vol_thresh = BC_VOL_MULT if require_rsi_climax else BC_VOL_MULT_SOFT
    bc_idx = None
    for i in p_highs:
        r = rsi_arr[i]
        v = vsma[i]
        if r is None or v is None:
            continue
        # RSI gate: pełny (>70) lub miękki (>60) przy require_rsi_climax=False
        if require_rsi_climax:
            if r <= rsi_high:
                continue
        else:
            if r < BC_SOFT_RSI_MIN:  # wyklucza oczywiste nie-kulminacje (RSI < 60)
                continue
        if candles[i]["volume"] < v * _bc_vol_thresh:
            continue
        c = candles[i]
        is_bullish_candle = c["close"] > c["open"] or (c["high"] - c["close"]) < (c["high"] - c["low"]) * 0.3
        if not is_bullish_candle:
            continue
        # BC musi pojawić się po trendzie bullish/neutral — nie może być w środku trendu spadkowego
        pre = _trend(candles[max(0, i - SC_BC_PRETREND_BARS):i], SC_BC_PRETREND_BARS)
        if pre == "bearish":
            continue
        bc_idx = i

    if bc_idx is not None:
        # PSY: ostatni pivot high PRZED BC z podwyższonym wolumenem
        # (Preliminary Supply — pierwsze ślady podaży wchodzące podczas rajdu)
        for i in reversed([j for j in p_highs if j < bc_idx]):
            v = vsma[i]
            if v is None:
                continue
            c = candles[i]
            if c["high"] >= candles[bc_idx]["high"]:  # PSY musi być niżej niż BC
                continue
            if c["volume"] < v * 0.8:
                continue
            dist["PSY"] = {"idx": i, "price": c["high"],
                           "rsi": rsi_arr[i], "volume": c["volume"]}
            break

        dist["BC"] = {"idx": bc_idx, "price": candles[bc_idx]["high"],
                      "rsi": rsi_arr[bc_idx], "volume": candles[bc_idx]["volume"],
                      "ar_confirmed": False}  # default; updated below if AR is genuine ChoCH

        # AR (dystrybucja): pierwsza pivot low PO BC z kluczowymi walidacjami:
        #  1. AR low musi być PONIŻEJ BC high (automatyczna reakcja w dół od BC)
        #  2. Między BC a AR nie może pojawić się wyższy szczyt — BC musi być THE top
        ar_d_idx = None
        bc_high_price = candles[bc_idx]["high"]
        for i in p_lows:
            if i <= bc_idx:
                continue
            r = rsi_arr[i]
            if r is None:
                continue
            if r > rsi_high:
                continue
            # AR musi cofnąć się poniżej BC high (brak tego = trend kontynuował się w górę)
            if candles[i]["low"] >= bc_high_price:
                continue
            # Między BC a AR nie może pojawić się wyższy szczyt (BC nie był prawdziwą kulminacją)
            if any(candles[j]["high"] > bc_high_price for j in range(bc_idx + 1, i)):
                continue
            ar_d_idx = i
            break

        if ar_d_idx is not None:
            _choch_dist = _ar_choch_strength(candles, bc_idx, ar_d_idx, is_acc=False)
            # Book Ch.16: AR (ChoCH) confirms BC genuineness.
            dist["BC"]["ar_confirmed"] = (_choch_dist >= AR_CHOCH_MIN_CONFIRM)
            # Volume slope within distribution AR window (skip BC bar to avoid climax-volume bias).
            _ar_vol_sl_dist = _range_vol_slope(candles, bc_idx + 1, ar_d_idx) if ar_d_idx > bc_idx + 2 else 0.0
            _ar_vol_anat_dist = _ar_vol_anatomy(candles, bc_idx, ar_d_idx)
            dist["AR"] = {"idx": ar_d_idx, "price": candles[ar_d_idx]["low"],
                          "rsi": rsi_arr[ar_d_idx], "volume": candles[ar_d_idx]["volume"],
                          "choch_strength": _choch_dist,
                          "vol_slope":      _ar_vol_sl_dist,
                          "vol_anatomy":    _ar_vol_anat_dist}

            cr_high_d = candles[bc_idx]["high"]
            cr_low_d  = candles[ar_d_idx]["low"]

            # ST (dystrybucja): pivot high PO AR + RSI nie wraca powyżej rsi_high + niski wolumen
            st_d_idx = None
            _st_above_bc = False  # Book Ch.16: ST > BC high = reaccumulation signal
            for i in p_highs:
                if i <= ar_d_idx:
                    continue
                r = rsi_arr[i]
                v = vsma[i]
                if r is None or v is None:
                    continue
                if r > rsi_high:
                    break  # nowy BC
                price_ok = candles[i]["high"] <= cr_high_d * 1.005
                if not price_ok:
                    # First pivot after AR exceeds BC high → ST doesn't hold → reaccumulation
                    _st_above_bc = True
                    break
                st_d_idx = i
                break
            dist["_st_above_bc"] = _st_above_bc

            if st_d_idx is not None:
                dist["ST"] = {"idx": st_d_idx, "price": candles[st_d_idx]["high"],
                              "rsi": rsi_arr[st_d_idx], "volume": candles[st_d_idx]["volume"],
                              "low_volume": vsma[st_d_idx] is not None and
                                            candles[st_d_idx]["volume"] <= (vsma[st_d_idx] or 1) * ST_VOL_RATIO}

            # UTAD: pivot high PO ST który przebija cr_high_d ale zamyka poniżej
            utad_start = st_d_idx or ar_d_idx
            for i in p_highs:
                if i <= utad_start:
                    continue
                c = candles[i]
                # UTAD musi przekroczyć BC high o min. UTAD_SPRING_MIN_PCT (nie wystarczy 1 tick)
                if c["high"] > cr_high_d * (1 + UTAD_SPRING_MIN_PCT) and c["close"] < cr_high_d:
                    v = vsma[i]
                    low_vol = v is None or c["volume"] <= v * 0.9
                    dist["UTAD"] = {"idx": i, "price": c["high"],
                                    "close": c["close"],
                                    "rsi": rsi_arr[i],
                                    "volume": c["volume"],
                                    "low_volume": low_vol}
                    break

            # SOW: pivot low PO (UTAD lub ST) + zamknięcie poniżej CR low
            sow_start = dist.get("UTAD", {}).get("idx") or st_d_idx or ar_d_idx
            if sow_start is not None:
                for i in p_lows:
                    if i <= sow_start:
                        continue
                    r = rsi_arr[i]
                    v = vsma[i]
                    if r is None:
                        continue
                    c = candles[i]
                    below_cr = c["close"] < cr_low_d * SOW_MIN_BELOW_AR
                    bear_rsi  = r < 45
                    if below_cr and bear_rsi:
                        dist["SOW"] = {"idx": i, "price": c["low"],
                                       "close": c["close"],
                                       "rsi": r,
                                       "volume": c["volume"]}
                        break

            # LPSY: pivot high PO SOW + cena poniżej CR high + niski wolumen + RSI < 50
            if "SOW" in dist:
                sow_i = dist["SOW"]["idx"]
                _utad_vol = dist.get("UTAD", {}).get("volume", None)
                for i in p_highs:
                    if i <= sow_i:
                        continue
                    r = rsi_arr[i]
                    v = vsma[i]
                    if r is None:
                        continue
                    c = candles[i]
                    below_resistance = c["high"] <= cr_high_d * 1.015
                    rsi_weak = r <= 55
                    low_vol  = v is None or c["volume"] <= v * ST_VOL_RATIO
                    if below_resistance and rsi_weak:
                        # Book: LPSY/test after UTAD should be weaker (lower vol) than UTAD
                        _utad_test_weak = (_utad_vol is None or c["volume"] < _utad_vol * 0.90)
                        dist["LPSY"] = {"idx": i, "price": c["high"],
                                        "rsi": r,
                                        "volume": c["volume"],
                                        "low_volume": low_vol,
                                        "utad_test_weak": _utad_test_weak}
                        break

        # ── Volume + SMC analysis across distribution range ───────────────────
        if "BC" in dist:
            _bc_i = dist["BC"]["idx"]
            _latest_i = max((v["idx"] for v in dist.values() if isinstance(v, dict) and "idx" in v), default=_bc_i)
            # Measure from AR onwards — BC climax vol is the highest point, including it
            # makes slope always negative. Phase B intra-range vol is the real diagnostic.
            _slope_start_d = dist.get("AR", {}).get("idx") or _bc_i
            dist["_range_vol_slope"] = _range_vol_slope(candles, _slope_start_d, _latest_i)

            # Weis wave: H for highs (BC, ST, UTAD, LPSY), L for lows (AR, SOW)
            _pivots = []
            for _key, _dir in [("BC", 'H'), ("AR", 'L'), ("ST", 'H'),
                                ("UTAD", 'H'), ("SOW", 'L'), ("LPSY", 'H')]:
                if _key in dist:
                    _pivots.append((dist[_key]["idx"], _dir))
            _pivots.sort()
            dist["_wave_vol_ratio"] = _wave_vol_ratio(candles, _pivots) if len(_pivots) >= 2 else 1.0

            # SMC: FVG bias and internal LH/LL structure within the range
            dist["_fvg_bias"] = _fvg_bias_in_range(candles, _bc_i, _latest_i)
            _hl_bull_d, _hl_bear_d = _range_hl_score(candles, _bc_i, _latest_i)
            dist["_hl_score_bull"] = _hl_bull_d
            dist["_hl_score_bear"] = _hl_bear_d

    # ── Walidacja wzorca — czy pattern jest jeszcze aktualny? ───────────────────
    # Sprawdza aktualną cenę względem wykrytego range'u.
    # Dwa powody do unieważnienia:
    #   1) Phase E — cena odeszła daleko poza range (wzorzec już "zagrał")
    #   2) Invalidated — cena przebiła SC/BC w złym kierunku (wzorzec nie trzymał)
    _validity_note = ""
    current_price = candles[-1]["close"]

    if acc and "SC" in acc and "AR" in acc:
        sc_price = acc["SC"]["price"]
        ar_price = acc["AR"]["price"]
        rng = ar_price - sc_price
        if rng > 0:
            if current_price > ar_price + VALIDITY_PHASE_E_MULT * rng:
                # Cena jest już N zakresów powyżej AR — akumulacja zakończona, Phase E
                _validity_note = "acc_phase_e"
                acc = {}
            elif current_price < sc_price - VALIDITY_INVALIDATE_MULT * rng:
                # Cena przebiła SC low — akumulacja upadła
                _validity_note = "acc_invalidated"
                acc = {}

    if dist and "BC" in dist and "AR" in dist:
        bc_price = dist["BC"]["price"]
        ar_price = dist["AR"]["price"]
        rng = bc_price - ar_price
        if rng > 0:
            if current_price < ar_price - VALIDITY_PHASE_E_MULT * rng:
                # Cena jest N zakresów poniżej AR — dystrybucja zakończona, Phase E
                _validity_note = _validity_note or "dist_phase_e"
                dist = {}
            elif current_price > bc_price + VALIDITY_INVALIDATE_MULT * rng:
                # Cena przebiła BC high — dystrybucja upadła
                _validity_note = _validity_note or "dist_invalidated"
                dist = {}

    # ── Kontekst struktury ────────────────────────────────────────────────────
    has_acc  = "SC" in acc
    has_dist = "BC" in dist

    if has_acc and has_dist:
        # Porównaj dojrzałość sekwencji — wygrywa ta z więcej potwierdzonymi punktami.
        # Indeks ostatniego punktu jako tiebreaker (nie SC vs BC — to błąd kierunku).
        ACC_SEQ  = ["PS", "SC", "AR", "ST", "Spring", "SOS", "LPS"]
        DIST_SEQ = ["PSY", "BC", "AR", "ST", "UTAD",  "SOW", "LPSY"]
        acc_score  = sum(1 for k in ACC_SEQ  if k in acc)
        dist_score = sum(1 for k in DIST_SEQ if k in dist)

        if acc_score != dist_score:
            context = "accumulation" if acc_score > dist_score else "distribution"
        else:
            # Remis — wygrywa ta której OSTATNI wykryty punkt jest późniejszy
            # (skip non-dict entries like _range_vol_slope / _wave_vol_ratio)
            acc_last  = max((v["idx"] for v in acc.values()  if isinstance(v, dict) and "idx" in v), default=0)
            dist_last = max((v["idx"] for v in dist.values() if isinstance(v, dict) and "idx" in v), default=0)
            context = "accumulation" if acc_last >= dist_last else "distribution"
    elif has_acc:
        context = "accumulation"
    elif has_dist:
        context = "distribution"
    else:
        context = "side" if context_zone == "side" else "unclear"

    return {
        "acc":           acc,
        "dist":          dist,
        "context":       context,
        "range_high":    rh,
        "range_low":     rl,
        "rsi":           rsi_arr,
        "vol_sma":       vsma,
        "validity_note": _validity_note,  # "" = valid; "acc_phase_e" / "acc_invalidated" / dist_*
    }


# ══════════════════════════════════════════════════════════════════════════════
# KOMPATYBILNA FUNKCJA analyze_wyckoff (drop-in replacement)
# ══════════════════════════════════════════════════════════════════════════════

_LABEL = {
    False: {
        "ps": "PS",                  "psy": "PSY",
        "sc": "SC (Selling Climax)", "bc": "BC (Buying Climax)",
        "spring": "Spring",          "utad": "UTAD",
        "sos": "SOS",                "sow": "SOW",
        "lps": "LPS",                "lpsy": "LPSY",
        "none": "No clear events",
    },
    True: {
        "ps": "PS (Preliminary Support)",      "psy": "PSY (Preliminary Supply)",
        "sc": "Possible SC (Selling Climax)",  "bc": "Possible BC (Buying Climax)",
        "spring": "Spring (test below support)", "utad": "UTAD (test above resistance)",
        "sos": "SOS — break above range high",   "sow": "SOW — break below range low",
        "lps": "LPS — pullback after SOS",       "lpsy": "LPSY — pullback after SOW",
        "none": "No clear Wyckoff events",
    },
}


def analyze_wyckoff(candles, verbose=False, require_rsi_climax=True):
    """
    Drop-in replacement dla starego wyckoff_core.analyze_wyckoff.
    Zwraca ten sam format dict co poprzednia wersja.

    require_rsi_climax=False — wyłącza filtr RSI dla SC/BC (tylko wolumen + trend).
    """
    lbl = _LABEL[bool(verbose)]
    n   = len(candles)

    if not candles or n < _MIN_CANDLES:
        return {
            "structure": "UNCLEAR", "phase": "Unclear",
            "events": [lbl["none"]], "confidence": "LOW",
            "range_high": None, "range_low": None,
            "full_trend": "unclear", "recent_trend": "unclear", "ranging": False,
        }

    pts = find_wyckoff_points(candles, require_rsi_climax=require_rsi_climax)
    acc  = pts["acc"]
    dist = pts["dist"]
    ctx  = pts["context"]
    rh   = pts["range_high"]
    rl   = pts["range_low"]

    full_trend   = _trend(candles, min(60, n))
    recent_trend = _trend(candles, min(15, n))

    # ── Trend strukturalny — liczony PRZED SOS/SOW żeby breakout nie zaburzał ──
    # Jeśli wiemy gdzie jest SOS lub SOW, liczymy trend na danych do tego punktu.
    # Dzięki temu akumulacja po trendzie spadkowym nie jest klasyfikowana jako
    # Reaccumulation tylko dlatego że SOS już wypchnął cenę w górę.
    _sos_idx  = pts["acc"].get("SOS",  {}).get("idx")
    _sow_idx  = pts["dist"].get("SOW", {}).get("idx")
    _sc_idx   = pts["acc"].get("SC",   {}).get("idx")
    _bc_idx   = pts["dist"].get("BC",  {}).get("idx")

    if ctx == "accumulation" and _sc_idx is not None:
        # Multi-window trend PRZED SC (pomijamy ostatnie 5 świec żeby nie wliczać samego SC)
        _pre_sc = candles[:_sc_idx] if _sc_idx > 20 else candles
        struct_trend = _multi_trend(_pre_sc, skip_last=5)

        # Override neutral → bearish when SC price is significantly below pre-SC median
        # (book: Accumulation follows a bearish trend; if multi-window is ambiguous but SC
        #  is clearly depressed vs prior action, the market was in a downtrend before SC)
        if struct_trend == "neutral" and _sc_idx >= 10:
            _pre_window = candles[max(0, _sc_idx - 60):_sc_idx]
            if len(_pre_window) >= 10:
                _pre_closes = sorted(c["close"] for c in _pre_window)
                _pre_median = _pre_closes[len(_pre_closes) // 2]
                _sc_close   = candles[_sc_idx]["close"]
                if _pre_median > 0 and _sc_close < _pre_median * 0.85:
                    struct_trend = "bearish"  # SC is 15%+ below median → Accumulation

    elif ctx == "distribution" and _bc_idx is not None:
        # Multi-window trend PRZED BC
        _pre_bc = candles[:_bc_idx] if _bc_idx > 20 else candles
        struct_trend = _multi_trend(_pre_bc, skip_last=5)
    else:
        struct_trend = full_trend

    # ── Zdarzenia ─────────────────────────────────────────────────────────────
    _validity_note = pts.get("validity_note", "")
    _VALIDITY_LABELS = {
        "acc_phase_e":    "⚡ Phase E — above range" if not verbose else "Pattern invalidated: Phase E — price far above range (markup complete)",
        "acc_invalidated":"⚡ Invalidated — below SC" if not verbose else "Pattern invalidated: price broke below SC low (accumulation failed)",
        "dist_phase_e":   "⚡ Phase E — below range" if not verbose else "Pattern invalidated: Phase E — price far below range (markdown complete)",
        "dist_invalidated":"⚡ Invalidated — above BC" if not verbose else "Pattern invalidated: price broke above BC high (distribution failed)",
    }
    events = []
    if ctx == "accumulation":
        if "PS"     in acc: events.append(lbl["ps"])
        if "SC"     in acc: events.append(lbl["sc"])
        if "Spring" in acc: events.append(lbl["spring"])
        if "SOS"    in acc: events.append(lbl["sos"])
        if "LPS"    in acc: events.append(lbl["lps"])
    elif ctx == "distribution":
        if "PSY"  in dist: events.append(lbl["psy"])
        if "BC"   in dist: events.append(lbl["bc"])
        if "UTAD" in dist: events.append(lbl["utad"])
        if "SOW"  in dist: events.append(lbl["sow"])
        if "LPSY" in dist: events.append(lbl["lpsy"])
    if _validity_note in _VALIDITY_LABELS:
        events.insert(0, _VALIDITY_LABELS[_validity_note])
    if not events:
        events.append(lbl["none"])

    # ── Faza i struktura ──────────────────────────────────────────────────────
    # struct_trend: trend PRZED SC/BC — kontekst który decyduje o Acc vs Reacc
    # neutral → UNCLEAR zamiast zgadywania (Redistribution 43% < losowego)
    if ctx == "accumulation":
        # neutral → Reaccumulation (backtest: 62.7% directional accuracy — wiarygodny sygnał)
        if struct_trend == "bearish":
            _acc_base = "Accumulation"
        else:
            _acc_base = "Reaccumulation"  # bullish lub neutral — oba bullish-bias

        if "LPS" in acc or "SOS" in acc:
            structure = _acc_base
            phase = "D"
            confidence = "HIGH" if ("Spring" in acc and "LPS" in acc) else \
                         "MEDIUM" if ("Spring" in acc or "LPS" in acc) else "LOW"
        elif "Spring" in acc:
            structure = _acc_base
            phase = "C"
            confidence = "MEDIUM"
        elif "ST" in acc:
            structure = _acc_base
            phase = "B"
            confidence = "LOW"
        elif "AR" in acc:
            structure = _acc_base
            phase = "A"
            confidence = "LOW"
        else:
            structure = "UNCLEAR"
            phase = "Unclear"
            confidence = "LOW"

    elif ctx == "distribution":
        # neutral przed BC → UNCLEAR tylko dla faz A/B.
        # Fazy C/D (potwierdzony breakout: UTAD/SOW) pokazuj zawsze — mają konkretny sygnał.
        if struct_trend == "bullish":
            _dist_base = "Distribution"
        elif struct_trend == "bearish":
            _dist_base = "Redistribution"
        else:
            _dist_base = None  # neutral — decyzja zależy od fazy

        if "LPSY" in dist or "SOW" in dist:
            # Phase D: potwierdzony breakdown — pokazuj nawet przy neutral trend
            structure = _dist_base or "Redistribution"
            phase = "D"
            confidence = "HIGH" if ("UTAD" in dist and "LPSY" in dist) else \
                         "MEDIUM" if ("UTAD" in dist or "LPSY" in dist) else "LOW"
        elif "UTAD" in dist:
            # Phase C: false break potwierdzony — zachowaj
            structure = _dist_base or "Redistribution"
            phase = "C"
            confidence = "MEDIUM"
        elif _dist_base is None:
            # Fazy A/B bez jasnego trendu poprzedzającego → UNCLEAR
            structure = "UNCLEAR"
            phase = "Unclear"
            confidence = "LOW"
        elif "ST" in dist:
            structure = _dist_base
            phase = "B"
            confidence = "LOW"
        elif "AR" in dist:
            structure = _dist_base
            phase = "A"
            confidence = "LOW"
        else:
            structure = "UNCLEAR"
            phase = "Unclear"
            confidence = "LOW"

    else:
        # Brak wyraźnej struktury
        if full_trend == "bullish":
            structure, phase, confidence = "Trend (Bullish)", "E", "MEDIUM"
        elif full_trend == "bearish":
            structure, phase, confidence = "Trend (Bearish)", "E", "MEDIUM"
        else:
            structure, phase, confidence = "UNCLEAR", "Unclear", "LOW"

    # ── Volume-based confidence adjustments ──────────────────────────────────
    # Applied after phase/structure logic, before the candle-count cap.

    if ctx == "accumulation":
        _wvr = pts["acc"].get("_wave_vol_ratio", 1.0)
        _rvs = pts["acc"].get("_range_vol_slope", 0.0)
        _spring = pts["acc"].get("Spring", {})
        _spring_type = _spring.get("spring_type", 2) if _spring else 2
        _fvg_b  = pts["acc"].get("_fvg_bias", 0.0)
        _hl_b   = pts["acc"].get("_hl_score_bull", 0)
        _hl_bear_acc = pts["acc"].get("_hl_score_bear", 0)
        _ar_choch_acc = pts["acc"].get("AR", {}).get("choch_strength", 1.0) if "AR" in pts["acc"] else 1.0

        # Spring Type 3 (exhaustion, low vol) is the strongest signal — boost confidence
        if "Spring" in pts["acc"] and _spring_type == 3 and confidence == "LOW":
            confidence = "MEDIUM"

        # Spring Type 1 (high-vol shakeout) requires a subsequent test — cap at MEDIUM
        if "Spring" in pts["acc"] and _spring_type == 1 and confidence == "HIGH" and "LPS" not in pts["acc"]:
            confidence = "MEDIUM"

        # Wave vol ratio < 0.8 in "accumulation" context = down-legs dominate → suspicious
        if _wvr < 0.80 and confidence == "HIGH":
            confidence = "MEDIUM"

        # SMC: bullish FVG bias within range confirms genuine demand absorption
        if _fvg_b > 0.002 and _hl_b > _hl_bear_acc and confidence == "MEDIUM":
            confidence = "HIGH"  # FVG + HH/HL confirm structure → upgrade

        # SMC: bearish FVG dominance inside supposed accumulation = suspicious → cap
        if _fvg_b < -0.002 and confidence == "HIGH":
            confidence = "MEDIUM"

        # ChoCH (Ch.16): AR is the primary confirmation of SC genuineness.
        # Book: "use AR ChoCH to identify the genuine Selling Climax."
        # < 0.3 = AR not a genuine ChoCH → SC unconfirmed → force LOW regardless of phase.
        # 0.3–0.6 = weak ChoCH → redistribution suspected → cap down 1 level.
        # NOTE: strong AR boost (>1.5 → HIGH) was tested and showed 48.3% accuracy → removed.
        if _ar_choch_acc < AR_CHOCH_MIN_CONFIRM:  # < 0.3: AR doesn't confirm SC
            confidence = "LOW"
        elif _ar_choch_acc < 0.60:
            if confidence == "HIGH":
                confidence = "MEDIUM"
            elif confidence == "MEDIUM":
                confidence = "LOW"

        # Book Ch.16: "ST ending below SC adds strength to redistribution scenario."
        # Bears still in control if ST can't hold above SC low.
        if pts["acc"].get("_st_below_sc", False) and confidence in ("HIGH", "MEDIUM"):
            confidence = "LOW"

    elif ctx == "distribution":
        _wvr  = pts["dist"].get("_wave_vol_ratio", 1.0)
        _rvs  = pts["dist"].get("_range_vol_slope", 0.0)
        _fvg_d = pts["dist"].get("_fvg_bias", 0.0)
        _ar_choch_dist = pts["dist"].get("AR", {}).get("choch_strength", 1.0) if "AR" in pts["dist"] else 1.0

        # Signal 1 (Villahermosa Ch.9): if up-legs dominate volume = Reaccumulation, not Distribution.
        # WVR > 1.5 alone is sufficient — price is absorbing supply, not distributing.
        # WVR > 1.2 + declining slope = declining vol + bullish waves = Reacc signature.
        if _wvr > 1.50:
            if confidence == "HIGH":
                confidence = "MEDIUM"
            elif confidence == "MEDIUM":
                confidence = "LOW"
        elif _wvr > 1.20 and _rvs < -0.010:
            if confidence == "HIGH":
                confidence = "MEDIUM"
            elif confidence == "MEDIUM":
                confidence = "LOW"

        # Signal 2: LPSY weaker than UTAD confirms genuine distribution Phase D
        # (kept from original logic — structural confirmation from sequence, not volume alone)
        if "LPSY" in pts["dist"] and "UTAD" in pts["dist"]:
            if pts["dist"]["LPSY"].get("utad_test_weak", False) and confidence == "MEDIUM":
                confidence = "HIGH"

        # SMC: bullish FVG inside distribution range → price absorbing supply = Reacc signal
        # Backtest result: distributions with bullish FVG had ~22% directional accuracy → cap.
        if _fvg_d > 0.002 and confidence in ("HIGH", "MEDIUM"):
            confidence = "LOW"

        # ChoCH (Ch.16): AR (downward reaction) is the primary confirmation of BC genuineness.
        # Book: "use AR ChoCH to identify the genuine Buying Climax."
        # < 0.3 = AR not a genuine ChoCH → BC unconfirmed → force LOW.
        # 0.3–0.6 = weak ChoCH → reaccumulation suspected → cap down 1 level.
        if _ar_choch_dist < AR_CHOCH_MIN_CONFIRM:  # < 0.3: AR doesn't confirm BC
            confidence = "LOW"
        elif _ar_choch_dist < 0.60 and confidence in ("HIGH", "MEDIUM"):
            if confidence == "HIGH":
                confidence = "MEDIUM"
            else:
                confidence = "LOW"

        # Book Ch.16: ST ending above BC high = demand exceeds supply after reaction → Reaccumulation.
        # This is a structural confirmation — the BC high did not hold as resistance → cap to LOW.
        if pts["dist"].get("_st_above_bc", False) and confidence in ("HIGH", "MEDIUM"):
            confidence = "LOW"

        # Backtest finding: Distribution/Redistribution MEDIUM confidence = 38-41% accuracy.
        # MEDIUM for distribution is structurally unreliable unless UTAD+LPSY sequence confirmed.
        # Without that structural proof, cap MEDIUM→LOW to avoid misleadingly confident signals.
        if confidence == "MEDIUM":
            has_utad_lpsy = ("UTAD" in pts["dist"] and "LPSY" in pts["dist"] and
                             pts["dist"]["LPSY"].get("utad_test_weak", False))
            if not has_utad_lpsy:
                confidence = "LOW"

    # Zakres
    ranging = False
    if rh is not None and rl is not None:
        cur = candles[-min(30, n):]
        cur_h = max(c["high"]  for c in cur)
        cur_l = min(c["low"]   for c in cur)
        cur_avg = sum(c["close"] for c in cur) / len(cur)
        if cur_avg > 0 and (cur_h - cur_l) / cur_avg < 0.10:
            ranging = True

    confidence = _cap_conf(confidence, n)

    return {
        "structure":    structure,
        "phase":        phase,
        "events":       events,
        "confidence":   confidence,
        "range_high":   rh,
        "range_low":    rl,
        "full_trend":   full_trend,
        "recent_trend": recent_trend,
        "ranging":      ranging,
        # Dodatkowe — nowe pola dostępne przy bezpośrednim użyciu
        "_points":      pts,
    }
