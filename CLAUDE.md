# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project structure

### Active development — `strategy_wyckoff_9_5/` (current)

| File | Role |
|---|---|
| `strategy_wyckoff_9_5/strategy.py` | Shared logic: data fetching, Wyckoff, FVG, OB, ChoCH, scoring, classify, TFS, Wyckoff Cause |
| `strategy_wyckoff_9_5/execution.py` | Trade simulation engine: Model A, Model B, FIXED_R, MFE/MAE, conservative/optimistic intrabar |
| `strategy_wyckoff_9_5/scanner.py` | Live scan + HTML report (v9.5) |
| `strategy_wyckoff_9_5/backtest.py` | Historical simulation: multi-config × multi-model × multi-mode |

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

Same file structure as v9.5. Scanner output goes to `strategy_wyckoff_9_4/output/`.

### Legacy / other files
- **`research/backtest.py`** — old backtest, now orphaned
- **`fetch_and_zip_crypto.py`** + **`prompt_crypto_intradays_9_1.txt`** — Approach B: data fetch + LLM prompt workflow
- **`analyze_instrument.py`** — single-instrument market structure report (no trade signals)
- **`clean_output.py`** — removes files from `output/`, `strategy_wyckoff_9_5/output/`, `strategy_wyckoff_9_4/output/`, `results/`, `cache/` (`--dry-run`, `--cache` flags)

## Environment setup

```bash
source venv/bin/activate
pip install requests   # only dependency
```

## Running the scanner

```bash
source venv/bin/activate
python strategy_wyckoff_9_5/scanner.py
```

Interactive: `t` = live data, `h` = historical (`dd/mm/yyyy hh:mm`).
Output: `strategy_wyckoff_9_5/output/signals_YYYY_MM_DD_HHMM.html`, auto-opens Chrome.

Fuel Filter is forced ON in `main()` (`st.USE_TARGET_FEASIBILITY_FILTER = True`).
Default trade management: **Model A** (per v9.4 backtest results).

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

Set `st.USE_TARGET_FEASIBILITY_FILTER = False` (or `--fuel-filter off`) for a baseline run with no TFS influence.

### Data pipeline

1. **Fetching** — `get_top_crypto()` → `fetch_symbol_data()` via `ThreadPoolExecutor` (25 workers, semaphore-capped). `get_400_candles()` fetches two batches of 200 candles; second batch uses `min(timestamps)` of first batch as the `end_time` anchor (safe regardless of Bybit sort order).
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
6. **Classification** — `classify()` → `premium_setup / high_quality / secondary_quality / watchlist / rejected`

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

`classify()` hard rejects (always active, not gated by flag):

| Condition | Rule |
|---|---|
| MACD divergence | `against` |
| Entry extension | > 0.50 |
| R:R | < 2.0 |
| FVG filled | yes |
| ChoCH age | > 4c |

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

`recommend_model()` — v9.4/v9.5 logic (less defensive than earlier versions):
- Hard → Model B: watchlist/rejected category, verdict TARGET BLOCKED/NO FUEL, ChoCH ≥ 4, entry_ext > 0.25, confluence_zone
- → Model A: active category + TFS ≥ 55 + valid status; or premium_setup; or MACD yes + entry_ext ≤ 0.25 + valid status; or MPS ≥ 75 + TFS ≥ 55

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

Active setups sorted by `_live_rank_key`: MPS → TFS → verdict → base strength → model → ts → −entry_ext.

TOP PICKS filter: MPS ≥ 70 + TFS ≥ 55 + verdict ∉ {TARGET BLOCKED, NO FUEL}.

Detail card panel order: Wyckoff → Wyckoff Cause → ChoCH/Pattern + Trade Plan → Target Feasibility + MPS + MACD/Regime → Model A plan (if model=A) → Trigger Checklist → Invalidation.

Table columns: # · Symbol · Dir · Wyckoff · Category · MPS · Score · **TFS · Verdict · WCS · Base · Obstacle · Magnet** · Status · Pattern · ChoCH · EntExt · FVG · FVG+OB · Entry · SL · TP1a · TP1b · TP2 · R:R · MACD · Model

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
- `simulate_auto_model(setup, candles_5m, conservative=True)` — reads `setup["model"]` from `recommend_model()`.
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

**Interactive prompts:** start/end date · scan interval · top-N symbols · configs · models · intrabar mode (`c`/`o`/`co`) · entry mode (`l`=limit / `i`=immediate / `li`=both)

**Four dimensions per run:** config × model × intrabar × entry_mode → `config_results` keyed by `(cfg_name, model_name, cons_label, entry_mode)` 4-tuple.

**Entry modes:**
- `limit` — pending order, filled if price touches `entry_mid` before TTL (= next scan step). Unfilled orders cancelled.
- `immediate` — instant fill at `entry_mid` at signal time (no TTL).

**close_at_end:** open trades at end_dt are closed at last available 5M candle price; `exit_reason = "close_at_end"`.

**Statistics (`compute_stats`):** `positive_wins` / `full_wins` / `partial_wins` / `losses` / `no_exit` · `win_rate` (pnl_r > 0 / closed) · `total_r` · `ev_per_trade` · `expectancy` · `payoff_ratio` · `breakeven_wr` · `profit_factor` · `avg_win_r` / `avg_loss_r` · `avg_mfe_r` / `avg_mae_r` · `max_dd_r` · `z_score` · `fill_rate` (limit mode). For TOP3: `max_concurrent` / `avg_concurrent` / `concurrent_dist`.

**Breakdowns per config:** Category · Status · MACD · Direction · ChoCH Age · Entry Extension · Entry Mode · TFS bucket · TFS Verdict · **Wyckoff Cause Score** · **Base Strength** · **Range Duration** · **Phase D Age**

**Edge diagnostics:** `_edge_groups()` scans all breakdown rows across all configs — reports EDGE CANDIDATES (N≥20, EV>0, PF>1.2) and EDGE KILLERS (N≥20, EV<0, PF<1.0).

**Output:**
- HTML: `strategy_wyckoff_9_5/results/backtest_{start}_{end}_{fuel_ON|fuel_OFF}.html`
- CSV: `strategy_wyckoff_9_5/results/backtest_{start}_{end}_{fuel_ON|fuel_OFF}_trades.csv` — one row per trade, all configs stacked. Columns include all trade fields plus Wyckoff Cause fields.

**Debug mode:** first `run_config()` call uses `debug_wcs=True`, printing up to 5 setups with Wyckoff Cause diagnostics to stdout for sanity-checking.

**TF fetched:** `BT_TIMEFRAMES = ["1H", "30m", "15m", "5m"]`

Raw data dicts include a `_turnover` float key — always filter with `isinstance(c, list)` when iterating `.items()` before passing to `slice_at()`. BTC and ETH always included for `st.market_regime()`.

## Single-instrument analysis (`analyze_instrument.py`)

Fetches 400 candles across 5M / 15M / 30M / 1H / 4H / 1D and produces a market-structure report (no trade signals).

```bash
source venv/bin/activate
python analyze_instrument.py
```

Interactive: symbol (auto-appends `USDT`) · `t` (live) or `h` (`dd/mm/yyyy hh:mm`).
Output: `output/analysis_{SYMBOL}_{TIMESTAMP}.html`, auto-opens Chrome.

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
