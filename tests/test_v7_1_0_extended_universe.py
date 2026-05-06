# -*- coding: utf-8 -*-
"""v7.1.0 \u2014 dynamic extended-hours universe overlay tests.

Coverage:
  * flag off \u2192 returns plain TRADE_TICKERS regardless of session
  * flag on, RTH session \u2192 returns plain TRADE_TICKERS (overlay only fires
    in extended hours)
  * flag on, extended session \u2192 returns prod_core + earnings overlay
  * earnings_watcher import failure \u2192 falls back to prod core
  * earnings calendar empty \u2192 returns prod core only
  * overlay capped at EXTENDED_HOURS_OVERLAY_MAX
  * overlay dedupes against prod core (no NVDA twice if NVDA reports)
  * cache TTL: second call within TTL hits cache, not the data source
  * cache invalidates on UTC date rollover
  * never returns empty list (defensive fallback to prod core)
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

import pytest

# Make sure the repo root is on sys.path
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from engine import extended_universe as eu


_PROD_CORE = ["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG", "AMZN",
              "AVGO", "NFLX", "ORCL", "SPY", "QQQ"]


@pytest.fixture(autouse=True)
def _wipe_cache_and_env(monkeypatch):
    """Reset module cache + clear feature-flag env between tests."""
    eu.reset_cache_for_test()
    monkeypatch.delenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", raising=False)
    monkeypatch.delenv("EXTENDED_HOURS_OVERLAY_MAX", raising=False)
    yield
    eu.reset_cache_for_test()


def _patch_prod_core(monkeypatch, tickers=None):
    fake_tg = mock.MagicMock()
    fake_tg.TRADE_TICKERS = list(tickers if tickers is not None else _PROD_CORE)
    monkeypatch.setitem(sys.modules, "trade_genius", fake_tg)
    return fake_tg


# ---------------------------------------------------------------------------
# Feature-flag gating
# ---------------------------------------------------------------------------

def test_flag_off_returns_prod_core_in_extended(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.delenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", raising=False)
    out = eu.effective_scan_tickers("extended")
    assert out == _PROD_CORE


def test_flag_off_returns_prod_core_in_rth(monkeypatch):
    _patch_prod_core(monkeypatch)
    out = eu.effective_scan_tickers("rth")
    assert out == _PROD_CORE


def test_rth_never_uses_overlay_even_with_flag_on(monkeypatch):
    """RTH must be byte-identical to pre-v7.1.0 behavior."""
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    fake_ews = mock.MagicMock(return_value=(["COIN", "NET"], ["DDOG"]))
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    fake_ews):
        out = eu.effective_scan_tickers("rth")
    assert out == _PROD_CORE
    # Earnings source must NOT have been called for RTH
    fake_ews.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_extended_with_flag_on_appends_overlay(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    bmo = ["COIN", "NET"]
    amc = ["DDOG", "ABNB"]
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    return_value=(bmo, amc)):
        out = eu.effective_scan_tickers("extended")
    # Core comes first, then BMO, then AMC
    assert out[:12] == _PROD_CORE
    assert out[12:] == ["COIN", "NET", "DDOG", "ABNB"]


def test_overlay_dedupes_against_prod_core(monkeypatch):
    """If NVDA reports tonight, it must appear exactly once (already in core)."""
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    return_value=(["NVDA", "COIN"], ["AAPL", "DDOG"])):
        out = eu.effective_scan_tickers("extended")
    # NVDA, AAPL appear once (in core); COIN, DDOG appear once each in overlay
    assert out.count("NVDA") == 1
    assert out.count("AAPL") == 1
    assert out.count("COIN") == 1
    assert out.count("DDOG") == 1
    # Total = 12 core + 2 unique overlay
    assert len(out) == 14


def test_overlay_capped_at_max(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    monkeypatch.setenv("EXTENDED_HOURS_OVERLAY_MAX", "5")
    bmo = [f"BMO{i}" for i in range(10)]
    amc = [f"AMC{i}" for i in range(10)]
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    return_value=(bmo, amc)):
        out = eu.effective_scan_tickers("extended")
    # 12 core + 5 overlay = 17
    assert len(out) == 17
    overlay = out[12:]
    assert overlay == ["BMO0", "BMO1", "BMO2", "BMO3", "BMO4"]


def test_overlay_normalizes_case_and_whitespace(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    return_value=([" coin ", "net"], ["DDOG"])):
        out = eu.effective_scan_tickers("extended")
    assert out[12:] == ["COIN", "NET", "DDOG"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_earnings_calendar_empty_returns_just_core(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    return_value=([], [])):
        out = eu.effective_scan_tickers("extended")
    assert out == _PROD_CORE


def test_earnings_calendar_raises_falls_back_to_core(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    def _boom(*a, **kw):
        raise RuntimeError("FMP timeout")
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    side_effect=_boom):
        out = eu.effective_scan_tickers("extended")
    assert out == _PROD_CORE


def test_earnings_module_import_error_falls_back(monkeypatch):
    """If earnings_watcher.data_sources can't be imported, fall back."""
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    # Force the import line in _fetch_earnings_overlay to raise
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__
    def _broken_import(name, *a, **kw):
        if name == "earnings_watcher.data_sources" or name.startswith("earnings_watcher"):
            raise ImportError("simulated missing module")
        return real_import(name, *a, **kw)
    with mock.patch("builtins.__import__", side_effect=_broken_import):
        out = eu.effective_scan_tickers("extended")
    assert out == _PROD_CORE


def test_never_returns_empty_even_if_core_resolves_empty(monkeypatch):
    """Defensive: if TRADE_TICKERS is somehow empty AND overlay is empty,
    we still return [] gracefully without raising."""
    _patch_prod_core(monkeypatch, tickers=[])
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe",
                    return_value=([], [])):
        out = eu.effective_scan_tickers("extended")
    assert out == []  # documented degenerate behavior, no crash


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_cache_within_ttl_does_not_refetch(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    fake = mock.MagicMock(return_value=(["COIN"], []))
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe", fake):
        out1 = eu.effective_scan_tickers("extended")
        out2 = eu.effective_scan_tickers("extended")
        out3 = eu.effective_scan_tickers("extended")
    assert out1 == out2 == out3
    # Only one fetch despite 3 calls
    assert fake.call_count == 1


def test_cache_expires_after_ttl(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    fake = mock.MagicMock(side_effect=[(["COIN"], []), (["NET"], [])])
    fake_now = [1000.0]
    real_time = eu.time.time
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe", fake):
        with mock.patch.object(eu.time, "time", lambda: fake_now[0]):
            out1 = eu.effective_scan_tickers("extended")
            assert out1[12:] == ["COIN"]
            # Advance past TTL
            fake_now[0] += eu._CACHE_TTL_SEC + 1
            out2 = eu.effective_scan_tickers("extended")
            assert out2[12:] == ["NET"]
    assert fake.call_count == 2


def test_cache_invalidates_on_date_rollover(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    fake = mock.MagicMock(side_effect=[(["COIN"], []), (["NET"], [])])
    dates = ["2026-05-07", "2026-05-08"]
    with mock.patch("earnings_watcher.data_sources.get_today_earnings_universe", fake):
        with mock.patch.object(eu, "_today_iso", side_effect=lambda: dates[0]):
            out1 = eu.effective_scan_tickers("extended")
        with mock.patch.object(eu, "_today_iso", side_effect=lambda: dates[1]):
            out2 = eu.effective_scan_tickers("extended")
    assert out1[12:] == ["COIN"]
    assert out2[12:] == ["NET"]
    assert fake.call_count == 2


# ---------------------------------------------------------------------------
# Off session (defensive)
# ---------------------------------------------------------------------------

def test_off_session_returns_prod_core(monkeypatch):
    _patch_prod_core(monkeypatch)
    monkeypatch.setenv("EXTENDED_HOURS_DYNAMIC_UNIVERSE_ENABLED", "1")
    out = eu.effective_scan_tickers("off")
    assert out == _PROD_CORE
