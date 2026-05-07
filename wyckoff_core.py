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
SC_VOL_MULT     = 1.5    # SC: wolumen >= X * sma(vol, 20)
BC_VOL_MULT     = 1.5    # BC: j.w.
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

def find_wyckoff_points(candles, pivot_len=PIVOT_LEN, rsi_sens=RSI_SENS):
    """
    Wykrywa sekwencyjnie punkty Wyckoffa na liście świec.

    Akumulacja:  SC → AR → ST → [Spring] → SOS → LPS
    Dystrybucja: BC → AR → ST → [UTAD]  → SOW → LPSY

    Parametry:
      candles   – lista dict {open, high, low, close, volume, time}
      pivot_len – liczba świec po każdej stronie dla pivot
      rsi_sens  – strefa neutralna RSI (domyślnie 20 → 30–70)

    Zwraca dict:
      "acc"  → dict z punktami akumulacji (SC, AR, ST, Spring, SOS, LPS)
      "dist" → dict z punktami dystrybucji (BC, AR, ST, UTAD, SOW, LPSY)
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

    # SC: pivot low + RSI < rsi_low + wysoki wolumen
    sc_idx = None
    for i in p_lows:
        r = rsi_arr[i]
        v = vsma[i]
        if r is None or v is None:
            continue
        # RSI w strefie bear przy pivot low
        if r >= rsi_low:
            continue
        # Wolumen >= SC_VOL_MULT * sma(vol,20)
        if candles[i]["volume"] < v * SC_VOL_MULT:
            continue
        # Świeca spadkowa (close < open) lub długi dolny cień
        c = candles[i]
        is_bearish_candle = c["close"] < c["open"] or (c["open"] - c["low"]) > (c["high"] - c["low"]) * 0.4
        if not is_bearish_candle:
            continue
        sc_idx = i  # bierzemy ostatni spełniający (najbardziej aktualny)

    if sc_idx is not None:
        acc["SC"] = {"idx": sc_idx, "price": candles[sc_idx]["low"],
                     "rsi": rsi_arr[sc_idx], "volume": candles[sc_idx]["volume"]}

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
            acc["AR"] = {"idx": ar_idx, "price": candles[ar_idx]["high"],
                         "rsi": rsi_arr[ar_idx], "volume": candles[ar_idx]["volume"]}

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
                acc["ST"] = {"idx": st_idx, "price": candles[st_idx]["low"],
                             "rsi": rsi_arr[st_idx], "volume": candles[st_idx]["volume"],
                             "low_volume": vsma[st_idx] is not None and
                                           candles[st_idx]["volume"] <= (vsma[st_idx] or 1) * ST_VOL_RATIO}

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
                    # Fałszywe wybicie: low < cr_low ale close > cr_low
                    if c["low"] < cr_low and c["close"] > cr_low:
                        low_vol = v is None or c["volume"] <= v * 0.9
                        acc["Spring"] = {"idx": i, "price": c["low"],
                                         "close": c["close"],
                                         "rsi": r,
                                         "volume": c["volume"],
                                         "low_volume": low_vol}
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
                    above_cr = c["close"] > cr_high * 1.001
                    bull_rsi  = r > 50  # RSI przekracza środek
                    hi_vol    = v is None or c["volume"] >= v * 1.0
                    if above_cr and bull_rsi:
                        acc["SOS"] = {"idx": i, "price": c["high"],
                                      "close": c["close"],
                                      "rsi": r,
                                      "volume": c["volume"],
                                      "high_volume": hi_vol}
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

    # ════════════════════════════════════════════════════════════════════════
    # DYSTRYBUCJA: BC → AR → ST → UTAD → SOW → LPSY
    # ════════════════════════════════════════════════════════════════════════
    dist = {}

    # BC: pivot high + RSI > rsi_high + wysoki wolumen + świeca wzrostowa
    bc_idx = None
    for i in p_highs:
        r = rsi_arr[i]
        v = vsma[i]
        if r is None or v is None:
            continue
        if r <= rsi_high:
            continue
        if candles[i]["volume"] < v * BC_VOL_MULT:
            continue
        c = candles[i]
        is_bullish_candle = c["close"] > c["open"] or (c["high"] - c["close"]) < (c["high"] - c["low"]) * 0.3
        if not is_bullish_candle:
            continue
        bc_idx = i

    if bc_idx is not None:
        dist["BC"] = {"idx": bc_idx, "price": candles[bc_idx]["high"],
                      "rsi": rsi_arr[bc_idx], "volume": candles[bc_idx]["volume"]}

        # AR (dystrybucja): pierwsza pivot low PO BC
        ar_d_idx = None
        for i in p_lows:
            if i <= bc_idx:
                continue
            r = rsi_arr[i]
            if r is None:
                continue
            if r > rsi_high:
                continue
            ar_d_idx = i
            break

        if ar_d_idx is not None:
            dist["AR"] = {"idx": ar_d_idx, "price": candles[ar_d_idx]["low"],
                          "rsi": rsi_arr[ar_d_idx], "volume": candles[ar_d_idx]["volume"]}

            cr_high_d = candles[bc_idx]["high"]
            cr_low_d  = candles[ar_d_idx]["low"]

            # ST (dystrybucja): pivot high PO AR + RSI nie wraca powyżej rsi_high + niski wolumen
            st_d_idx = None
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
                if price_ok:
                    st_d_idx = i
                    break

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
                if c["high"] > cr_high_d and c["close"] < cr_high_d:
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
                    below_cr = c["close"] < cr_low_d * 0.999
                    bear_rsi  = r < 50
                    if below_cr and bear_rsi:
                        dist["SOW"] = {"idx": i, "price": c["low"],
                                       "close": c["close"],
                                       "rsi": r,
                                       "volume": c["volume"]}
                        break

            # LPSY: pivot high PO SOW + cena poniżej CR high + niski wolumen + RSI < 50
            if "SOW" in dist:
                sow_i = dist["SOW"]["idx"]
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
                        dist["LPSY"] = {"idx": i, "price": c["high"],
                                        "rsi": r,
                                        "volume": c["volume"],
                                        "low_volume": low_vol}
                        break

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
        ACC_SEQ  = ["SC", "AR", "ST", "Spring", "SOS", "LPS"]
        DIST_SEQ = ["BC", "AR", "ST", "UTAD",   "SOW", "LPSY"]
        acc_score  = sum(1 for k in ACC_SEQ  if k in acc)
        dist_score = sum(1 for k in DIST_SEQ if k in dist)

        if acc_score != dist_score:
            context = "accumulation" if acc_score > dist_score else "distribution"
        else:
            # Remis — wygrywa ta której OSTATNI wykryty punkt jest późniejszy
            acc_last  = max((v["idx"] for v in acc.values()),  default=0)
            dist_last = max((v["idx"] for v in dist.values()), default=0)
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
        "sc": "SC (Selling Climax)", "bc": "BC (Buying Climax)",
        "spring": "Spring",          "utad": "UTAD",
        "sos": "SOS",                "sow": "SOW",
        "lps": "LPS",                "lpsy": "LPSY",
        "none": "No clear events",
    },
    True: {
        "sc": "Possible SC (Selling Climax)",  "bc": "Possible BC (Buying Climax)",
        "spring": "Spring (test below support)", "utad": "UTAD (test above resistance)",
        "sos": "SOS — break above range high",   "sow": "SOW — break below range low",
        "lps": "LPS — pullback after SOS",       "lpsy": "LPSY — pullback after SOW",
        "none": "No clear Wyckoff events",
    },
}


def analyze_wyckoff(candles, verbose=False):
    """
    Drop-in replacement dla starego wyckoff_core.analyze_wyckoff.
    Zwraca ten sam format dict co poprzednia wersja.
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

    pts = find_wyckoff_points(candles)
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
        # Trend przed SC — to jest kontekst wejścia w akumulację
        _pre_sc = candles[:_sc_idx] if _sc_idx > 20 else candles
        struct_trend = _trend(_pre_sc, min(60, len(_pre_sc)))
    elif ctx == "distribution" and _bc_idx is not None:
        # Trend przed BC
        _pre_bc = candles[:_bc_idx] if _bc_idx > 20 else candles
        struct_trend = _trend(_pre_bc, min(60, len(_pre_bc)))
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
        if "SC"     in acc: events.append(lbl["sc"])
        if "Spring" in acc: events.append(lbl["spring"])
        if "SOS"    in acc: events.append(lbl["sos"])
        if "LPS"    in acc: events.append(lbl["lps"])
    elif ctx == "distribution":
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
    if ctx == "accumulation":
        _acc_base = "Accumulation" if struct_trend == "bearish" else "Reaccumulation"
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
        else:
            structure = _acc_base
            phase = "A"
            confidence = "LOW"

    elif ctx == "distribution":
        _dist_base = "Distribution" if struct_trend == "bullish" else "Redistribution"
        if "LPSY" in dist or "SOW" in dist:
            structure = _dist_base
            phase = "D"
            confidence = "HIGH" if ("UTAD" in dist and "LPSY" in dist) else \
                         "MEDIUM" if ("UTAD" in dist or "LPSY" in dist) else "LOW"
        elif "UTAD" in dist:
            structure = _dist_base
            phase = "C"
            confidence = "MEDIUM"
        elif "ST" in dist:
            structure = _dist_base
            phase = "B"
            confidence = "LOW"
        else:
            structure = _dist_base
            phase = "A"
            confidence = "LOW"

    else:
        # Brak wyraźnej struktury
        if full_trend == "bullish":
            structure, phase, confidence = "Trend (Bullish)", "E", "MEDIUM"
        elif full_trend == "bearish":
            structure, phase, confidence = "Trend (Bearish)", "E", "MEDIUM"
        else:
            structure, phase, confidence = "UNCLEAR", "Unclear", "LOW"

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
