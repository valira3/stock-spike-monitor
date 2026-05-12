"""v7.93.0 -- tests for tools.trade_replay rendering.

The fetch / login path is integration-only (would need to mock
DashboardClient over the network). The pure-function surface
covers everything we can assert without GHA:

  - _fmt_dollar           dollar formatting incl. negatives
  - _fmt_hold             seconds -> humanized duration
  - _r_multiple           R-multiple from row fields
  - _summary_stats        aggregation
  - render_markdown       end-to-end markdown shape
"""

from tools.trade_replay import (
    _fmt_dollar,
    _fmt_hold,
    _r_multiple,
    _summary_stats,
    render_markdown,
)


# ---------------------------------------------------------------------------
# Dollar formatting
# ---------------------------------------------------------------------------


def test_fmt_dollar_positive():
    assert _fmt_dollar(1234.5) == "$1,234.50"


def test_fmt_dollar_negative_uses_unicode_minus():
    # The renderer uses U+2212 minus, not ASCII hyphen, to align
    # with the rest of the dashboard's dollar formatters.
    assert _fmt_dollar(-42.1) == "−$42.10"


def test_fmt_dollar_none_returns_em_dash():
    assert _fmt_dollar(None) == "—"


def test_fmt_dollar_garbage_returns_em_dash():
    assert _fmt_dollar("not a number") == "—"


# ---------------------------------------------------------------------------
# Hold formatting
# ---------------------------------------------------------------------------


def test_fmt_hold_seconds_only():
    assert _fmt_hold(45) == "45s"


def test_fmt_hold_minutes_and_seconds():
    assert _fmt_hold(125) == "2m05s"


def test_fmt_hold_hours_minutes():
    assert _fmt_hold(3725) == "1h02m"


def test_fmt_hold_none():
    assert _fmt_hold(None) == "—"


# ---------------------------------------------------------------------------
# R-multiple
# ---------------------------------------------------------------------------


def test_r_multiple_long_winner():
    # entry=100, hard_stop=99, exit=102. R = (102-100)/(100-99) = 2.0
    row = {
        "side": "LONG",
        "entry_price": 100.0,
        "exit_price": 102.0,
        "hard_stop_at_exit": 99.0,
    }
    assert _r_multiple(row) == 2.0


def test_r_multiple_short_winner():
    # entry=100, hard_stop=101, exit=98. R = (100-98)/(101-100) = 2.0
    row = {
        "side": "SHORT",
        "entry_price": 100.0,
        "exit_price": 98.0,
        "hard_stop_at_exit": 101.0,
    }
    assert _r_multiple(row) == 2.0


def test_r_multiple_long_loser():
    # entry=100, hard_stop=99, exit=99.5. R = (99.5-100)/(100-99) = -0.5
    row = {
        "side": "LONG",
        "entry_price": 100.0,
        "exit_price": 99.5,
        "hard_stop_at_exit": 99.0,
    }
    assert _r_multiple(row) == -0.5


def test_r_multiple_falls_back_to_effective_stop():
    """v7.102.0: when hard_stop_at_exit is missing (legacy row from
    before _trade_log_snapshot_pos was added), fall back to
    effective_stop_at_exit so the metric still computes something."""
    row = {
        "side": "LONG",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "effective_stop_at_exit": 95.0,
    }
    assert _r_multiple(row) == 1.0


def test_r_multiple_missing_stop_returns_none():
    row = {"side": "LONG", "entry_price": 100.0, "exit_price": 105.0}
    assert _r_multiple(row) is None


def test_r_multiple_zero_risk_returns_none():
    # entry == hard_stop -> division by zero -> None
    row = {
        "side": "LONG",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "hard_stop_at_exit": 100.0,
    }
    assert _r_multiple(row) is None


def test_r_multiple_trail_past_breakeven_uses_original_risk():
    """v7.102.0 regression: NFLX SHORT on 2026-05-11 showed -1.30R
    despite a $244 winner because effective_stop_at_exit had
    trailed past breakeven (below entry on a SHORT), inverting the
    denominator sign. With the v7.102.0 fix, we use hard_stop_at_exit
    (original stop = 86.0 above entry for a SHORT) so R stays
    positive for the profitable trade.
    """
    row = {
        "side": "SHORT",
        "entry_price": 85.43,
        "exit_price": 85.15,
        "hard_stop_at_exit": 86.00,           # original protective stop
        "effective_stop_at_exit": 85.15,      # trail has moved to exit price
    }
    r = _r_multiple(row)
    assert r is not None
    assert r > 0  # winner -> positive R, not negative
    # (entry-exit)/(stop-entry) = (85.43-85.15)/(86.00-85.43) = 0.28/0.57 ≈ 0.491
    assert abs(r - (0.28 / 0.57)) < 1e-6


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------


def test_summary_stats_empty_list():
    stats = _summary_stats([])
    assert stats["trades"] == 0
    assert stats["total_pnl"] == 0
    assert stats["wins"] == 0
    assert stats["losses"] == 0
    assert stats["win_rate"] is None
    assert stats["avg_r"] is None


def test_summary_stats_two_wins_one_loss():
    rows = [
        {"side": "LONG", "entry_price": 100, "exit_price": 102, "pnl": 200,
         "effective_stop_at_exit": 99},
        {"side": "LONG", "entry_price": 50, "exit_price": 49, "pnl": -100,
         "effective_stop_at_exit": 48},
        {"side": "SHORT", "entry_price": 30, "exit_price": 28, "pnl": 200,
         "effective_stop_at_exit": 31},
    ]
    stats = _summary_stats(rows)
    assert stats["trades"] == 3
    assert stats["total_pnl"] == 300
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert abs(stats["win_rate"] - (2 / 3)) < 1e-9
    # R values: long winner 2.0, long loser -0.5, short winner 2.0 -> avg 1.166...
    assert stats["avg_r"] is not None
    assert abs(stats["avg_r"] - (3.5 / 3)) < 1e-6
    assert stats["biggest_win"] == 200
    assert stats["biggest_loss"] == -100


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def test_render_markdown_includes_summary_and_per_trade_sections():
    rows = [
        {
            "ticker": "AAPL", "side": "LONG", "shares": 10,
            "entry_price": 100, "exit_price": 105, "pnl": 50, "pnl_pct": 5.0,
            "hold_seconds": 180, "reason": "TARGET",
            "effective_stop_at_exit": 99,
        },
    ]
    md = render_markdown(rows, since="2026-05-11")
    assert "# Trade replay" in md
    assert "2026-05-11" in md
    assert "## Summary" in md
    assert "## Per-trade detail" in md
    assert "AAPL" in md
    # Total P&L row should be present and positive
    assert "$50.00" in md
    # R-multiple = (105-100)/(100-99) = 5.0
    assert "+5.00R" in md


def test_render_markdown_omits_open_rows_from_summary():
    """Open rows (no exit_price) skipped in per-trade table; summary
    stats only count closes."""
    rows = [
        {"ticker": "AAPL", "side": "LONG", "shares": 1, "entry_price": 100},
        {
            "ticker": "TSLA", "side": "LONG", "shares": 1,
            "entry_price": 200, "exit_price": 210, "pnl": 10,
            "effective_stop_at_exit": 199,
        },
    ]
    md = render_markdown(rows, since="2026-05-11")
    # AAPL is open -> skipped in per-trade table
    assert "AAPL" not in md
    # TSLA is closed -> appears
    assert "TSLA" in md
    # Total P&L only counts TSLA's 10
    assert "$10.00" in md


def test_render_markdown_log_slice_embedded_when_provided():
    md = render_markdown(
        [],
        since="2026-05-11",
        log_slice="2026-05-11T13:30:00Z [SIGNAL-BUS-EMIT] kind=ENTRY_LONG ticker=NVDA",
    )
    assert "Railway log slice" in md
    assert "SIGNAL-BUS-EMIT" in md


def test_render_markdown_log_slice_absent_when_none():
    md = render_markdown([], since="2026-05-11", log_slice=None)
    assert "Railway log slice" not in md


# v7.96.0 -- timestamp in header ensures re-runs produce unique
# markdown bodies even when the underlying trade data hasn't
# changed, so the trade-replay-archive branch's no-diff-skip
# logic doesn't make it look like the workflow never ran.
def test_render_markdown_explicit_timestamp_appears_in_header():
    md = render_markdown([], since="2026-05-11",
                        generated_at="2026-05-11 18:42:00 ET")
    assert "Generated at 2026-05-11 18:42:00 ET" in md


def test_render_markdown_default_timestamp_is_not_empty():
    md = render_markdown([], since="2026-05-11")
    # Default branch produces a real ET timestamp string; just check
    # the prefix is present and not None-stringified.
    assert "Generated at" in md
    assert "Generated at None" not in md
