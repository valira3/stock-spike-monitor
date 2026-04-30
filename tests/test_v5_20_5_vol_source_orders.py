"""v5.20.5 \u2014 Volume Bucket source-of-truth contract tests.

Covers ``broker.orders._resolve_last_completed_volume``: the gate
preferring the WS consumer's IEX volume for the just-closed 1m bucket,
falling back to Yahoo's ``volumes[-2]`` only when the WS feed is
unavailable or has not yet captured the bucket.

Forensics that motivated this change: 2026-04-30 13:35 UTC the bot was
SHORT-permitted on 9/10 tickers with two-consec-outside on the boundary
gate, but every single Volume Bucket gate evaluation at the entry path
read ``current_vol=0`` because Yahoo ships volume=0/None on the
trailing-edge bar for ~30-60s after each minute close. The WS feed
(StockDataStream) had the correct cumulative volume in its bucket map
the whole time. This test fixes the precedence at the unit level.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import broker.orders as orders  # noqa: E402


ET = ZoneInfo("America/New_York")


class _FakeWsConsumer:
    """Minimal stub matching the WebsocketBarConsumer.current_volume()
    contract: callable taking ``(ticker, bucket_hhmm)`` and returning
    the cumulative IEX volume captured for that bucket, or None.
    """

    def __init__(self, mapping=None):
        # mapping: {(ticker, bucket): vol}
        self._m = mapping or {}
        self.calls = []

    def current_volume(self, ticker, bucket):
        self.calls.append((ticker, bucket))
        return self._m.get((ticker, bucket))


def _fake_tg(ws_consumer=None):
    """Build a minimal stand-in for the trade_genius module so the
    helper does not need the full prod module to import. Mirrors only
    the attributes ``_resolve_last_completed_volume`` reads.
    """
    m = types.SimpleNamespace()
    m._ws_consumer = ws_consumer
    m.logger = types.SimpleNamespace(warning=lambda *a, **k: None)
    return m


def _now_et(hour=10, minute=27, second=30):
    return datetime(2026, 4, 30, hour, minute, second, tzinfo=ET)


# ---------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------


def test_orders_uses_ws_vol_when_available():
    """WS reports 12345 for the previous bucket; Yahoo reports 0 on the
    trailing-edge bar. The gate must receive 12345 (the truth).
    """
    now = _now_et(10, 27, 30)
    # previous_session_bucket(10:27:30) == '1026'
    ws = _FakeWsConsumer({("NVDA", "1026"): 12345})
    bars = {"volumes": [50000, 0]}  # Yahoo trailing-edge=0
    out = orders._resolve_last_completed_volume(_fake_tg(ws), "NVDA", now, bars)
    assert out == 12345, f"WS value should win when > 0; got {out}"
    assert ws.calls == [("NVDA", "1026")]


def test_orders_falls_back_to_yahoo_when_ws_unavailable():
    """No WS consumer wired (e.g. import-time race or feed disabled)
    \u2014 the helper falls through to Yahoo[-2].
    """
    now = _now_et(10, 27, 30)
    bars = {"volumes": [50000, 8000, 0]}  # [-2]=8000, [-1]=0
    out = orders._resolve_last_completed_volume(_fake_tg(None), "AAPL", now, bars)
    assert out == 8000


def test_orders_falls_back_to_yahoo_when_ws_returns_none():
    """WS consumer is wired but has not captured this bucket yet (e.g.
    just reconnected); the helper falls back to Yahoo[-2].
    """
    now = _now_et(10, 27, 30)
    ws = _FakeWsConsumer({})  # any lookup returns None
    bars = {"volumes": [50000, 8000, 0]}
    out = orders._resolve_last_completed_volume(_fake_tg(ws), "TSLA", now, bars)
    assert out == 8000


def test_orders_falls_back_when_ws_reports_zero():
    """WS captured the bucket but with zero volume (extremely rare;
    happens on the very first second of a minute before any tick).
    Treated as not-yet-captured \u2192 Yahoo wins.
    """
    now = _now_et(10, 27, 30)
    ws = _FakeWsConsumer({("MSFT", "1026"): 0})
    bars = {"volumes": [50000, 8000, 0]}
    out = orders._resolve_last_completed_volume(_fake_tg(ws), "MSFT", now, bars)
    assert out == 8000


def test_orders_uses_previous_bucket_not_current():
    """Given now_et=10:27:30 ET, the WS lookup must use the closed
    bucket '1026' \u2014 not the still-forming '1027'. This matches the
    precedence v5.5.5 wired into engine/scan.py.
    """
    now = _now_et(10, 27, 30)
    ws = _FakeWsConsumer({("NVDA", "1026"): 12345, ("NVDA", "1027"): 9999})
    bars = {"volumes": [50000, 0]}
    out = orders._resolve_last_completed_volume(_fake_tg(ws), "NVDA", now, bars)
    assert out == 12345
    # Verify lookup key, not just final value.
    assert ws.calls[-1] == ("NVDA", "1026")


# ---------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------


def test_orders_returns_none_when_both_sources_empty():
    now = _now_et(10, 27, 30)
    out = orders._resolve_last_completed_volume(_fake_tg(None), "ABC", now, {})
    assert out is None


def test_orders_reads_yahoo_minus_2_when_minus_1_is_none():
    """Yahoo's RTH shape: [-1]=None forming, [-2]=last closed minute."""
    now = _now_et(10, 27, 30)
    bars = {"volumes": [50000, 8000, None]}
    out = orders._resolve_last_completed_volume(_fake_tg(None), "ABC", now, bars)
    assert out == 8000


def test_orders_falls_back_to_minus_1_when_minus_2_is_none():
    """Pathological Yahoo response: [-2]=None, [-1]=last value seen.
    Last-resort fallback returns [-1] rather than failing the gate
    outright. The wider-context check_breakout still rejects the trade
    if this value is below threshold.
    """
    now = _now_et(10, 27, 30)
    bars = {"volumes": [50000, None, 8000]}
    out = orders._resolve_last_completed_volume(_fake_tg(None), "ABC", now, bars)
    assert out == 8000


def test_orders_swallows_ws_consumer_exception():
    """A misbehaving ws_consumer must NOT crash the entry path; the
    helper logs a warning and falls back to Yahoo.
    """
    now = _now_et(10, 27, 30)

    class _BoomWs:
        def current_volume(self, *a, **k):
            raise RuntimeError("boom")

    bars = {"volumes": [50000, 8000, None]}
    out = orders._resolve_last_completed_volume(_fake_tg(_BoomWs()), "ABC", now, bars)
    assert out == 8000


def test_orders_skipped_outside_session_returns_yahoo():
    """When previous_session_bucket() returns None (premarket /
    weekend), the WS branch does not execute and the helper returns
    whatever Yahoo has.
    """
    # 04:00 ET premarket \u2014 previous_session_bucket() returns None.
    now = _now_et(4, 0, 0)
    ws = _FakeWsConsumer({("ABC", "0359"): 99999})
    bars = {"volumes": [100, 200, None]}
    out = orders._resolve_last_completed_volume(_fake_tg(ws), "ABC", now, bars)
    assert out == 200
    # WS was never queried because no valid prev bucket existed.
    assert ws.calls == []
