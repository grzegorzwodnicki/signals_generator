#!/usr/bin/env python3
"""
Porównanie wykrywania struktur Wyckoffa:
  - Strict mode (require_rsi_climax=True):  RSI<30 + volume >= 1.5x SMA
  - Soft   mode (require_rsi_climax=False): no RSI gate + volume >= 1.0x SMA

Test na top-N symbolach krypto, dane 1H.
Wyniki: zestawienie struktur, faz, jakości SC/BC.
"""

import sys, os, time, concurrent.futures
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "strategy_wyckoff_9_5"))
import strategy as st
import wyckoff_core as wc

# ── Konfiguracja ─────────────────────────────────────────────────────────────
TOP_N    = 100
TF       = "1H"
WORKERS  = 20

def fetch_data(symbol):
    try:
        candles = st.get_400_candles(symbol, TF)
        return symbol, candles
    except Exception as e:
        return symbol, None

def analyze(symbol, candles):
    if not candles or len(candles) < 50:
        return None

    r_strict = wc.analyze_wyckoff(candles, verbose=False, require_rsi_climax=True)
    r_soft   = wc.analyze_wyckoff(candles, verbose=False, require_rsi_climax=False)

    pts_strict = wc.find_wyckoff_points(candles, require_rsi_climax=True)
    pts_soft   = wc.find_wyckoff_points(candles, require_rsi_climax=False)

    vsma = pts_soft["vol_sma"]

    def sc_info(pts):
        sc = pts["acc"].get("SC")
        if sc and vsma[sc["idx"]]:
            return {
                "idx": sc["idx"],
                "rsi": sc["rsi"],
                "vol_mult": sc["volume"] / vsma[sc["idx"]] if vsma[sc["idx"]] else 0,
                "events": list(pts["acc"].keys()),
            }
        return None

    def bc_info(pts):
        bc = pts["dist"].get("BC")
        if bc and vsma[bc["idx"]]:
            return {
                "idx": bc["idx"],
                "rsi": bc["rsi"],
                "vol_mult": bc["volume"] / vsma[bc["idx"]] if vsma[bc["idx"]] else 0,
                "events": list(pts["dist"].keys()),
            }
        return None

    return {
        "symbol": symbol,
        "n_candles": len(candles),
        "strict": {
            "structure": r_strict["structure"],
            "phase":     r_strict["phase"],
            "sc":        sc_info(pts_strict),
            "bc":        bc_info(pts_strict),
        },
        "soft": {
            "structure": r_soft["structure"],
            "phase":     r_soft["phase"],
            "sc":        sc_info(pts_soft),
            "bc":        bc_info(pts_soft),
        },
    }

def main():
    print(f"\n=== Wyckoff Core — porównanie strict vs soft mode ===")
    print(f"Top {TOP_N} symboli, timeframe {TF}, {wc.PIVOT_LEN} pivot bars\n")

    # Pobierz listę symboli
    print("Pobieranie listy symboli...")
    symbols_raw = st.get_top_crypto(TOP_N)
    symbols = [s["symbol"] for s in symbols_raw if isinstance(s, dict) and "symbol" in s][:TOP_N]
    print(f"  {len(symbols)} symboli\n")

    # Pobierz dane równolegle
    print(f"Pobieranie danych {TF}...")
    data = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_data, s): s for s in symbols}
        done = 0
        for f in concurrent.futures.as_completed(futs):
            sym, candles = f.result()
            if candles:
                data[sym] = candles
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{len(symbols)} pobranych...")
    print(f"  Dane dostępne dla {len(data)} symboli\n")

    # Analizuj
    results = []
    for sym, candles in data.items():
        r = analyze(sym, candles)
        if r:
            results.append(r)

    # ── Agregacja wyników ─────────────────────────────────────────────────────
    strict_structs = defaultdict(int)
    soft_structs   = defaultdict(int)
    strict_phases  = defaultdict(int)
    soft_phases    = defaultdict(int)

    agree = 0
    disagree_list = []

    strict_sc_rsi, soft_sc_rsi = [], []
    strict_sc_vol, soft_sc_vol = [], []
    strict_bc_rsi, soft_bc_rsi = [], []
    strict_bc_vol, soft_bc_vol = [], []

    # Sekwencje — ile punktów wykrytych
    soft_sc_seq_len   = defaultdict(int)
    strict_sc_seq_len = defaultdict(int)
    soft_bc_seq_len   = defaultdict(int)
    strict_bc_seq_len = defaultdict(int)

    for r in results:
        s, soft = r["strict"], r["soft"]

        strict_structs[s["structure"]] += 1
        soft_structs[soft["structure"]] += 1
        strict_phases[f"{s['structure']} Ph{s['phase']}"] += 1
        soft_phases[f"{soft['structure']} Ph{soft['phase']}"] += 1

        if s["structure"] == soft["structure"]:
            agree += 1
        else:
            disagree_list.append(r)

        if s["sc"]:
            strict_sc_rsi.append(s["sc"]["rsi"])
            strict_sc_vol.append(s["sc"]["vol_mult"])
            strict_sc_seq_len[len(s["sc"]["events"])] += 1
        if soft["sc"]:
            soft_sc_rsi.append(soft["sc"]["rsi"])
            soft_sc_vol.append(soft["sc"]["vol_mult"])
            soft_sc_seq_len[len(soft["sc"]["events"])] += 1
        if s["bc"]:
            strict_bc_rsi.append(s["bc"]["rsi"])
            strict_bc_vol.append(s["bc"]["vol_mult"])
            strict_bc_seq_len[len(s["bc"]["events"])] += 1
        if soft["bc"]:
            soft_bc_rsi.append(soft["bc"]["rsi"])
            soft_bc_vol.append(soft["bc"]["vol_mult"])
            soft_bc_seq_len[len(soft["bc"]["events"])] += 1

    n = len(results)

    def pct(x): return f"{100*x/n:.1f}%" if n else "—"
    def avg(lst): return f"{sum(lst)/len(lst):.1f}" if lst else "—"
    def med(lst):
        s = sorted(lst)
        return f"{s[len(s)//2]:.1f}" if s else "—"

    # ── Raport ────────────────────────────────────────────────────────────────
    print(f"{'='*65}")
    print(f"Przeanalizowano: {n} symboli")
    print(f"{'='*65}\n")

    print(f"{'STRUKTURA':<28} {'STRICT':>8} {'SOFT':>8}")
    print(f"{'-'*46}")
    all_structs = sorted(set(list(strict_structs.keys()) + list(soft_structs.keys())))
    for k in all_structs:
        sv = strict_structs.get(k, 0)
        so = soft_structs.get(k, 0)
        print(f"  {k:<26} {sv:>6} ({pct(sv)})  {so:>6} ({pct(so)})")

    print(f"\n{'ZGODNOŚĆ MIĘDZY TRYBAMI':}")
    print(f"  Zgoda:    {agree}/{n}  ({pct(agree)})")
    print(f"  Niezgoda: {len(disagree_list)}/{n}  ({pct(len(disagree_list))})")

    print(f"\n{'STATYSTYKI SC (Selling Climax)':}")
    print(f"  {'':30} {'STRICT':>10} {'SOFT':>10}")
    print(f"  {'Wykrytych SC':30} {len(strict_sc_rsi):>10} {len(soft_sc_rsi):>10}")
    print(f"  {'Avg RSI przy SC':30} {avg(strict_sc_rsi):>10} {avg(soft_sc_rsi):>10}")
    print(f"  {'Med RSI przy SC':30} {med(strict_sc_rsi):>10} {med(soft_sc_rsi):>10}")
    print(f"  {'Avg Vol/SMA przy SC':30} {avg(strict_sc_vol):>10} {avg(soft_sc_vol):>10}")
    print(f"  {'Med Vol/SMA przy SC':30} {med(strict_sc_vol):>10} {med(soft_sc_vol):>10}")
    if strict_sc_rsi:
        sc_rsi_ok = sum(1 for r in strict_sc_rsi if r < 30)
        print(f"  {'SC z RSI<30 (strict)':30} {sc_rsi_ok:>10}")
    if soft_sc_rsi:
        sc_rsi_ok_soft = sum(1 for r in soft_sc_rsi if r < 40)
        sc_rsi_mid = sum(1 for r in soft_sc_rsi if 40 <= r < 50)
        sc_rsi_high = sum(1 for r in soft_sc_rsi if r >= 50)
        print(f"  {'SC z RSI<40 (soft)':30} {sc_rsi_ok_soft:>10}")
        print(f"  {'SC z RSI 40-50 (soft)':30} {sc_rsi_mid:>10}")
        print(f"  {'SC z RSI>=50 (soft)':30} {sc_rsi_high:>10} ← potencjalne false pos.")

    print(f"\n{'STATYSTYKI BC (Buying Climax)':}")
    print(f"  {'':30} {'STRICT':>10} {'SOFT':>10}")
    print(f"  {'Wykrytych BC':30} {len(strict_bc_rsi):>10} {len(soft_bc_rsi):>10}")
    print(f"  {'Avg RSI przy BC':30} {avg(strict_bc_rsi):>10} {avg(soft_bc_rsi):>10}")
    print(f"  {'Avg Vol/SMA przy BC':30} {avg(strict_bc_vol):>10} {avg(soft_bc_vol):>10}")
    if soft_bc_rsi:
        bc_rsi_ok = sum(1 for r in soft_bc_rsi if r >= 70)
        bc_rsi_mid = sum(1 for r in soft_bc_rsi if 60 <= r < 70)
        print(f"  {'BC z RSI>=70 (soft)':30} {bc_rsi_ok:>10}")
        print(f"  {'BC z RSI 60-70 (soft)':30} {bc_rsi_mid:>10} ← exhaustion type")

    print(f"\n{'DŁUGOŚĆ SEKWENCJI ACC (ile punktów po SC)':}")
    print(f"  {'Punkty':>8} {'STRICT':>10} {'SOFT':>10}")
    all_lens = sorted(set(list(strict_sc_seq_len.keys()) + list(soft_sc_seq_len.keys())))
    for l in all_lens:
        sv = strict_sc_seq_len.get(l, 0)
        so = soft_sc_seq_len.get(l, 0)
        print(f"  {l:>8} {'pkt':5} {sv:>10} {so:>10}")

    print(f"\n{'DŁUGOŚĆ SEKWENCJI DIST (ile punktów po BC)':}")
    print(f"  {'Punkty':>8} {'STRICT':>10} {'SOFT':>10}")
    all_lens_d = sorted(set(list(strict_bc_seq_len.keys()) + list(soft_bc_seq_len.keys())))
    for l in all_lens_d:
        sv = strict_bc_seq_len.get(l, 0)
        so = soft_bc_seq_len.get(l, 0)
        print(f"  {l:>8} {'pkt':5} {sv:>10} {so:>10}")

    # ── Szczegóły niezgodności ────────────────────────────────────────────────
    if disagree_list:
        print(f"\n{'='*65}")
        print(f"SYMBOLE GDZIE TRYBY SIĘ RÓŻNIĄ ({len(disagree_list)}):")
        print(f"  {'Symbol':<12} {'STRICT':>20} {'SOFT':>20}")
        print(f"  {'-'*54}")
        for r in sorted(disagree_list, key=lambda x: x["symbol"])[:30]:
            sv = f"{r['strict']['structure']} Ph{r['strict']['phase']}"
            so = f"{r['soft']['structure']} Ph{r['soft']['phase']}"
            print(f"  {r['symbol']:<12} {sv:>20} {so:>20}")
        if len(disagree_list) > 30:
            print(f"  ... (+{len(disagree_list)-30} więcej)")

    print(f"\n{'='*65}")
    print("LEGENDA:")
    print("  STRICT = require_rsi_climax=True  (RSI<30 dla SC + vol>=1.5x SMA)")
    print("  SOFT   = require_rsi_climax=False (brak RSI gate + vol>=1.0x SMA) ← domyślny")

if __name__ == "__main__":
    main()
