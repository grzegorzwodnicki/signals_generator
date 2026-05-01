"""
Global configuration for the Wave 3 scanner / backtester.
Strategy-level parameters live in StrategyConfig (strategy.py).
This file holds I/O, API, and runtime settings.
"""

import os

# ---------------------------------------------------------------------------
# Bybit API
# ---------------------------------------------------------------------------

BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")
BYBIT_BASE_URL: str = "https://api.bybit.com"

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

CACHE_DIR: str = os.path.join(_THIS_DIR, "data", "cache")

SCANNER_OUTPUT_DIR: str = os.path.join(_THIS_DIR, "results", "scanner")
BACKTEST_OUTPUT_DIR: str = os.path.join(_THIS_DIR, "results", "backtest")

# ---------------------------------------------------------------------------
# Scanner defaults
# ---------------------------------------------------------------------------

DEFAULT_TIMEFRAMES = ["15m", "1H", "4H"]
DEFAULT_TOP_N_SYMBOLS = 50
MIN_TURNOVER_USD = 5_000_000  # 24h minimum

# ---------------------------------------------------------------------------
# Chrome auto-open
# ---------------------------------------------------------------------------

CHROME_PATH: str = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
