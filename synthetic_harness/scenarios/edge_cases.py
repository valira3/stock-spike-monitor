"""Edge-case scenarios (v4.8.2).

These scenarios exercise narrower gate paths that the v4.8.1 corpus
left uncovered: cooldown windows, per-ticker pnl cap, OR-staleness,
volume gating, extension cap, sovereign regime, DI threshold, stop
cap, market-open clock, midnight rollover, ring-buffer eviction, and
trail-promotion threshold crossing.

All scenarios are deterministic and rely only on FrozenClock +
SyntheticMarket + the existing setup_callbacks hook. No production
code is modified.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from synthetic_harness.scenarios import Scenario, Action
from synthetic_harness.market import (
    make_long_breakout_frame,
    make_short_breakdown_frame,
    make_index_bull_frame,
    make_index_bear_frame,
    TickerFrame,
)

ET = ZoneInfo("America/New_York")
TODAY = "2026-04-24"
NOW = datetime(2026, 4, 24, 10, 30, 0, tzinfo=ET)


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------
def _bull_indices():
    return {
        "SPY": make_index_bull_frame("SPY", 510.00, 505.00),
        "QQQ": make_index_bull_frame("QQQ", 430.00, 425.00),
    }


def _bear_indices():
    return {
        "SPY": make_index_bear_frame("SPY", 500.00, 505.00),
        "QQQ": make_index_bear_frame("QQQ", 420.00, 425.00),
    }


def _common_long_state(ticker, or_h, pdc_v, or_l=None):
    return {
        "or_high": {ticker: or_h, "SPY": 504.00, "QQQ": 424.00},
        "or_low":  {ticker: or_l if or_l is not None else or_h - 4.0},
        "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
        "daily_entry_date": TODAY,
        "daily_short_entry_date": TODAY,
    }


def _common_short_state(ticker, or_l, pdc_v, or_h=None):
    return {
        "or_high": {ticker: or_h if or_h is not None else or_l + 4.0,
                    "SPY": 506.00, "QQQ": 426.00},
        "or_low":  {ticker: or_l, "SPY": 504.00, "QQQ": 424.00},
        "pdc":     {ticker: pdc_v, "SPY": 505.00, "QQQ": 425.00},
        "daily_entry_date": TODAY,
        "daily_short_entry_date": TODAY,
    }


# ------------------------------------------------------------------
# Cooldown
# ------------------------------------------------------------------
def edge_cooldown_blocks_reentry() -> Scenario:
    """Open + close a long; re-check 899s later \u2192 still in cooldown."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    return Scenario(
        name="edge_cooldown_blocks_reentry",
        description="Re-entry blocked by 15-min cooldown (T+899s).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="execute_entry", args=(ticker, px),
                   label="open long"),
            Action(kind="close_position", args=(ticker, px, "MANUAL"),
                   label="close (stamps cooldown)"),
            Action(kind="tick_seconds", args=(899,),
                   label="advance 899s"),
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: cooldown 1m left"),
        ],
    )


def edge_cooldown_releases_at_901s() -> Scenario:
    """Same setup, scan at T+901s passes the cooldown gate."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    return Scenario(
        name="edge_cooldown_releases_at_901s",
        description="Cooldown gate releases at T+901s (later gates may still block).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="execute_entry", args=(ticker, px),
                   label="open long"),
            Action(kind="close_position", args=(ticker, px, "MANUAL"),
                   label="close (stamps cooldown)"),
            Action(kind="tick_seconds", args=(901,),
                   label="advance 901s"),
            Action(kind="check_entry", args=(ticker,),
                   label="cooldown released; later gates rule"),
        ],
    )


# ------------------------------------------------------------------
# Per-ticker pnl cap
# ------------------------------------------------------------------
def edge_per_ticker_pnl_cap() -> Scenario:
    """Today's realized pnl on this ticker is -$60 \u2192 reject."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    losing_close = {
        "ticker": ticker,
        "side": "long",
        "action": "SELL",
        "shares": 30,
        "entry_price": 270.00,
        "exit_price": 268.00,
        "pnl": -60.0,
        "pnl_pct": -0.74,
        "reason": "STOP",
        "entry_time": "10:00 CDT",
        "exit_time": "10:05 CDT",
        "entry_time_iso": "2026-04-24T14:00:00+00:00",
        "exit_time_iso": "2026-04-24T14:05:00+00:00",
        "entry_num": 1,
        "date": TODAY,
    }
    return Scenario(
        name="edge_per_ticker_pnl_cap",
        description="Long blocked: ticker P&L today <= -$50.",
        initial_state={
            **_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
            "trade_history": [losing_close],
        },
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: per-ticker pnl cap"),
        ],
    )


# ------------------------------------------------------------------
# OR / data sanity
# ------------------------------------------------------------------
def edge_or_price_sane_reject() -> Scenario:
    """OR_High far from live price \u2192 OR-staleness reject."""
    ticker = "AAPL"
    or_h = 100.00       # live=150, drift > 5%
    pdc_v = 149.00
    px = 150.00
    return Scenario(
        name="edge_or_price_sane_reject",
        description="Long blocked: OR-staleness (OR vs live drift > 5%).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=99.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: OR stale"),
        ],
    )


def edge_bars_none_data_failure() -> Scenario:
    """fetch_1min_bars returns None \u2192 bail cleanly."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    return Scenario(
        name="edge_bars_none_data_failure",
        description="Long blocked: fetch_1min_bars returned None.",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            # ticker omitted: SyntheticMarket.fetch_1min_bars returns None
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="bail: bars None"),
        ],
    )


def edge_current_price_zero() -> Scenario:
    """current_price=0 \u2192 reject early."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    frame = make_long_breakout_frame(ticker, 270.00, or_h, pdc_v)
    # Force current_price to 0; clear FMP quote so override can't save it.
    frame.bars_1min["current_price"] = 0.0
    frame.quote = None
    return Scenario(
        name="edge_current_price_zero",
        description="Long blocked: current_price=0 (Yahoo bad quote).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: frame,
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="bail: price <= 0"),
        ],
    )


# ------------------------------------------------------------------
# Volume gating (TIGER_V2_REQUIRE_VOL=true)
# ------------------------------------------------------------------
def _set_require_vol_true(m, clock, market, recorder):
    m.TIGER_V2_REQUIRE_VOL = True


def _set_require_vol_false(m, clock, market, recorder):
    m.TIGER_V2_REQUIRE_VOL = False


def edge_volume_not_ready() -> Scenario:
    """TIGER_V2_REQUIRE_VOL=true but the closed bar volume is None \u2192 DATA NOT READY skip."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    frame = make_long_breakout_frame(ticker, px, or_h, pdc_v)
    # Wipe volumes on closed bars (indices -2..-6) so _entry_bar_volume
    # walks back through Nones \u2192 returns (0, False).
    vols = list(frame.bars_1min["volumes"])
    for i in range(2, min(2 + 5, len(vols) + 1)):
        if i <= len(vols):
            vols[-i] = None
    frame.bars_1min["volumes"] = vols
    return Scenario(
        name="edge_volume_not_ready",
        description="Long blocked: TIGER_V2_REQUIRE_VOL on, no closed-bar volume.",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: frame,
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: data not ready"),
        ],
        setup_callbacks=[_set_require_vol_true],
    )


def edge_volume_below_threshold() -> Scenario:
    """TIGER_V2_REQUIRE_VOL=true, entry-bar volume = 1.0\u00d7 avg \u2192 LOW VOL skip."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    # breakout_vol_ratio=1.0 makes the entry bar equal to avg.
    frame = make_long_breakout_frame(
        ticker, px, or_h, pdc_v, breakout_vol_ratio=1.0,
    )
    return Scenario(
        name="edge_volume_below_threshold",
        description="Long blocked: entry bar volume below 1.5\u00d7 average.",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: frame,
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: low vol"),
        ],
        setup_callbacks=[_set_require_vol_true],
    )


# ------------------------------------------------------------------
# Extension / stop-cap rejects
# ------------------------------------------------------------------
def edge_extension_max_pct() -> Scenario:
    """Price 5% above OR_High \u2192 EXTENDED reject."""
    ticker = "AAPL"
    or_h = 200.00
    pdc_v = 199.00
    px = 210.00            # 5% above or_h \u2192 stop-cap likely fires first
    return Scenario(
        name="edge_extension_max_pct",
        description="Long blocked: extension above OR_High beyond cap.",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=196.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: extended/stop-capped"),
        ],
    )


# ------------------------------------------------------------------
# Sovereign regime (index polarity at entry gate)
# ------------------------------------------------------------------
def edge_sovereign_long_eject() -> Scenario:
    """SPY current <= PDC: long index gate fails."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    return Scenario(
        name="edge_sovereign_long_eject",
        description="Long blocked: SPY <= PDC (sovereign long-eject).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            "SPY": make_index_bear_frame("SPY", 504.50, 505.00),
            "QQQ": make_index_bull_frame("QQQ", 430.00, 425.00),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: SPY index"),
        ],
    )


def edge_sovereign_short_eject() -> Scenario:
    """SPY+QQQ both > PDC \u2192 short index gate fails."""
    ticker = "AAPL"
    or_l = 270.00
    pdc_v = 270.50
    px = 269.50
    return Scenario(
        name="edge_sovereign_short_eject",
        description="Short blocked: SPY+QQQ both > PDC (short index gate).",
        initial_state=_common_short_state(ticker, or_l, pdc_v, or_h=273.00),
        initial_market={
            ticker: make_short_breakdown_frame(ticker, px, or_l, pdc_v),
            "SPY": make_index_bull_frame("SPY", 510.00, 505.00),
            "QQQ": make_index_bull_frame("QQQ", 430.00, 425.00),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_short_entry", args=(ticker,),
                   label="blocked: index polarity (long regime)"),
        ],
    )


# ------------------------------------------------------------------
# DI gate
# ------------------------------------------------------------------
def edge_di_below_threshold() -> Scenario:
    """DI+ = 15 < TIGER_V2_DI_THRESHOLD (25) \u2192 reject."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    frame = make_long_breakout_frame(
        ticker, px, or_h, pdc_v, di_plus=15.0, di_minus=10.0,
    )
    return Scenario(
        name="edge_di_below_threshold",
        description="Long blocked: DI+ below TIGER_V2_DI_THRESHOLD.",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: frame,
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: DI+ < 25"),
        ],
    )


def edge_di_none() -> Scenario:
    """DI is None (warmup) \u2192 reject."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    frame = make_long_breakout_frame(ticker, px, or_h, pdc_v)
    frame.di = (None, None)
    return Scenario(
        name="edge_di_none",
        description="Long blocked: DI warmup (DI+ is None).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: frame,
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: DI warmup"),
        ],
    )


# ------------------------------------------------------------------
# Stop cap reject
# ------------------------------------------------------------------
def edge_stop_cap_reject() -> Scenario:
    """OR baseline stop wider than 0.75% cap \u2192 reject under
    ENTRY_STOP_CAP_REJECT=1.

    or_h=200, entry=201.00. baseline = or_h - 0.90 = 199.10.
    floor = entry * (1-0.0075) = 199.4925. floor > baseline \u2192 capped
    \u2192 reject. Extension = 0.5% (< 1.5% cap), so the extension gate
    does not fire first.
    """
    ticker = "AAPL"
    or_h = 200.00
    pdc_v = 199.00
    px = 201.00
    return Scenario(
        name="edge_stop_cap_reject",
        description="Long blocked: stop-cap reject (baseline too loose).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=196.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: stop-cap"),
        ],
    )


# ------------------------------------------------------------------
# Pre-market / time gate
# ------------------------------------------------------------------
def edge_before_market_open() -> Scenario:
    """Clock at 09:25 ET \u2192 reject (timing gate)."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    early = datetime(2026, 4, 24, 9, 25, 0, tzinfo=ET)
    return Scenario(
        name="edge_before_market_open",
        description="Long blocked: clock 09:25 ET (before 09:35 cutoff).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=early,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="blocked: pre-market"),
        ],
    )


# ------------------------------------------------------------------
# Daily date reset
# ------------------------------------------------------------------
def edge_daily_date_reset() -> Scenario:
    """daily_entry_date is yesterday \u2192 counters reset on first
    check_entry today, fresh entry proceeds.
    """
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    state = _common_long_state(ticker, or_h, pdc_v, or_l=265.00)
    # Stale date and counter; the gate should clear them today.
    state["daily_entry_date"] = "2026-04-23"
    state["daily_short_entry_date"] = "2026-04-23"
    state["daily_entry_count"] = {ticker: 5}
    return Scenario(
        name="edge_daily_date_reset",
        description="New trading day rolls counter; check + execute proceed.",
        initial_state=state,
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="check_entry", args=(ticker,),
                   label="check passes (counter reset)"),
            Action(kind="execute_entry", args=(ticker, px),
                   label="entry recorded"),
        ],
    )


# ------------------------------------------------------------------
# execute_entry edge cases
# ------------------------------------------------------------------
def edge_shares_zero_high_price() -> Scenario:
    """current_price 0 \u2192 paper_shares_for returns 0 (the only path
    that yields 0). We force the early-bail by passing a non-positive
    price directly to execute_entry.
    """
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    return Scenario(
        name="edge_shares_zero_high_price",
        description="execute_entry bails when shares would be 0 (price <= 0).",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, 270.00, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="execute_entry", args=(ticker, 0.0),
                   label="invalid price \u2192 no entry"),
        ],
    )


def edge_insufficient_cash() -> Scenario:
    """paper_cash drained \u2192 cost > cash \u2192 reject."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    state = _common_long_state(ticker, or_h, pdc_v, or_l=265.00)
    state["paper_cash"] = 1.00      # nowhere near $10k slot cost
    return Scenario(
        name="edge_insufficient_cash",
        description="execute_entry bails when paper_cash < cost.",
        initial_state=state,
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="execute_entry", args=(ticker, px),
                   label="blocked: insufficient cash"),
        ],
    )


def edge_stop_capped_path() -> Scenario:
    """Entry recorded with the 0.75% capped stop.

    or_h=200, entry=201.00. baseline = 199.10, floor = 199.4925 (capped).
    With ENTRY_STOP_CAP_REJECT default ON, check_entry would reject; but
    execute_entry can be invoked directly on a polarity-clean ticker
    and will still record the capped stop. We bypass the check_entry
    rejection by calling execute_entry directly.
    """
    ticker = "AAPL"
    or_h = 200.00
    pdc_v = 199.00
    px = 201.00
    return Scenario(
        name="edge_stop_capped_path",
        description="execute_entry records stop snapped to 0.75% floor.",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=196.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="execute_entry", args=(ticker, px),
                   label="entry: stop capped to floor"),
        ],
    )


# ------------------------------------------------------------------
# close_position edge cases
# ------------------------------------------------------------------
def edge_idempotent_close_no_position() -> Scenario:
    """close_position on an unknown ticker \u2192 silent no-op."""
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    return Scenario(
        name="edge_idempotent_close_no_position",
        description="close_position no-op when ticker not in positions.",
        initial_state=_common_long_state(ticker, or_h, pdc_v, or_l=265.00),
        initial_market={
            ticker: make_long_breakout_frame(ticker, 270.00, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_position", args=(ticker, 270.00, "MANUAL"),
                   label="no-op: not in positions"),
        ],
    )


def _seed_500_trades(m, clock, market, recorder):
    """Pre-load 500 closed-trade dicts into trade_history to test ring-buffer."""
    seed = []
    for i in range(500):
        seed.append({
            "ticker": "FILL%03d" % i,
            "side": "long",
            "action": "SELL",
            "shares": 10,
            "entry_price": 100.0,
            "exit_price": 100.0,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "reason": "MANUAL",
            "entry_time": "09:00 CDT",
            "exit_time": "09:30 CDT",
            "entry_time_iso": "2026-04-23T13:00:00+00:00",
            "exit_time_iso": "2026-04-23T13:30:00+00:00",
            "entry_num": 1,
            "date": "2026-04-23",
        })
    m.trade_history.clear()
    m.trade_history.extend(seed)


def edge_trade_history_ring_buffer() -> Scenario:
    """500 prior trades + one new close \u2192 oldest evicted, len stays 500."""
    ticker = "AAPL"
    pos = {
        "shares": 30,
        "entry_price": 270.00,
        "stop": 268.00,
        "initial_stop": 268.00,
        "trail_active": False,
        "trail_high": 270.00,
        "entry_count": 1,
        "entry_time": "09:00:00",
        "entry_ts_utc": "2026-04-24T13:00:00+00:00",
        "date": TODAY,
        "pdc": 269.00,
    }
    return Scenario(
        name="edge_trade_history_ring_buffer",
        description="trade_history capped at 500: oldest evicted on close.",
        initial_state={
            "positions": {ticker: pos},
            "or_high": {ticker: 269.00},
            "or_low":  {ticker: 265.00},
            "pdc":     {ticker: 269.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(ticker, 270.50, 269.00, 269.00),
        },
        initial_time=NOW,
        setup_callbacks=[_seed_500_trades],
        actions=[
            Action(kind="close_position", args=(ticker, 270.50, "MANUAL"),
                   label="close \u2192 ring buffer evicts oldest"),
        ],
    )


def edge_retro_cap_close() -> Scenario:
    """Close path with reason=RETRO_CAP."""
    ticker = "AAPL"
    pos = {
        "shares": 30,
        "entry_price": 270.00,
        "stop": 268.00,
        "initial_stop": 268.00,
        "trail_active": False,
        "trail_high": 270.50,
        "entry_count": 1,
        "entry_time": "09:00:00",
        "entry_ts_utc": "2026-04-24T13:00:00+00:00",
        "date": TODAY,
        "pdc": 269.00,
    }
    return Scenario(
        name="edge_retro_cap_close",
        description="Long close: RETRO_CAP reason path.",
        initial_state={
            "positions": {ticker: pos},
            "or_high": {ticker: 269.00},
            "or_low":  {ticker: 265.00},
            "pdc":     {ticker: 269.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
        },
        initial_market={
            ticker: make_long_breakout_frame(ticker, 268.10, 269.00, 269.00),
        },
        initial_time=NOW,
        actions=[
            Action(kind="close_position",
                   args=(ticker, 268.10, "RETRO_CAP"),
                   label="close RETRO_CAP"),
        ],
    )


# ------------------------------------------------------------------
# Multi-action: midnight rollover & isolated counter resets
# ------------------------------------------------------------------
def _seed_5_long_entries(m, clock, market, recorder):
    m.daily_entry_count[ "AAPL"] = 5
    m.daily_entry_date = TODAY


def edge_midnight_rollover() -> Scenario:
    """End-of-day with 5 longs counted; advance clock to next-day 09:36
    \u2192 daily_entry_count resets on first check_entry, fresh entry counts as 1.
    """
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    state = _common_long_state(ticker, or_h, pdc_v, or_l=265.00)
    state["daily_entry_count"] = {ticker: 5}
    return Scenario(
        name="edge_midnight_rollover",
        description="Cap 5/day clears at next-day 09:36; new entry = 1/day.",
        initial_state=state,
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=datetime(2026, 4, 24, 15, 30, 0, tzinfo=ET),
        actions=[
            Action(kind="tick_minutes", args=(18 * 60 + 6,),
                   label="advance to 2026-04-25 09:36 ET"),
            Action(kind="check_entry", args=(ticker,),
                   label="check passes (counter reset)"),
            Action(kind="execute_entry", args=(ticker, px),
                   label="fresh entry: 1/day"),
        ],
    )


def edge_short_count_isolated_reset() -> Scenario:
    """Long count and short count reset independently across midnight
    rollover (Stage A fix verification).
    """
    ticker = "AAPL"
    or_h = 269.00
    pdc_v = 267.50
    px = 270.00
    state = _common_long_state(ticker, or_h, pdc_v, or_l=265.00)
    state["daily_entry_count"] = {ticker: 5}
    state["daily_short_entry_count"] = {"NVDA": 3}
    return Scenario(
        name="edge_short_count_isolated_reset",
        description="Long check resets long counter; short counter untouched.",
        initial_state=state,
        initial_market={
            ticker: make_long_breakout_frame(ticker, px, or_h, pdc_v),
            **_bull_indices(),
        },
        initial_time=datetime(2026, 4, 24, 15, 30, 0, tzinfo=ET),
        actions=[
            Action(kind="tick_minutes", args=(18 * 60 + 6,),
                   label="advance to next 09:36 ET"),
            Action(kind="check_entry", args=(ticker,),
                   label="long counter reset; short counter unchanged"),
        ],
    )


def edge_trail_promotion_threshold() -> Scenario:
    """Long position; price crosses the +1% peak \u2192 trail_active flips True."""
    ticker = "AAPL"
    pos = {
        "shares": 30,
        "entry_price": 100.00,
        "stop": 99.50,
        "initial_stop": 99.50,
        "trail_active": False,
        "trail_high": 100.00,
        "entry_count": 1,
        "entry_time": "09:00:00",
        "entry_ts_utc": "2026-04-24T13:00:00+00:00",
        "date": TODAY,
        "pdc": 99.00,
    }
    return Scenario(
        name="edge_trail_promotion_threshold",
        description="Trail arms exactly when peak crosses +1% (entry=$100, peak=$101).",
        initial_state={
            "positions": {ticker: pos},
            "or_high": {ticker: 99.50, "SPY": 504.00, "QQQ": 424.00},
            "or_low":  {ticker: 98.00, "SPY": 503.00, "QQQ": 423.00},
            "pdc":     {ticker: 99.00, "SPY": 505.00, "QQQ": 425.00},
            "daily_entry_date": TODAY,
            "daily_short_entry_date": TODAY,
            "daily_entry_count": {ticker: 1},
        },
        initial_market={
            ticker: make_long_breakout_frame(ticker, 100.50, 99.50, 99.00),
            **_bull_indices(),
        },
        initial_time=datetime(2026, 4, 24, 11, 0, 0, tzinfo=ET),
        actions=[
            Action(kind="manage_positions", args=(),
                   label="below threshold: trail still off"),
            Action(kind="set_price", args=(ticker, 101.00),
                   label="advance price to +1.0%"),
            Action(kind="manage_positions", args=(),
                   label="threshold crossed: trail arms"),
        ],
    )


SCENARIOS = [
    edge_cooldown_blocks_reentry,
    edge_cooldown_releases_at_901s,
    edge_per_ticker_pnl_cap,
    edge_or_price_sane_reject,
    edge_bars_none_data_failure,
    edge_current_price_zero,
    edge_volume_not_ready,
    edge_volume_below_threshold,
    edge_extension_max_pct,
    edge_sovereign_long_eject,
    edge_sovereign_short_eject,
    edge_di_below_threshold,
    edge_di_none,
    edge_stop_cap_reject,
    edge_before_market_open,
    edge_daily_date_reset,
    edge_shares_zero_high_price,
    edge_insufficient_cash,
    edge_stop_capped_path,
    edge_idempotent_close_no_position,
    edge_trade_history_ring_buffer,
    edge_retro_cap_close,
    edge_midnight_rollover,
    edge_short_count_isolated_reset,
    edge_trail_promotion_threshold,
]
