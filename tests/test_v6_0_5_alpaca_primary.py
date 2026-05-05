# tests/test_v6_0_5_alpaca_primary.py
# v6.0.5 -- Alpaca-IEX promoted to primary 1m source in fetch_1min_bars,
# Yahoo retained as fallback. Dual-source failure surfaces a CRITICAL
# log line + one-shot Telegram notify per ticker per process.
#
# These tests stub out the Alpaca/Yahoo helpers and the FMP quote and
# Telegram send, then drive the orchestrator to verify:
#   1. Alpaca success short-circuits Yahoo (Yahoo NEVER called).
#   2. Alpaca-None routes to Yahoo, returns Yahoo dict shape.
#   3. Both-None returns None, logs CRITICAL, fires telegram exactly
#      once per ticker per process (re-call same ticker -> no second
#      telegram), different tickers each get their own one-shot.
#   4. The cycle bar cache short-circuits both helpers on a hit.
#   5. Negative-cache sentinel ("__FAILED__") returns None on a hit
#      without re-calling either helper.
#   6. The Alpaca path includes premarket coverage (08:00 ET start).
#
# No em-dashes in this file (constraint for .py test files).
from __future__ import annotations

import os

import pytest

# v5.10.3 startup-smoke pattern: SSM_SMOKE_TEST=1 short-circuits the
# main-thread side effects (Telegram bot init, web server bind, /data
# perm probes) so we can import trade_genius cleanly in a test process.
os.environ.setdefault("SSM_SMOKE_TEST", "1")


@pytest.fixture
def tg():
    """Import trade_genius once, clear v6.0.5 caches between tests so
    stateful guards (_dual_source_critical_emitted, _cycle_bar_cache,
    _alpaca_pdc_cache) don't leak across tests. We do NOT reload the
    module \u2014 reload triggers module-init side effects (Telegram bot
    construction, /data perm probes) that aren't relevant here."""
    import trade_genius

    trade_genius._cycle_bar_cache.clear()
    trade_genius._alpaca_pdc_cache.clear()
    trade_genius._dual_source_critical_emitted.clear()
    yield trade_genius
    trade_genius._cycle_bar_cache.clear()
    trade_genius._alpaca_pdc_cache.clear()
    trade_genius._dual_source_critical_emitted.clear()


def _shape(closes_len=10, pdc=99.5):
    return {
        "timestamps": list(range(closes_len)),
        "opens": [100.0] * closes_len,
        "highs": [100.5] * closes_len,
        "lows": [99.5] * closes_len,
        "closes": [100.0 + i * 0.1 for i in range(closes_len)],
        "volumes": [1000] * closes_len,
        "current_price": 101.0,
        "pdc": pdc,
    }


def test_alpaca_success_short_circuits_yahoo(tg, monkeypatch):
    yahoo_calls = []
    monkeypatch.setattr(tg, "_fetch_1min_bars_alpaca", lambda t: _shape())
    monkeypatch.setattr(
        tg, "_fetch_1min_bars_yahoo",
        lambda t: yahoo_calls.append(t) or _shape(),
    )
    out = tg.fetch_1min_bars("TSLA")
    assert out is not None
    assert out["pdc"] == 99.5
    assert yahoo_calls == [], "Yahoo should not be called when Alpaca succeeds"


def test_alpaca_none_falls_back_to_yahoo(tg, monkeypatch):
    monkeypatch.setattr(tg, "_fetch_1min_bars_alpaca", lambda t: None)
    monkeypatch.setattr(tg, "_fetch_1min_bars_yahoo", lambda t: _shape(pdc=88.0))
    out = tg.fetch_1min_bars("NFLX")
    assert out is not None
    assert out["pdc"] == 88.0


def test_dual_source_failure_returns_none_and_logs_critical(tg, monkeypatch, caplog):
    notify_calls = []
    monkeypatch.setattr(tg, "_fetch_1min_bars_alpaca", lambda t: None)
    monkeypatch.setattr(tg, "_fetch_1min_bars_yahoo", lambda t: None)
    monkeypatch.setattr(tg, "send_telegram", lambda msg: notify_calls.append(msg))
    with caplog.at_level("ERROR", logger="trade_genius"):
        out = tg.fetch_1min_bars("AAPL")
    assert out is None
    crit_lines = [r for r in caplog.records if "[SENTINEL][CRITICAL]" in r.getMessage()]
    assert len(crit_lines) >= 1
    assert len(notify_calls) == 1
    assert "AAPL" in notify_calls[0]


def test_dual_source_failure_notifies_only_once_per_ticker(tg, monkeypatch):
    notify_calls = []
    monkeypatch.setattr(tg, "_fetch_1min_bars_alpaca", lambda t: None)
    monkeypatch.setattr(tg, "_fetch_1min_bars_yahoo", lambda t: None)
    monkeypatch.setattr(tg, "send_telegram", lambda msg: notify_calls.append(msg))
    # Same ticker, three cycles -> still exactly one telegram. (Cycle
    # cache is cleared between cycles in real bot via
    # _clear_cycle_bar_cache; we mimic that here.)
    for _ in range(3):
        tg._clear_cycle_bar_cache()
        tg.fetch_1min_bars("AAPL")
    assert len(notify_calls) == 1


def test_dual_source_failure_notifies_per_ticker(tg, monkeypatch):
    notify_calls = []
    monkeypatch.setattr(tg, "_fetch_1min_bars_alpaca", lambda t: None)
    monkeypatch.setattr(tg, "_fetch_1min_bars_yahoo", lambda t: None)
    monkeypatch.setattr(tg, "send_telegram", lambda msg: notify_calls.append(msg))
    tg._clear_cycle_bar_cache()
    tg.fetch_1min_bars("AAPL")
    tg._clear_cycle_bar_cache()
    tg.fetch_1min_bars("TSLA")
    assert len(notify_calls) == 2
    assert any("AAPL" in m for m in notify_calls)
    assert any("TSLA" in m for m in notify_calls)


def test_telegram_failure_does_not_break_orchestrator(tg, monkeypatch):
    monkeypatch.setattr(tg, "_fetch_1min_bars_alpaca", lambda t: None)
    monkeypatch.setattr(tg, "_fetch_1min_bars_yahoo", lambda t: None)

    def boom(_msg):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(tg, "send_telegram", boom)
    out = tg.fetch_1min_bars("AAPL")
    # Still returns None gracefully; CRITICAL log already emitted.
    assert out is None


def test_cycle_cache_hit_skips_both_helpers(tg, monkeypatch):
    alpaca_calls = []
    yahoo_calls = []
    monkeypatch.setattr(
        tg, "_fetch_1min_bars_alpaca",
        lambda t: alpaca_calls.append(t) or _shape(),
    )
    monkeypatch.setattr(
        tg, "_fetch_1min_bars_yahoo",
        lambda t: yahoo_calls.append(t) or _shape(),
    )
    tg.fetch_1min_bars("TSLA")
    tg.fetch_1min_bars("TSLA")
    tg.fetch_1min_bars("TSLA")
    assert len(alpaca_calls) == 1
    assert yahoo_calls == []


def test_negative_cache_returns_none_without_recall(tg, monkeypatch):
    alpaca_calls = []
    yahoo_calls = []
    monkeypatch.setattr(
        tg, "_fetch_1min_bars_alpaca",
        lambda t: alpaca_calls.append(t) or None,
    )
    monkeypatch.setattr(
        tg, "_fetch_1min_bars_yahoo",
        lambda t: yahoo_calls.append(t) or None,
    )
    monkeypatch.setattr(tg, "send_telegram", lambda msg: None)
    # First call: both helpers run, both fail, sentinel cached.
    assert tg.fetch_1min_bars("AAPL") is None
    # Second call inside same cycle: cache short-circuit, neither
    # helper re-invoked.
    assert tg.fetch_1min_bars("AAPL") is None
    assert len(alpaca_calls) == 1
    assert len(yahoo_calls) == 1


def test_alpaca_path_window_includes_premarket(tg, monkeypatch):
    # Sanity check: the Alpaca request the production code constructs
    # spans 08:00 ET to 18:00 ET (covers 08:00-09:30 ET premarket warm-
    # up loop that v5.30.1 introduced via Yahoo's includePrePost=true).
    captured = {}

    class FakeBar:
        def __init__(self, ts):
            from datetime import datetime, timezone
            self.timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
            self.open = 100.0
            self.high = 100.5
            self.low = 99.5
            self.close = 100.0
            self.volume = 1000

    class FakeResp:
        def __init__(self, sym):
            self.data = {sym: [FakeBar(1735739100)]}  # arbitrary ts

    class FakeClient:
        def get_stock_bars(self, req):
            captured["start"] = req.start
            captured["end"] = req.end
            captured["feed"] = req.feed
            captured["timeframe"] = req.timeframe
            return FakeResp(req.symbol_or_symbols)

    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: FakeClient())
    monkeypatch.setattr(tg, "_alpaca_pdc", lambda sym, client: 99.0)
    monkeypatch.setattr(tg, "get_fmp_quote", lambda sym: {"price": 101.5})

    out = tg._fetch_1min_bars_alpaca("TSLA")
    assert out is not None
    assert out["pdc"] == 99.0
    assert out["current_price"] == 101.5  # FMP path used, not last bar close
    # Window must START at 08:00 ET to cover premarket warmup.
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    start_et = captured["start"].astimezone(et)
    end_et = captured["end"].astimezone(et)
    # v6.0.5 originally pinned start to 08:00 ET. Post-v6.0.6 the window
    # was widened to 04:00 ET (full pre-market 04:00-09:30 + after-hours
    # 16:00-20:00) per trade_genius._fetch_1min_bars_alpaca. The contract
    # this test guards is "window starts at-or-before pre-market open",
    # not the literal 08:00 cutoff.
    assert start_et.hour <= 8 and start_et.minute == 0
    assert end_et.hour >= 18  # 18:00 ET + 1m


def test_alpaca_current_price_falls_back_to_last_close_when_fmp_down(tg, monkeypatch):
    class FakeBar:
        def __init__(self, ts, close):
            from datetime import datetime, timezone
            self.timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
            self.open = 100.0
            self.high = 100.5
            self.low = 99.5
            self.close = close
            self.volume = 1000

    class FakeResp:
        def __init__(self, sym):
            self.data = {sym: [FakeBar(1735739100, 102.5), FakeBar(1735739160, 103.25)]}

    class FakeClient:
        def get_stock_bars(self, req):
            return FakeResp(req.symbol_or_symbols)

    monkeypatch.setattr(tg, "_alpaca_data_client", lambda: FakeClient())
    monkeypatch.setattr(tg, "_alpaca_pdc", lambda sym, client: 100.0)
    monkeypatch.setattr(tg, "get_fmp_quote", lambda sym: None)

    out = tg._fetch_1min_bars_alpaca("TSLA")
    assert out is not None
    assert out["current_price"] == 103.25


def test_dict_shape_matches_yahoo_contract(tg, monkeypatch):
    # Downstream consumers depend on the exact key set; missing any
    # one of these will silently degrade (e.g. bars["pdc"] -> KeyError
    # inside compute_5m_ohlc_and_ema9).
    monkeypatch.setattr(tg, "_fetch_1min_bars_alpaca", lambda t: _shape())
    out = tg.fetch_1min_bars("TSLA")
    expected_keys = {
        "timestamps", "opens", "highs", "lows", "closes",
        "volumes", "current_price", "pdc",
    }
    assert set(out.keys()) >= expected_keys
