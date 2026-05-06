# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project structure

### Active development — `strategy_wyckoff_9_5/` (current)

| File | Role |
|---|---|
| `strategy_wyckoff_9_5/config.py` | Central config: version, thresholds, output flags — **edit here, not scanner.py** |
| `strategy_wyckoff_9_5/strategy.py` | Shared logic: data fetching, Wyckoff, FVG, OB, ChoCH, scoring, classify, TFS, Wyckoff Cause, multi-family (v95) |
| `strategy_wyckoff_9_5/execution.py` | Trade simulation engine: Model A, Model B, FIXED_R, MFE/MAE, conservative/optimistic intrabar |
| `strategy_wyckoff_9_5/scanner.py` | Live scan + HTML report (v9.5, multi-family) |
| `strategy_wyckoff_9_5/backtest.py` | Historical simulation: multi-config × multi-model × multi-mode × multi-family |

Both `scanner.py` and `backtest.py` do `sys.path.insert(0, dirname)` so `import strategy` resolves to the sibling `strategy.py`. Run from project root:

```bash
python strategy_wyckoff_9_5/scanner.py
python strategy_wyckoff_9_5/backtest.py [--fuel-filter on|off]
```

Output dirs (all relative to the script's own directory):
- Scanner reports → `strategy_wyckoff_9_5/output/`
- Backtest results → `strategy_wyckoff_9_5/results/`
- Cache → `strategy_wyckoff_9_5/cache/`

### Stable reference — `strategy_wyckoff_9_4/`

Frozen v9.4 baseline. Do not modify — used as comparison point for v9.5 work.

Same file structure as v9.5. Scanner output goes to `strategy_wyckoff_9_4/output/`. Exception: v9.4 scanner has a "Top Watchlist Manual Review Candidates" section added by explicit request — this is the only intentional post-freeze modification.

### Shared Wyckoff core — `wyckoff_core.py`

Shared module imported by **both** `analyze_instrument.py` and `wyckoff_phase_scanner.py`. Single source of truth for Wyckoff market-structure analysis. Do **not** duplicate or modify the analysis logic in the consumer scripts — edit `wyckoff_core.py` only.

```python
from wyckoff_core import analyze_wyckoff
result = analyze_wyckoff(candles, verbose=False)
# returns: structure, phase, events, confidence, range_high, range_low,
#          full_trend, recent_trend, ranging, _points
```

- `verbose=False` → short scanner labels (`"SOS"`, `"Spring"`, …)
- `verbose=True` → descriptive labels (`"SOS — break above range high"`, …) — used by `analyze_instrument.py`
- `_points` → raw detection dict with `acc`, `dist`, `context`, `rsi`, `vol_sma`, `range_high`, `range_low` — used by report renderers to show per-point RSI/Vol/time badges

**Detection algorithm (v2 — pivot + RSI + volume):**

Sequential detection: `SC → AR → ST → Spring → SOS → LPS` (accumulation) / `BC → AR → ST → UTAD → SOW → LPSY` (distribution).

- **SC**: pivot low + RSI < 30 + volume ≥ 1.5× SMA(20) + bearish candle
- **AR**: first pivot high after SC with RSI exiting bear zone
- **ST**: first pivot low after AR with price ≥ SC low × 0.995 (no new climax)
- **Spring**: pivot low after ST with low < range_low but close > range_low (false break)
- **SOS**: pivot high after Spring/ST with close > AR high × 1.001 + RSI > 50
- **LPS**: pivot low after SOS with low ≥ AR high × 0.985 + RSI ≥ 40
- **BC/UTAD/SOW/LPSY**: mirror logic for distribution

**Context selection (Błąd 1 fix):** when both `acc` (SC found) and `dist` (BC found) are detected simultaneously, the winner is chosen by **sequence score** (how many points confirmed), not by SC.idx vs BC.idx. Tiebreak: whichever direction's last detected point is later.

**`struct_trend` (Błąd 2 fix):** trend used for Accumulation vs Reaccumulation (and Distribution vs Redistribution) classification is computed on candles **before SC/BC**, not on the full 60-bar window. This prevents a post-SOS rally from causing a true Accumulation to be mislabelled as Reaccumulation.

**Tuning constants (all at module top — edit here, never inline):**

| Constant | Value | Meaning |
|---|---|---|
| `PIVOT_LEN` | 5 | bars on each side for pivot high/low detection |
| `RSI_LEN` | 14 | RSI period |
| `RSI_SENS` | 20 | neutral band: 50±20 (bull > 70, bear < 30) |
| `SC_VOL_MULT` | 1.5 | SC/BC: volume ≥ 1.5× SMA(20) |
| `ST_VOL_RATIO` | 0.80 | ST: volume ≤ 0.80× SMA(20) preferred |
| `RANGE_SPAN_MAX` | 0.25 | `(high−low)/avg` < this for a range window |
| `RANGE_SLOPE_MAX` | 0.010 | `\|slope\| / avg_price` per bar |
| `_MIN_CANDLES` | 30 | minimum candles; fewer → returns `Insufficient` |

**Confidence:** graduated caps via `_CONF_CAPS` — `n < 45` → LOW, `n < 70` → MEDIUM, `n ≥ 70` → unrestricted.

### Legacy / other files
- **`research/backtest.py`** — old backtest, now orphaned
- **`fetch_and_zip_crypto.py`** + **`prompt_crypto_intradays_9_1.txt`** — Approach B: data fetch + LLM prompt workflow
- **`analyze_instrument.py`** — single-instrument market structure report (no trade signals); imports `wyckoff_core.analyze_wyckoff` with `verbose=True`; Wyckoff section includes summary table (Structure / Phase / Range / Events / RSI(14) / Vol/SMA / Conf) and a separate Key Points table (per-TF × per-point with price, `dd/mm HH:MM` for SC/BC/AR, RSI, Vol/SMA); supports three data sources: Bybit (krypto), Polygon (akcje), **yfinance** (złoto, forex, indeksy, akcje US — wybór `y` w menu)
- **`wyckoff_phase_scanner.py`** — multi-timeframe Wyckoff phase scanner; imports `wyckoff_core.analyze_wyckoff` with `verbose=False`; timeframes: `5m / 15m / 30m / 1H / 4H / 1D / 1W`; multi-TF table includes RSI(14) and Vol/SMA columns; detail cards show RSI/Vol badges in header + Wyckoff points panel (`_wyckoff_points_panel`) with per-point price/RSI/Vol and `dd/mm HH:MM` candle time for SC, BC, AR; supports up to 6 conditions (each with its own TF + phases); **same-direction enforcement**: instruments where any condition's TF has a different Wyckoff direction than another are rejected — `Accumulation == Reaccumulation` (both bullish), `Distribution == Redistribution` (both bearish); e.g. `4H Ph B,C` + `1H Ph B,C` returns only instruments where both TFs agree on direction
- **`clean_output.py`** — removes files from `output/`, `strategy_wyckoff_9_5/output/`, `strategy_wyckoff_9_4/output/`, `results/`, `cache/` (`--dry-run`, `--cache` flags)

## Environment setup

```bash
source venv/bin/activate
pip install requests yfinance   # yfinance required for analyze_instrument.py (złoto/forex/indeksy)
```

## Running the scanner

```bash
source venv/bin/activate
python strategy_wyckoff_9_5/scanner.py
```

Interactive: `t` = live data, `h` = historical (`dd/mm/yyyy hh:mm`).

**Scanner outputs (live mode only):**
- `output/signals_YYYY_MM_DD_HHMM.html` — timestamped report (if `WRITE_TIMESTAMPED_HTML`)
- `output/latest.html` — always-overwritten latest report (if `WRITE_LATEST_HTML`)
- `output/alerts.json` — top picks + WL review in machine-readable form (if `WRITE_ALERTS_JSON`)
- `output/alert_state.json` — cooldown state, persisted between runs
- `output/scan_log.csv` — append-mode per-setup log (if `WRITE_SCAN_LOG_CSV`)

All output flags and thresholds are in `config.py`. `USE_TARGET_FEASIBILITY_FILTER` is read from `cfg` in `main()` — do **not** hardcode it.

Default trade management: **family-aware** — Model A for WYCKOFF_STRICT / strongest setups, Model B defensive.

## `strategy_wyckoff_9_5/config.py`

Single source of truth for all scanner settings. Key fields:

```python
SCANNER_VERSION = "v9.5"        # used in HTML title, print banner, alerts.json engine field
ENGINE          = "v95"         # controls which analysis path runs in backtest
USE_TARGET_FEASIBILITY_FILTER = True

ACTIVE_SETUP_FAMILIES = {"WYCKOFF_STRICT", "TREND_PULLBACK"}

SCAN_TOP_N               = 400
LIVE_SCAN_INTERVAL_MINUTES = 60
MAX_ACTIVE_SETUPS_HTML   = 15
MAX_TOP_PICKS            = 3
MAX_WATCHLIST_REVIEW     = 4

ALERT_COOLDOWN_HOURS          = 6
SYMBOL_DIRECTION_COOLDOWN_HOURS = 6
SAME_FAMILY_COOLDOWN_HOURS    = 6

ENABLE_WATCHLIST_REVIEW  = True
WATCHLIST_REVIEW_MIN_MPS = 55
WATCHLIST_REVIEW_MIN_TFS = 40

DEFAULT_TREND_PULLBACK_MODEL    = "Model B"
AGGRESSIVE_TREND_PULLBACK_MODEL = "FIXED_2R"
DEFAULT_WYCKOFF_MODEL           = "Model A"

WRITE_LATEST_HTML      = True
WRITE_TIMESTAMPED_HTML = True
WRITE_ALERTS_JSON      = True
WRITE_SCAN_LOG_CSV     = True
```

To change the version, bump `SCANNER_VERSION` here only — all HTML and print statements read from `meta["scanner_version"]`.

## API credentials

Stored in `.env` at project root (gitignored):
```
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
```
Loaded via `_load_env()` in `strategy.py` — no `python-dotenv` needed. Optional; scripts work without credentials but may hit rate limits.

## Architecture of `strategy_wyckoff_9_5/strategy.py`

### Module-level globals

```python
BACKTEST_TIME_MS = None               # set by scanner/backtest before fetching
USE_TARGET_FEASIBILITY_FILTER = True  # controls TFS gates in classify() and MPS
```

Scanner sets this from `cfg.USE_TARGET_FEASIBILITY_FILTER`. Backtest sets via `--fuel-filter on|off`.

### Setup families (v9.5 multi-family)

Two active families:

| Family | Detection function | Key fields |
|---|---|---|
| `WYCKOFF_STRICT` | `analyze_symbol()` | Wyckoff accumulation/distribution, Phase D, SOS/SOW |
| `TREND_PULLBACK` | `detect_trend_pullback()` | HTF trend (1H + 4H) + LTF FVG/OB pullback |

`detect_trend_pullback()` key fields added to setup:
- `zone_type`: `"DEMAND"` (LONG) / `"SUPPLY"` (SHORT)
- `setup_variant`: `"HTF_{direction}_{zone_type}_PULLBACK_{loc_label}"` (e.g. `HTF_LONG_DEMAND_PULLBACK_FVG`)
- `trend_conflict`: `True` if 1H and 4H disagree — reported in family panel, not a hard reject
- `htf_context`, `family_reason`, `family_risk`, `countertrend`, `recommended_tp_mode`

### `analyze_symbol_v95()` — classify-before-select design

Runs both WYCKOFF_STRICT and TREND_PULLBACK candidates, scores and classifies **all of them first**, then selects the best using:

```python
rank = (is_active, mps, tfs, family_priority, is_clean_path, ts, -entry_ext)
```

This ensures an active TREND_PULLBACK beats a watchlist WYCKOFF_STRICT, regardless of family priority order.

### Data pipeline

1. **Fetching** — `get_top_crypto()` → `fetch_symbol_data()` via `ThreadPoolExecutor` (25 workers, semaphore-capped). `get_400_candles()` fetches two batches of 200 candles; second batch uses `min(timestamps)` of first batch as the `end_time` anchor.
2. **Technical analysis** per symbol (5M candles unless noted):
   - `detect_wyckoff()` — 1H candles; 80th/20th percentile of last 45 bars defines range; SOS/SOW detected in **last 15 bars**
   - `detect_fvg()` — lookback 120 candles
   - `detect_ob()` — last opposite candle before displacement ≥ 0.1%
   - `detect_choch()` — local window 5 candles, searches 30 back; rejects age > 6
   - `detect_engulfing()` / `detect_pin_bar()`
   - `macd_divergence()` — two halves of last 30 candles vs MACD histogram
3. **Wyckoff Cause** — `compute_wyckoff_cause(setup, data_by_tf)` — called first in `analyze_symbol()`, before TFS
4. **Target Feasibility** — `compute_target_feasibility(setup, candles_5m)` — reads `wyckoff_cause_score` already in setup
5. **Scoring** — `total_score()` (0–100) + `manual_pick_score()` (0–100; TFS component gated by `USE_TARGET_FEASIBILITY_FILTER`)
6. **Classification** — `classify_v95()` → `premium_setup / high_quality / secondary_quality / watchlist / rejected`

`analyze_symbol()` call order:
```python
setup = { ...all base fields... }
setup.update(compute_wyckoff_cause(setup, data["timeframes"]))
setup.update(compute_target_feasibility(setup, candles_5m))
return setup
```

### ATR helpers

- `_compute_atr(candles, period=14)` — private, used internally
- `calc_atr(candles, period=14)` — public wrapper, used by `compute_wyckoff_cause`

## Filter thresholds — v9.5

`classify_v95()` hard rejects (always active, not gated by flag):

| Condition | Rule |
|---|---|
| MACD divergence | `against` |
| Entry extension | > 0.50 |
| R:R | < 2.0 |
| FVG filled | yes |
| ChoCH age | > 4c |
| Verdict | `TARGET BLOCKED` or `TARGET_BLOCKED` (both forms accepted) |

TFS-gated rejects (only when `USE_TARGET_FEASIBILITY_FILTER = True`):

| Condition | Rule |
|---|---|
| TFS | < 40 → rejected |
| ts < 73 OR tfs < 55 | → watchlist |

When flag is `False`: only `ts < 73` gates watchlist. TFS is computed but has no effect on classify() or MPS ranking.

`manual_pick_score()` TFS component (only when flag is `True`):
- `tfs ≥ 70` → +20 pts
- `tfs ≥ 55` → +12 pts
- `tfs ≥ 40` → +5 pts
- `tfs < 55` → cap MPS at 65

Classification thresholds:
- **`premium_setup`**: MACD yes + entry_ext ≤ 0.25 + at_fvg/ob + ChoCH ≤ 3c + engulfing + rr ≥ 2.0 + mps ≥ 75
- **`high_quality`**: entry_ext ≤ 0.25 + engulfing + MACD yes/none + at_fvg/ob + ChoCH ≤ 3c + rr ≥ 2.0
- **`secondary_quality`**: passes hard filters + ts ≥ 73, engulfing, misses premium/HQ thresholds
- **Pin bar**: watchlist unless ts ≥ 85 + ChoCH ≤ 1c + MACD yes + at_fvg_and_order_block + entry_ext ≤ 0.25
- **`confluence_zone` only**: watchlist unless MACD yes + ChoCH ≤ 2c + entry_ext ≤ 0.25
- **entry_ext 0.25–0.50**: needs ≥ 3 premium confirmations or → watchlist

`recommend_model_v95()` — family-aware:
- TREND_PULLBACK → Model B by default; Model A if TFS ≥ 65 + verdict CLEAN/POSSIBLE
- Hard → Model B: watchlist/rejected, TARGET BLOCKED/NO FUEL, ChoCH ≥ 4, entry_ext > 0.25, confluence_zone
- → Model A: active + TFS ≥ 55 + valid status; or premium_setup; or MACD yes + entry_ext ≤ 0.25; or MPS ≥ 75 + TFS ≥ 55

## Wyckoff Cause / Base Strength (`compute_wyckoff_cause`)

Scores the quality of the Wyckoff base. Called before TFS so the score feeds into TFS as a 6th component.

**Inputs:** `setup["wyckoff"]["range_high/low"]`, `setup["direction"]`, `data_by_tf["1H"]`

**Breakout search:** last 15 bars of 1H only (matches `detect_wyckoff()` tail) — prevents stale historical breakouts from corrupting phase_d_age and displacement scores.

**Score components (total 0–15):**

| Component | Max | Description |
|---|---|---|
| range duration | 4 | count of 1H closes inside range before fresh SOS/SOW |
| range compression | 3 | range_height / ATR_1H (sweet spot 1–3 ATR) |
| SOS/SOW displacement | 4 | breakout candle displacement in ATR units; +1 bonus for volume ≥ 1.2× avg |
| Phase D age | 2 | bars since breakout: ≤6 → 2, ≤24 → 1, else 0 |
| pullback hold | 2 | pullback depth ≤ 50% of range + no 1H close through mid-range |

**Labels:** `WEAK_BASE` (0–4) / `NORMAL_BASE` (5–8) / `STRONG_BASE` (9–12) / `VERY_STRONG_BASE` (13–15) / `CHOP_BASE` (duration > 168h or range_atr > 5, with weak displacement — capped at 5)

**Fields added to setup:** `wyckoff_cause_score`, `base_strength_label`, `range_duration_bars_1h`, `range_duration_hours`, `range_width_percent`, `range_atr_ratio`, `range_compression_score`, `sos_sow_displacement_atr`, `sos_sow_volume_ratio`, `phase_d_age_bars`, `pullback_depth_percent_of_range`, `pullback_hold_score`, `wyckoff_cause_note`

## Target Feasibility Score (`compute_target_feasibility`)

Scores how feasible it is that price reaches TP2. Max 100 across 6 components:

| Component | Max | Description |
|---|---|---|
| clean_path | 20 | swing obstacles between entry and TP2 |
| liquidity_magnet | 18 | TP2 near a swing high/low or range boundary |
| poc | 17 | POC position relative to SL / entry / TP zones |
| momentum | 18 | trigger candle body, close position, volume |
| atr_capacity | 12 | TP2 distance in 5M ATR units |
| wyckoff_cause | 15 | from `compute_wyckoff_cause()` already in setup |

**Verdicts:** `CLEAN PATH` (≥85) / `TARGET POSSIBLE` (≥70) / `TARGET DIFFICULT` (≥55) / `TARGET BLOCKED` (≥40) / `NO FUEL` (<40)

**Key field names returned:** `target_feasibility_score`, `verdict`, `clean_path_score`, `liquidity_magnet_score`, `poc_score`, `momentum_score`, `atr_capacity_score`, `nearest_obstacle`, `nearest_magnet`, `tp2_atr_ratio`, `poc`, `poc_position`

## Scanner HTML report — v9.5

Active setups sorted by `_live_rank_key` → `st.rank_key_v95(s)`: MPS → TFS → verdict → base strength → model → ts → −entry_ext.

TOP PICKS filter: MPS ≥ 70 + TFS ≥ 55 + verdict ∉ {TARGET BLOCKED, NO FUEL}.

Detail card panel order: Wyckoff → Wyckoff Cause → ChoCH/Pattern + Trade Plan → Target Feasibility + MPS + MACD/Regime → Model A plan (if model=A) → Trigger Checklist → Invalidation.

Table columns: # · Symbol · Dir · Wyckoff · Category · MPS · Score · **TFS · Verdict · WCS · Base · Obstacle · Magnet** · Status · Pattern · ChoCH · EntExt · FVG · FVG+OB · Entry · SL · TP1a · TP1b · TP2 · R:R · MACD · Model · **Family**

Family summary breakdown section shows: Family · Active · Premium · HQ · Avg TFS · Avg MPS · Top Symbols.

Watchlist Fuel Candidates section (if `ENABLE_WATCHLIST_REVIEW`): strongest watchlist setups for manual chart verification, filtered by `_is_fuel_candidate()`, sorted by `_watchlist_fuel_rank()`.

### `generate_html()` signature

```python
def generate_html(all_setups, meta, now_dt=None, alert_state=None):
    ...
    return _html, top_picks, fuel_candidates  # tuple — unpack in caller
```

`meta` dict expected keys: `report_time`, `data_time`, `is_backtest`, `total_symbols`, `regime`, `btc_regime`, `eth_regime`, `scanner_version`, `fuel_filter`.

### `_entry_price(s)` helper

```python
def _entry_price(s):
    return s.get("entry_mid") or s.get("entry")
```

Used in `alerts.json` and `scan_log.csv` writes. Do not use `s.get("entry")` directly — setup fields use `entry_mid` as the primary price key.

### Alert cooldown system

- State stored in `output/alert_state.json`, keyed by `_alert_key(s)` = `"{symbol}_{direction}_{family}"`
- `_cooldown_status(s, state, now_dt, hours)` → `"NEW"` / `"COOLDOWN"` / `"SEEN"`
- Cooldown badge shown on each TOP PICK in HTML
- After each scan, `alert_state` is updated with `last_seen / last_mps / last_tfs / last_category / last_verdict` for each top pick
- Skipped entirely in backtest mode

### `scan_log.csv` columns

`timestamp · symbol · direction · setup_family · setup_variant · category · mps · ts · tfs · verdict · model · entry · sl · tp1a · tp1b · tp2 · rr · status · macd_5m · choch_age · entry_ext · trend_conflict · base_strength_label · watchlist_review_candidate · alert_status`

Appended each scan for all active setups + fuel candidates. Active setups = `premium_setup | high_quality | secondary_quality`.

### `alerts.json` structure

```json
{
  "generated_at": "2026-05-01 12:00:00",
  "engine": "v9.5",
  "top_picks": [{"symbol":..., "direction":..., "setup_family":..., "entry":..., ...}],
  "watchlist_review": [...]
}
```

`entry` uses `_entry_price(s)` — never `s.get("entry")` directly.

## Key implementation details

- Exponential backoff on Bybit rate-limit codes `10002 / 10006 / 10018`.
- `BACKTEST_TIME_MS` passed as `end_time` to all candle requests in backtest mode; current price from last 1m close.
- Symbols with `turnover24h < 800_000` USD are hard-rejected.
- `build_symbol_data()` uses `.get(sym, {})` defensively — symbols missing from the cache return `price=0` and are skipped.
- After saving HTML, auto-opens in Chrome, falling back to system default.
- Scanner uses `_THIS_DIR` to anchor `OUTPUT_DIR` → reports always land in the script's own `output/` subdirectory regardless of working directory.

## Execution engine (`strategy_wyckoff_9_5/execution.py`)

**Models:**
- `simulate_model_a(setup, candles_5m, conservative=True)` — 35% @ 1.1R → BE, 35% @ 2.0R, 30% @ 3.0R. Max = 1.985R.
- `simulate_model_b(setup, candles_5m, conservative=True)` — 50% @ 1.1R → BE, 50% @ 2.0R. Max = 1.55R.
- `simulate_auto_model(setup, candles_5m, conservative=True)` — reads `setup["model"]` from `recommend_model_v95()`.
- `simulate_fixed_r(setup, candles_5m, tp_mult, conservative=True)` — single position, SL = −1R, TP = +mult×R, no partials.
- `simulate_fixed_2r` / `simulate_fixed_15r` / `simulate_fixed_3r` — convenience wrappers for 2.0R, 1.5R, 3.0R.

**Intrabar conflict** (both SL and TP touched in same 5M bar):
- `conservative=True` (default) → SL wins
- `conservative=False` → TP wins

**Returns ExecResult dict:** `exit_reason` (`tp_full` / `sl` / `be_exit` / `open` / `close_at_end`) · `pnl_r` · `mfe_r` · `mae_r` · `bars_held` · `conservative_intrabar` · `model`

## Backtesting (`strategy_wyckoff_9_5/backtest.py`)

```bash
source venv/bin/activate
python strategy_wyckoff_9_5/backtest.py [--fuel-filter on|off]
```

`--fuel-filter off` sets `st.USE_TARGET_FEASIBILITY_FILTER = False` before any simulation → TFS disabled. Default: `on` (TFS active).

**ENGINE constant:** `ENGINE = "v95"` — controls which analysis/classify/recommend functions run. When `"v95"`: uses `analyze_symbol_v95()`, `classify_v95()`, `recommend_model_v95()`.

**TF fetched:** `BT_TIMEFRAMES = ["4H", "1H", "30m", "15m", "5m"]` — 4H required for TREND_PULLBACK HTF detection.

**Cache filename:** `bt_{start}_{end}_top{N}_4H1H30m15m5m.json` — TF set encoded in name to prevent old caches without 4H from being reused.

**Configurations (CONFIGS dict):**

| Name | Top-N | Category filter | basket_size | core_v93 |
|---|---|---|---|---|
| `TOP1` | 1 | all active | 1 | No |
| `TOP3` | 3 | all active | 3 | No |
| `PREMIUM` | 99 | premium_setup | 1 | No |
| `HQ` | 99 | high_quality | 1 | No |
| `PREMIUM_HQ` | 99 | premium_setup OR high_quality | 1 | No |
| `CORE_V93` | 99 | all active + core filter | 1 | Yes |

`CORE_V93` additional filter (`_passes_core_v93`): status at_fvg/at_fvg_and_ob + entry_ext ≤ 0.25 + ChoCH ≤ 2 + Engulfing + MACD ≠ against + ts ≥ 73 + mps ≥ 70 + FVG not filled + rr ≥ 2.

**Models:** `A` / `B` / `Auto` / `FIXED_2R` / `FIXED_1_5R` / `FIXED_3R`

**Interactive prompts:** start/end date · scan interval · top-N symbols · configs · models · intrabar mode (`c`/`o`/`co`) · entry mode (`l`=limit / `i`=immediate / `li`=both) · **cooldown hours** · **max one per day** · **enabled families** · **exclude OB-only**

**`run_config()` dedup parameters:**

| Parameter | Default | Effect |
|---|---|---|
| `cooldown_hours=0` | off | Skip symbol if last entry was within N hours |
| `max_one_per_day=False` | off | Only one trade per symbol per calendar day |
| `enabled_families=None` | all | Filter to specific setup families (v95 only) |
| `exclude_ob_only=False` | off | Skip setups where `setup_variant` ends with `_OB` |

`last_entry_by_sym` dict tracks when each symbol last had a trade placed. Updated at the moment of placing (not filling). Dedup label encoded in output filename, e.g. `_cd6h_1perday_famTR_noOB`.

**Four dimensions per run:** config × model × intrabar × entry_mode → `config_results` keyed by `(cfg_name, model_name, cons_label, entry_mode)` 4-tuple.

**Entry modes:**
- `limit` — pending order, filled if price touches `entry_mid` before TTL (= next scan step). Unfilled orders cancelled.
- `immediate` — instant fill at `entry_mid` at signal time (no TTL).

**close_at_end:** open trades at end_dt are closed at last available 5M candle price; `exit_reason = "close_at_end"`.

**Statistics (`compute_stats`):** `positive_wins` / `full_wins` / `partial_wins` / `losses` / `no_exit` · `win_rate` (pnl_r > 0 / closed) · `total_r` · `ev_per_trade` · `expectancy` · `payoff_ratio` · `breakeven_wr` · `profit_factor` · `avg_win_r` / `avg_loss_r` · `avg_mfe_r` / `avg_mae_r` · `max_dd_r` · `z_score` · `fill_rate` (limit mode). For TOP3: `max_concurrent` / `avg_concurrent` / `concurrent_dist`.

**Breakdowns per config:** Category · Status · MACD · Direction · ChoCH Age · Entry Extension · Entry Mode · TFS bucket · TFS Verdict · **Wyckoff Cause Score** · **Base Strength** · **Range Duration** · **Phase D Age** · **Setup Family** · **Setup Variant** · **Trend Conflict**

**Edge diagnostics:** `_edge_groups()` scans all breakdown rows across all configs — reports EDGE CANDIDATES (N≥20, EV>0, PF>1.2) and EDGE KILLERS (N≥20, EV<0, PF<1.0).

**Output:**
- HTML: `strategy_wyckoff_9_5/results/backtest_{start}_{end}_{fuel_ON|fuel_OFF}_{ENGINE}[{dedup_label}].html`
- CSV: same basename `_trades.csv` — one row per trade, all configs stacked. Columns include all trade fields plus family fields (`setup_family`, `setup_variant`, `countertrend`, `trend_conflict`, `htf_context`, `recommended_tp_mode`, `family_reason`, `family_risk`).

**Debug mode:** first `run_config()` call uses `debug_wcs=True`, printing up to 5 setups with Wyckoff Cause diagnostics to stdout for sanity-checking.

Raw data dicts include a `_turnover` float key — always filter with `isinstance(c, list)` when iterating `.items()` before passing to `slice_at()`. BTC and ETH always included for `st.market_regime()`.

## Single-instrument analysis (`analyze_instrument.py`)

Fetches 400 candles across 5M / 15M / 30M / 1H / 4H / 1D / **1W** and produces a market-structure report (no trade signals).

```bash
source venv/bin/activate
python analyze_instrument.py
```

Interactive: rynek (`c` / `s` / `y`) · symbol · `t` (live) or `h` (`dd/mm/yyyy hh:mm`).
Output: `output/analysis_{SYMBOL}_{TIMESTAMP}.html`, auto-opens Chrome.

**Data sources (menu `c/s/y`):**

| Opcja | Źródło | Przykładowe symbole |
|---|---|---|
| `c` | Bybit (krypto) | `BTC`, `ETHUSDT` |
| `s` | Polygon (akcje US) | `AAPL`, `NVDA` |
| `y` | yfinance (złoto, forex, indeksy, akcje) | `GC=F`, `EURUSD=X`, `GBPUSD=X`, `USDJPY=X`, `^GSPC`, `^IXIC` |

**`get_candles_yfinance(symbol, interval, end_time_ms=None)`:**
- 4H nie istnieje w yfinance — pobierane jako 1H i resample `pandas.resample("4h")`
- Limity lookback: 5M/15M/30M → 59 dni; 1H/4H → 720 dni; 1D → 1800 dni; 1W → 3650 dni
- Dla forex/złota freshness check może pokazać `STALE` w weekendy (rynki zamknięte) — to normalne
- yfinance importowany wewnątrz funkcji (soft dependency); bez niego reszta skryptu działa

Wyckoff analysis delegates to `wyckoff_core.analyze_wyckoff(candles, verbose=True)` — descriptive event labels. Do not modify the Wyckoff logic in this file; edit `wyckoff_core.py`.

**Wyckoff HTML sections in `generate_report()`:**

1. **Wyckoff Analysis** — summary table per TF: Structure · Phase · SC/AR or BC/AR range · Key Events · RSI(14) · Vol/SMA(20) · Confidence

2. **Wyckoff Key Points** — per-TF × per-point table (columns: SC/BC · AR · ST · Spring/UTAD · SOS/SOW · LPS/LPSY). Each detected cell shows: price · `dd/mm HH:MM` UTC (SC/BC/AR only) · RSI · Vol/SMA · ↓v/↑v low/high-volume markers. Built from `w["_points"]["acc"|"dist"]`.

**`analyze_liquidity()` — enhanced level detection:**
- Lookback 150 candles; three swing windows lb=3/5/8 (minor/intermediate/major)
- All significant swings returned; labels: `EQH/EQL`, `Major SH/SL`, `Swing H/L`, `Minor SH/SL`, `Period High/Low`
- Score: `count × lb_rank` (lb=8 → 3×, lb=5 → 2×, lb=3 → 1×)
- Consolidated multi-TF map (tolerance 0.4%): up to 10 levels above / 10 below current price; above-price sorted descending

## Approach B — LLM-based scanner

```bash
source venv/bin/activate
python fetch_and_zip_crypto.py
```

Interactive: mode (current/backtest), symbol source (1=Top400 / 2=`crypto_ftmo.txt` / 3=`crypto_breakout.txt`). Output to `output/`. Paste JSON into `prompt_crypto_intradays_9_1.txt`, replacing `{{MARKET_JSON}}`, then send to LLM.

## Optional symbol list files

- `crypto_ftmo.txt` — option 2 in `fetch_and_zip_crypto.py`
- `crypto_breakout.txt` — option 3

---

## Active development — `strategy_find_3rd_wave/` (Wave 3 scanner)

Separate strategy project: detects Elliott Wave 3 entry opportunities on crypto perpetuals.

### File structure

| File | Role |
|---|---|
| `strategy.py` | Pure analysis — no I/O. Wave detection, BOS, POC, harmonic, scoring. |
| `data.py` | Bybit OHLCV fetch + local CSV cache. Returns raw int-ms timestamps. |
| `scanner.py` | Live / historical scan → dark-theme HTML report, Chrome auto-open. |
| `backtest.py` | Candle-by-candle replay → CSV trades + HTML report with gross/net stats. |
| `config.py` | I/O paths and API settings (not strategy params). |
| `utils.py` | `normalize_timeframe()` — shared between scanner and backtest. |
| `tests/test_strategy.py` | 23 pytest unit tests for strategy.py. |

Run from project root:

```bash
source venv/bin/activate
python strategy_find_3rd_wave/scanner.py
python strategy_find_3rd_wave/backtest.py
```

Output dirs (relative to the script's own directory):
- Scanner reports → `strategy_find_3rd_wave/results/scanner/`
- Backtest results → `strategy_find_3rd_wave/results/backtest/`
- OHLCV cache → `strategy_find_3rd_wave/data/cache/`

### Strategy logic (`strategy.py`)

**Wave structure: X → A → (B → C → D)**

| Point | Role |
|---|---|
| X | Wave 1 origin (swing low for LONG, swing high for SHORT) |
| A | Wave 1 peak (swing high for LONG, swing low for SHORT) |
| B/C/D | Wave 2 correction (D is the entry zone) |

**`StrategyConfig` — key parameters:**

```python
@dataclass
class StrategyConfig:
    swing_left: int = 3           # pivot lookback
    swing_right: int = 3          # pivot confirmation bars (no lookahead)
    min_wave1_atr: float = 2.0    # minimum wave 1 size in ATR units
    retracement_min: float = 0.382
    retracement_max: float = 0.886
    preferred_retracement_min: float = 0.5   # golden pocket
    preferred_retracement_max: float = 0.786
    poc_distance_atr: float = 0.5  # D must be within N ATR of wave 1 POC
    harmonic_tolerance: float = 0.08  # AB=CD equality tolerance
    require_bos: bool = True       # BOS (break of structure at C) required
    min_rr_to_wave1_high: float = 1.5
    max_bars_after_bos: int = 1    # freshness gate: scanner=3, backtest=0
    max_wave1_bars: int = 100
    max_correction_bars: int = 150
    max_bars_after_d: int = 50
    require_harmonic: bool = False
```

**`StrategyResult` — key fields:**

```python
signal: SignalType  # LONG_SETUP / SHORT_SETUP / WAITING_FOR_BOS / NO_SIGNAL / INVALID
entry_price, stop_loss, target_1, target_2, target_3
risk_reward_to_t1/t2/t3
bos_index, bos_price          # BOS candle bar index and close price
signal_index, signal_time     # BOS candle (require_bos=True) or D candle (False)
setup_id                      # stable dedup key: direction + wave timestamps
wave: WaveStructure           # full wave geometry
```

**Signal semantics:**
- `signal_index` = index of the BOS candle when `require_bos=True`, or D candle when `False`
- Signal rank: `INVALID(0) < NO_SIGNAL(1) < WAITING_FOR_BOS(2) < LONG_SETUP/SHORT_SETUP(3)`
- LONG and SHORT are evaluated independently; higher-ranked result wins (tied: higher RR to T2)

**Core functions:**

| Function | Description |
|---|---|
| `normalize_ohlcv_dataframe(df)` | Ensures timestamp column, RangeIndex, ascending sort; converts Bybit ms timestamps |
| `analyze_strategy(df, config)` | Main entry point — normalises, detects swings, evaluates LONG+SHORT, returns best |
| `find_wave_structures(...)` | Returns all valid wave structures for one direction, sorted by `_score_wave_structure()` |
| `_score_wave_structure(wave)` | Score: +5 ret valid, +10 preferred zone, +15 POC valid, +5 harmonic, +3 POC proximity |
| `_validate_and_signal(wave, ...)` | Applies all gates to one wave → StrategyResult |
| `_find_best_bcd(...)` | Scored search over up to 5×5×5 = 125 B/C/D combinations |
| `validate_bos(df, d_idx, c_price, dir, as_of)` | First close past C after D; returns (found, bar_idx, close_price) |
| `calculate_volume_poc(df, start, end)` | Volume profile POC via 50-bin histogram |
| `calculate_atr(df, period=14)` | EW-smoothed true range |
| `detect_swings(df, left, right)` | Pivot H/L with `confirmed_index = i + right` (no lookahead) |

**Targets (LONG):**
- T1 = A price (wave 1 high)
- T2 = A + 1.272 × wave1_size
- T3 = A + 1.618 × wave1_size

**SL:** D price − 0.1 × ATR (LONG) / D price + 0.1 × ATR (SHORT)

### Scanner (`scanner.py`)

```
Interactive: t = live data | h = historical (dd/mm/yyyy hh:mm)
```

- `SCANNER_CONFIG.max_bars_after_bos = 3` — shows setups up to 3 bars after BOS
- Historical mode: `analysis_price = df["close"].iloc[-1]` (not live ticker price)
- Historical mode: uses `fetch_candles_range()` — hits CSV cache before Bybit
- Freshness panel on each card: 🟢 0 bars / 🟡 1–2 bars / 🔴 3+ bars since BOS
- Historical mode warning banner in HTML
- `DEBUG_WAVE3=1` env var → print per-symbol errors
- Timeframe input normalised via `normalize_timeframe()` (e.g. `1h → 1H`, `60 → 1H`)

### Backtest (`backtest.py`)

```
Interactive prompts:
  Symbol         → auto-appends USDT
  Timeframe      → normalized via normalize_timeframe()
  Start / End    → dd/mm/yyyy or dd/mm/yyyy HH:MM
                   End date-only → 23:59:59 UTC (full day included)
  TP mode        → t2 (full at TP2) | t1t2 (50% at T1, 50% at T2)
  Overlap mode   → y = overlapping trades allowed | N = one at a time (default)
```

**`BACKTEST_CONFIG.max_bars_after_bos = 0`** — enter only on the exact BOS/D candle.

**`run_backtest()` parameters:**
- `allow_overlapping_trades=False` — when False, new signals skipped while `i <= last_exit_bar`
- `fee_rate=0.0006` — taker fee per side (0.06%)
- `slippage_rate=0.0002` — estimated slippage per side (0.02%)

**Trade simulation (`_simulate_trade`):**
- SL always wins over TP on the same candle (conservative intrabar)
- `cost_r = 2 * entry * (fee_rate + slippage_rate) / risk` (round-trip cost in R)
- `pnl_r` = gross; `pnl_r_net = pnl_r - cost_r`
- t1t2 mode: partial TP1 locks in 50% at T1; blending only if `partial_hit=True`

**Deduplication:** `setup_id` = `direction + x_time + a_time + b_time + c_time + d_time` — prevents re-entry on the same wave as the window advances.

**CSV output columns (key):**
`setup_id · signal · direction · signal_bar · signal_time · signal_index · strategy_signal_time · exit_bar · exit_time · entry_price · stop_loss · target_1/2/3 · exit_price · exit_reason · pnl_r · cost_r · pnl_r_net · bars_held · rr_t1/t2 · bos_index · bos_price · reason · x/a/d_price · retracement · harmonic_valid · poc_price`

**HTML report:** Gross vs Net summary table (Win Rate, Total R, EV, Profit Factor, Max DD) + execution assumptions table (entry model, SL/TP intrabar rule, position model, fee rate, slippage rate, round-trip cost).

**Stats (`compute_stats`):** returns both gross keys (`win_rate`, `total_r`, `ev_per_trade`, `profit_factor`, `max_dd_r`) and net keys (`net_total_r`, `net_ev_per_trade`, `net_profit_factor`, `net_win_rate`, `net_max_dd_r`).

### Data layer (`data.py`)

- `get_top_symbols(n, min_turnover_usd)` — top USDT perpetuals by 24h turnover from Bybit v5 tickers
- `fetch_candles(symbol, tf, end_time_ms, use_cache)` — 2 × 200 candle batches; saves to CSV cache
- `fetch_candles_range(symbol, tf, start_ms, end_ms, use_cache)` — multi-batch historical fetch; reads CSV cache first
- Returns DataFrame with `timestamp` as int64 ms — `normalize_ohlcv_dataframe()` converts to pd.Timestamp
- Rate-limit handling: retCode 10002/10006/10018 → exponential backoff (up to 5 retries)
- Cache path: `data/cache/{symbol}_{tf}_{YYYYMMDD}_{YYYYMMDD}.csv`

### Shared utilities (`utils.py`)

`normalize_timeframe(s)` — canonical form mapping:

| Input | Output |
|---|---|
| `"1h"`, `"4h"` | `"1H"`, `"4H"` |
| `"60"`, `"240"` | `"1H"`, `"4H"` |
| `"15"`, `"30"` | `"15m"`, `"30m"` |
| `"15m"`, `"1H"` | unchanged |

### Unit tests

```bash
python -m pytest strategy_find_3rd_wave/tests/ -v
```

23 tests across: `TestNormalizeOhlcv` (6) · `TestLongSetup` (5) · `TestShortSetup` (1) · `TestRejectionReasons` (3) · `TestBOS` (3) · `TestLongDoesNotBlockShort` (1) · `TestSignalMetadata` (2) · `TestSetupId` (2)

Key fixtures: `cfg_d_entry` uses `require_bos=False` for basic signal tests (entry at D avoids RR issues with synthetic data). BOS-specific tests are in `TestBOS` with `max_bars_after_bos=0`.

### Important invariants

- `signal_index` always refers to the trigger candle (BOS candle or D candle), never `as_of_index`
- Backtest enters trades only when `result.signal_index == i` (exact candle match)
- Scanner uses `analysis_price = df["close"].iloc[-1]` — never `sym_info["price"]` (live ticker)
- `find_wave_structures()` returns all candidates sorted by score; `_evaluate_direction()` picks the highest-ranked signal across all of them
- `swing.confirmed_index = i + swing_right` — pivots are never used before they are confirmed (no lookahead)
