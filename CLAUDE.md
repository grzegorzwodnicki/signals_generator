# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A crypto trading signal generator with two approaches:

### Approach A — All-in-one Python pipeline (`signal_generator.py`)
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
python signal_generator.py
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

## Architecture of `signal_generator.py`

All logic lives in a single file, executed top-to-bottom:

1. **Data fetching** — `get_top_crypto()` → `fetch_symbol_data()` via `ThreadPoolExecutor` (25 workers, semaphore-capped). Two batches of 200 candles paginated via `end_time`.
2. **Technical analysis** per symbol (all on 5M candles unless noted):
   - `detect_wyckoff()` — trading range on 1H, SOS/SOW detection, Phase D, pullback check
   - `detect_fvg()` — Fair Value Gap (bullish: `candle[i].low > candle[i-2].high`)
   - `detect_ob()` — Order Block (last opposite candle before displacement)
   - `detect_choch()` — Change of Character; rejects if age > 4 candles
   - `detect_engulfing()` / `detect_pin_bar()`
   - `macd_divergence()` — compares two halves of last 30 candles vs histogram
3. **Scoring** — `total_score()` (0–100) + `manual_pick_score()` (0–100) per v9.1 rules; MACD `against` returns -1 (hard reject).
4. **Classification** — `classify()` → `premium_setup / high_quality / secondary_quality / watchlist / rejected`
5. **HTML generation** — `generate_html()` builds the full report in memory and writes it to `output/`.

`BACKTEST_TIME_MS` is a module-level global set in `main()` and read by all candle-fetching functions.

## Key implementation details

- Exponential backoff on Bybit rate-limit codes `10002 / 10006 / 10018`.
- In backtest mode, `BACKTEST_TIME_MS` is passed as `end_time` to all candle requests; current price comes from last 1m close.
- Symbols with `turnover24h < 800_000` USD are hard-rejected (liquidity pre-filter).
- `entry_extension > 0.50` is a hard reject (chased entry).
- ChoCH age > 4 candles is a hard reject.

## Optional symbol list files

Place one `SYMBOL` per line (e.g. `BTCUSDT`) in:
- `crypto_ftmo.txt` — symbols for option 2 in `fetch_and_zip_crypto.py`
- `crypto_breakout.txt` — symbols for option 3
