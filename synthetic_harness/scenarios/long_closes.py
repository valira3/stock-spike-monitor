"""Long-close scenarios (5)."""
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
NOW = datetime(2026, 4, 24, 11, 0, 0, tzinfo=ET)


def _open_long(ticker, entry_price, stop, *, trail_high=None,
               entry_count=1):
    return {
        "shares": 30,
        "entry_price": entry_price,
        "stop": stop,
        "initial_stop": stop,
        "trail_active": False,
        "trail_high": trail_high or entry_price,
        "entry_count": entry_count,
        "entry_time": "09:00:00",
        "entry_ts_utc": "2026-04-24T13:00:00+00:00",
        "date": TODAY,
        "pdc": entry_price - 1.0,
    }


def long_close_stop() -> Scenario:
    """STOP: simple loss exit."""
    ticker = "AAPL"
    return Scenario(
        name="long_close_stop",
        description="Long close: STOP — exit at initial stop.",
        initial_state={
            "positions": {ticker: _open_long(ticker, 270.00, 268.00)},
            "or_high": {ticker: 269.00},
            "pdc":     {ticker: 269.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(
                ticker, 268.00, 269.00, 269.00,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_position", args=(ticker, 268.00, "STOP"),
                   label="close STOP"),
        ],
    )


def long_close_trail() -> Scenario:
    """TRAIL: stop already ratcheted above entry."""
    ticker = "NVDA"
    return Scenario(
        name="long_close_trail",
        description="Long close: TRAIL — stop ratcheted above entry.",
        initial_state={
            "positions": {ticker: _open_long(
                ticker, 200.00, 202.50, trail_high=205.00,
            )},
            "or_high": {ticker: 199.00},
            "pdc":     {ticker: 199.50},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(
                ticker, 202.50, 199.00, 199.50,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_position", args=(ticker, 202.50, "TRAIL"),
                   label="close TRAIL"),
        ],
    )


def long_close_eod() -> Scenario:
    """EOD: time gate close."""
    ticker = "MSFT"
    return Scenario(
        name="long_close_eod",
        description="Long close: EOD reason at 15:55 ET.",
        initial_state={
            "positions": {ticker: _open_long(ticker, 380.00, 378.00)},
            "or_high": {ticker: 379.00},
            "pdc":     {ticker: 378.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(
                ticker, 381.00, 379.00, 378.00,
            ),
        },
        initial_time=datetime(2026, 4, 24, 15, 55, 0, tzinfo=ET),
        actions=[
            Action(kind="close_position", args=(ticker, 381.00, "EOD"),
                   label="close EOD"),
        ],
    )


def long_close_hard_eject_tiger() -> Scenario:
    """HARD_EJECT_TIGER reason."""
    ticker = "TSLA"
    return Scenario(
        name="long_close_hard_eject_tiger",
        description="Long close: HARD_EJECT_TIGER (DI flipped).",
        initial_state={
            "positions": {ticker: _open_long(ticker, 250.00, 248.00)},
            "or_high": {ticker: 249.00},
            "pdc":     {ticker: 249.50},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(
                ticker, 247.50, 249.00, 249.50,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_position",
                   args=(ticker, 247.50, "HARD_EJECT_TIGER"),
                   label="close HARD_EJECT_TIGER"),
        ],
    )


def long_close_manual() -> Scenario:
    """Manual close — /forceclose path."""
    ticker = "META"
    return Scenario(
        name="long_close_manual",
        description="Long close: MANUAL (operator /forceclose).",
        initial_state={
            "positions": {ticker: _open_long(ticker, 525.00, 522.00)},
            "or_high": {ticker: 524.00},
            "pdc":     {ticker: 524.50},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(
                ticker, 525.50, 524.00, 524.50,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_position",
                   args=(ticker, 525.50, "MANUAL"),
                   label="close MANUAL"),
        ],
    )


SCENARIOS = [
    long_close_stop,
    long_close_trail,
    long_close_eod,
    long_close_hard_eject_tiger,
    long_close_manual,
]
