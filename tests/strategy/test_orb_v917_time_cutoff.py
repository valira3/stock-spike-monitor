"""v9.1.7 -- entry-time-cutoff tests.

The live engine pre-v9.1.7 had NO entry-cutoff field at all -- only
`eod_cutoff_minutes` (15:55 ET). The v9.0.0 backtest projection of
+$24,784/yr / 0/4 neg quarters assumed `ORB_TIME_CUTOFF_ET=11:00`
would be enforced live, but the env var was only read by
`tools/orb_backtest.py`. v9.1.7 wires it through.

Tests cover:
  - Reject when signal-bar ET >= cutoff (default 11:00 ET).
  - Admit when signal-bar ET < cutoff.
  - Disabled when cutoff = 0.
  - Fail-open when signal_bar_close_iso is malformed.
  - Counter increments on reject + appears in snapshot.
  - DST regimes (EDT vs EST) handled correctly.
"""

import pytest

from orb.engine import OrbConfig, OrbEngine


def _config(*, time_cutoff_minutes=11 * 60):
    return OrbConfig(
        or_minutes=30,
        rr=2.5,
        stop_buffer_bps=5.0,
        range_min_pct=0.001,
        range_max_pct=0.5,
        max_trades_per_day=5,
        risk_per_trade_pct=1.0,
        max_concurrent_risk_dollars=10_000.0,
        max_concurrent_notional_mult=10.0,
        max_trade_notional_pct=200.0,
        daily_loss_kill_pct=10.0,
        ticker_side_blocklist={},
        skip_vix_above=0.0,
        skip_earnings_window=False,
        skip_gap_above_pct=0.0,
        skip_prior_spy_ret_lt_bps=0.0,
        atr_stop_mult=0.0,
        partial_profit_at_1r=False,
        min_break_bps=0.0,          # disable chase filters so the
        max_vwap_dev_bps=0.0,       # cutoff is the only gate exercised
        time_cutoff_minutes=time_cutoff_minutes,
    )


def _eng_with_locked_or(cfg, *, ticker="AAPL", or_low=99.5, or_high=100.5):
    eng = OrbEngine(cfg, portfolio_ids=["main"])
    eng.start_new_session(
        date_iso="2026-05-13",
        tickers=[ticker],
        vix_close_d1=18.0,
        ticker_open_today={ticker: 100.0},
        ticker_prev_close={ticker: 100.0},
        equity_per_portfolio={"main": 100_000.0},
    )
    w = eng._state.get_or_window(ticker, 30)
    for bucket in range(570, 600):
        w.add_bar(
            bar_high=or_high, bar_low=or_low,
            bar_open=(or_low + or_high) / 2,
            bar_close=(or_low + or_high) / 2,
            bar_volume=1000.0,
            bar_bucket_min=bucket, or_end_min=600,
        )
    w.lock(locked_at_iso="test")
    eng._lock_and_arm(ticker, w)
    return eng


# ----- 1. Cutoff blocks after-window entries --------------------------


class TestTimeCutoff:
    def test_rejects_entry_after_cutoff_edt(self):
        # 15:30 UTC in May = 11:30 EDT > 11:00 cutoff -> reject.
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="2026-05-13T15:30:00+00:00",
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is None
        assert eng._time_cutoff_reject_count == 1

    def test_admits_entry_before_cutoff_edt(self):
        # 14:30 UTC in May = 10:30 EDT < 11:00 cutoff -> admit.
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="2026-05-13T14:30:00+00:00",
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is not None
        assert eng._time_cutoff_reject_count == 0

    def test_rejects_entry_after_cutoff_est(self):
        # 16:30 UTC in January = 11:30 EST > 11:00 cutoff -> reject.
        # (EST = UTC-5; DST regime switch from May -> Jan.)
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="2026-01-15T16:30:00+00:00",
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is None
        assert eng._time_cutoff_reject_count == 1

    def test_admits_at_cutoff_minute_minus_one(self):
        # 14:59 UTC EDT = 10:59 ET < 11:00 -> admit.
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="2026-05-13T14:59:00+00:00",
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is not None

    def test_rejects_exactly_at_cutoff(self):
        # 15:00 UTC EDT = 11:00 ET == cutoff -> reject (>= boundary).
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="2026-05-13T15:00:00+00:00",
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is None

    def test_disabled_with_zero_cutoff(self):
        # cutoff=0 -> filter inactive even at end-of-day.
        cfg = _config(time_cutoff_minutes=0)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="2026-05-13T18:30:00+00:00",  # 14:30 ET
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is not None
        assert eng._time_cutoff_reject_count == 0

    def test_malformed_iso_fails_open(self):
        # Garbage ISO -> _utc_iso_to_et_minutes returns None -> admit.
        # Single bad bar must not strand the engine.
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="not-an-iso-timestamp",
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is not None
        assert eng._time_cutoff_reject_count == 0

    def test_empty_iso_fails_open_documents_v918_hotfix(self):
        # v9.1.8 HOTFIX regression guard. Pre-v9.1.8 scan.py defaulted
        # signal_iso to "" when calling check_entry. _utc_iso_to_et_minutes
        # returns None for "" -> cutoff fails-open -> v9.1.7 was a no-op
        # in production. This test pins the fail-open contract for empty
        # ISO. The scan.py call site is now wired with datetime.now(UTC).
        # If you ever consider tightening this to "fail-closed on empty",
        # also update scan.py:_orb_long_entry / _orb_short_entry and the
        # live_adapter check_entry signal_iso default.
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        sig = eng.detect_breakout(
            portfolio_id="main", ticker="AAPL",
            five_min_close=100.6, next_open=100.7,
            five_min_close_iso="",
        )
        assert sig is not None
        adm = eng.try_enter(sig, equity=100_000.0)
        assert adm is not None
        assert eng._time_cutoff_reject_count == 0


# ----- 2. Counter + snapshot ------------------------------------------


class TestCounterAndSnapshot:
    def test_counter_increments_per_reject(self):
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        for i in range(3):
            sig = eng.detect_breakout(
                portfolio_id="main", ticker="AAPL",
                five_min_close=100.6, next_open=100.7,
                five_min_close_iso=f"2026-05-13T15:0{i}:00+00:00",
            )
            assert sig is not None
            eng.try_enter(sig, equity=100_000.0)
        assert eng._time_cutoff_reject_count == 3

    def test_snapshot_includes_cutoff_fields(self):
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        snap = eng.snapshot()
        # cutoff config sits in the config block alongside other levers
        assert "time_cutoff_minutes" in snap["config"]
        assert snap["config"]["time_cutoff_minutes"] == 11 * 60
        # the runtime counter is a sibling of mbr/chase counters
        assert "time_cutoff_reject_count" in snap
        assert snap["time_cutoff_reject_count"] == 0


# ----- 3. Env-var wiring ----------------------------------------------


class TestEnvWiring:
    def test_default_cutoff_is_eleven_am(self, monkeypatch):
        monkeypatch.delenv("ORB_TIME_CUTOFF_ET", raising=False)
        from orb.live_runtime import _build_config_from_env
        cfg = _build_config_from_env()
        assert cfg.time_cutoff_minutes == 11 * 60

    def test_env_override_to_noon(self, monkeypatch):
        monkeypatch.setenv("ORB_TIME_CUTOFF_ET", "12:00")
        from orb.live_runtime import _build_config_from_env
        cfg = _build_config_from_env()
        assert cfg.time_cutoff_minutes == 12 * 60

    def test_env_override_to_disable(self, monkeypatch):
        # "0:00" -> 0 minutes -> filter inactive.
        monkeypatch.setenv("ORB_TIME_CUTOFF_ET", "0:00")
        from orb.live_runtime import _build_config_from_env
        cfg = _build_config_from_env()
        assert cfg.time_cutoff_minutes == 0

    def test_env_malformed_falls_back_to_default(self, monkeypatch):
        # Garbage value -> log warning, use default 11:00.
        monkeypatch.setenv("ORB_TIME_CUTOFF_ET", "not-a-time")
        from orb.live_runtime import _build_config_from_env
        cfg = _build_config_from_env()
        assert cfg.time_cutoff_minutes == 11 * 60


# ----- 4. live_adapter reject-reason disambiguation -------------------


class TestScanPyWiring:
    """v9.1.8 HOTFIX regression guard: scan.py must compute and pass
    a real signal_iso to check_entry. Pre-v9.1.8 it defaulted to "",
    which silently disabled the v9.1.7 cutoff in production.
    """

    def test_scan_long_entry_passes_signal_iso(self):
        import inspect
        from engine import scan
        src = inspect.getsource(scan._orb_long_entry)
        assert "signal_iso=_signal_iso" in src, (
            "scan._orb_long_entry must pass signal_iso to check_entry "
            "or the v9.1.7 time cutoff is dead. See v9.1.8 HOTFIX."
        )
        assert "datetime.now(timezone.utc).isoformat()" in src, (
            "signal_iso must come from wall-clock UTC."
        )

    def test_scan_short_entry_passes_signal_iso(self):
        import inspect
        from engine import scan
        src = inspect.getsource(scan._orb_short_entry)
        assert "signal_iso=_signal_iso" in src
        assert "datetime.now(timezone.utc).isoformat()" in src


class TestLiveAdapterReason:
    def test_adapter_returns_time_cutoff_reason(self):
        from orb.live_adapter import LiveAdapter
        cfg = _config(time_cutoff_minutes=11 * 60)
        eng = _eng_with_locked_or(cfg)
        adapter = LiveAdapter(engine=eng, portfolio_id="main")
        # After-cutoff timestamp -> time_cutoff_reject path.
        result = adapter.check_entry(
            ticker="AAPL", side="long",
            five_min_close=100.6, next_open=100.7,
            signal_iso="2026-05-13T15:30:00+00:00",
            equity=100_000.0,
        )
        assert result.ok is False
        assert result.reason_no == "time_cutoff"
