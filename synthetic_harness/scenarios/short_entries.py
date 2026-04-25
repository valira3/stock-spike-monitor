"""Short-entry scenarios (5)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from synthetic_harness.scenarios import Scenario, Action
from synthetic_harness.market import (
    make_short_breakdown_frame,
    make_index_bear_frame,
)

ET = ZoneInfo("America/New_York")
TODAY = "2026-04-24"
NOW = datetime(2026, 4, 24, 10, 30, 0, tzinfo=ET)


def _common_market(ticker, current_price, or_l, pdc_v):
    return {
        ticker: make_short_breakdown_frame(
            ticker, current_price, or_l, pdc_v,
        ),
        "SPY": make_index_bear_frame("SPY", 500.00, 505.00),
        "QQQ": make_index_bear_frame("QQQ", 420.00, 425.00),
    }


def short_clean_entry() -> Scenario:
    """All gates pass — opens short."""
    ticker = "AAPL"
    # entry close to or_l so baseline (pdc + 0.90) stays tighter than
    # the 0.75% cap (entry * 1.0075), so ENTRY_STOP_CAP_REJECT
    # doesn't trigger.
    or_l = 270.00
    pdc_v = 270.50
    px = 269.50
    return Scenario(
        name="short_clean_entry",
        description="Short entry: all gates pass, short opens.",
        initial_state={
            "or_high": {ticker: 273.00, "SPY": 506.00, "QQQ": 426.00},
            "or_low":  {ticker: or_l, "SPY": 504.00, "QQQ": 424.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_l, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_short_entry", args=(ticker,),
                   label="check passes"),
            Action(kind="execute_short_entry", args=(ticker, px),
                   label="executes paper short"),
        ],
    )


def short_blocked_in_position() -> Scenario:
    """Already short -> blocked."""
    ticker = "AAPL"
    or_l = 271.00
    pdc_v = 272.00
    px = 270.50
    return Scenario(
        name="short_blocked_in_position",
        description="Short entry blocked: ticker already in short_positions.",
        initial_state={
            "short_positions": {
                ticker: {
                    "shares": 30,
                    "entry_price": 271.50,
                    "stop": 273.00,
                    "initial_stop": 273.00,
                    "trail_active": False,
                    "trail_low": 271.50,
                    "entry_count": 1,
                    "entry_time": "09:00:00",
                    "entry_ts_utc": "2026-04-24T13:00:00+00:00",
                    "date": TODAY,
                    "pdc": pdc_v,
                    "side": "SHORT",
                    "trail_stop": None,
                },
            },
            "or_high": {ticker: 273.00, "SPY": 506.00, "QQQ": 426.00},
            "or_low":  {ticker: or_l, "SPY": 504.00, "QQQ": 424.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_short_entry_count": {ticker: 1},
            "daily_short_entry_date": TODAY,
            "daily_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_l, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_short_entry", args=(ticker,),
                   label="blocked: in position"),
        ],
    )


def short_blocked_at_cap() -> Scenario:
    ticker = "NVDA"
    or_l = 199.00
    pdc_v = 200.00
    px = 198.50
    return Scenario(
        name="short_blocked_at_cap",
        description="Short entry blocked: daily short cap reached.",
        initial_state={
            "or_high": {ticker: 202.00, "SPY": 506.00, "QQQ": 426.00},
            "or_low":  {ticker: or_l, "SPY": 504.00, "QQQ": 424.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_short_entry_count": {ticker: 5},
            "daily_short_entry_date": TODAY,
            "daily_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_l, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_short_entry", args=(ticker,),
                   label="blocked: at cap"),
        ],
    )


def short_blocked_polarity() -> Scenario:
    """current_price > PDC -> blocked at polarity gate."""
    ticker = "AMD"
    or_l = 145.00
    pdc_v = 144.00      # PDC < current_price -> polarity fails
    px = 145.50
    return Scenario(
        name="short_blocked_polarity",
        description="Short blocked: current_price >= PDC (polarity fail).",
        initial_state={
            "or_high": {ticker: 148.00, "SPY": 506.00, "QQQ": 426.00},
            "or_low":  {ticker: or_l, "SPY": 504.00, "QQQ": 424.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_short_entry_date": TODAY,
            "daily_entry_date": TODAY,
        },
        initial_market=_common_market(ticker, px, or_l, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="check_short_entry", args=(ticker,),
                   label="blocked: polarity"),
        ],
    )


def short_blocked_loss_limit() -> Scenario:
    """Stage A bug fix: shorts now also respect daily loss limit."""
    ticker = "META"
    or_l = 525.00
    pdc_v = 526.00
    px = 524.50
    big_loss_short = {
        "action": "COVER",
        "ticker": "MSFT",
        "shares": 50,
        "entry_price": 110.0,
        "exit_price": 122.0,
        "pnl": -600.00,
        "pnl_pct": -10.0,
        "reason": "STOP",
        "entry_time": "09:00 CDT",
        "exit_time": "10:00 CDT",
        "entry_time_iso": "2026-04-24T13:00:00+00:00",
        "exit_time_iso": "2026-04-24T14:00:00+00:00",
        "entry_num": 1,
        "date": TODAY,
        "side": "short",
    }
    return Scenario(
        name="short_blocked_loss_limit",
        description="Short execute halted by daily loss limit (Stage A fix).",
        initial_state={
            "or_high": {ticker: 528.00, "SPY": 506.00, "QQQ": 426.00},
            "or_low":  {ticker: or_l, "SPY": 504.00, "QQQ": 424.00},
            "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
            "daily_short_entry_date": TODAY,
            "daily_entry_date": TODAY,
            "short_trade_history": [big_loss_short],
        },
        initial_market=_common_market(ticker, px, or_l, pdc_v),
        initial_time=NOW,
        actions=[
            Action(kind="execute_short_entry", args=(ticker, px),
                   label="halts: daily loss limit"),
        ],
    )


SCENARIOS = [
    short_clean_entry,
    short_blocked_in_position,
    short_blocked_at_cap,
    short_blocked_polarity,
    short_blocked_loss_limit,
]
