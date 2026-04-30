# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project structure

### Primary — `strategy_9_1/` (canonical)

Three files sharing one source of truth:

| File | Role |
|---|---|
| `strategy_9_1/strategy.py` | Shared logic: data fetching, Wyckoff, FVG, OB, ChoCH, scoring, classify |
| `strategy_9_1/scanner.py` | Live scan + HTML report |
| `strategy_9_1/backtest.py` | Historical simulation with limit order fill model |

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

## Filter thresholds (current calibration)

These were loosened after initial deployment produced zero active signals:

| Filter | Hard reject | Watchlist |
|---|---|---|
| Entry extension | > 0.50 | — |
| ChoCH age | > 6c | 5–6c |
| Total score | — | < 65 |
| R:R | < 2.0 | — |
| Confluence zone radius | — | 15% of range height |
| MACD | `against` | — |
| Liquidity | turnover < $800k | — |

Classification thresholds:
- **Premium**: MACD yes + entry_ext ≤ 0.25 + at_fvg/ob + ChoCH ≤ 3c + engulfing touches zone
- **High quality**: entry_ext ≤ 0.35 + engulfing + at_fvg/ob + ChoCH ≤ 4c
- **Confluence zone active**: (MACD yes OR entry_ext ≤ 0.25) AND ChoCH ≤ 3c

## Key implementation details

- Exponential backoff on Bybit rate-limit codes `10002 / 10006 / 10018`.
- In backtest mode, `BACKTEST_TIME_MS` is passed as `end_time` to all candle requests; current price comes from last 1m close.
- Symbols with `turnover24h < 800_000` USD are hard-rejected (liquidity pre-filter).
- After saving the HTML report, the script automatically opens it in Google Chrome (`open -a "Google Chrome"`), falling back to the system default browser if Chrome is not found.

## Running the data fetcher (legacy Approach B)

```bash
source venv/bin/activate
python fetch_and_zip_crypto.py
```

Interactive prompts:
- **Mode**: current (`t`) or backtest (`n`) → single date (`d`) or range (`z`)
- **Symbol source**: `1` Top 400 by turnover | `2` `crypto_ftmo.txt` | `3` `crypto_breakout.txt`

Output goes to `output/`. Use with `prompt_crypto_intradays_9_1.txt` (replace `{{MARKET_JSON}}`).

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

## Backtesting (`strategy_9_1/backtest.py`)

Walks through a date range step-by-step, runs the full strategy analysis at each step, trades only the single TOP signal by MPS using a limit order model, then finds the next signal.

```bash
source venv/bin/activate
python strategy_9_1/backtest.py
```

Interactive prompts: start date, end date (`dd/mm/yyyy hh:mm`), scan interval (hours), number of symbols to scan.

**Trade model — limit order simulation:**
1. Signal detected → `pending_order` placed at `entry_mid`, TTL = next scan timestamp.
2. 5M candles in window `(signal_ts, ttl_ts]` are checked: LONG fill if `low ≤ entry`, SHORT fill if `high ≥ entry`.
3. Fill → `activate_trade()` sets `entry_ts` to the fill candle; exits tracked from that point.
4. No fill by TTL → order cancelled, new scan at same step.
5. Active trade: TP1a at 1.1R (SL moves to BE + 0.2%) → TP2 at 3.0R full close (win) or SL hit → −1R loss / 0R BE.

HTML trade log shows three time columns: **Signal Time** (when detected) · **Fill Time** (when limit hit) · **Exit Time**.

**Caching:** OHLCV data is fetched once per run and saved to `strategy_9_1/cache/bt_{start}_{end}_top{N}.json`. Subsequent runs with same params reuse the cache. Multi-batch fetching includes 400-candle lookback before `start_ms`.

**Key functions:**
- `fetch_full_candles()` — multi-batch Bybit fetch with dedup + chronological sort
- `slice_at(candles, ts)` — binary search slice for "data available at time T" simulation
- `check_pending_fill(pending, candles_5m)` — scans 5M window for limit order fill
- `activate_trade(pending, fill_candle)` — converts pending → active trade at fill timestamp
- `check_exits(trade, candles_5m)` — applies TP1a / TP2 / SL against 5M OHLCV
- `run_backtest()` — main loop: pending fill check → exit check → signal scan
- `compute_stats()` — win rate, total R, avg win/loss R, max drawdown R, profit factor, equity curve
- `generate_report()` — dark-mode HTML with equity curve SVG + stats cards + full trade log

Raw data dicts include a `_turnover` float key alongside timeframe lists — always filter with `isinstance(c, list)` when iterating `.items()` before passing to `slice_at()`.

Output: `strategy_9_1/results/backtest_{start}_{end}.html`, auto-opens Chrome.

BTC and ETH are always included in the symbol list for `st.market_regime()` even if not in the top-N scan list.

## Optional symbol list files

Place one `SYMBOL` per line (e.g. `BTCUSDT`) in:
- `crypto_ftmo.txt` — symbols for option 2 in `fetch_and_zip_crypto.py`
- `crypto_breakout.txt` — symbols for option 3
