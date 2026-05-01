"""
execution.py — Trade simulation engine for strategy_wyckoff_9_4.

simulate_model_a : 35% @ 1.1R, 35% @ 2.0R, 30% @ 3.0R  (max 1.985R)
simulate_model_b : 50% @ 1.1R, 50% @ 2.0R               (max 1.55R)
simulate_auto_model : picks A or B based on recommend_model() logic

conservative=True  : when both SL and TP touched in same 5M candle → SL wins
conservative=False : when both SL and TP touched in same 5M candle → TP wins

Returns an ExecResult dict.
"""

from __future__ import annotations

_FEE_BUFFER_PCT = 0.002   # 0.2% above/below entry for BE stop


def _sign(direction: str) -> int:
    return 1 if direction == "LONG" else -1


def _crossed_tp(c: dict, level: float, direction: str) -> bool:
    return c["high"] >= level if direction == "LONG" else c["low"] <= level


def _crossed_sl(c: dict, level: float, direction: str) -> bool:
    return c["low"] <= level if direction == "LONG" else c["high"] >= level


def _intrabar_conservative(c: dict, sl: float, tp: float, direction: str) -> str:
    """Both SL and TP touched → SL wins."""
    sl_hit = _crossed_sl(c, sl, direction)
    tp_hit = _crossed_tp(c, tp, direction)
    if sl_hit and tp_hit:
        return "sl"
    if sl_hit:
        return "sl"
    if tp_hit:
        return "tp"
    return "none"


def _intrabar_optimistic(c: dict, sl: float, tp: float, direction: str) -> str:
    """Both SL and TP touched → TP wins."""
    sl_hit = _crossed_sl(c, sl, direction)
    tp_hit = _crossed_tp(c, tp, direction)
    if sl_hit and tp_hit:
        return "tp"
    if tp_hit:
        return "tp"
    if sl_hit:
        return "sl"
    return "none"


def _run_model(
    setup: dict,
    candles_5m: list[dict],
    tps: list[tuple[float, float]],   # [(r_multiple, fraction), ...] ascending
    conservative: bool,
) -> dict:
    """
    Core simulation loop shared by Model A and B.

    tps  — list of (r_multiple, fraction_to_close), fractions must sum to 1.0.
    """
    direction = setup["direction"]
    entry     = setup["entry_mid"]
    sl_orig   = setup["sl"]
    risk      = abs(entry - sl_orig)
    sign      = _sign(direction)

    tp_levels  = [(entry + sign * r * risk, frac) for r, frac in tps]
    sl_current = sl_orig
    be_active  = False
    fee_buf    = entry * _FEE_BUFFER_PCT
    tp_idx     = 0
    pnl_r      = 0.0
    remaining  = 1.0
    mfe_r      = 0.0
    mae_r      = 0.0

    _check = _intrabar_conservative if conservative else _intrabar_optimistic

    for bar_i, c in enumerate(candles_5m):
        if tp_idx >= len(tp_levels):
            break

        tp_price, tp_frac = tp_levels[tp_idx]

        # MFE / MAE excursion from entry in R units
        if direction == "LONG":
            mfe_r = max(mfe_r, (c["high"] - entry) / risk)
            mae_r = min(mae_r, (c["low"]  - entry) / risk)
        else:
            mfe_r = max(mfe_r, (entry - c["low"])  / risk)
            mae_r = min(mae_r, (entry - c["high"]) / risk)

        verdict = _check(c, sl_current, tp_price, direction)

        if verdict == "sl":
            # conservative_intrabar = True only when conservative mode resolved
            # a same-bar conflict (both SL and TP touched, SL won)
            both_hit = (
                _crossed_sl(c, sl_current, direction) and
                _crossed_tp(c, tp_price, direction)
            )
            pnl_r += remaining * (0.0 if be_active else -1.0)
            return _result(
                "be_exit" if be_active else "sl",
                round(pnl_r, 4), round(mfe_r, 4), round(mae_r, 4),
                bar_i + 1,
                conservative and both_hit,
                tps,
            )

        if verdict == "tp":
            pnl_r    += tp_frac * tps[tp_idx][0]
            remaining -= tp_frac
            tp_idx   += 1
            # After first TP: move SL to break-even + fee buffer
            if tp_idx == 1:
                be_active  = True
                sl_current = entry + sign * fee_buf
            if tp_idx >= len(tp_levels):
                return _result(
                    "tp_full",
                    round(pnl_r, 4), round(mfe_r, 4), round(mae_r, 4),
                    bar_i + 1, False, tps,
                )
            # One TP per bar — move to next candle

    # End of supplied candles — trade still open
    return _result(
        "open",
        round(pnl_r, 4), round(mfe_r, 4), round(mae_r, 4),
        len(candles_5m), False, tps,
    )


def _result(
    exit_reason: str,
    pnl_r: float,
    mfe_r: float,
    mae_r: float,
    bars_held: int,
    conservative_intrabar: bool,
    tps: list,
) -> dict:
    return {
        "exit_reason":           exit_reason,
        "pnl_r":                 pnl_r,
        "mfe_r":                 mfe_r,
        "mae_r":                 mae_r,
        "bars_held":             bars_held,
        "conservative_intrabar": conservative_intrabar,
        "model_tps":             [(r, f) for r, f in tps],
    }


# ── Public API ────────────────────────────────────────────────

_MODEL_A_TPS = [(1.1, 0.35), (2.0, 0.35), (3.0, 0.30)]
_MODEL_B_TPS = [(1.1, 0.50), (2.0, 0.50)]


def simulate_model_a(
    setup: dict,
    candles_5m: list[dict],
    conservative: bool = True,
) -> dict:
    """35% @ 1.1R → BE, 35% @ 2.0R, 30% @ 3.0R. Max PnL = 1.985R."""
    result = _run_model(setup, candles_5m, _MODEL_A_TPS, conservative)
    result["model"] = "A"
    return result


def simulate_model_b(
    setup: dict,
    candles_5m: list[dict],
    conservative: bool = True,
) -> dict:
    """50% @ 1.1R → BE, 50% @ 2.0R. Max PnL = 1.55R."""
    result = _run_model(setup, candles_5m, _MODEL_B_TPS, conservative)
    result["model"] = "B"
    return result


def simulate_auto_model(
    setup: dict,
    candles_5m: list[dict],
    conservative: bool = True,
) -> dict:
    """Selects Model A or B based on recommend_model() criteria embedded in setup."""
    if setup.get("model") == "Model A":
        return simulate_model_a(setup, candles_5m, conservative)
    return simulate_model_b(setup, candles_5m, conservative)


def simulate_fixed_r(
    setup: dict,
    candles_5m: list[dict],
    tp_mult: float = 2.0,
    conservative: bool = True,
) -> dict:
    """Single-position fixed-R model: SL = -1R, TP = +tp_mult*R. No partial exits."""
    direction = setup["direction"]
    entry     = setup["entry_mid"]
    sl_price  = setup["sl"]
    risk      = abs(entry - sl_price)
    if risk == 0:
        return {
            "exit_reason": "open", "pnl_r": 0.0,
            "mfe_r": 0.0, "mae_r": 0.0,
            "bars_held": 0, "conservative_intrabar": False,
            "model": f"FIXED_{tp_mult}R", "model_tps": [(tp_mult, 1.0)],
        }
    sign     = _sign(direction)
    tp_price = entry + sign * tp_mult * risk
    _check   = _intrabar_conservative if conservative else _intrabar_optimistic

    mfe_r = mae_r = 0.0
    for bar_i, c in enumerate(candles_5m):
        if direction == "LONG":
            mfe_r = max(mfe_r, (c["high"] - entry) / risk)
            mae_r = min(mae_r, (c["low"]  - entry) / risk)
        else:
            mfe_r = max(mfe_r, (entry - c["low"])  / risk)
            mae_r = min(mae_r, (entry - c["high"]) / risk)

        verdict  = _check(c, sl_price, tp_price, direction)
        both_hit = _crossed_sl(c, sl_price, direction) and _crossed_tp(c, tp_price, direction)

        if verdict == "sl":
            return {
                "exit_reason": "sl", "pnl_r": -1.0,
                "mfe_r": round(mfe_r, 4), "mae_r": round(mae_r, 4),
                "bars_held": bar_i + 1,
                "conservative_intrabar": conservative and both_hit,
                "model": f"FIXED_{tp_mult}R", "model_tps": [(tp_mult, 1.0)],
            }
        if verdict == "tp":
            return {
                "exit_reason": "tp_full", "pnl_r": tp_mult,
                "mfe_r": round(mfe_r, 4), "mae_r": round(mae_r, 4),
                "bars_held": bar_i + 1, "conservative_intrabar": False,
                "model": f"FIXED_{tp_mult}R", "model_tps": [(tp_mult, 1.0)],
            }

    return {
        "exit_reason": "open", "pnl_r": 0.0,
        "mfe_r": round(mfe_r, 4), "mae_r": round(mae_r, 4),
        "bars_held": len(candles_5m), "conservative_intrabar": False,
        "model": f"FIXED_{tp_mult}R", "model_tps": [(tp_mult, 1.0)],
    }


def simulate_fixed_2r(setup, candles_5m, conservative=True):
    return simulate_fixed_r(setup, candles_5m, 2.0, conservative)

def simulate_fixed_15r(setup, candles_5m, conservative=True):
    return simulate_fixed_r(setup, candles_5m, 1.5, conservative)

def simulate_fixed_3r(setup, candles_5m, conservative=True):
    return simulate_fixed_r(setup, candles_5m, 3.0, conservative)
