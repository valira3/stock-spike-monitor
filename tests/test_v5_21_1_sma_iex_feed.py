"""v5.21.1 regression tests: engine.daily_bars must request the IEX feed.

The v5.21.0 release shipped without specifying a feed on
``StockBarsRequest``, which made alpaca-py default to SIP. The paper-tier
Alpaca subscription used in production rejects SIP daily bars with
``subscription does not permit querying recent SIP data``, so every
ticker's ``sma_stack`` came back as ``None`` and the dashboard rendered
"data not available" on every Titan.

These tests make sure the fetcher always requests the IEX feed.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DAILY_BARS_PATH = REPO_ROOT / "engine" / "daily_bars.py"


# ---------------------------------------------------------------------------
# Static source guard
# ---------------------------------------------------------------------------


def test_daily_bars_source_specifies_iex_feed():
    """The default fetcher must build StockBarsRequest with feed='iex'."""
    src = DAILY_BARS_PATH.read_text(encoding="utf-8")
    assert 'feed="iex"' in src, (
        'engine/daily_bars.py must pass feed="iex" to StockBarsRequest. '
        "Without it Alpaca defaults to SIP and paper-tier rejects with "
        "'subscription does not permit querying recent SIP data'."
    )


def test_daily_bars_no_unparameterised_stockbars_request():
    """Every StockBarsRequest constructor in daily_bars.py must include feed=."""
    src = DAILY_BARS_PATH.read_text(encoding="utf-8")
    # Capture each StockBarsRequest(...) call and verify the kwargs list contains feed=.
    matches = re.findall(r"StockBarsRequest\(([^)]*)\)", src, flags=re.DOTALL)
    assert matches, "Expected at least one StockBarsRequest(...) call in engine/daily_bars.py"
    for kwargs in matches:
        assert "feed=" in kwargs, f"StockBarsRequest call missing feed= kwarg:\n{kwargs.strip()}"


# ---------------------------------------------------------------------------
# Behavioural test: monkeypatch StockBarsRequest and confirm the call
# ---------------------------------------------------------------------------


class _StubBar:
    def __init__(self, close):
        self.close = close


class _StubBarSet:
    def __init__(self, ticker, bars):
        self._data = {ticker: bars}

    def __getitem__(self, key):
        return self._data[key]


def test_default_fetcher_passes_feed_iex(monkeypatch):
    """When invoked, _default_fetcher passes feed='iex' to StockBarsRequest."""
    from engine import daily_bars as db

    db._cache_clear()

    captured: dict = {}

    class _FakeRequest:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _FakeClient:
        def get_stock_bars(self, request):
            return _StubBarSet("AAPL", [_StubBar(c) for c in [100.0, 101.0, 102.0]])

    fake_module = type("FakeRequestsModule", (), {"StockBarsRequest": _FakeRequest})
    fake_tf_module = type("FakeTfModule", (), {"TimeFrame": type("TF", (), {"Day": "Day"})})

    monkeypatch.setitem(__import__("sys").modules, "alpaca.data.requests", fake_module)
    monkeypatch.setitem(__import__("sys").modules, "alpaca.data.timeframe", fake_tf_module)

    with patch.object(db, "_build_alpaca_client", return_value=_FakeClient()):
        closes = db._default_fetcher("AAPL", lookback=3)

    assert closes == [100.0, 101.0, 102.0]
    assert captured.get("feed") == "iex", (
        f"Expected feed='iex' in StockBarsRequest kwargs, got {captured!r}"
    )
    assert captured.get("symbol_or_symbols") == "AAPL"


# ---------------------------------------------------------------------------
# Smoke test: confirm the warning string we expect in prod logs would
# come from the safe wrapper, so future regressions are recognisable.
# ---------------------------------------------------------------------------


def test_snapshot_safe_wrapper_logs_warning_on_failure(monkeypatch, caplog):
    """If get_recent_daily_closes raises, _compute_sma_stack_safe logs a WARNING."""
    import importlib

    snapshot = importlib.import_module("v5_13_2_snapshot")

    def _boom(ticker, lookback=250, *, fetcher=None):
        raise RuntimeError("subscription does not permit querying recent SIP data")

    monkeypatch.setattr("engine.daily_bars.get_recent_daily_closes", _boom, raising=True)

    import logging

    with caplog.at_level(logging.WARNING, logger="trade_genius"):
        result = snapshot._compute_sma_stack_safe("AAPL")

    assert result is None
    assert any("sma_stack: failed for AAPL" in rec.message for rec in caplog.records), (
        f"Expected warning about AAPL failure; got {[r.message for r in caplog.records]}"
    )
