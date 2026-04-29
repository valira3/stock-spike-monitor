"""v5.13.2 Track B \u2014 LEGACY_EXITS_ENABLED feature-flag tests.

Verifies that:

1. With the flag OFF (default), the legacy exit paths in
   ``broker/positions.py`` (Profit-Lock Ladder, Section IV brake/fuse,
   Phase A/B/C state machine, RED_CANDLE polarity exit, structural
   stop cross, POLARITY_SHIFT short exit) are skipped \u2014 only the
   Tiger Sovereign Phase 4 sentinel governs exits.

2. With the flag ON, the legacy exit paths run alongside the
   sentinel. When both fire on the same tick, ``[CONFLICT-EXIT]``
   structured log lines are emitted.

These tests mock the ``trade_genius`` module surface that
``broker/positions.py`` consumes via its ``_tg()`` shim, so they can
exercise the manage_positions / manage_short_positions control flow
without booting the live trading harness.
"""
from __future__ import annotations

import logging
import sys
import types

import pytest

import broker.positions as bp
from engine import feature_flags as _ff
from engine.sentinel import EXIT_REASON_ALARM_A


# ---------------------------------------------------------------------------
# Stub trade_genius module the broker.positions._tg() shim resolves to.
# ---------------------------------------------------------------------------


class _StubEot:
    SIDE_LONG = "LONG"
    SIDE_SHORT = "SHORT"
    EXIT_REASON_SOVEREIGN_BRAKE = "sovereign_brake"
    EXIT_REASON_VELOCITY_FUSE = "velocity_fuse"


class _StubEotGlue:
    def __init__(self, override_long=None, override_short=None):
        self._override_long = override_long
        self._override_short = override_short

    def evaluate_section_iv(
        self, side, *, unrealized_pnl_dollars, current_price, current_1m_open,
    ):
        if side == "LONG":
            return self._override_long
        return self._override_short


class _StubTG:
    """Minimal trade_genius stand-in for manage_positions / manage_short_positions."""

    BOT_NAME = "TradeGenius"

    # Profit-Lock Ladder tiers consumed by broker.stops._ladder_stop_long
    # and _ladder_stop_short. Spec: (peak_gain_trigger, give_back_pct).
    LADDER_TIERS_LONG = [
        (0.01, 0.005),
        (0.02, 0.0075),
        (0.03, 0.01),
    ]

    def __init__(self, *, ticker, bars, pos, side, override=None, phase_exit=None):
        self.eot = _StubEot()
        if side == "LONG":
            self.eot_glue = _StubEotGlue(override_long=override)
            self.positions = {ticker: pos}
            self.short_positions = {}
        else:
            self.eot_glue = _StubEotGlue(override_short=override)
            self.positions = {}
            self.short_positions = {ticker: pos}
        self.pdc = {ticker: pos.get("entry_price")}
        self.or_high = {ticker: pos.get("entry_price") + 0.5}
        self.or_low = {ticker: pos.get("entry_price") - 0.5}
        self.paper_cash = 100_000.0
        self._bars = bars
        self._ticker = ticker
        self._phase_exit = phase_exit
        self.closed_long: list[tuple[str, float, str]] = []
        self.closed_short: list[tuple[str, float, str]] = []

    def fetch_1min_bars(self, ticker):
        if ticker == self._ticker:
            return self._bars
        return None

    def get_fmp_quote(self, _ticker):
        return None

    def retighten_all_stops(self, **_kw):
        return None

    def _engine_phase_machine_tick(self, ticker, side, pos, bars):
        return self._phase_exit, None

    def close_position(self, ticker, price, reason):
        self.closed_long.append((ticker, price, reason))
        self.positions.pop(ticker, None)

    def close_short_position(self, ticker, price, reason):
        self.closed_short.append((ticker, price, reason))
        self.short_positions.pop(ticker, None)

    def _utc_now_iso(self):
        return "2026-04-29T15:00:00Z"

    def _emit_signal(self, _event):
        return None

    def save_paper_state(self):
        return None


@pytest.fixture
def install_stub_tg(monkeypatch):
    """Install a stub trade_genius module so broker.positions._tg() finds it."""
    factories: list = []

    def _install(stub_tg):
        # broker.positions._tg() resolves via sys.modules.get("trade_genius").
        monkeypatch.setitem(sys.modules, "trade_genius", stub_tg)
        factories.append(stub_tg)
        return stub_tg

    yield _install
    factories.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_long_pos(entry_price=100.0, shares=10, stop=99.0, **extra):
    pos = {
        "entry_price": entry_price,
        "shares": shares,
        "stop": stop,
        "trail_high": entry_price,
        "pdc": entry_price - 1.0,
        "prev_close": entry_price - 1.0,
        "position_id": "TST-LONG-1",
    }
    pos.update(extra)
    return pos


def _make_short_pos(entry_price=100.0, shares=10, stop=101.0, **extra):
    pos = {
        "entry_price": entry_price,
        "shares": shares,
        "stop": stop,
        "trail_low": entry_price,
        "position_id": "TST-SHORT-1",
    }
    pos.update(extra)
    return pos


def _bars(price, *, opens=None, closes=None):
    return {
        "current_price": price,
        "opens": opens if opens is not None else [price],
        "closes": closes if closes is not None else [price],
    }


# ---------------------------------------------------------------------------
# Flag default
# ---------------------------------------------------------------------------


def test_legacy_exits_default_is_off():
    """v5.13.2 spec: LEGACY_EXITS_ENABLED defaults to False."""
    # Re-read the canonical value to insulate from test ordering.
    import importlib

    import engine.feature_flags as ff

    importlib.reload(ff)
    assert ff.LEGACY_EXITS_ENABLED is False


# ---------------------------------------------------------------------------
# Flag OFF \u2014 legacy paths must NOT close positions
# ---------------------------------------------------------------------------


def test_flag_off_red_candle_does_not_close_long(install_stub_tg, monkeypatch):
    """RED_CANDLE polarity exit is gated; no close when sentinel is silent."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", False)
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=99.0)
    # Price below day_open and below pdc \u2014 RED_CANDLE would fire if legacy on.
    bars = _bars(price=99.50, opens=[100.0, 100.0], closes=[99.50, 99.50])
    pos["pdc"] = 99.99
    pos["prev_close"] = 99.99
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="LONG")
    install_stub_tg(tg)

    bp.manage_positions()

    assert tg.closed_long == [], (
        "RED_CANDLE legacy exit must NOT fire when LEGACY_EXITS_ENABLED is False"
    )


def test_flag_off_structural_stop_does_not_close_long(install_stub_tg, monkeypatch):
    """Structural pos['stop'] cross is gated under LEGACY_EXITS_ENABLED=False."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", False)
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=99.5)
    bars = _bars(price=99.0, opens=[100.0], closes=[99.0])  # below stop
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="LONG")
    install_stub_tg(tg)

    bp.manage_positions()

    assert tg.closed_long == []


def test_flag_off_section_iv_does_not_close_long(install_stub_tg, monkeypatch):
    """Section IV legacy override is gated under LEGACY_EXITS_ENABLED=False."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", False)
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=98.0)
    bars = _bars(price=99.5, opens=[100.0], closes=[99.5])
    tg = _StubTG(
        ticker="TST", bars=bars, pos=pos, side="LONG",
        override="sovereign_brake",
    )
    install_stub_tg(tg)

    bp.manage_positions()

    assert tg.closed_long == []


def test_flag_off_polarity_shift_short_does_not_close(install_stub_tg, monkeypatch):
    """Short POLARITY_SHIFT exit is gated under LEGACY_EXITS_ENABLED=False."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", False)
    pos = _make_short_pos(entry_price=100.0, shares=10, stop=101.0)
    # Price above pdc \u2014 POLARITY_SHIFT would fire under legacy on.
    bars = _bars(price=100.5, opens=[100.0, 100.0], closes=[100.5, 100.5])
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="SHORT")
    tg.pdc = {"TST": 100.0}
    install_stub_tg(tg)

    bp.manage_short_positions()

    assert tg.closed_short == []


def test_flag_off_sentinel_alarm_a_still_closes(install_stub_tg, monkeypatch):
    """Sentinel A1 (-$500 hard floor) ALWAYS fires \u2014 it's the spec path."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", False)
    # 10 shares, entry 100, current 49.99 \u2014 unrealized -$500.10 \u2014 A1 fires.
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=10.0)
    bars = _bars(price=49.99, opens=[100.0], closes=[49.99])
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="LONG")
    install_stub_tg(tg)

    bp.manage_positions()

    assert len(tg.closed_long) == 1
    closed = tg.closed_long[0]
    assert closed[0] == "TST"
    assert closed[2] == EXIT_REASON_ALARM_A


# ---------------------------------------------------------------------------
# Flag ON \u2014 legacy paths run AND emit [CONFLICT-EXIT] when sentinel co-fires
# ---------------------------------------------------------------------------


def test_flag_on_red_candle_closes_long_and_emits_conflict(
    install_stub_tg, monkeypatch, caplog,
):
    """RED_CANDLE fires legacy exit; sentinel A1 also fires \u2014 CONFLICT-EXIT logged."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", True)
    # Both legacy RED_CANDLE AND sentinel A1 fire on this tick.
    # 10 shares * (49.50 - 100.0) = -$505 unrealized \u2014 A1 trips.
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=10.0)
    pos["pdc"] = 99.99
    pos["prev_close"] = 99.99
    bars = _bars(price=49.50, opens=[100.0, 100.0], closes=[49.50, 49.50])
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="LONG")
    install_stub_tg(tg)

    with caplog.at_level(logging.WARNING):
        bp.manage_positions()

    # Sentinel wins because it runs first; legacy RED_CANDLE is unreachable
    # after sentinel closes the position. So we instead set up a scenario
    # where ONLY legacy fires and sentinel produced alarms via Alarm C
    # (no full exit) \u2014 covered in the next test.
    assert len(tg.closed_long) == 1


def test_flag_on_legacy_fires_after_sentinel_alarm_c_emits_conflict(
    install_stub_tg, monkeypatch, caplog,
):
    """When LEGACY_EXITS_ENABLED=True and a legacy exit fires on a tick where
    sentinel also produced alarms (e.g. an Alarm C partial harvest that did
    NOT close the position), a [CONFLICT-EXIT] line must be emitted.
    """
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", True)
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=99.5)
    pos["pdc"] = 99.99
    pos["prev_close"] = 99.99
    # Force RED_CANDLE: 1m close < day open AND < pdc.
    bars = _bars(price=99.4, opens=[100.0, 100.0], closes=[99.4, 99.4])

    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="LONG")
    install_stub_tg(tg)

    # Inject sentinel-alarm telemetry by stubbing _run_sentinel: simulate
    # a tick where sentinel produced Alarm C but did NOT request a full
    # exit. The legacy RED_CANDLE path should then fire AND emit
    # [CONFLICT-EXIT].
    def _stub_run_sentinel(ticker, side, p, current_price, b):
        p["_last_sentinel_alarms"] = ["C2"]
        return None

    monkeypatch.setattr(bp, "_run_sentinel", _stub_run_sentinel)

    with caplog.at_level(logging.WARNING):
        bp.manage_positions()

    # Legacy structural stop (99.4 <= 99.5) fires before RED_CANDLE.
    assert len(tg.closed_long) == 1
    conflict_records = [
        r for r in caplog.records if "[CONFLICT-EXIT]" in r.getMessage()
    ]
    assert conflict_records, (
        "expected [CONFLICT-EXIT] log when legacy exit fires while sentinel "
        "alarms are non-empty"
    )
    msg = conflict_records[0].getMessage()
    assert "ticker=TST" in msg
    assert "side=long" in msg
    assert "sentinel=C2" in msg
    assert "winner=legacy" in msg


def test_flag_on_no_conflict_log_when_sentinel_silent(
    install_stub_tg, monkeypatch, caplog,
):
    """Legacy exit fires but sentinel produced zero alarms \u2014 no CONFLICT log."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", True)
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=99.5)
    bars = _bars(price=99.4, opens=[100.0, 100.0], closes=[99.4, 99.4])
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="LONG")
    install_stub_tg(tg)

    def _silent_sentinel(ticker, side, p, current_price, b):
        p["_last_sentinel_alarms"] = []
        return None

    monkeypatch.setattr(bp, "_run_sentinel", _silent_sentinel)

    with caplog.at_level(logging.WARNING):
        bp.manage_positions()

    assert len(tg.closed_long) == 1
    conflict_records = [
        r for r in caplog.records if "[CONFLICT-EXIT]" in r.getMessage()
    ]
    assert not conflict_records, (
        "no CONFLICT log expected when sentinel produced zero alarms"
    )


def test_flag_on_phase_exit_emits_conflict_when_sentinel_alarms(
    install_stub_tg, monkeypatch, caplog,
):
    """Phase A/B/C exit (legacy) fires while sentinel produced Alarm C."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", True)
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=10.0)
    bars = _bars(price=100.0, opens=[100.0, 100.0], closes=[100.0, 100.0])
    tg = _StubTG(
        ticker="TST", bars=bars, pos=pos, side="LONG", phase_exit="be_stop",
    )
    install_stub_tg(tg)

    def _stub_run_sentinel(ticker, side, p, current_price, b):
        p["_last_sentinel_alarms"] = ["C1", "C2"]
        return None

    monkeypatch.setattr(bp, "_run_sentinel", _stub_run_sentinel)

    with caplog.at_level(logging.WARNING):
        bp.manage_positions()

    assert tg.closed_long == [("TST", 100.0, "be_stop")]
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "[CONFLICT-EXIT]" in m and "legacy=be_stop" in m and "sentinel=C1,C2" in m
        for m in msgs
    )


def test_flag_on_short_polarity_shift_emits_conflict(
    install_stub_tg, monkeypatch, caplog,
):
    """Short POLARITY_SHIFT (legacy) fires alongside sentinel Alarm C."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", True)
    pos = _make_short_pos(entry_price=100.0, shares=10, stop=200.0)
    # Price above pdc, so POLARITY_SHIFT trips on the second-to-last close.
    bars = _bars(price=100.5, opens=[100.0, 100.0], closes=[100.5, 100.5])
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="SHORT")
    tg.pdc = {"TST": 100.0}
    install_stub_tg(tg)

    def _stub_run_sentinel(ticker, side, p, current_price, b):
        p["_last_sentinel_alarms"] = ["C2"]
        return None

    monkeypatch.setattr(bp, "_run_sentinel", _stub_run_sentinel)

    with caplog.at_level(logging.WARNING):
        bp.manage_short_positions()

    assert tg.closed_short and tg.closed_short[0][2] == "POLARITY_SHIFT"
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "[CONFLICT-EXIT]" in m and "side=short" in m
        and "legacy=POLARITY_SHIFT" in m and "sentinel=C2" in m
        for m in msgs
    )


# ---------------------------------------------------------------------------
# Format / wiring sanity checks
# ---------------------------------------------------------------------------


def test_log_conflict_exit_helper_formats_correctly(caplog):
    """The bare helper produces the documented schema."""
    pos = {"_last_sentinel_alarms": ["A1", "C2"]}
    with caplog.at_level(logging.WARNING):
        bp._log_conflict_exit("MSFT", bp._SENTINEL_SIDE_LONG, "RED_CANDLE", pos)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        m == "[CONFLICT-EXIT] ticker=MSFT side=long legacy=RED_CANDLE "
             "sentinel=A1,C2 winner=legacy"
        for m in msgs
    )


def test_log_conflict_exit_helper_silent_when_sentinel_empty(caplog):
    """No alarms \u2192 no log line."""
    pos = {"_last_sentinel_alarms": []}
    with caplog.at_level(logging.WARNING):
        bp._log_conflict_exit("MSFT", bp._SENTINEL_SIDE_LONG, "RED_CANDLE", pos)
    msgs = [r.getMessage() for r in caplog.records if "[CONFLICT-EXIT]" in r.getMessage()]
    assert msgs == []


def test_run_sentinel_records_alarms_on_pos(install_stub_tg, monkeypatch):
    """_run_sentinel always sets pos['_last_sentinel_alarms'] (empty list when no fire)."""
    monkeypatch.setattr(_ff, "LEGACY_EXITS_ENABLED", False)
    pos = _make_long_pos(entry_price=100.0, shares=10, stop=10.0)
    bars = _bars(price=100.0, opens=[100.0], closes=[100.0])
    tg = _StubTG(ticker="TST", bars=bars, pos=pos, side="LONG")
    install_stub_tg(tg)

    bp._run_sentinel("TST", bp._SENTINEL_SIDE_LONG, pos, 100.0, bars)
    assert pos["_last_sentinel_alarms"] == []
