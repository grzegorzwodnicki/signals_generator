# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A crypto trading signal generator with two approaches:

### Approach A — All-in-one Python pipeline (`signal_generator_9_1.py`)
Fetches top 400 USDT Futures from Bybit, performs Wyckoff + SMC analysis in Python, and writes a self-contained dark-mode HTML report directly — no LLM required.

### Approach B — Data fetch + LLM prompt (original workflow)
1. **`fetch_and_zip_crypto.py`** — fetches OHLCV data, saves to `output/market_data_crypto_<date>.json` (zipped for live runs).
2. **`prompt_crypto_intradays_9_1.txt`** — LLM prompt (Polish) that consumes the JSON and returns a full HTML report. Replace `{{MARKET_JSON}}` with the JSON content before sending to an LLM (Claude Opus/Sonnet).

## Environment setup

A virtual environment lives at `venv/`. Activate it before running any script:

```bash
source venv/bin/activate
```

Install dependencies inside the venv if not already present:

```bash
pip install requests
```

## Running the signal generator (Approach A)

```bash
source venv/bin/activate
python signal_generator_9_1.py
```

Interactive prompts:
- **`t`** — live data (current prices)
- **`h`** — historical/backtest, then enter date as `dd/mm/yyyy hh:mm`

Output: `output/signals_YYYY_MM_DD_HHMM.html` (or `_backtest.html`). Open in a browser.

## Running the data fetcher (Approach B)

```bash
source venv/bin/activate
python fetch_and_zip_crypto.py
```

Interactive prompts:
- **Mode**: current data (`t`) or backtest (`n`)
  - Backtest: single date (`d`) with `dd/mm/yyyy hh:mm`, or date range (`z`) with `dd/mm/yyyy - dd/mm/yyyy` + time
- **Symbol source**: `1` Top 400 by turnover (default) | `2` `crypto_ftmo.txt` | `3` `crypto_breakout.txt`

Output goes to `output/` (created automatically).

## API credentials

`API_KEY` and `API_SECRET` at the top of both scripts are Bybit credentials for higher rate limits. Optional — scripts work without them but may hit rate limits faster.

## Architecture of `signal_generator_9_1.py`

All logic lives in a single file, executed top-to-bottom:

1. **Data fetching** — `get_top_crypto()` → `fetch_symbol_data()` via `ThreadPoolExecutor` (25 workers, semaphore-capped). Two batches of 200 candles paginated via `end_time`.
2. **Technical analysis** per symbol (all on 5M candles unless noted):
   - `detect_wyckoff()` — trading range on 1H; uses **80th/20th percentile** of highs/lows over last 45 candles (not absolute max/min) to define range, then detects SOS/SOW in last 15 candles
   - `detect_fvg()` — Fair Value Gap (bullish: `candle[i].low > candle[i-2].high`), lookback 150 candles
   - `detect_ob()` — Order Block: last opposite candle before displacement ≥ 0.1%
   - `detect_choch()` — Change of Character; local high/low window = **5 candles**, searches **30 candles** back; rejects if age > 6
   - `detect_engulfing()` / `detect_pin_bar()`
   - `macd_divergence()` — compares two halves of last 30 candles vs MACD histogram
3. **Scoring** — `total_score()` (0–100) + `manual_pick_score()` (0–100) per v9.1 rules; MACD `against` returns -1 (hard reject).
4. **Classification** — `classify()` → `premium_setup / high_quality / secondary_quality / watchlist / rejected`
5. **HTML generation** — `generate_html()` builds the full report in memory, saves to `output/`, and opens in Chrome automatically.

`BACKTEST_TIME_MS` is a module-level global set in `main()` and read by all candle-fetching functions.

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

## Single-instrument analysis (`analyze_instrument.py`)

Fetches 400 candles across 5M / 15M / 1H / 4H / 1D and produces a pure market-structure report — no trade signals.

```bash
source venv/bin/activate
python analyze_instrument.py
```

Interactive prompts: symbol (auto-appends `USDT`), then `t` (live) or `h` (`dd/mm/yyyy hh:mm`).

Output: `output/analysis_{SYMBOL}_{TIMESTAMP}.html`, auto-opens Chrome.

Analysis sections: Wyckoff (phase + confidence per TF) · Liquidity map (equal highs/lows) · Sweep analysis (wick-beyond-swing + close back inside) · Structure (BOS / ChoCH) · Volume POC (60-bucket profile) · Elliott light · Final interpretation.

Key functions: `analyze_wyckoff()`, `analyze_liquidity()`, `analyze_sweeps()`, `analyze_structure()`, `estimate_poc()`, `analyze_elliott()`.

## Backtesting (`research/backtest.py`)

Walks through a date range step-by-step, runs the full `signal_generator_9_1` analysis at each step, trades only the single TOP signal by MPS, waits for exit (TP2 = 3R or SL = −1R), then finds the next signal.

```bash
source venv/bin/activate
python research/backtest.py
```

Interactive prompts: start date, end date (`dd/mm/yyyy hh:mm`), scan interval (hours), number of symbols to scan.

**Trade model — limit order simulation:**
1. Signal detected → `pending_order` placed at `entry_mid`, TTL = next scan timestamp.
2. 5M candles in window `(signal_ts, ttl_ts]` are checked: LONG fill if `low ≤ entry`, SHORT fill if `high ≥ entry`.
3. Fill → `activate_trade()` sets `entry_ts` to the fill candle; exits tracked from that point.
4. No fill by TTL → order cancelled, new scan at same step.
5. Active trade: TP1a at 1.1R (SL moves to BE + 0.2%) → TP2 at 3.0R full close (win) or SL hit → −1R loss / 0R BE.

HTML trade log shows three time columns: **Signal Time** (when detected) · **Fill Time** (when limit hit) · **Exit Time**.

**Caching:** OHLCV data is fetched once per run and saved to `research/cache/bt_{start}_{end}_top{N}.json`. Subsequent runs with same params reuse the cache. Multi-batch fetching includes 400-candle lookback before `start_ms`.

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

Output: `research/results/backtest_{start}_{end}.html`, auto-opens Chrome.

BTC and ETH are always included in the symbol list for `sg.market_regime()` even if not in the top-N scan list.

## Optional symbol list files

Place one `SYMBOL` per line (e.g. `BTCUSDT`) in:
- `crypto_ftmo.txt` — symbols for option 2 in `fetch_and_zip_crypto.py`
- `crypto_breakout.txt` — symbols for option 3
