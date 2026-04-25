"""Short-close scenarios (5)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from synthetic_harness.scenarios import Scenario, Action
from synthetic_harness.market import (
    make_short_breakdown_frame,
)

ET = ZoneInfo("America/New_York")
TODAY = "2026-04-24"
NOW = datetime(2026, 4, 24, 11, 0, 0, tzinfo=ET)


def _open_short(ticker, entry_price, stop, *, trail_low=None,
                entry_count=1):
    return {
        "shares": 30,
        "entry_price": entry_price,
        "stop": stop,
        "initial_stop": stop,
        "trail_active": False,
        "trail_low": trail_low or entry_price,
        "entry_count": entry_count,
        "entry_time": "09:00:00",
        "entry_ts_utc": "2026-04-24T13:00:00+00:00",
        "date": TODAY,
        "pdc": entry_price + 1.0,
        "side": "SHORT",
        "trail_stop": None,
    }


def short_close_stop() -> Scenario:
    """STOP: short loss — covered above stop."""
    ticker = "AAPL"
    return Scenario(
        name="short_close_stop",
        description="Short close: STOP — cover at initial stop.",
        initial_state={
            "short_positions": {ticker: _open_short(
                ticker, 270.00, 272.00,
            )},
            "or_low": {ticker: 271.00},
            "pdc":    {ticker: 271.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_short_breakdown_frame(
                ticker, 272.00, 271.00, 271.00,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_short_position",
                   args=(ticker, 272.00, "STOP"),
                   label="cover STOP"),
        ],
    )


def short_close_trail() -> Scenario:
    """TRAIL: stop ratcheted below entry (profit locked)."""
    ticker = "NVDA"
    return Scenario(
        name="short_close_trail",
        description="Short close: TRAIL — stop ratcheted below entry.",
        initial_state={
            "short_positions": {ticker: _open_short(
                ticker, 200.00, 197.50, trail_low=195.00,
            )},
            "or_low": {ticker: 201.00},
            "pdc":    {ticker: 200.50},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_short_breakdown_frame(
                ticker, 197.50, 201.00, 200.50,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_short_position",
                   args=(ticker, 197.50, "TRAIL"),
                   label="cover TRAIL"),
        ],
    )


def short_close_eod() -> Scenario:
    ticker = "MSFT"
    return Scenario(
        name="short_close_eod",
        description="Short close: EOD reason at 15:55 ET.",
        initial_state={
            "short_positions": {ticker: _open_short(
                ticker, 380.00, 382.00,
            )},
            "or_low": {ticker: 381.00},
            "pdc":    {ticker: 381.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_short_breakdown_frame(
                ticker, 379.00, 381.00, 381.00,
            ),
        },
        initial_time=datetime(2026, 4, 24, 15, 55, 0, tzinfo=ET),
        actions=[
            Action(kind="close_short_position",
                   args=(ticker, 379.00, "EOD"),
                   label="cover EOD"),
        ],
    )


def short_close_hard_eject_tiger() -> Scenario:
    ticker = "TSLA"
    return Scenario(
        name="short_close_hard_eject_tiger",
        description="Short close: HARD_EJECT_TIGER (DI flipped).",
        initial_state={
            "short_positions": {ticker: _open_short(
                ticker, 250.00, 252.00,
            )},
            "or_low": {ticker: 251.00},
            "pdc":    {ticker: 250.50},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_short_breakdown_frame(
                ticker, 252.50, 251.00, 250.50,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_short_position",
                   args=(ticker, 252.50, "HARD_EJECT_TIGER"),
                   label="cover HARD_EJECT_TIGER"),
        ],
    )


def short_close_manual() -> Scenario:
    ticker = "META"
    return Scenario(
        name="short_close_manual",
        description="Short close: MANUAL (operator /forceclose).",
        initial_state={
            "short_positions": {ticker: _open_short(
                ticker, 525.00, 528.00,
            )},
            "or_low": {ticker: 526.00},
            "pdc":    {ticker: 525.50},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_short_breakdown_frame(
                ticker, 524.50, 526.00, 525.50,
            ),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_short_position",
                   args=(ticker, 524.50, "MANUAL"),
                   label="cover MANUAL"),
        ],
    )


SCENARIOS = [
    short_close_stop,
    short_close_trail,
    short_close_eod,
    short_close_hard_eject_tiger,
    short_close_manual,
]
