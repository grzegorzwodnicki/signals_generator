# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project structure

### Primary — `strategy_9_1/` (canonical)

Three files sharing one source of truth:

| File | Role |
|---|---|
| `strategy_9_1/strategy.py` | Shared logic: data fetching, Wyckoff, FVG, OB, ChoCH, scoring, classify |
| `strategy_9_1/execution.py` | Trade simulation engine: Model A, Model B, auto-select, MFE/MAE, conservative/optimistic intrabar |
| `strategy_9_1/scanner.py` | Live scan + HTML report |
| `strategy_9_1/backtest.py` | Historical simulation: multi-config × multi-model × multi-mode (conservative/optimistic) |

Both `scanner.py` and `backtest.py` do `sys.path.insert(0, dirname)` so `import strategy` resolves to the sibling `strategy.py`. Run from project root:

```bash
python strategy_9_1/scanner.py
python strategy_9_1/backtest.py
```

Cache goes to `strategy_9_1/cache/`, results to `strategy_9_1/results/`.

### Legacy files (kept for reference)
- **`signal_generator_9_1.py`** — original monolithic scanner (Approach A)
- **`research/backtest.py`** — old backtest importing `signal_generator_9_1`
- **`fetch_and_zip_crypto.py`** + **`prompt_crypto_intradays_9_1.txt`** — Approach B: data fetch + LLM prompt workflow

## Environment setup

A virtual environment lives at `venv/`. Activate it before running any script:

```bash
source venv/bin/activate
```

Install dependencies inside the venv if not already present:

```bash
pip install requests
```

## Running the scanner

```bash
source venv/bin/activate
python strategy_9_1/scanner.py
```

Interactive prompts:
- **`t`** — live data
- **`h`** — historical, then enter date as `dd/mm/yyyy hh:mm`

Output: `output/signals_YYYY_MM_DD_HHMM.html`, auto-opens Chrome.

## API credentials

Stored in `.env` in the project root (gitignored):
```
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
```
Both `strategy_9_1/strategy.py` and `signal_generator_9_1.py` load this via a built-in `_load_env()` parser — no `python-dotenv` dependency needed. Credentials are optional; scripts work without them but may hit rate limits faster.

## Architecture of `strategy_9_1/strategy.py`

1. **Data fetching** — `get_top_crypto()` → `fetch_symbol_data()` via `ThreadPoolExecutor` (25 workers, semaphore-capped). Two batches of 200 candles paginated via `end_time`.
2. **Technical analysis** per symbol (all on 5M candles unless noted):
   - `detect_wyckoff()` — trading range on 1H; uses **80th/20th percentile** of highs/lows over last 45 candles (not absolute max/min) to define range, then detects SOS/SOW in last 15 candles
   - `detect_fvg()` — Fair Value Gap (bullish: `candle[i].low > candle[i-2].high`), lookback 120 candles
   - `detect_ob()` — Order Block: last opposite candle before displacement ≥ 0.1%
   - `detect_choch()` — Change of Character; local window = **5 candles**, searches **30 candles** back; rejects if age > 6
   - `detect_engulfing()` / `detect_pin_bar()`
   - `macd_divergence()` — compares two halves of last 30 candles vs MACD histogram
3. **Scoring** — `total_score()` (0–100) + `manual_pick_score()` (0–100); MACD `against` returns -1 (hard reject).
4. **Classification** — `classify()` → `premium_setup / high_quality / secondary_quality / watchlist / rejected`

`BACKTEST_TIME_MS` is a module-level global in `strategy.py`; set it via `st.BACKTEST_TIME_MS = ...` from scanner or backtest before fetching.

## Filter thresholds — strict v9.1

`classify()` hard rejects (→ `"rejected"`):

| Condition | Rule |
|---|---|
| MACD divergence | `against` |
| Entry extension | > 0.50 |
| R:R | < 2.0 |
| FVG filled | yes |
| ChoCH age | > 4c |

Below-threshold (→ `"watchlist"`): `total_score < 73`

Classification thresholds:

- **`premium_setup`**: MACD yes + entry_ext ≤ 0.25 + at_fvg/ob + ChoCH ≤ 3c + engulfing + rr ≥ 2.0 + **mps ≥ 75**
- **`high_quality`**: entry_ext ≤ 0.25 + engulfing + MACD yes/none + at_fvg/ob + ChoCH ≤ 3c + rr ≥ 2.0
- **`secondary_quality`**: passes all hard filters, ts ≥ 73, engulfing — but misses premium/HQ thresholds
- **Pin bar**: always `watchlist` unless ts ≥ 85 + ChoCH ≤ 1c + MACD yes + at_fvg_and_order_block + entry_ext ≤ 0.25
- **`confluence_zone` only**: always `watchlist` unless MACD yes + ChoCH ≤ 2c + entry_ext ≤ 0.25
- **entry_ext 0.25–0.50**: needs ≥ 3 premium confirmations (MACD yes, valid status, ChoCH ≤ 2, ts ≥ 85, at_fvg_and_order_block, rr ≥ 3) or → `watchlist`

`recommend_model()` — defensive-first:
- Any defensive condition (regime=chop, ChoCH ≥ 4, entry_ext > 0.25, secondary_quality, confluence_zone, mps < 75, or MACD not yes + not strong_confluence) → **Model B**
- Otherwise: premium_setup → **Model A**; high_quality + mps ≥ 75 → **Model A**; MACD yes + entry_ext ≤ 0.25 + strong status → **Model A**

## Key implementation details

- Exponential backoff on Bybit rate-limit codes `10002 / 10006 / 10018`.
- In backtest mode, `BACKTEST_TIME_MS` is passed as `end_time` to all candle requests; current price comes from last 1m close.
- Symbols with `turnover24h < 800_000` USD are hard-rejected (liquidity pre-filter).
- After saving the HTML report, the script automatically opens it in Google Chrome (`open -a "Google Chrome"`), falling back to the system default browser if Chrome is not found.

## Approach B — LLM-based scanner (`fetch_and_zip_crypto.py` + `prompt_crypto_intradays_9_1.txt`)

Alternative workflow: fetch raw OHLCV to JSON, then send to an LLM with a structured prompt.

```bash
source venv/bin/activate
python fetch_and_zip_crypto.py
```

Interactive prompts:
- **Mode**: current (`t`) or backtest (`n`) → single date (`d`) or range (`z`)
- **Symbol source**: `1` Top 400 by turnover | `2` `crypto_ftmo.txt` | `3` `crypto_breakout.txt`

Output goes to `output/`. Paste JSON content into `prompt_crypto_intradays_9_1.txt` by replacing `{{MARKET_JSON}}`, then send to an LLM (Claude).

### `prompt_crypto_intradays_9_1.txt` — LLM analysis prompt

Full v9.1 Wyckoff + SMC analysis prompt for LLM-based scanning. The LLM returns a complete dark-mode HTML report.

**Upstream assumption:** data is already pre-filtered to `quote_volume_usd > 800k`; the prompt does NOT apply an additional volume hard-filter.

**Strategy logic (matches `strategy_9_1/strategy.py`):**
- HTF context: Wyckoff on 1H/30M — trading range, SOS/SOW, Phase D, pullback
- LTF trigger: 5M/15M ChoCH → FVG/OB → engulfing candle
- LONG: Accumulation/Reaccumulation → SOS → pullback to LPS → FVG/OB → 5M ChoCH → engulfing
- SHORT: Distribution/Redistribution → SOW → pullback to LPSY → FVG/OB → 5M ChoCH → engulfing

**Scoring (two independent scores):**
- `total_score` (0–100): Wyckoff quality (0–14) + Phase D (0–16) + pullback (0–12) + FVG (0–14) + OB/FVG confluence (0–12) + ChoCH freshness (0–12) + pattern (0–10) + R:R/location (0–10) + regime (–5 to +6) + MACD (–100/0/+8) + liquidity (–3 to +3)
- `manual_pick_score` (0–100): MACD (0–20) + entry quality (0–20) + location (0–20) + ChoCH (0–15) + pattern (0–10) + R:R (0–10) + regime (–5 to +5)

**Key hard filters (same as `strategy.py`):**

| Filter | Rule |
|---|---|
| entry_extension | > 0.50 → reject; 0.25–0.50 → needs 3+ premium confirmations |
| ChoCH age | > 4c → reject (> 2c needs extra conditions) |
| MACD divergence | `against` → hard reject ACTIVE SETUPS |
| Pattern | engulfing required for ACTIVE; pin bar → watchlist only |
| total_score | < 73 → watchlist/rejected |
| quote_volume_usd | < 800k → reject (if field available) |
| FVG | filled before entry → reject |

**Categories produced:**
- `PREMIUM` — MACD yes + entry_ext ≤ 0.25 + at_fvg/ob + ChoCH ≤ 3c + engulfing touches zone; Manual Pick Score typically ≥ 75
- `HIGH_QUALITY` — entry_ext ≤ 0.25 + engulfing + MACD neutral/yes + at_fvg/ob + ChoCH ≤ 3c
- `SECONDARY_QUALITY` — passes active filter but weaker (MACD neutral, ChoCH 4c, entry_ext 0.25–0.50)
- `WATCHLIST` — confluence_zone only, ChoCH 4c without MACD, pin bar, good structure but no fresh ChoCH
- `REJECTED`

**Trade management models (same as `execution.py`):**
- **Model A (staged):** 35% @ 1.1R → SL to BE, 35% @ 2.0R, 30% @ 3.0R runner; preferred for PREMIUM
- **Model B (protective):** 50% @ 1.1R → SL to BE, 50% @ 2.0R; preferred for SECONDARY / chop regime

**Report sections:** Header · Market Summary · Stats Grid · **TOP MANUAL PICKS** (most prominent) · Premium Setups · Active Setups Ranking Table · Detail Cards per setup (Wyckoff/Pattern/ChoCH/FVG/OB/Entry/MACD/MPS breakdown/Trigger Checklist/Trade Plan/Risks/Invalidation) · Backup Setups · Watchlist · Rejected · Footer

## Single-instrument analysis (`analyze_instrument.py`)

Fetches 400 candles across 5M / 15M / 1H / 4H / 1D and produces a pure market-structure report — no trade signals.

```bash
source venv/bin/activate
python analyze_instrument.py
```

Interactive prompts: symbol (auto-appends `USDT`), then `t` (live) or `h` (`dd/mm/yyyy hh:mm`).

Output: `output/analysis_{SYMBOL}_{TIMESTAMP}.html`, auto-opens Chrome.

Analysis sections: Wyckoff (phase + confidence per TF) · Liquidity map (multi-TF consolidated) · Sweep analysis · Structure (BOS / ChoCH) · Volume POC (60-bucket profile) · Elliott light · Final interpretation.

Key functions: `analyze_wyckoff()`, `analyze_liquidity()`, `analyze_sweeps()`, `analyze_structure()`, `estimate_poc()`, `analyze_elliott()`.

**`analyze_liquidity()` — enhanced level detection:**
- Lookback: **150 candles** (was 60)
- Three swing windows: **lb=3 (minor) / lb=5 (intermediate) / lb=8 (major)** — wider window = more significant level
- All significant swings returned (not just equal-highs/lows), labeled: `EQH/EQL` (≥2 touches), `Major SH/SL`, `Swing H/L`, `Minor SH/SL`, `Period High/Low`
- Each level scored: `count × lb_rank` (lb=8 scores 3×, lb=5 scores 2×, lb=3 scores 1×)

**HTML liquidity map — consolidated multi-TF view:**
- All 5 timeframes merged into one map (tolerance 0.4%); levels appearing on multiple TFs accumulate score
- Up to **10 levels above / 10 below** current price
- Each row shows: price · type · touch count · source TFs · strength dots (●●●)
- **Above-price levels sorted descending** (highest first) so the map matches chart order: top = highest resistance, price line in middle, below levels going down

## Execution engine (`strategy_9_1/execution.py`)

All trade simulation logic lives here. Imported by `backtest.py`.

**Models:**
- `simulate_model_a(setup, candles_5m, conservative=True)` — 35% @ 1.1R → BE, 35% @ 2.0R, 30% @ 3.0R. Max = 1.985R.
- `simulate_model_b(setup, candles_5m, conservative=True)` — 50% @ 1.1R → BE, 50% @ 2.0R. Max = 1.55R.
- `simulate_auto_model(setup, candles_5m, conservative=True)` — reads `setup["model"]` from `recommend_model()`.

**Intrabar conflict resolution** — two modes, controlled by `conservative` param:
- **conservative** (default `True`): both SL and TP touched in same 5M candle → SL wins (`_intrabar_conservative`)
- **optimistic** (`False`): same conflict → TP wins (`_intrabar_optimistic`)

**Returns ExecResult dict:**
- `exit_reason` — `"tp_full"` / `"sl"` / `"be_exit"` / `"open"`
- `pnl_r` — realized R (partial closes accumulated)
- `mfe_r` — maximum favorable excursion in R
- `mae_r` — maximum adverse excursion in R (negative)
- `bars_held` — number of 5M bars from fill to exit
- `conservative_intrabar` — bool, `True` only when conservative mode resolved a same-bar SL+TP conflict (not for all SL exits)
- `model` — `"A"` or `"B"`

## Backtesting (`strategy_9_1/backtest.py`)

Multi-config × multi-model comparison. Runs in sequence (not parallel), same OHLCV cache shared across all runs.

```bash
source venv/bin/activate
python strategy_9_1/backtest.py
```

Interactive prompts: start/end date, scan interval (hours), top-N symbols, which configs, which models, intrabar mode (`c` = conservative / `o` = optimistic / `co` = both).

**Configurations (CONFIGS dict):**
| Name | Top-N | Category filter | basket_size |
|---|---|---|---|
| `TOP1` | 1 | all active | 1 |
| `TOP3` | 3 | all active | **3** |
| `PREMIUM` | 99 | premium_setup only | 1 |
| `HQ` | 99 | high_quality only | 1 |
| `PREMIUM_HQ` | 99 | premium_setup OR high_quality | 1 |

`_ACTIVE = {"premium_setup", "high_quality", "secondary_quality"}` — watchlist/rejected are never traded.

**TOP3 basket mode:** up to 3 concurrent trades open simultaneously. New pending orders placed for each free slot each scan. Symbol dedup: no two trades on the same symbol at the same time.

**Trade model — limit order simulation (basket-aware):**
1. All pending orders from last step checked for fill in `(signal_ts, ttl_ts]`. Unfilled → cancelled.
2. All active trades checked for exit via `execution.py` `sim_fn(setup, candles_after_fill_up_to_now)`.
3. `slots_free = basket_size - len(active_trades)`. If slots > 0: scan for new signals, place up to `min(top_n, slots_free)` new pending orders on non-busy symbols.

**Signal ranking** (`_rank_key`) — matches scanner.py order: MPS → total_score → MACD yes → -entry_ext → category tier.

**Three dimensions in every backtest run:** config × model × intrabar mode. `config_results` is keyed by `(cfg_name, model_name, cons_label)` 3-tuple. `INTRABAR_MODES = {"conservative": True, "optimistic": False}`.

**Statistics (`compute_stats`):** wins_full, wins_be (partial), losses, no_exit, win_rate, EV/trade, profit factor, avg MFE/MAE, max DD, **Z-score** (runs test), **fill_rate** (orders_filled / orders_created). For TOP3: also max_concurrent, avg_concurrent, concurrent_dist (steps with 0/1/2/3 active trades).

**Breakdowns per config:** category · MACD · direction · ChoCH age · entry extension bucket.

**Report:** comparison table (all config×model×mode combos; columns: Config, Model, Intrabar, Basket, Trades, Win Rate, Total R, EV/Trade, PF, Max DD, Z-Score, Fill Rate) + per-config equity curve + breakdown tables + collapsible trade log (★ = `conservative_intrabar` triggered on that trade). TOP3 shows extra basket stats cards.

**TF fetched:** `BT_TIMEFRAMES = ["1H", "30m", "15m", "5m"]` — 30m included for Wyckoff Phase D confirmation context.

Output: `strategy_9_1/results/backtest_{start}_{end}.html`, auto-opens Chrome.

Raw data dicts include a `_turnover` float key alongside timeframe lists — always filter with `isinstance(c, list)` when iterating `.items()` before passing to `slice_at()`.

BTC and ETH are always included in the symbol list for `st.market_regime()` even if not in the top-N scan list.

## Optional symbol list files

Place one `SYMBOL` per line (e.g. `BTCUSDT`) in:
- `crypto_ftmo.txt` — symbols for option 2 in `fetch_and_zip_crypto.py`
- `crypto_breakout.txt` — symbols for option 3
