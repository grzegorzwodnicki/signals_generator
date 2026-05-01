# Strategy Wyckoff 9 — dokumentacja

Strategia łączy analizę struktury Wyckoffa (1H) z konceptami Smart Money Concepts (5M): FVG, Order Block, ChoCH i dywergencją MACD. Sygnał powstaje tylko wtedy, gdy wyższy timeframe daje kierunek, a niższy — precyzyjny punkt wejścia.

---

## Pliki

| Plik | Rola |
|---|---|
| `strategy.py` | Logika wspólna: pobieranie danych, wszystkie detektory, scoring, klasyfikacja |
| `execution.py` | Silnik symulacji transakcji: Model A, Model B, FIXED_R |
| `scanner.py` | Skan na żywo → raport HTML |
| `backtest.py` | Backtest historyczny: multi-config × multi-model × multi-tryb |

---

## Uruchamianie

```bash
source venv/bin/activate

# Skan na żywo
python strategy_wyckoff_9_4/scanner.py

# Backtest
python strategy_wyckoff_9_4/backtest.py [--fuel-filter on|off]
```

Scanner pyta o tryb: `t` = dane live, `h` = historyczne (`dd/mm/yyyy hh:mm`).
Backtest pyta interaktywnie o zakres dat, interwał, konfiguracje i modele.

Cache: `strategy_wyckoff_9_4/cache/` · Wyniki: `strategy_wyckoff_9_4/results/`

---

## Jak działa strategia

### 1. Kontekst rynkowy (1H) — Wyckoff

Algorytm bierze 45 ostatnich świec 1H i wyznacza zakres akumulacji/dystrybucji:

- **Range High** = 80. percentyl maksimów
- **Range Low** = 20. percentyl minimów
- Zakres musi mieścić się w przedziale 0,2–30% ceny (odrzuca trend i flatline)

Następnie w ostatnich 15 świecach 1H szuka:

- **SOS** (Sign of Strength) — zamknięcie powyżej range high → kierunek LONG
- **SOW** (Sign of Weakness) — zamknięcie poniżej range low → kierunek SHORT

Jeśli nie ma jednoznacznego SOS ani SOW — symbol jest odrzucany.

---

### 2. Jakość bazy — Wyckoff Cause Score (0–15)

Ocenia, jak dobra jest baza Wyckoffa zanim cena wróci do wejścia.

| Składnik | Max | Opis |
|---|---|---|
| Czas trwania zakresu | 4 | Liczba świec 1H wewnątrz zakresu |
| Kompresja zakresu | 3 | Stosunek wysokości zakresu do ATR 1H (sweet spot 1–3 ATR) |
| Przemieszczenie SOS/SOW | 4 | Jak mocno wybił breakout (w ATR); +1 jeśli wolumen ≥ 1,2× średniej |
| Wiek fazy D | 2 | Ile świec minęło od breakoutu: ≤6 → 2 pkt, ≤24 → 1 pkt |
| Trzymanie pullbacku | 2 | Pullback ≤ 50% zakresu + brak zamknięcia przez środek |

**Etykiety:**

| Wynik | Etykieta |
|---|---|
| 0–4 | WEAK_BASE |
| 5–8 | NORMAL_BASE |
| 9–12 | STRONG_BASE |
| 13–15 | VERY_STRONG_BASE |
| cap 5 | CHOP_BASE (zakres > 168h lub > 5 ATR z słabym wybiciem) |

---

### 3. Punkty wejścia na 5M — SMC

Po ustaleniu kierunku algorytm szuka struktury wejściowej na 5M:

**FVG (Fair Value Gap)** — luka cenowa (low[i] > high[i-2] dla LONG) w ostatnich 120 świecach. Środek FVG = wejście, -1% poniżej FVG_low = SL.

**Order Block** — ostatnia przeciwna świeca przed silnym wybiciem (≥ 0,1%). Środek OB = wejście. Jeśli FVG i OB nakładają się → status `at_fvg_and_order_block` (najwyżej punktowany).

**Confluence Zone** — gdy nie ma FVG ani OB, ale cena jest blisko (±15% zakresu) krawędzi Wyckoffa.

**ChoCH (Change of Character)** — potwierdzenie zmiany struktury na 5M: zamknięcie powyżej lokalnego maksimum (LONG) lub poniżej lokalnego minimum (SHORT) w oknie 5 świec, wyszukiwane 30 świec wstecz. ChoCH starszy niż 6 świec = odrzucenie.

**Trigger candle** — wymagany engulfing lub pin bar. Bez wzorca → brak sygnału.

---

### 4. Dywergencja MACD

Liczona na 5M (i pomocniczo 15M). Algorytm dzieli ostatnie 30 świec na dwie połowy i porównuje ekstrema ceny z ekstremami histogramu MACD:

- `yes` — dywergencja zgodna z kierunkiem (bycza/niedźwiedzia)
- `against` — dywergencja odwrotna → **twarde odrzucenie**
- `none` — brak dywergencji

---

### 5. Poziomy TP i SL

```
risk = |entry - SL|

TP1a = entry ± 1,1 × risk   (pierwszy częściowy zamknięcie)
TP1b = entry ± 2,0 × risk   (drugi częściowy zamknięcie)
TP2  = entry ± 3,0 × risk   (cel główny)
TP3  = entry ± 4,0 × risk   (cel rozszerzony)
R:R  = 3,0 (stały)
```

Minimalny R:R = 2,0 — poniżej odrzucenie.

---

### 6. Target Feasibility Score — TFS (0–100)

Ocenia, czy cena ma realną szansę dotrzeć do TP2. Składa się z 6 komponentów:

| Składnik | Max | Opis |
|---|---|---|
| clean_path | 20 | Brak swing high/low między entry a TP2 |
| liquidity_magnet | 18 | TP2 blisko swing high/low lub krawędzi zakresu (±1,5%) |
| poc | 17 | Położenie POC: pomiędzy SL a entry (support) = max; pomiędzy entry a TP1 (obstacle) = 0 |
| momentum | 18 | Siła trigger candle: body ratio, close position, wolumen |
| atr_capacity | 12 | Odległość do TP2 w ATR: ≤4 ATR = 12 pkt, >10 ATR = 0 pkt |
| wyckoff_cause | 15 | Wyckoff Cause Score (z powyżej) |

**Werdykty:**

| TFS | Werdykt |
|---|---|
| ≥ 85 | CLEAN PATH |
| ≥ 70 | TARGET POSSIBLE |
| ≥ 55 | TARGET DIFFICULT |
| ≥ 40 | TARGET BLOCKED |
| < 40 | NO FUEL |

Gdy `--fuel-filter off`: TFS jest obliczany, ale nie wpływa na klasyfikację ani scoring.

---

### 7. Scoring

**Total Score (TS, 0–100)** — ocena techniczna: Wyckoff, FVG, OB, ChoCH, wzorzec, R:R, regime, MACD. Wynik < 73 → watchlist.

**Manual Pick Score (MPS, 0–100)** — waga jakości wejścia z perspektywy tradera:

| Składnik | Max |
|---|---|
| MACD zgodny + status wejścia | 20 |
| Entry extension (jak daleko od krawędzi) | 20 |
| Status (FVG+OB = 20, FVG = 16, OB = 14) | 20 |
| Wiek ChoCH | 15 |
| Engulfing w FVG/OB | 10 |
| R:R | 10 |
| Reżim rynkowy | ±5 |
| TFS (gdy fuel-filter on) | 20 |

TFS < 55 ogranicza MPS do max 65 (gdy fuel-filter on).

---

### 8. Klasyfikacja

**Twarde odrzucenia (zawsze aktywne):**
- MACD against
- Entry extension > 50%
- R:R < 2,0
- FVG wypełnione
- ChoCH starszy niż 4 świece
- TFS < 40 (gdy fuel-filter on)

**Kategorie:**

| Kategoria | Warunki |
|---|---|
| `premium_setup` | MACD yes + entry_ext ≤ 0,25 + FVG/OB + ChoCH ≤ 3 + engulfing + MPS ≥ 75 |
| `high_quality` | entry_ext ≤ 0,25 + engulfing + MACD yes/none + FVG/OB + ChoCH ≤ 3 |
| `secondary_quality` | Przechodzi twarde filtry + TS ≥ 73 + engulfing, ale nie spełnia premium/HQ |
| `watchlist` | Słabszy sygnał, nie spełnia warunków aktywnych kategorii |
| `rejected` | Twarde odrzucenie |

---

### 9. Modele egzekucji

**Model A** — agresywny, 3 poziomy wyjścia:
- 35% pozycji zamknięte @ TP1a (1,1R) → BE
- 35% @ TP1b (2,0R)
- 30% @ TP2 (3,0R)
- Max zysk: 1,985R

**Model B** — defensywny, 2 poziomy wyjścia:
- 50% @ TP1a (1,1R) → BE
- 50% @ TP1b (2,0R)
- Max zysk: 1,55R

**Auto** — `recommend_model()` wybiera automatycznie: warunki defensywne (chop, ChoCH ≥ 4, entry_ext > 0,25, MPS < 75) → Model B; premium/HQ → Model A.

**FIXED_R** — jedna pozycja, jeden cel, bez częściowych zamknięć (dostępne: 1,5R / 2,0R / 3,0R).

**Konflikt intrabar** (SL i TP w tej samej świecy):
- `conservative` (domyślny) → SL wygrywa
- `optimistic` → TP wygrywa

---

### 10. Reżim rynkowy

BTC (1H, 15M) + ETH (1H) tworzą reżim globalny: `bullish` / `bearish` / `mixed` / `chop`. Sygnały zgodne z reżimem dostają bonus TS; przeciwne — karę.

---

## Konfiguracje backtestu

| Config | Top-N | Filtr kategorii |
|---|---|---|
| TOP1 | 1 | wszystkie aktywne |
| TOP3 | 3 | wszystkie aktywne (do 3 równocześnie) |
| PREMIUM | wszystkie | tylko premium_setup |
| HQ | wszystkie | tylko high_quality |
| PREMIUM_HQ | wszystkie | premium_setup lub high_quality |
| CORE_V93 | wszystkie | FVG/OB + entry_ext ≤ 0,25 + ChoCH ≤ 2 + engulfing + MACD ≠ against + TS ≥ 73 + MPS ≥ 70 |

---

## Flaga fuel-filter

```bash
python strategy_wyckoff_9_4/backtest.py --fuel-filter off   # czysty baseline v9.1
python strategy_wyckoff_9_4/backtest.py --fuel-filter on    # v9.3 z TFS (domyślny)
```

`off` — TFS jest obliczany, ale nie wpływa na classify() ani MPS → czyste porównanie.



NAJLEPSZ KONFIGURACJA

Scanner:
fuel-filter ON

Entry:
immediate / manual confirmation
nie limit jako główny model

Selection:
TOP1/TOP3

Management:
Model A jako default

Avoid:
CORE_V93
limit jako default
NO FUEL
TFS < 55
TFS < 40 hard reject