"""v5.10.4 \u2014 Entry 2 (Section III) live-hot-path wiring tests.

Confirms that ``_v5104_maybe_fire_entry_2`` in trade_genius.py correctly
fires a 50%-sized scale-in when:

  - The position has Entry 1 active and Entry 2 not yet fired,
  - 1m DI crosses the > 30 edge (di_1m_prev <= 30, di_1m_now > 30),
  - The current print is a fresh NHOD past Entry 1's HWM (long) /
    fresh NLOD past Entry 1's LWM (short),
  - Section I (global permit) re-evaluates OPEN at trigger time,
  - The Entry-1 ts strictly precedes the current call.

And does NOT fire when any of those gates is missing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SSM_SMOKE_TEST", "1")


@pytest.fixture
def tg(monkeypatch):
    """Import trade_genius once per test with a fresh module cache so
    monkeypatching the module-level globals (paper_cash, _QQQ_REGIME,
    fetch_1min_bars, etc.) doesn't leak across tests.
    """
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    # Force a fresh import so paper_cash starts at its default and
    # eot_glue's di_1m_prev / position state caches start empty.
    for mod in [m for m in list(sys.modules) if m in ("trade_genius", "v5_10_1_integration")]:
        del sys.modules[mod]
    import trade_genius

    # Belt-and-suspenders: clear the integration module's caches
    # explicitly even if the import-cache flush above already ran.
    import v5_10_1_integration as eot_glue

    eot_glue._di_1m_prev.clear()
    eot_glue._position_state_long.clear()
    eot_glue._position_state_short.clear()
    return trade_genius


def _stub_market(tg, monkeypatch, *, ticker_price: float, qqq_price: float = 500.0):
    """Stub market data + Section I inputs so Section I evaluates OPEN
    on long side. Section I LONG requires qqq_5m_close > qqq_5m_ema9
    AND qqq_current_price > qqq_avwap_0930.
    """

    def fake_fetch_1min_bars(t):
        if t == "QQQ":
            return {"current_price": qqq_price, "closes": [qqq_price] * 5, "volumes": [1000] * 5}
        return {"current_price": ticker_price, "closes": [ticker_price] * 5, "volumes": [1000] * 5}

    monkeypatch.setattr(tg, "fetch_1min_bars", fake_fetch_1min_bars)
    monkeypatch.setattr(tg, "get_fmp_quote", lambda t: None)
    monkeypatch.setattr(tg, "_opening_avwap", lambda t: 100.0)

    # _QQQ_REGIME exposes last_close + ema9 attributes.
    class _R:
        last_close = qqq_price
        ema9 = qqq_price - 1.0  # close > ema9 \u2192 LONG permit open

    monkeypatch.setattr(tg, "_QQQ_REGIME", _R())
    monkeypatch.setattr(tg, "_utc_now_iso", lambda: "2026-04-28T15:00:00Z")
    monkeypatch.setattr(tg, "save_paper_state", lambda: None)


def _make_pos(
    tg, *, entry_price: float, shares: int, hwm: float, entry_ts: str = "2026-04-28T14:30:00Z"
):
    return {
        "entry_price": entry_price,
        "shares": shares,
        "stop": entry_price * 0.99,
        "v5104_entry1_price": entry_price,
        "v5104_entry1_shares": shares,
        "v5104_entry1_hwm": hwm,
        "v5104_entry1_ts_utc": entry_ts,
        "v5104_entry2_fired": False,
    }


def test_entry_2_fires_on_long_di_cross_and_fresh_nhod(tg, monkeypatch):
    """Long: 1m DI crosses 25 \u2192 35 (edge > 30) AND price extends
    above Entry-1 HWM. Section I open. Expect Entry 2 fires at 50%."""
    # Step 1: price 104 (BELOW current HWM 105) so the seed call does
    # NOT push HWM forward. Caches di_1m_prev=20 in eot_glue.
    _stub_market(tg, monkeypatch, ticker_price=104.0)
    pos = _make_pos(tg, entry_price=100.0, shares=10, hwm=105.0)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 20.0, "di_minus_1m": 0.0, "di_plus_5m": 30.0, "di_minus_5m": 0.0},
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["v5104_entry2_fired"] is False
    assert pos["v5104_entry1_hwm"] == 105.0  # unchanged

    # Step 2: price climbs to 110 (> HWM 105 \u2192 fresh NHOD), DI flips
    # to 35 (> 30 cross from prev=20). Section I still open.
    _stub_market(tg, monkeypatch, ticker_price=110.0)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 35.0, "di_minus_1m": 0.0, "di_plus_5m": 30.0, "di_minus_5m": 0.0},
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)

    assert pos["v5104_entry2_fired"] is True
    assert pos["v5104_entry2_shares"] == 5  # 50% of 10
    assert pos["shares"] == 15
    # Avg entry: (100*10 + 110*5) / 15 = 1550/15 = 103.333...
    assert abs(pos["entry_price"] - (100 * 10 + 110 * 5) / 15.0) < 1e-6


def test_entry_2_skips_when_no_di_cross(tg, monkeypatch):
    """Pre-seed di_1m_prev=35 in the eot_glue cache; with di_1m_now=35
    every tick, the > 30 edge is never crossed and Entry 2 never
    fires even with a fresh NHOD print available."""
    import v5_10_1_integration as eot_glue

    eot_glue._di_1m_prev[("AAPL", "LONG")] = 35.0
    _stub_market(tg, monkeypatch, ticker_price=110.0)
    pos = _make_pos(tg, entry_price=100.0, shares=10, hwm=105.0)

    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 35.0, "di_minus_1m": 0.0, "di_plus_5m": 30.0, "di_minus_5m": 0.0},
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["v5104_entry2_fired"] is False


def test_entry_2_skips_when_not_fresh_nhod(tg, monkeypatch):
    """Even with a clean DI cross, if the price hasn't extended past
    the Entry-1 HWM, Entry 2 must not fire."""
    _stub_market(tg, monkeypatch, ticker_price=104.0)  # below HWM=105
    pos = _make_pos(tg, entry_price=100.0, shares=10, hwm=105.0)

    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 20.0, "di_minus_1m": 0.0, "di_plus_5m": 30.0, "di_minus_5m": 0.0},
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 35.0, "di_minus_1m": 0.0, "di_plus_5m": 30.0, "di_minus_5m": 0.0},
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["v5104_entry2_fired"] is False


def test_entry_2_skips_when_already_fired(tg, monkeypatch):
    _stub_market(tg, monkeypatch, ticker_price=120.0)
    pos = _make_pos(tg, entry_price=100.0, shares=10, hwm=105.0)
    pos["v5104_entry2_fired"] = True

    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 35.0, "di_minus_1m": 0.0, "di_plus_5m": 30.0, "di_minus_5m": 0.0},
    )
    pre_shares = pos["shares"]
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["shares"] == pre_shares  # unchanged
