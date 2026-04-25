"""Long-entry scenarios (5)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from synthetic_harness.scenarios import Scenario, Action
from synthetic_harness.market import (
    make_long_breakout_frame,
    make_index_bull_frame,
)

ET = ZoneInfo("America/New_York")
TODAY = "2026-04-24"
NOW = datetime(2026, 4, 24, 10, 30, 0, tzinfo=ET)


def _common_market(ticker, current_price, or_h, pdc_v):
    return {
        ticker: make_long_breakout_frame(
            ticker, current_price, or_h, pdc_v,
        ),
        "SPY": make_index_bull_frame("SPY", 510.00, 505.00),
        "QQQ": make_index_bull_frame("QQQ", 430.00, 425.00),
    }


def long_clean_entry() -> Scenario:
    """Fresh ticker passes every gate -> opens position."""
    ticker = "AAPL"
    # entry close to or_h so the OR-baseline stop (or_h - 0.90 = 268.10)
    # stays tighter than the 0.75% cap (270.00 * 0.9925 = 267.97), so
    # ENTRY_STOP_CAP_REJECT doesn't trigger.
    or_h = 269.00
    px = 270.00
    pdc_v = 267.50
    return Scenario(
        name="long_clean_entry",
        description="Long entry: all gates pass, position opens.",
        initial_state={
            "or_high": {ticker: or_h, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 265.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_h, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="check passes"),
            Action(kind="execute_entry", args=(ticker, px),
                   label="executes paper buy"),
        ],
    )


def long_blocked_in_position() -> Scenario:
    """Already open -> check_entry returns (False, None)."""
    ticker = "AAPL"
    px = 270.50
    or_h = 269.00
    pdc_v = 268.00
    return Scenario(
        name="long_blocked_in_position",
        description="Long entry blocked: ticker already in positions.",
        initial_state={
            "positions": {
                ticker: {
                    "shares": 30,
                    "entry_price": 269.00,
                    "stop": 267.00,
                    "initial_stop": 267.00,
                    "trail_active": False,
                    "trail_high": 269.00,
                    "entry_count": 1,
                    "entry_time": "09:00:00",
                    "entry_ts_utc": "2026-04-24T13:00:00+00:00",
                    "date": TODAY,
                    "pdc": pdc_v,
                },
            },
            "or_high": {ticker: or_h, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 265.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_count": {ticker: 1},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_h, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: in position"),
        ],
    )


def long_blocked_at_cap() -> Scenario:
    """Daily entry count = 5 -> blocked."""
    ticker = "AAPL"
    px = 270.50
    or_h = 269.00
    pdc_v = 268.00
    return Scenario(
        name="long_blocked_at_cap",
        description="Long entry blocked: daily cap (5/day) reached.",
        initial_state={
            "or_high": {ticker: or_h, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 265.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_count": {ticker: 5},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_h, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: at cap"),
        ],
    )


def long_blocked_polarity() -> Scenario:
    """current_price <= PDC -> blocked at polarity gate.

    All earlier gates pass (no in-position, daily count 0, OR data
    present, sane price). Two-bar break still passes; polarity fails.
    """
    ticker = "NVDA"
    or_h = 199.00
    pdc_v = 201.00       # PDC > current_price -> polarity fails
    px = 200.00
    return Scenario(
        name="long_blocked_polarity",
        description="Long blocked: current_price <= PDC (polarity fail).",
        initial_state={
            "or_high": {ticker: or_h, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 195.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_h, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: polarity"),
        ],
    )


def long_blocked_loss_limit() -> Scenario:
    """Today's realized P&L exceeds DAILY_LOSS_LIMIT.

    execute_entry calls _check_daily_loss_limit which sums today's
    SELL pnl from paper_trades and any COVER pnl from
    short_trade_history. Make total <= -500 so trading halts.
    """
    ticker = "TSLA"
    or_h = 220.00
    pdc_v = 219.00
    px = 220.50
    big_loss_trade = {
        "action": "SELL",
        "ticker": "MSFT",
        "price": 100.0,
        "shares": 50,
        "pnl": -600.00,
        "pnl_pct": -10.0,
        "reason": "STOP",
        "entry_price": 112.0,
        "time": "10:00 CDT",
        "date": TODAY,
    }
    return Scenario(
        name="long_blocked_loss_limit",
        description="Long execute halted by daily loss limit.",
        initial_state={
            "or_high": {ticker: or_h, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 215.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
            "paper_trades": [big_loss_trade],
            "paper_all_trades": [big_loss_trade],
        },
        initial_market=_common_market(ticker, px, or_h, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="execute_entry", args=(ticker, px),
                   label="halts: daily loss limit"),
        ],
    )


SCENARIOS = [
    long_clean_entry,
    long_blocked_in_position,
    long_blocked_at_cap,
    long_blocked_polarity,
    long_blocked_loss_limit,
]
