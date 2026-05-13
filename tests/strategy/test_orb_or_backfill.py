"""v8.3.0 -- OrbEngine.backfill_or_windows tests.

Covers automatic OR-window backfill: after a Railway redeploy mid-RTH
the in-memory OR windows are empty, but a 1m-bar replay rebuilds them
idempotently so the rest of the trading day isn't lost.
"""
from __future__ import annotations

import pytest

from orb.engine import OrbConfig, OrbEngine


def _cfg():
    return OrbConfig(
        or_minutes=30,
        skip_earnings_window=False,
        fail_closed_on_missing_vix=False,
        ticker_side_blocklist=None,
    )


def _bars_30(*, high_at=580, low_at=585, or_high=101.0, or_low=99.0):
    """30 1m bars covering buckets 570..599 (09:30..09:59 ET).

    Returns list[(bucket, high, low, open, close, volume)].
    """
    rows = []
    for m in range(570, 600):
        h = or_high if m == high_at else or_high - 0.5
        lo = or_low if m == low_at else or_low + 0.5
        rows.append((m, h, lo, 100.0, 100.0, 10000.0))
    return rows


def _start_session(eng: OrbEngine, tickers):
    eng.start_new_session(
        date_iso="2026-05-12",
        tickers=list(tickers),
        vix_close_d1=18.0,
        ticker_open_today={t: 100.0 for t in tickers},
        ticker_prev_close={t: 100.0 for t in tickers},
        equity_per_portfolio={"main": 100_000.0},
    )


class TestBackfillRebuildsOrWindow:

    def test_rebuilds_or_window_from_30_bars(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        # Pre-condition: OR window empty / unlocked
        w_pre = eng._state.or_windows.get("AAPL")
        assert w_pre is None or not w_pre.locked
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": _bars_30()},
            current_et_minutes=11 * 60,  # 11:00 ET, well past or_end (10:00)
        )
        # Counter shape
        assert result["backfilled"] == 1
        # OR window now locked with full data
        w = eng._state.or_windows["AAPL"]
        assert w.locked
        assert w.or_high == 101.0
        assert w.or_low == 99.0
        assert w.bars_seen == 30

    def test_post_or_locks_via_postwindow_bar_when_959_missing(self):
        """If the 09:59 bucket is missing (Alpaca occasionally drops a
        bar), the v7.73.0 post-window-bar fallback should still lock
        the window via a 10:00+ bar."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        rows = [r for r in _bars_30() if r[0] != 599]  # drop 09:59
        # Append a 10:01 bar
        rows.append((601, 102.0, 100.5, 101.0, 101.0, 5000.0))
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": rows},
            current_et_minutes=11 * 60,
        )
        assert result["backfilled"] == 1
        assert result["locked"] == 1
        w = eng._state.or_windows["AAPL"]
        assert w.locked
        # 29 in-window bars (570..598), bars_seen >= or_minutes // 2 so
        # the lock should NOT have been a "thin OR insufficient" block.
        assert w.bars_seen == 29


class TestBackfillIdempotency:

    def test_idempotent_on_repeated_call(self):
        """Second call after a locked OR should be a fast no-op."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        eng.backfill_or_windows(
            bars_by_ticker={"AAPL": _bars_30()},
            current_et_minutes=11 * 60,
        )
        w_after_first = eng._state.or_windows["AAPL"]
        bars_seen_after_first = w_after_first.bars_seen
        or_high_after_first = w_after_first.or_high
        # Second call -- should hit the locked-skip fast-path.
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": _bars_30()},
            current_et_minutes=11 * 60,
        )
        assert result["skipped"] == 1
        assert result["backfilled"] == 0
        # State unchanged
        w = eng._state.or_windows["AAPL"]
        assert w.bars_seen == bars_seen_after_first
        assert w.or_high == or_high_after_first

    def test_already_locked_ticker_skipped_without_change(self):
        """When the live scan already locked AAPL, backfill leaves it
        alone even if the supplied bars carry different OR bounds."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        # Lock OR via the normal live-scan path with one set of bounds.
        for m in range(570, 600):
            h = 110.0 if m == 580 else 109.5
            lo = 90.0 if m == 585 else 90.5
            eng.on_bar_arrival(
                ticker="AAPL",
                bar_high=h, bar_low=lo,
                bar_open=100.0, bar_close=100.0,
                bar_volume=10000.0, bar_bucket_min=m,
            )
        assert eng._state.or_windows["AAPL"].locked
        # Now feed totally different bars via backfill -- should
        # be ignored (locked window rejects).
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": _bars_30(or_high=200.0, or_low=50.0)},
            current_et_minutes=11 * 60,
        )
        assert result["skipped"] == 1
        # OR bounds unchanged from the live path
        w = eng._state.or_windows["AAPL"]
        assert w.or_high == 110.0
        assert w.or_low == 90.0


class TestBackfillGuards:

    def test_pre_or_end_is_no_op(self):
        """When current ET time is before or_end, backfill returns
        early without touching state (live scan covers the active OR)."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": _bars_30()},
            current_et_minutes=9 * 60 + 45,  # 09:45, mid-OR
        )
        assert result["skipped"] == 1
        assert result["backfilled"] == 0
        w = eng._state.or_windows.get("AAPL")
        # No bars fed; window is still empty
        assert w is None or w.bars_seen == 0

    def test_empty_rows_marks_failed(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": []},
            current_et_minutes=11 * 60,
        )
        assert result["failed"] == 1
        assert result["backfilled"] == 0

    def test_bars_outside_window_are_filtered(self):
        """Bars before 09:30 or at 10:00+ shouldn't pollute OR width."""
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        # Mix in a pre-market 09:00 bar and a 10:30 bar
        rows = [(540, 150.0, 50.0, 100.0, 100.0, 1000.0)]  # 09:00
        rows.extend(_bars_30())
        rows.append((630, 150.0, 50.0, 100.0, 100.0, 1000.0))  # 10:30
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": rows},
            current_et_minutes=11 * 60,
        )
        assert result["backfilled"] == 1
        w = eng._state.or_windows["AAPL"]
        # OR bounds match the in-window rows (101, 99), not the
        # 09:00 / 10:30 outliers (150 / 50).
        assert w.or_high == 101.0
        assert w.or_low == 99.0

    def test_multiple_tickers_independent(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL", "NVDA"])
        result = eng.backfill_or_windows(
            bars_by_ticker={
                "AAPL": _bars_30(or_high=101.0, or_low=99.0),
                "NVDA": _bars_30(or_high=205.0, or_low=195.0),
            },
            current_et_minutes=11 * 60,
        )
        assert result["backfilled"] == 2
        assert eng._state.or_windows["AAPL"].or_high == 101.0
        assert eng._state.or_windows["NVDA"].or_high == 205.0
        assert eng._state.or_windows["AAPL"].locked
        assert eng._state.or_windows["NVDA"].locked

    def test_malformed_rows_dropped(self):
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        good = _bars_30()
        # Inject one malformed row (None price) and one bucket=None
        bad = [(580, None, 99.0, 100.0, 100.0, 100.0),
               (None, 101.0, 99.0, 100.0, 100.0, 100.0)]
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": bad + good},
            current_et_minutes=11 * 60,
        )
        assert result["backfilled"] == 1
        w = eng._state.or_windows["AAPL"]
        assert w.locked


class TestBackfillThinOrInsufficient:
    """When the source only returns a handful of bars (<or_minutes//2)
    the lock should still happen but the per-portfolio FSM should
    transition to PHASE_BLOCKED_OR_INSUFFICIENT, not to ARMED."""

    def test_thin_or_blocks_insufficient(self):
        from orb.state import PHASE_BLOCKED_OR_INSUFFICIENT
        eng = OrbEngine(_cfg(), portfolio_ids=["main"])
        _start_session(eng, ["AAPL"])
        # Only 10 bars (< 15 = or_minutes // 2) + a post-window bar
        rows = [(570 + i, 101.0, 99.0, 100.0, 100.0, 1000.0)
                for i in range(10)]
        rows.append((601, 101.0, 99.0, 100.0, 100.0, 1000.0))
        result = eng.backfill_or_windows(
            bars_by_ticker={"AAPL": rows},
            current_et_minutes=11 * 60,
        )
        assert result["backfilled"] == 1
        ds = eng._state.get_day_state("main", "AAPL")
        assert ds.phase == PHASE_BLOCKED_OR_INSUFFICIENT
