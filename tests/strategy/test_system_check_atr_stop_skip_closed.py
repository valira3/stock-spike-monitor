"""Monitor atr_stop check must skip CLOSED positions.

2026-05-19: Val TSLA SHORT entered at 10:11 ET, exited at 14:22 ET.
The entry row in /api/trade_log carried a tight OR-edge stop ($1.19
distance vs ATR(14)x1.75=$5.04). The monitor's `atr_stop` check fired
the WARN at every subsequent 5-min poll for hours -- through end of
RTH and beyond -- because the entry row persists in the trade log.

This patch filters the atr_stop loop to positions still OPEN across
Main + Val + Gene. Closed positions are historical; nothing actionable
once flat.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tools.system_check_bot import checks_market_validation


ET = ZoneInfo("America/New_York")


def _today_et() -> str:
    return datetime.now(ET).date().isoformat()


def _row(ticker, side, entry, stop, *, entry_time="10:11:00"):
    return {
        "ticker": ticker,
        "side": side,
        "entry_price": entry,
        "entry_stop": stop,
        "entry_time": entry_time,
        "date": _today_et(),
    }


def _ticker_bars(n=20, base=400.0, step=0.5):
    """Build n synthetic 1m bars starting 10:00 UTC (=06:00 ET)."""
    out = []
    for i in range(n):
        h, m = 10 + (i // 60), i % 60
        out.append({
            "t": f"2026-05-19T{h:02d}:{m:02d}:00+00:00",
            "h": base + step,
            "l": base - step,
            "c": base,
        })
    return out


def _raw(trade_rows, *, main_long=None, main_short=None, val_positions=None):
    return {
        "/api/state": {
            "positions": {tk: {} for tk in (main_long or [])},
            "short_positions": {tk: {} for tk in (main_short or [])},
        },
        "/api/executor/val": {"positions": val_positions or []},
        "/api/executor/gene": {"positions": []},
        "/api/trade_log?limit=5000": {"rows": trade_rows},
    }


def _market_data(bars_by_ticker):
    return {
        "bars": bars_by_ticker,
        "fills": [],
    }


def test_atr_stop_skips_closed_position():
    """The Val TSLA case: entry row in trade_log, no open position. Must NOT fire WARN."""
    rows = [_row("TSLA", "SHORT", entry=397.79, stop=396.60)]
    raw = _raw(rows, main_long=[], main_short=[], val_positions=[])
    md = _market_data({"TSLA": _ticker_bars()})
    checks = checks_market_validation(raw, md)
    atr_checks = [c for c in checks if c.name == "atr_stop"]
    # When all candidate rows are skipped (none open), we get NO atr_stop
    # check entry (no OK, no WARN) -- the loop produces no output.
    assert not [c for c in atr_checks if c.status == "WARN"], (
        f"atr_stop fired WARN on closed historical position: {atr_checks}"
    )


def test_atr_stop_fires_for_currently_open_main_long():
    """Position still open on Main long => check runs and fires."""
    # AAPL is open; tight stop relative to ATR should WARN.
    rows = [_row("AAPL", "LONG", entry=200.0, stop=199.95)]
    raw = _raw(rows, main_long=["AAPL"], main_short=[], val_positions=[])
    md = _market_data({"AAPL": _ticker_bars(base=200.0, step=2.0)})
    checks = checks_market_validation(raw, md)
    atr_checks = [c for c in checks if c.name == "atr_stop"]
    # At minimum the check ran; whether it WARNs depends on ATR vs stop math,
    # but the row was NOT silently skipped.
    assert atr_checks, "atr_stop check should run for currently-open position"


def test_atr_stop_fires_for_currently_open_val_short():
    """Val executor position open => check runs."""
    rows = [_row("TSLA", "SHORT", entry=400.0, stop=399.50)]
    raw = _raw(
        rows,
        val_positions=[{"symbol": "TSLA", "side": "SHORT", "qty": 50}],
    )
    md = _market_data({"TSLA": _ticker_bars(base=400.0, step=3.0)})
    checks = checks_market_validation(raw, md)
    atr_checks = [c for c in checks if c.name == "atr_stop"]
    assert atr_checks, "atr_stop check should run for Val open position"


def test_atr_stop_silent_when_bot_flat():
    """Empty everything => no atr_stop output, but `no_trades` INFO if empty rows.
    With rows but all closed, no atr_stop entry at all."""
    rows = [
        _row("TSLA", "SHORT", entry=397.79, stop=396.60),
        _row("AVGO", "LONG", entry=411.19, stop=410.50),
        _row("ORCL", "LONG", entry=181.81, stop=181.40),
    ]
    raw = _raw(rows)  # nothing open
    md = _market_data({
        "TSLA": _ticker_bars(base=400.0, step=2.0),
        "AVGO": _ticker_bars(base=411.0, step=1.5),
        "ORCL": _ticker_bars(base=182.0, step=1.0),
    })
    checks = checks_market_validation(raw, md)
    warns = [c for c in checks if c.name == "atr_stop" and c.status == "WARN"]
    assert not warns, f"atr_stop must be silent when bot is flat: {warns}"
