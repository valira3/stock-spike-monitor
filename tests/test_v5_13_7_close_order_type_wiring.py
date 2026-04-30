"""v5.13.7 — close-path order-type wiring tests.

The ``broker.order_types.order_type_for_reason`` mapping has always
been correct, but ``broker.orders.close_breakout`` did not consume
it — the order type was metadata-only. v5.13.7 threads the resolved
order type into:

  1. The ``_emit_signal`` payload's ``order_type`` field, so a future
     Alpaca live-broker bridge submits LIMIT / STOP_MARKET / MARKET
     per spec.
  2. The v5.13.6 lifecycle log's new ``ORDER_SUBMIT`` (close-side)
     event payload, for forensic alignment.

These tests stub the ``trade_genius`` module surface that
``close_breakout`` consumes via ``broker.orders._tg()`` so they can
exercise the close path without the live trading harness.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import broker.orders as bo  # noqa: E402
from broker.order_types import (  # noqa: E402
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP_MARKET,
    REASON_ALARM_A,
    REASON_CIRCUIT_BREAKER,
    REASON_EOD,
    REASON_RATCHET,
    REASON_RUNNER_EXIT,
    REASON_STAGE1_HARVEST,
    REASON_STAGE3_HARVEST,
)
from side import Side  # noqa: E402


class _SideCfg:
    def __init__(self, side):
        self.side = side
        self.positions_attr = "positions" if side.is_long else "short_positions"
        self.trade_history_attr = "trade_history" if side.is_long else "short_trade_history"
        self.history_side_label = "LONG" if side.is_long else "SHORT"
        self.paper_log_close_verb = "SELL" if side.is_long else "COVER"
        self.log_side_label = "LONG" if side.is_long else "SHORT"
        self.exit_signal_kind = "EXIT" if side.is_long else "SHORT_EXIT"
        self.trail_peak_attr = "trail_high" if side.is_long else "trail_low"

    def realized_pnl(self, entry_price, exit_price, shares):
        if self.side.is_long:
            return (exit_price - entry_price) * shares
        return (entry_price - exit_price) * shares

    def close_cash_delta(self, shares, price):
        if self.side.is_long:
            return shares * price
        return -shares * price


class _StubTG:
    BOT_NAME = "TradeGenius"
    BOT_VERSION = "5.13.7"
    TRADE_HISTORY_MAX = 1000
    REASON_LABELS: dict = {}

    def __init__(self, *, side, ticker, pos):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        self.datetime = datetime
        self._tz = ZoneInfo("America/Chicago")
        self.CONFIGS = {Side.LONG: _SideCfg(Side.LONG), Side.SHORT: _SideCfg(Side.SHORT)}
        if side.is_long:
            self.positions = {ticker: pos}
            self.short_positions = {}
            self.trade_history: list = []
            self.short_trade_history: list = []
        else:
            self.positions = {}
            self.short_positions = {ticker: pos}
            self.trade_history = []
            self.short_trade_history = []
        self.paper_trades: list = []
        self.paper_all_trades: list = []
        self._last_exit_time: dict = {}
        self.paper_cash = 100_000.0
        # Recorders.
        self.signals: list = []
        self.telegrams: list = []
        self.paper_logs: list = []
        self.trade_log_rows: list = []
        # Lifecycle id storage.
        self.eot = types.SimpleNamespace(SIDE_LONG="LONG", SIDE_SHORT="SHORT")

        class _EotGlue:
            def clear_position_state(self, *_a, **_kw):
                return None

        self.eot_glue = _EotGlue()

    # Surface used by close_breakout.
    def fetch_1min_bars(self, _t):
        return None

    def get_fmp_quote(self, _t):
        return None

    def _utc_now_iso(self):
        return "2026-04-29T15:00:00Z"

    def _now_et(self):
        return self.datetime(2026, 4, 29, 11, 0, 0)

    def _now_cdt(self):
        return self.datetime(2026, 4, 29, 10, 0, 0)

    def _to_cdt_hhmm(self, _ts):
        return "10:00 CDT"

    def _engine_clear_phase_bucket(self, *_a, **_kw):
        return None

    def send_telegram(self, msg):
        self.telegrams.append(msg)

    def paper_log(self, msg):
        self.paper_logs.append(msg)

    def _emit_signal(self, event):
        self.signals.append(dict(event))

    def trade_log_append(self, row):
        self.trade_log_rows.append(dict(row))

    def _trade_log_snapshot_pos(self, _pos):
        return {}

    def save_paper_state(self):
        return None

    def _v561_log_trade_closed(self, **_kw):
        return None

    def _v561_compose_entry_id(self, ticker, ts):
        return f"{ticker}-{ts}-1"

    @property
    def logger(self):
        import logging

        return logging.getLogger(__name__)


@pytest.fixture
def install_stub_tg(monkeypatch):
    def _install(stub):
        monkeypatch.setitem(sys.modules, "trade_genius", stub)
        return stub

    return _install


@pytest.fixture
def lifecycle_capture(monkeypatch, tmp_path):
    """Reset the lifecycle_logger that broker.orders is bound to.

    broker.orders holds a module-level reference (``_lifecycle``) bound
    at import time. Other test files (test_lifecycle_api.py) delete +
    reimport lifecycle_logger which leaves broker.orders pointed at a
    stale module instance whose ``_default_logger`` is independent of
    any new instance imported via ``import lifecycle_logger``.

    We patch through broker.orders._lifecycle directly so the reset
    operates on the exact module instance that close_breakout uses.
    """
    monkeypatch.setenv("LIFECYCLE_DIR", str(tmp_path))
    ll = bo._lifecycle  # the module instance close_breakout actually uses
    assert ll is not None, "broker.orders._lifecycle should be importable"
    fresh = ll.reset_default_logger_for_tests(data_dir=str(tmp_path), bot_version="5.13.7")
    captured: list[dict] = []
    orig = fresh.log_event

    def _capture(position_id, kind, payload, **kw):
        captured.append(
            {
                "position_id": position_id,
                "kind": kind,
                "payload": dict(payload or {}),
            }
        )
        return orig(position_id, kind, payload, **kw)

    fresh.log_event = _capture  # type: ignore[assignment]
    return fresh, captured


def _make_long_pos(ticker="AAPL"):
    return {
        "entry_price": 100.0,
        "shares": 50,
        "entry_time": "10:00 CDT",
        "entry_ts_utc": "2026-04-29T14:30:00Z",
        "entry_count": 1,
        "lifecycle_position_id": f"{ticker}_20260429T143000Z_long",
    }


def _make_short_pos(ticker="AAPL"):
    return {
        "entry_price": 100.0,
        "shares": 50,
        "entry_time": "10:00 CDT",
        "entry_ts_utc": "2026-04-29T14:30:00Z",
        "entry_count": 1,
        "side": "SHORT",
        "lifecycle_position_id": f"{ticker}_20260429T143000Z_short",
    }


# ---------------------------------------------------------------------------
# _emit_signal carries resolved order_type
# ---------------------------------------------------------------------------


def test_close_position_resolves_order_type_from_reason(install_stub_tg):
    """LONG close with REASON_CIRCUIT_BREAKER → MARKET in emit payload."""
    pos = _make_long_pos()
    tg = _StubTG(side=Side.LONG, ticker="AAPL", pos=pos)
    install_stub_tg(tg)
    bo.close_breakout("AAPL", 99.0, Side.LONG, reason=REASON_CIRCUIT_BREAKER)
    assert tg.signals, "no _emit_signal payload captured"
    assert tg.signals[-1]["order_type"] == ORDER_TYPE_MARKET


def test_close_short_position_resolves_order_type_from_reason(install_stub_tg):
    """SHORT close with REASON_EOD → MARKET in emit payload."""
    pos = _make_short_pos()
    tg = _StubTG(side=Side.SHORT, ticker="AAPL", pos=pos)
    install_stub_tg(tg)
    bo.close_breakout("AAPL", 101.0, Side.SHORT, reason=REASON_EOD)
    assert tg.signals[-1]["order_type"] == ORDER_TYPE_MARKET


def test_legacy_harvest_close_reason_still_maps_to_LIMIT(install_stub_tg):
    """v5.16.0: Stage 1 / Stage 3 harvest reason codes are dead in production
    (no engine emits them post-Velocity Ratchet) but the lookup table still
    maps them to LIMIT for back-compat. Test pinned so the reason → type
    contract doesn't drift if these codes ever reappear."""
    for reason in (REASON_STAGE1_HARVEST, REASON_STAGE3_HARVEST):
        pos = _make_long_pos()
        tg = _StubTG(side=Side.LONG, ticker="AAPL", pos=pos)
        install_stub_tg(tg)
        bo.close_breakout("AAPL", 102.0, Side.LONG, reason=reason)
        assert tg.signals[-1]["order_type"] == ORDER_TYPE_LIMIT, reason


def test_alarm_a_b_and_legacy_runner_close_use_STOP_MARKET(install_stub_tg):
    """v5.16.0: Alarm A / Alarm B fire from the live sentinel; runner_exit /
    ratchet are dead reason codes from the deleted Titan Grip Harvest
    pinned here for back-compat."""
    for reason in (REASON_RUNNER_EXIT, REASON_RATCHET, REASON_ALARM_A):
        pos = _make_long_pos()
        tg = _StubTG(side=Side.LONG, ticker="AAPL", pos=pos)
        install_stub_tg(tg)
        bo.close_breakout("AAPL", 99.0, Side.LONG, reason=reason)
        assert tg.signals[-1]["order_type"] == ORDER_TYPE_STOP_MARKET, reason


def test_unknown_reason_falls_back_to_MARKET(install_stub_tg):
    """Unknown reason codes fall back to MARKET (legacy safety)."""
    pos = _make_long_pos()
    tg = _StubTG(side=Side.LONG, ticker="AAPL", pos=pos)
    install_stub_tg(tg)
    bo.close_breakout("AAPL", 99.0, Side.LONG, reason="something_unknown")
    assert tg.signals[-1]["order_type"] == ORDER_TYPE_MARKET


# ---------------------------------------------------------------------------
# Lifecycle log's ORDER_SUBMIT event carries order_type
# ---------------------------------------------------------------------------


def test_lifecycle_log_order_submit_carries_order_type(install_stub_tg, lifecycle_capture):
    """v5.13.6 lifecycle log: close-path ORDER_SUBMIT event payload now
    carries the resolved order_type field."""
    _logger, captured = lifecycle_capture
    pos = _make_long_pos()
    tg = _StubTG(side=Side.LONG, ticker="AAPL", pos=pos)
    install_stub_tg(tg)
    bo.close_breakout("AAPL", 102.0, Side.LONG, reason=REASON_STAGE1_HARVEST)
    submits = [e for e in captured if e["kind"] == "ORDER_SUBMIT"]
    assert submits, f"no ORDER_SUBMIT events captured: {captured!r}"
    assert submits[-1]["payload"]["order_type"] == ORDER_TYPE_LIMIT
    assert submits[-1]["payload"]["raw_reason"] == REASON_STAGE1_HARVEST


def test_lifecycle_log_order_submit_short_runner(install_stub_tg, lifecycle_capture):
    _logger, captured = lifecycle_capture
    pos = _make_short_pos()
    tg = _StubTG(side=Side.SHORT, ticker="AAPL", pos=pos)
    install_stub_tg(tg)
    bo.close_breakout("AAPL", 101.5, Side.SHORT, reason=REASON_RUNNER_EXIT)
    submits = [e for e in captured if e["kind"] == "ORDER_SUBMIT"]
    assert submits and submits[-1]["payload"]["order_type"] == ORDER_TYPE_STOP_MARKET
