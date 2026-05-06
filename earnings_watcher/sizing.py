"""v6.16.0 \u2014 DMI sizing curve + portfolio-relative notional helper.

Pure functions, no side effects, no RTH imports. Safe to import from
shadow path, backtest harness, or unit tests.

Public API:
  - dmi_conviction_multiplier(conviction) -> float
  - dmi_sized_notional(equity, conviction, open_dmi_exposure) -> (notional, reason)

Constants are the source of truth for the v4 Phase 0 calibration. Any
change here MUST be reflected in earnings_watcher_spec/replay/decision_engine.py
and re-validated against the 84-day corpus.
"""
from __future__ import annotations


# Sizing curve --------------------------------------------------------------

DMI_BASE_NOTIONAL_PCT = 0.10           # 10% of portfolio equity per trade (1x size)
DMI_CONVICTION_SIZE_MAX = 3.0          # piecewise multiplier ceiling
DMI_MAX_POSITION_PCT = 0.30            # = base x max multiplier; explicit per-position guard
DMI_MIN_POSITION_PCT = 0.02            # if scale-to-fit drops below 2% of equity, skip trade entirely

# Concurrency
DMI_MAX_PORTFOLIO_EXPOSURE_PCT = 0.50  # cap simultaneous open DMI positions at 50% of equity

# Risk machinery (mirrors decision_engine.py in earnings_watcher_spec/replay/)
DMI_HARD_STOP = 0.03                   # tightened from 0.04 in v4 (clips no winners on Phase 0 corpus)
DMI_TRAIL_TRIGGER = 0.02
DMI_TRAIL_PCT = 0.05
DMI_TIME_STOP_MIN = 90


def dmi_conviction_multiplier(conviction: float) -> float:
    """v6.16.0 \u2014 piecewise conviction -> size multiplier.

    Mirrors the v4 sizing curve in earnings_watcher_spec/replay/decision_engine.py:
      - conv < 3:        1x  (below threshold; minimum size)
      - 3 <= conv <= 8:  conv/3, clamped 1x to 2x  (legacy curve)
      - 8 < conv <= 12:  ramp from 2x to 3x linearly
      - conv > 12:       3x  (DMI_CONVICTION_SIZE_MAX cap)

    Returns a float in [1.0, DMI_CONVICTION_SIZE_MAX].
    """
    if conviction < 3.0:
        return 1.0
    if conviction <= 8.0:
        return max(1.0, min(conviction / 3.0, 2.0))
    return min(2.0 + (conviction - 8.0) / 4.0, DMI_CONVICTION_SIZE_MAX)


def dmi_sized_notional(
    equity: float | None,
    conviction: float,
    open_dmi_exposure: float,
) -> tuple[float, str]:
    """v6.16.0 \u2014 portfolio-relative DMI sizing with concurrency cap.

    Computes the dollar notional for a new DMI runaway entry given current
    portfolio equity, the breakout conviction score, and total dollar value
    of open DMI-managed positions.

    Logic:
      1. notional = equity x DMI_BASE_NOTIONAL_PCT x dmi_conviction_multiplier(conv)
      2. clamp to equity x DMI_MAX_POSITION_PCT (defense in depth)
      3. if (open_exposure + notional) > equity x DMI_MAX_PORTFOLIO_EXPOSURE_PCT,
         scale down to fit cap ("proportional scale" strategy from Phase 0)
      4. if scaled notional < equity x DMI_MIN_POSITION_PCT, return 0
         ("exposure_minimal" \u2014 not worth the slippage)

    Returns:
      tuple of (notional_dollars, reason_code) where reason_code is one of:
        "ok"                \u2014 full size, no cap
        "exposure_cap"      \u2014 scaled down to fit the 50% concurrency cap
        "exposure_minimal"  \u2014 cap-room < 2% of equity; skip the trade
        "no_equity"         \u2014 equity is None or non-positive; cannot size
    """
    if equity is None or equity <= 0:
        return 0.0, "no_equity"

    size_mult = dmi_conviction_multiplier(conviction)
    proposed = equity * DMI_BASE_NOTIONAL_PCT * size_mult

    # Hard cap per-position (defense in depth in case multiplier math drifts).
    proposed = min(proposed, equity * DMI_MAX_POSITION_PCT)

    # Concurrency cap: proportional scale to fit.
    max_total = equity * DMI_MAX_PORTFOLIO_EXPOSURE_PCT
    if open_dmi_exposure + proposed > max_total:
        scaled = max(0.0, max_total - open_dmi_exposure)
        if scaled < equity * DMI_MIN_POSITION_PCT:
            return 0.0, "exposure_minimal"
        return scaled, "exposure_cap"

    return proposed, "ok"
