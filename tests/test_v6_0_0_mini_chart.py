"""v6.0.0 \u2014 unit tests for the mini-chart sparkline payload helper.

`v5_10_6_snapshot._mini_chart_per_ticker` reads `fetch_1min_bars` for
each ticker and returns a downsampled close series the dashboard can
paint as a 80\u00d724 SVG polyline. The function MUST:

  - tolerate a missing `fetch_1min_bars` callable on the module
  - tolerate a None / empty bars dict for any ticker
  - cap the points list at 60 entries via stride downsampling
  - append `current_price` if it differs from the last close
  - emit hi / lo / open / last / count consistent with `points`
  - never raise (failure on a single ticker drops to empty payload)
"""

from __future__ import annotations

import os
import sys
import types

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


from v5_10_6_snapshot import _mini_chart_per_ticker  # noqa: E402


def _stub_module(per_ticker: dict) -> types.SimpleNamespace:
    """Build a minimal trade_genius-stand-in with a fetch_1min_bars
    callable backed by ``per_ticker``."""
    def fetch(t):
        return per_ticker.get(t)

    return types.SimpleNamespace(fetch_1min_bars=fetch)


# ---------------------------------------------------------------------------
# 1. No fetch callable on the module \u2014 returns empty dict.
# ---------------------------------------------------------------------------


def test_mini_chart_no_fetch_returns_empty():
    m = types.SimpleNamespace()  # no fetch_1min_bars attribute
    out = _mini_chart_per_ticker(m, ["AAPL", "TSLA"])
    assert out == {}


# ---------------------------------------------------------------------------
# 2. None bars / empty bars => empty payload entry per ticker.
# ---------------------------------------------------------------------------


def test_mini_chart_none_bars_empty_payload():
    m = _stub_module({"AAPL": None})
    out = _mini_chart_per_ticker(m, ["AAPL"])
    assert "AAPL" in out
    payload = out["AAPL"]
    assert payload["points"] == []
    assert payload["count"] == 0
    assert payload["hi"] is None and payload["lo"] is None
    assert payload["open"] is None and payload["last"] is None


# ---------------------------------------------------------------------------
# 3. Small (under 60) close list \u2014 returned verbatim plus current_price.
# ---------------------------------------------------------------------------


def test_mini_chart_small_series_appends_current_price():
    closes = [100.0, 101.0, 102.0]
    m = _stub_module({"AAPL": {
        "closes_1m": closes,
        "current_price": 102.5,
    }})
    out = _mini_chart_per_ticker(m, ["AAPL"])
    p = out["AAPL"]
    assert p["points"] == [100.0, 101.0, 102.0, 102.5]
    assert p["count"] == 4
    assert p["open"] == 100.0
    assert p["last"] == 102.5
    assert p["hi"] == 102.5
    assert p["lo"] == 100.0


def test_mini_chart_current_matches_last_close_no_dup():
    closes = [100.0, 101.0]
    m = _stub_module({"AAPL": {
        "closes_1m": closes,
        "current_price": 101.0,
    }})
    out = _mini_chart_per_ticker(m, ["AAPL"])
    p = out["AAPL"]
    assert p["points"] == [100.0, 101.0]
    assert p["count"] == 2


# ---------------------------------------------------------------------------
# 4. Long (> 60) close list \u2014 downsampled to <= 60 + current price.
# ---------------------------------------------------------------------------


def test_mini_chart_downsamples_to_at_most_sixty():
    # 390 closes (a full RTH session of 1m bars). Step = 6, so we keep
    # ~65 candidates and slice the trailing 60.
    closes = [float(100 + (i % 7)) for i in range(390)]
    m = _stub_module({"AAPL": {
        "closes_1m": closes,
        "current_price": 999.0,  # forces an append
    }})
    out = _mini_chart_per_ticker(m, ["AAPL"])
    p = out["AAPL"]
    # 60 downsampled + 1 current_price (different from last) = at most 61.
    assert len(p["points"]) <= 61
    assert p["points"][-1] == 999.0
    assert p["count"] == len(p["points"])


# ---------------------------------------------------------------------------
# 5. Bars with no closes_1m fall back to current_price-only.
# ---------------------------------------------------------------------------


def test_mini_chart_only_current_price_available():
    m = _stub_module({"AAPL": {
        "current_price": 250.5,
    }})
    out = _mini_chart_per_ticker(m, ["AAPL"])
    p = out["AAPL"]
    assert p["points"] == [250.5]
    assert p["open"] == 250.5
    assert p["last"] == 250.5
    assert p["count"] == 1


# ---------------------------------------------------------------------------
# 6. Non-numeric / zero closes are filtered out.
# ---------------------------------------------------------------------------


def test_mini_chart_filters_garbage_closes():
    closes = [None, "abc", 0.0, -5.0, 100.0, 101.0]
    m = _stub_module({"AAPL": {
        "closes_1m": closes,
        "current_price": 102.0,
    }})
    out = _mini_chart_per_ticker(m, ["AAPL"])
    p = out["AAPL"]
    # 100, 101, 102 retained; the rest filtered.
    assert p["points"] == [100.0, 101.0, 102.0]


# ---------------------------------------------------------------------------
# 7. fetch raising on one ticker must not nuke the others.
# ---------------------------------------------------------------------------


def test_mini_chart_isolates_per_ticker_failures():
    state = {"AAPL": {"closes_1m": [100.0, 101.0], "current_price": 102.0}}

    def fetch(t):
        if t == "TSLA":
            raise RuntimeError("simulated feed error")
        return state.get(t)

    m = types.SimpleNamespace(fetch_1min_bars=fetch)
    out = _mini_chart_per_ticker(m, ["AAPL", "TSLA"])
    # AAPL is good.
    assert out["AAPL"]["count"] >= 2
    # TSLA degrades to an empty payload, not a missing key.
    assert out["TSLA"]["points"] == []
    assert out["TSLA"]["count"] == 0
