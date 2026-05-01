"""
config.py — Live scanner configuration for v9.5.
All live-scan thresholds in one place. No need to edit scanner.py.
"""

SCANNER_VERSION = "v9.5"

ENGINE = "v95"

USE_TARGET_FEASIBILITY_FILTER = True

ACTIVE_SETUP_FAMILIES = {
    "WYCKOFF_STRICT",
    "TREND_PULLBACK",
}

SCAN_TOP_N = 400

LIVE_SCAN_INTERVAL_MINUTES = 60

MAX_ACTIVE_SETUPS_HTML  = 15
MAX_TOP_PICKS           = 3
MAX_WATCHLIST_REVIEW    = 4

# ── Live dedup / anti-spam ────────────────────────────────────
ALERT_COOLDOWN_HOURS          = 6
SYMBOL_DIRECTION_COOLDOWN_HOURS = 6
SAME_FAMILY_COOLDOWN_HOURS    = 6

# ── Manual review ─────────────────────────────────────────────
ENABLE_WATCHLIST_REVIEW  = True
WATCHLIST_REVIEW_MIN_MPS = 55
WATCHLIST_REVIEW_MIN_TFS = 40

# ── Live management defaults ──────────────────────────────────
DEFAULT_TREND_PULLBACK_MODEL      = "Model B"
AGGRESSIVE_TREND_PULLBACK_MODEL   = "FIXED_2R"
DEFAULT_WYCKOFF_MODEL             = "Model A"

# ── Output flags ──────────────────────────────────────────────
WRITE_LATEST_HTML      = True
WRITE_TIMESTAMPED_HTML = True
WRITE_ALERTS_JSON      = True
WRITE_SCAN_LOG_CSV     = True
