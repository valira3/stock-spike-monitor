"""Multi-tick scan_loop scenarios (5).

These exercise scan_loop() and manage_positions() / manage_short_positions()
with the FrozenClock advancing across multiple steps. They are the
flagship "integration" tests in the corpus.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from synthetic_harness.scenarios import Scenario, Action
from synthetic_harness.market import (
    make_long_breakout_frame,
    make_short_breakdown_frame,
    make_index_bull_frame,
    make_index_bear_frame,
)

ET = ZoneInfo("America/New_York")
TODAY = "2026-04-24"
MID = datetime(2026, 4, 24, 11, 0, 0, tzinfo=ET)


def loop_full_cycle() -> Scenario:
    """One scan cycle with two open longs and one open short — exercises
    manage_positions, manage_short_positions, _tiger_hard_eject_check.
    No new entries (TRADE_TICKERS pruning is module-level).
    """
    return Scenario(
        name="loop_full_cycle",
        description="One full scan_loop cycle with mixed open positions.",
        initial_state={
            "positions": {
                "AAPL": {
                    "shares": 30, "entry_price": 270.00, "stop": 268.00,
                    "initial_stop": 268.00, "trail_active": False,
                    "trail_high": 270.50, "entry_count": 1,
                    "entry_time": "09:00:00",
                    "entry_ts_utc": "2026-04-24T13:00:00+00:00",
                    "date": TODAY, "pdc": 269.00,
                },
                "NVDA": {
                    "shares": 50, "entry_price": 200.00, "stop": 198.00,
                    "initial_stop": 198.00, "trail_active": False,
                    "trail_high": 201.00, "entry_count": 1,
                    "entry_time": "09:00:00",
                    "entry_ts_utc": "2026-04-24T13:00:00+00:00",
                    "date": TODAY, "pdc": 199.00,
                },
            },
            "short_positions": {
                "TSLA": {
                    "shares": 40, "entry_price": 250.00, "stop": 252.00,
                    "initial_stop": 252.00, "trail_active": False,
                    "trail_low": 249.50, "entry_count": 1,
                    "entry_time": "09:00:00",
                    "entry_ts_utc": "2026-04-24T13:00:00+00:00",
                    "date": TODAY, "pdc": 251.00,
                    "side": "SHORT", "trail_stop": None,
                },
            },
            "or_high": {
                "AAPL": 269.00, "NVDA": 199.00, "TSLA": 252.00,
                "SPY": 504.00, "QQQ": 424.00,
            },
            "or_low": {
                "AAPL": 265.00, "NVDA": 195.00, "TSLA": 248.00,
                "SPY": 503.00, "QQQ": 423.00,
            },
            "pdc": {
                "AAPL": 269.00, "NVDA": 199.00, "TSLA": 251.00,
                "SPY": 505.00, "QQQ": 425.00,
            },
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
            "daily_entry_count": {"AAPL": 1, "NVDA": 1},
            "daily_short_entry_count": {"TSLA": 1},
        },
        initial_market={
            "AAPL": make_long_breakout_frame("AAPL", 271.00, 269.00, 269.00),
            "NVDA": make_long_breakout_frame("NVDA", 201.00, 199.00, 199.00),
            "TSLA": make_short_breakdown_frame("TSLA", 249.50, 252.00, 251.00),
            "SPY":  make_index_bull_frame("SPY", 510.00, 505.00),
            "QQQ":  make_index_bull_frame("QQQ", 430.00, 425.00),
        },
        initial_time=MID,
        actions=[
            Action(kind="manage_positions", args=(),
                   label="manage longs (no exits)"),
            Action(kind="manage_short_positions", args=(),
                   label="manage shorts (no exits)"),
        ],
    )


def loop_trail_promotion() -> Scenario:
    """Multi-tick: a long position hits +1% peak then more, trail arms."""
    ticker = "AAPL"
    pos = {
        "shares": 30, "entry_price": 270.00, "stop": 268.00,
        "initial_stop": 268.00, "trail_active": False,
        "trail_high": 270.00, "entry_count": 1,
        "entry_time": "09:00:00",
        "entry_ts_utc": "2026-04-24T13:00:00+00:00",
        "date": TODAY, "pdc": 269.00,
    }
    return Scenario(
        name="loop_trail_promotion",
        description="Multi-tick: long peak rises, trail arms across ticks.",
        initial_state={
            "positions": {ticker: pos},
            "or_high": {ticker: 269.00, "SPY": 504.00, "QQQ": 424.00},
            "pdc":     {ticker: 269.00, "SPY": 505.00, "QQQ": 425.00},
            "or_low":  {ticker: 265.00, "SPY": 503.00, "QQQ": 423.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
            "daily_entry_count": {ticker: 1},
        },
        initial_market={
            ticker: make_long_breakout_frame(ticker, 271.00, 269.00, 269.00),
            "SPY":  make_index_bull_frame("SPY", 510.00, 505.00),
            "QQQ":  make_index_bull_frame("QQQ", 430.00, 425.00),
        },
        initial_time=MID,
        actions=[
            Action(kind="manage_positions", args=(), label="tick 1"),
            Action(kind="set_price", args=(ticker, 273.50),
                   label="advance price +1.3%"),
            Action(kind="manage_positions", args=(),
                   label="tick 2 (trail arms)"),
            Action(kind="set_price", args=(ticker, 275.00),
                   label="advance price +1.85%"),
            Action(kind="manage_positions", args=(),
                   label="tick 3 (peak higher)"),
        ],
    )


def loop_eod_cleanup() -> Scenario:
    """eod_close at 15:55 ET — flatten all open positions."""
    return Scenario(
        name="loop_eod_cleanup",
        description="eod_close: long + short positions flatten at 15:55.",
        initial_state={
            "positions": {
                "AAPL": {
                    "shares": 30, "entry_price": 270.00, "stop": 268.00,
                    "initial_stop": 268.00, "trail_active": False,
                    "trail_high": 270.50, "entry_count": 1,
                    "entry_time": "09:00:00",
                    "entry_ts_utc": "2026-04-24T13:00:00+00:00",
                    "date": TODAY, "pdc": 269.00,
                },
            },
            "short_positions": {
                "TSLA": {
                    "shares": 40, "entry_price": 250.00, "stop": 252.00,
                    "initial_stop": 252.00, "trail_active": False,
                    "trail_low": 249.50, "entry_count": 1,
                    "entry_time": "09:00:00",
                    "entry_ts_utc": "2026-04-24T13:00:00+00:00",
                    "date": TODAY, "pdc": 251.00,
                    "side": "SHORT", "trail_stop": None,
                },
            },
            "or_high": {"AAPL": 269.00, "TSLA": 252.00,
                        "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {"AAPL": 265.00, "TSLA": 248.00,
                        "SPY": 503.00, "QQQ": 423.00},
            "pdc":     {"AAPL": 269.00, "TSLA": 251.00,
                        "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
            "daily_entry_count": {"AAPL": 1},
            "daily_short_entry_count": {"TSLA": 1},
        },
        initial_market={
            "AAPL": make_long_breakout_frame("AAPL", 271.00, 269.00, 269.00),
            "TSLA": make_short_breakdown_frame("TSLA", 249.50, 252.00, 251.00),
            "SPY":  make_index_bull_frame("SPY", 510.00, 505.00),
            "QQQ":  make_index_bull_frame("QQQ", 430.00, 425.00),
        },
        initial_time=datetime(2026, 4, 24, 15, 55, 0, tzinfo=ET),
        actions=[
            Action(kind="eod_close", args=(),
                   label="EOD flatten all"),
        ],
    )


def loop_halted_trading() -> Scenario:
    """_trading_halted=True — no new entries; close paths still work."""
    ticker = "AAPL"
    return Scenario(
        name="loop_halted_trading",
        description="Trading halted: check_entry/check_short_entry refuse.",
        initial_state={
            "or_high": {ticker: 269.00, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 265.00, "SPY": 503.00, "QQQ": 423.00},
            "pdc":     {ticker: 269.00, "SPY": 505.00, "QQQ": 425.00},
            "_trading_halted": True,
            "_trading_halted_reason": "Daily loss limit hit: $-501.00",
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(ticker, 271.00, 269.00, 269.00),
            "SPY":  make_index_bull_frame("SPY", 510.00, 505.00),
            "QQQ":  make_index_bull_frame("QQQ", 430.00, 425.00),
        },
        initial_time=MID,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="halt: long check refused"),
            Action(kind="check_short_entry", args=(ticker,),
                   label="halt: short check refused"),
        ],
    )


def loop_scan_paused() -> Scenario:
    """_scan_paused=True — no new entries via check_entry."""
    ticker = "AAPL"
    return Scenario(
        name="loop_scan_paused",
        description="Scan paused: check_entry returns (False, None).",
        initial_state={
            "or_high": {ticker: 269.00, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 265.00, "SPY": 503.00, "QQQ": 423.00},
            "pdc":     {ticker: 269.00, "SPY": 505.00, "QQQ": 425.00},
            "_scan_paused": True,
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(ticker, 271.00, 269.00, 269.00),
            "SPY":  make_index_bull_frame("SPY", 510.00, 505.00),
            "QQQ":  make_index_bull_frame("QQQ", 430.00, 425.00),
        },
        initial_time=MID,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="paused: long check refused"),
            Action(kind="check_short_entry", args=(ticker,),
                   label="paused: short check refused"),
        ],
    )


SCENARIOS = [
    loop_full_cycle,
    loop_trail_promotion,
    loop_eod_cleanup,
    loop_halted_trading,
    loop_scan_paused,
]
